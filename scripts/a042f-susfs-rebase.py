#!/usr/bin/env python3
"""Rebase the paired Jack SUSFS patch/helper on the bounded A04e port."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class RebaseError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def command(argv, cwd, data=None):
    result = subprocess.run(
        argv, cwd=cwd, input=data, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=60,
        env={"PATH": os.defpath, "LC_ALL": "C", "TMPDIR": str(cwd)},
    )
    if result.returncode:
        detail = (result.stderr + result.stdout).decode(errors="replace")[:4000]
        raise RebaseError(f"{' '.join(map(str, argv[:4]))} failed ({result.returncode}):\n{detail}")
    return result.stdout


def read_regular(root, name):
    path = root / name
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise RebaseError(f"symlink in scoped path: {path}")
    if not path.is_file():
        raise RebaseError(f"missing scoped regular file: {path}")
    data = path.read_bytes()
    if b"\0" in data:
        raise RebaseError(f"binary scoped file is unsupported: {path}")
    return data


def load_assets(assets):
    manifest = json.loads((assets / "baseline.json").read_text())
    for name, expected in manifest["asset_sha256"].items():
        if sha(read_regular(assets, name)) != expected:
            raise RebaseError(f"baseline asset integrity mismatch: {name}")
    return manifest


def normalized_patch(data):
    return b"".join(line for line in data.splitlines(keepends=True)
                    if not line.startswith(b"index "))


def patch_paths(data):
    names = []
    for line in data.decode("utf-8").splitlines():
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/([A-Za-z0-9_./-]+) b/\1", line)
            if not match or ".." in match[1].split("/"):
                raise RebaseError(f"unsupported patch path/header: {line}")
            names.append(match[1])
        if line.startswith(("rename ", "copy ", "deleted file", "old mode", "new mode", "GIT binary")):
            raise RebaseError(f"unsupported patch operation: {line}")
    if not names or len(names) != len(set(names)):
        raise RebaseError("patch has no unique file sections")
    return sorted(names)


def merge(ours, base, theirs, work, label):
    if ours == theirs or base == theirs:
        return ours
    if ours == base:
        return theirs
    paths = [work / name for name in ("ours", "base", "theirs")]
    for path, data in zip(paths, (ours, base, theirs)):
        path.write_bytes(data)
    try:
        return command([
            "git", "merge-file", "-p", "--diff3", "-L", "current-port",
            "-L", "known-Jack-baseline", "-L", "latest-Jack", *map(str, paths),
        ], work)
    except RebaseError as error:
        raise RebaseError(f"three-way conflict in {label}; refresh the reviewed baseline. "
                          f"No target files have been changed.\n{error}") from error


def ksu_capabilities(root):
    ksu = root / "drivers/kernelsu"
    if not ksu.is_dir():
        raise RebaseError("drivers/kernelsu is missing; prepare BakaSU first")
    symbols = (b"ksu_handle_setresuid", b"ksu_hide_setprocattr")
    found = set()
    for path in ksu.rglob("*"):
        if path.is_file() and path.suffix in (".c", ".h"):
            data = path.read_bytes()
            found.update(symbol for symbol in symbols if symbol in data)
    return sorted(symbol.decode() for symbol in found)


def seed_from_current(root, assets, manifest, work):
    seed = work / "seed"
    for name in manifest["targets"] + manifest["context_paths"]:
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(read_regular(root, name))
    integration = (assets / "integration.patch").read_bytes()
    command(["git", "apply", "-R", "--check", "-"], seed, integration)
    command(["git", "apply", "-R", "-"], seed, integration)
    # The helper only greps these capabilities; no BakaSU code is changed.
    probe = seed / "drivers/kernelsu/probes.h"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("\n".join(ksu_capabilities(root)) + "\n")
    return seed


def generate(seed, manifest, adapted, hooks, work):
    tree = work / "vendor"
    shutil.copytree(seed, tree)
    helper = work / "hooks.sh"
    helper.write_bytes(hooks)
    command(["bash", "-n", str(helper)], work)
    command(["git", "apply", "--recount", "--check", "--whitespace=nowarn", "-"], tree, adapted)
    command(["git", "apply", "--recount", "--whitespace=nowarn", "-"], tree, adapted)
    log = command(["bash", "-e", "-o", "pipefail", str(helper)], tree)
    if b"patch failed" in log.lower() or b"contains KernelSU" in log:
        raise RebaseError("inline helper reported a skipped/failed hook; no target files changed:\n"
                          + log.decode(errors="replace")[:4000])
    allowed = set(manifest["seed_files"]) | set(manifest["targets"])
    actual = {str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != allowed:
        raise RebaseError(f"helper/patch changed file scope: {sorted(actual ^ allowed)}")
    for name in set(manifest["seed_files"]) - set(manifest["targets"]):
        if read_regular(tree, name) != read_regular(seed, name):
            raise RebaseError(f"helper modified a context-only file: {name}")
    return {name: read_regular(tree, name) for name in manifest["targets"]}


def prepare(root, assets, manifest, raw, hooks, work):
    if patch_paths(raw) != manifest["patch_paths"]:
        raise RebaseError("upstream patch file scope changed; review and refresh the baseline")
    seed = seed_from_current(root, assets, manifest, work)
    patch_text = work / "patch-text"
    patch_text.mkdir()
    original = normalized_patch((assets / "upstream.patch").read_bytes())
    (patch_text / "susfs.patch").write_bytes(original)
    adaptation = (assets / "vendor-adaptation.patch").read_bytes()
    command(["git", "apply", "--check", "-"], patch_text, adaptation)
    command(["git", "apply", "-"], patch_text, adaptation)
    adapted = merge((patch_text / "susfs.patch").read_bytes(), original,
                    normalized_patch(raw), work, "vendor patch adaptation")
    ancestor = work / "ancestor"
    ancestor.mkdir()
    for name in manifest["targets"]:
        path = ancestor / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(read_regular(root, name))
    fixes = (assets / "known-local-fixes.patch").read_bytes()
    command(["git", "apply", "-R", "--check", "-"], ancestor, fixes)
    command(["git", "apply", "-R", "-"], ancestor, fixes)
    upstream = generate(seed, manifest, adapted, hooks, work)
    current, merged = {}, {}
    for name in manifest["targets"]:
        current[name] = read_regular(root, name)
        merged[name] = merge(current[name], read_regular(ancestor, name),
                             upstream[name], work, name)
        for symbol, minimum in manifest["required_hooks"].get(name, {}).items():
            if len(re.findall(rb"\b" + symbol.encode() + rb"\s*\(", merged[name])) < minimum:
                raise RebaseError(f"required hook disappeared: {name}: {symbol}; review required")
    lines = []
    for name in manifest["targets"]:
        if current[name] == merged[name]:
            continue
        before = current[name].decode("utf-8")
        after = merged[name].decode("utf-8")
        if not before.endswith("\n") or not after.endswith("\n"):
            raise RebaseError(f"missing final newline in {name}; review required")
        lines.append(f"diff --git a/{name} b/{name}\n")
        lines.extend(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                           fromfile=f"a/{name}", tofile=f"b/{name}"))
    return "".join(lines).encode(), current, merged


def rebase(root, patch, hooks, assets, check=False, output=None):
    root = root.resolve()
    manifest = load_assets(assets)
    raw, helper = patch.read_bytes(), hooks.read_bytes()
    result = {"patch_sha256": sha(raw), "hooks_sha256": sha(helper),
              "baseline_commit": manifest["upstream_commit"], "changed_files": []}
    if (result["patch_sha256"], result["hooks_sha256"]) == (
            manifest["patch_sha256"], manifest["hooks_sha256"]) and ksu_capabilities(root) == manifest["ksu_capabilities"]:
        result["status"] = "same-paired-content"
        if output:
            output.write_bytes(b"")
        return result
    # Everything up to the final git apply runs against bounded scratch files.
    with tempfile.TemporaryDirectory(prefix="a042f-susfs-", dir=os.environ.get("TMPDIR")) as tmp:
        work = Path(tmp)
        delta, before, after = prepare(root, assets, manifest, raw, helper, work)
        result["changed_files"] = [name for name in before if before[name] != after[name]]
        result["delta_sha256"] = sha(delta)
        if delta:
            command(["git", "apply", "--check", "--whitespace=nowarn", "-"], root, delta)
        if output:
            output.write_bytes(delta)
        # Recheck every scoped input before writing; a changed input aborts the batch.
        for name, data in before.items():
            if read_regular(root, name) != data:
                raise RebaseError(f"concurrent target change: {name}; no update applied")
        if delta and not check:
            command(["git", "apply", "--whitespace=nowarn", "-"], root, delta)
            for name, data in after.items():
                if read_regular(root, name) != data:
                    raise RebaseError(f"post-apply verification failed: {name}; abort the build")
        result["status"] = "checked" if check else ("updated" if delta else "same-result")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--hooks", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="prepare and validate without applying")
    parser.add_argument("--output", type=Path, help="also write the exact update patch")
    args = parser.parse_args()
    try:
        result = rebase(args.root, args.patch, args.hooks,
                        Path(__file__).resolve().with_name("a042f-susfs"), args.check, args.output)
    except (RebaseError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"a042f-susfs-rebase: ERROR: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
