#!/usr/bin/env python3
"""Exercise the archive importer against real, temporary Git repositories."""

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("import-samsung.py").resolve()


def tar_bytes(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, contents, mode, link in entries:
            item = tarfile.TarInfo(name)
            item.mode = mode
            if link is not None:
                item.type = tarfile.SYMTYPE
                item.linkname = link
                archive.addfile(item)
            else:
                item.size = len(contents)
                archive.addfile(item, io.BytesIO(contents))
    return data.getvalue()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="samsung-import-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.assets = self.root / "assets"
        self.repo.mkdir()
        self.assets.mkdir()
        self.git("init", "-b", "a04e")
        self.git("config", "user.name", "Import Test")
        self.git("config", "user.email", "import-test@example.invalid")
        (self.repo / "README.md").write_text("Bootstrap fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "test fixture")
        self.before = self.git("rev-parse", "HEAD")
        self.index = (self.repo / ".git/index").read_bytes()
        self.kernel = [
            ("./Makefile", b"VERSION = 4\n", 0o644, None),
            ("source with space.c", b"old\n", 0o644, None),
            ("scripts/run.sh", b"#!/bin/sh\nexit 0\n", 0o755, None),
            ("docs/فارسی.txt", b"UTF8 path\n", 0o644, None),
            ("current", b"", 0o777, "source with space.c"),
        ]
        self.update = [("Kernel/source with space.c", b"updated\n", 0o644, None)]

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE).strip()

    def prepare(self):
        full = self.assets / "full.zip"
        update = self.assets / "update.zip"
        with zipfile.ZipFile(full, "w") as archive:
            archive.writestr("Kernel.tar.gz", tar_bytes(self.kernel))
            archive.writestr("Platform.tar.gz", tar_bytes([("module/file.c", b"platform\n", 0o644, None)]))
            archive.writestr("README_Kernel.txt", "Original kernel guide\n")
            archive.writestr("README_Platform.txt", "Original platform guide\n")
        with zipfile.ZipFile(update, "w") as archive:
            archive.writestr("A042FXXSGEZE1_kernel.tar.gz", tar_bytes(self.update))
            archive.writestr("A042FXXSGEZE1_kernel.txt", "Original update guide\n")
        manifest = {
            "full": {"asset": full.name, "sha256": hashlib.sha256(full.read_bytes()).hexdigest(), "kernel_regular_files": 4, "kernel_symlinks": 1, "platform_regular_files": 1},
            "update": {"asset": update.name, "sha256": hashlib.sha256(update.read_bytes()).hexdigest()},
            "update_regular_files": 1,
        }
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(manifest))
        return manifest

    def invoke(self):
        return subprocess.run(
            ["python3", "-B", str(SCRIPT), "--assets", str(self.assets), "--manifest", str(self.manifest), "--record", str(self.root / "record.json")],
            cwd=self.repo, capture_output=True, text=True, timeout=20,
        )

    def unchanged_checkout(self):
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), self.index)
        self.assertEqual((self.repo / "README.md").read_text(), "Bootstrap fixture\n")

    def test_exact_import_and_update_preserve_checkout(self):
        self.prepare()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("show", "samsung-base:source with space.c"), b"old")
        self.assertEqual(self.git("show", "samsung-stock:source with space.c"), b"updated")
        self.assertEqual(self.git("show", "samsung-base:docs/فارسی.txt"), b"UTF8 path")
        self.assertEqual(self.git("show", "samsung-base:current"), b"source with space.c")
        self.assertTrue(self.git("ls-tree", "samsung-base", "scripts/run.sh").startswith(b"100755"))
        self.assertTrue(self.git("ls-tree", "samsung-base", "current").startswith(b"120000"))
        self.assertEqual(self.git("show", "samsung-platform:module/file.c"), b"platform")
        self.assertEqual(self.git("rev-parse", "samsung-stock^"), self.git("rev-parse", "samsung-base"))
        self.assertEqual(self.git("diff", "--name-only", "samsung-base", "samsung-stock"), b"source with space.c")
        record = json.loads((self.root / "record.json").read_text())
        self.assertEqual(record["branches"]["samsung-base"]["files"], 6)
        self.unchanged_checkout()

    def test_bad_hash_creates_no_branches(self):
        manifest = self.prepare()
        manifest["full"]["sha256"] = "0" * 64
        self.manifest.write_text(json.dumps(manifest))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA256 mismatch", result.stderr)
        self.assertEqual(self.git("branch", "--format=%(refname:short)"), b"a04e")
        self.unchanged_checkout()

    def test_preexisting_branch_is_not_overwritten(self):
        self.prepare()
        self.git("branch", "samsung-base")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.git("rev-parse", "samsung-base"), self.before)
        self.unchanged_checkout()

    def test_unsafe_archive_entries_are_rejected(self):
        for name, link, error in [("../escape", None, "Unsafe archive path"), (".git/config", None, "Unsafe archive path"), ("escaping-link", "../outside", "escapes source tree")]:
            with self.subTest(name=name):
                original = self.kernel
                self.kernel = original + [(name, b"bad", 0o644, link)]
                self.prepare()
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.kernel = original
                self.unchanged_checkout()

    def test_duplicate_source_path_is_rejected(self):
        self.kernel.append(("Makefile", b"duplicate\n", 0o644, None))
        self.prepare()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate archive path", result.stderr)

    def test_update_requires_prefix_and_existing_base(self):
        for name, error in [("source with space.c", "Unexpected update prefix"), ("Kernel/missing.c", "no regular base preimage")]:
            with self.subTest(name=name):
                self.update = [(name, b"new\n", 0o644, None)]
                self.prepare()
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.unchanged_checkout()


if __name__ == "__main__":
    unittest.main()
