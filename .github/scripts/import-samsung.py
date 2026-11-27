#!/usr/bin/env python3
"""Import the supplied Samsung archives into separate Git branches."""

import argparse
import hashlib
import io
import json
import subprocess
import tempfile
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


BRANCHES = ("samsung-base", "samsung-stock", "samsung-platform")


def git(*args):
    return subprocess.check_output(["git", *args])


def checked_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError(f"Unsafe archive path: {name!r}")
    return str(path)


def quoted_path(path):
    data = path.encode("utf-8", errors="surrogateescape")
    return b'"' + b"".join(
        bytes([c]) if 32 <= c < 127 and c not in (34, 92)
        else (b"\\" + bytes([c])) if c in (34, 92)
        else f"\\{c:03o}".encode() for c in data
    ) + b'"'


def inventory(ref):
    result = {}
    for entry in git("ls-tree", "-r", "-z", ref).split(b"\0"):
        if not entry:
            continue
        header, path = entry.split(b"\t", 1)
        mode, kind, oid = header.split()
        if kind != b"blob":
            raise ValueError("Unexpected non-blob tree entry")
        result[path.decode("utf-8", errors="surrogateescape")] = (
            mode.decode(), oid.decode()
        )
    return result


class Importer:
    def __init__(self):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            ["git", "fast-import", "--quiet"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=self.errors,
        )
        self.number = 0
        self.send(b"feature done\n")

    def send(self, data):
        self.process.stdin.write(data)

    def mark(self):
        self.number += 1
        return self.number

    def blob(self, stream, size):
        if not 0 <= size <= 536870912:
            raise ValueError("Archive file exceeds the import size limit")
        mark = self.mark()
        self.send(f"blob\nmark :{mark}\ndata {size}\n".encode())
        digest = hashlib.sha1(f"blob {size}\0".encode())
        remaining = size
        while remaining:
            chunk = stream.read(min(1048576, remaining))
            if not chunk:
                raise ValueError("Truncated archive file")
            self.send(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        self.send(b"\n")
        return digest.hexdigest(), mark

    def tar(self, stream, prefix="", base=None):
        records, seen, directories = {}, set(), set()
        count, total, regular, symlinks = 0, 0, 0, 0
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            for item in archive:
                count += 1
                total += item.size
                if count > 200000 or total > 6442450944:
                    raise ValueError("Archive exceeds the import budget")
                name = checked_path(item.name)
                if name in seen:
                    raise ValueError(f"Duplicate archive path: {name!r}")
                seen.add(name)
                if prefix:
                    if name == prefix and item.isdir():
                        continue
                    if not name.startswith(prefix + "/"):
                        raise ValueError(f"Unexpected update prefix: {name!r}")
                    name = name[len(prefix) + 1:]
                if item.isdir():
                    continue
                if name == "." or not (item.isfile() or item.issym()):
                    raise ValueError(f"Unsupported archive entry: {name!r}")
                if base is not None and (name not in base or base[name][0] == "120000"):
                    raise ValueError(f"Update has no regular base preimage: {name!r}")
                if name in directories or any(str(p) in records for p in PurePosixPath(name).parents):
                    raise ValueError(f"Conflicting archive paths: {name!r}")
                directories.update(str(p) for p in PurePosixPath(name).parents)
                if item.issym():
                    link = PurePosixPath(item.linkname)
                    parts = list(PurePosixPath(name).parent.parts)
                    if link.is_absolute():
                        raise ValueError("Absolute symbolic link")
                    for part in link.parts:
                        if part == "..":
                            if not parts:
                                raise ValueError("Symbolic link escapes source tree")
                            parts.pop()
                        else:
                            parts.append(part)
                    if ".git" in parts:
                        raise ValueError("Symbolic link targets Git metadata")
                    data = item.linkname.encode("utf-8", errors="surrogateescape")
                    oid, mark = self.blob(io.BytesIO(data), len(data))
                    mode = "120000"
                    symlinks += 1
                else:
                    with archive.extractfile(item) as contents:
                        oid, mark = self.blob(contents, item.size)
                    mode = "100755" if item.mode & 0o111 else "100644"
                    regular += 1
                records[name] = (mode, oid, mark)
        for chunk in iter(lambda: stream.read(1048576), b""):
            pass
        return records, {"regular": regular, "symlinks": symlinks, "bytes": total}

    def add_text(self, records, archive, name):
        if name in records:
            raise ValueError(f"Duplicate outer guide: {name}")
        info = archive.getinfo(name)
        with archive.open(info) as stream:
            oid, mark = self.blob(stream, info.file_size)
        records[name] = ("100644", oid, mark)

    def commit(self, branch, records, message, parent=None):
        mark = self.mark()
        identity = git("var", "GIT_COMMITTER_IDENT").strip()
        data = message.encode()
        self.send(f"commit refs/heads/{branch}\nmark :{mark}\n".encode())
        self.send(b"committer " + identity + b"\n")
        self.send(f"data {len(data)}\n".encode() + data + b"\n")
        if parent is not None:
            self.send(f"from :{parent}\n".encode())
        for name, (mode, _, blob_mark) in records.items():
            self.send(f"M {mode} :{blob_mark} ".encode() + quoted_path(name) + b"\n")
        self.send(b"\n")
        return mark

    def finish(self):
        self.send(b"done\n")
        self.process.stdin.close()
        code = self.process.wait()
        if code:
            self.errors.seek(0)
            raise RuntimeError(self.errors.read().decode(errors="replace")[-5000:])

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.errors.close()


def import_sources(assets, manifest):
    if git("rev-parse", "--show-object-format").strip() != b"sha1":
        raise ValueError("This importer requires a SHA1 Git repository")
    for branch in BRANCHES:
        p = subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/" + branch])
        if p.returncode != 1:
            raise ValueError(f"Output branch already exists or cannot be checked: {branch}")
    for kind in ("full", "update"):
        spec = manifest[kind]
        if Path(spec["asset"]).name != spec["asset"]:
            raise ValueError("Asset must be a filename")
        path = assets / spec["asset"]
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
        if digest.hexdigest() != spec["sha256"]:
            raise ValueError(f"SHA256 mismatch: {spec['asset']}")
        if "bytes" in spec and path.stat().st_size != spec["bytes"]:
            raise ValueError(f"Size mismatch: {spec['asset']}")
    expected, stats = {}, {}
    importer = Importer()
    try:
        with zipfile.ZipFile(assets / manifest["full"]["asset"]) as full:
            if len(full.namelist()) != 4 or set(full.namelist()) != {"Kernel.tar.gz", "Platform.tar.gz", "README_Kernel.txt", "README_Platform.txt"}:
                raise ValueError("Unexpected full-package members")
            with full.open("Kernel.tar.gz") as stream:
                records, stats["kernel"] = importer.tar(stream)
            for key, actual in (("kernel_regular_files", stats["kernel"]["regular"]), ("kernel_symlinks", stats["kernel"]["symlinks"])):
                if key in manifest["full"] and manifest["full"][key] != actual:
                    raise ValueError(f"Unexpected {key}: {actual}")
            importer.add_text(records, full, "README_Kernel.txt")
            expected["samsung-base"] = {n: (m, oid) for n, (m, oid, _) in records.items()}
            base = importer.commit("samsung-base", records, "import(samsung): preserve complete Android 14 kernel source\n\nSamsung source archive SHA256: " + manifest["full"]["sha256"] + "\nOriginal copyright notices and licenses retained.\n")
            with zipfile.ZipFile(assets / manifest["update"]["asset"]) as update:
                if len(update.namelist()) != 2 or set(update.namelist()) != {"A042FXXSGEZE1_kernel.tar.gz", "A042FXXSGEZE1_kernel.txt"}:
                    raise ValueError("Unexpected incremental-package members")
                with update.open("A042FXXSGEZE1_kernel.tar.gz") as stream:
                    changes, stats["update"] = importer.tar(stream, prefix="Kernel", base=expected["samsung-base"])
                update.read("A042FXXSGEZE1_kernel.txt")
                if stats["update"]["regular"] != manifest["update_regular_files"] or stats["update"]["symlinks"]:
                    raise ValueError("Unexpected incremental source count")
                if any(expected["samsung-base"][n] == (m, oid) for n, (m, oid, _) in changes.items()):
                    raise ValueError("Incremental package contains an unchanged file")
                expected["samsung-stock"] = dict(expected["samsung-base"])
                expected["samsung-stock"].update({n: (m, oid) for n, (m, oid, _) in changes.items()})
                importer.commit("samsung-stock", changes, "import(samsung): apply official EZE1 kernel update\n\nThe supplied guide requests the EXJ3 base.\nUpdate archive SHA256: " + manifest["update"]["sha256"] + "\nOriginal copyright notices and licenses retained.\n", parent=base)
            with full.open("Platform.tar.gz") as stream:
                platform, stats["platform"] = importer.tar(stream)
            if "platform_regular_files" in manifest["full"] and stats["platform"]["regular"] != manifest["full"]["platform_regular_files"]:
                raise ValueError("Unexpected platform source count")
            importer.add_text(platform, full, "README_Platform.txt")
            expected["samsung-platform"] = {n: (m, oid) for n, (m, oid, _) in platform.items()}
            importer.commit("samsung-platform", platform, "import(samsung): preserve separate Android 14 platform sources\n\nSamsung source archive SHA256: " + manifest["full"]["sha256"] + "\nOriginal copyright notices and licenses retained.\n")
        importer.finish()
    finally:
        importer.close()
    result = {"archives": {k: manifest[k]["sha256"] for k in ("full", "update")}, "stats": stats, "branches": {}}
    for branch in BRANCHES:
        if inventory(branch) != expected[branch]:
            raise ValueError(f"Git tree read-back differs from archive: {branch}")
        result["branches"][branch] = {"commit": git("rev-parse", branch).decode().strip(), "tree": git("rev-parse", branch + "^{tree}").decode().strip(), "files": len(expected[branch])}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    args = parser.parse_args()
    result = import_sources(args.assets, json.loads(args.manifest.read_text()))
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
