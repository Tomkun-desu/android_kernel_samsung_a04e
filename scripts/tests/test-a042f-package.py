"""Packaging contract tests; optional native magiskboot via A042F_TEST_MAGISKBOOT."""

import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('package_a042f', SOURCE / 'scripts/package-a042f.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def image(payload=b'test kernel'):
    header = bytearray(64)
    header[56:60] = b'ARM\x64'
    return gzip.compress(bytes(header) + payload, mtime=0)


def boot(version=2):
    page = 2048
    header = bytearray(page)
    header[:8] = b'ANDROID!'
    parts = [image(b'stock kernel'), gzip.compress(b'070701stock ramdisk', mtime=0), b'second']
    if version:
        parts.append(b'recovery dtbo')
    if version == 2:
        parts.append(b'\xd0\x0d\xfe\xed' + bytes(60))
    struct.pack_into('<10I', header, 8, len(parts[0]), 0x40080000, len(parts[1]),
                     0x44000000, len(parts[2]), 0x40f00000, 0x40000100, page, version, 0x1a001501)
    header[48:53] = b'a04e\x00'
    header[64:576] = b'console=ttyS0 keep_stock '.ljust(512, b'x')
    header[608:619] = b'extra stock'
    if version:
        struct.pack_into('<IQI', header, 1632, len(parts[3]), 4 * page, (1648, 1660)[version - 1])
    if version == 2:
        struct.pack_into('<IQ', header, 1648, len(parts[4]), 0x48000000)
    for part in parts:
        header.extend(part)
        header.extend(bytes((-len(header)) % page))
    return bytes(header)


# Exercise the real subprocess/CLI path with a deterministic fixture executable.
FAKE = r'''#!/usr/bin/env python3
import os, struct, sys
from pathlib import Path
assert os.environ['PATCHVBMETAFLAG'] == 'false'
action = sys.argv[1]
fault = os.environ.get('A042F_PACKAGE_FAULT', '')
if fault == 'tool':
    sys.exit(9)
source = Path(sys.argv[-1] if action == 'unpack' else sys.argv[2]).read_bytes()
page = struct.unpack_from('<I', source, 36)[0]
sizes = [struct.unpack_from('<I', source, n)[0] for n in (8, 16, 24, 1632, 1648)]
names = ['kernel', 'ramdisk.cpio', 'second', 'recovery_dtbo', 'dtb']
if action == 'unpack':
    assert sys.argv[2:4] == ['-n', '-h']
    Path('header').write_bytes(source[:page])
    offset = page
    for name, size in zip(names, sizes):
        if size:
            Path(name).write_bytes(source[offset:offset + size])
        offset += (size + page - 1) // page * page
    if fault == 'layout':
        Path('surprise').touch()
elif action == 'repack':
    assert len(sys.argv) == 4
    header = bytearray(Path('header').read_bytes())
    parts = [Path(n).read_bytes() if Path(n).exists() else b'' for n in names]
    if fault == 'ramdisk':
        parts[1] = b'corrupted ramdisk'
    if fault == 'dtb':
        parts[4] = b'corrupted dtb'
    if fault == 'metadata':
        header[64] ^= 1
    struct.pack_into('<I', header, 8, len(parts[0]))
    struct.pack_into('<I', header, 16, len(parts[1]))
    struct.pack_into('<I', header, 1648, len(parts[4]))
    recovery_offset = page + sum((len(p) + page - 1) // page * page for p in parts[:3])
    struct.pack_into('<Q', header, 1636, recovery_offset)
    for part in parts:
        header.extend(part)
        header.extend(bytes((-len(header)) % page))
    Path(sys.argv[3]).write_bytes(header)
else:
    raise AssertionError(action)
'''


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='a042f-package-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        for name, content in {'Image.gz': image(), 'BuildInfo.txt': b'EXPERIMENTAL\n',
                              'source-revisions.env': b'BAKASU_SHA=' + b'a' * 40 + b'\n',
                              'kernel.config': b'CONFIG_ARM64=y\n', 'dtbs/test.dtb': b'unused dtb'}.items():
            path = self.artifacts / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(content)
        self.checksums()
        self.main, self.arm64 = self.root / 'main', self.root / 'arm64'
        for root in (self.main, self.arm64):
            root.mkdir()
            self.git(root, 'init', '-q')
        for name in ('META-INF/com/google/android/update-binary',
                     'META-INF/com/google/android/updater-script', 'tools/ak3-core.sh', 'LICENSE'):
            path = self.main / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture upstream script\n')
        binary = bytearray(64)
        binary[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<H', binary, 18, 183)
        for name in package.BINARIES:
            (self.arm64 / name).write_bytes(binary)
        for root, pin in ((self.main, 'ANYKERNEL_SHA'), (self.arm64, 'ARM64_SHA')):
            self.commit(root)
            patcher = mock.patch.object(package, pin, self.git(root, 'rev-parse', 'HEAD'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stock = self.root / 'stock.img'
        self.stock.write_bytes(boot())
        self.tool = self.root / 'magiskboot'
        self.tool.write_text(FAKE)
        self.tool.chmod(0o755)
        self.output = self.root / 'package'

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              capture_output=True, text=True, timeout=20).stdout.strip()

    def commit(self, root):
        self.git(root, 'add', '.')
        self.git(root, '-c', 'user.name=Packaging fixture', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')

    def checksums(self):
        (self.artifacts / 'SHA256SUMS').write_text(''.join(
            f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(self.artifacts)}\n'
            for p in sorted(self.artifacts.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS'))

    def invoke(self, command='anykernel', success=True, extra=()):
        args = [command, '--artifacts', str(self.artifacts), '--output', str(self.output)]
        if command == 'anykernel':
            args += ['--anykernel', str(self.main), '--anykernel-arm64', str(self.arm64)]
        elif command == 'odin-reference':
            args += ['--reference-tar', str(self.reference), '--magiskboot', str(self.tool)]
        else:
            args += ['--stock-boot', str(self.stock), '--stock-sha256', package.sha256(self.stock.read_bytes()),
                     '--firmware', 'A042FXXSGEZF2', '--magiskboot', str(self.tool)]
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = package.main(args + list(extra))
        self.assertEqual(result, 0 if success else 1, errors.getvalue())
        if not success:
            self.assertFalse(self.output.exists())
            self.assertFalse(list(self.root.glob('.a042f-package-*')))
        return errors.getvalue()

    def manifest(self):
        checks = self.output / 'SHA256SUMS'
        for line in checks.read_text().splitlines():
            digest, name = line.split('  ', 1)
            self.assertEqual(package.sha256((self.output / name).read_bytes()), digest)
        return json.loads((self.output / 'package-manifest.json').read_text())

    def test_anykernel_success(self):
        self.invoke()
        manifest = self.manifest()
        self.assertEqual(manifest['upstreams']['anykernel'], package.ANYKERNEL_SHA)
        self.assertTrue(manifest['experimental'])
        expected = set(package.ARTIFACTS) | {'anykernel.sh', 'package-manifest.json', 'SHA256SUMS',
                    'META-INF/com/google/android/update-binary', 'META-INF/com/google/android/updater-script',
                    'tools/ak3-core.sh', 'LICENSE'} | {'tools/' + name for name in package.BINARIES}
        self.assertEqual({str(p.relative_to(self.output)) for p in self.output.rglob('*') if p.is_file()}, expected)
        self.assertEqual((self.output / 'anykernel.sh').read_bytes(), package.TEMPLATE.read_bytes())
        self.assertEqual((self.output / 'LICENSE').read_bytes(), (self.main / 'LICENSE').read_bytes())
        self.assertTrue((self.output / 'tools/busybox').stat().st_mode & 0o111)

    def test_artifact_guards(self):
        original = (self.artifacts / 'SHA256SUMS').read_bytes()
        for invalid in (b'bad\n', b'0' * 64 + b'  ../outside\n', b'0' * 64 + b'  /etc/passwd\n',
                        original + original, original.replace(b'Image.gz', b'missing.gz')):
            with self.subTest(invalid=invalid[:75]):
                (self.artifacts / 'SHA256SUMS').write_bytes(invalid)
                self.invoke(success=False)
        (self.artifacts / 'SHA256SUMS').write_bytes(original)
        (self.artifacts / 'BuildInfo.txt').write_text('tampered')
        self.invoke(success=False)
        (self.artifacts / 'BuildInfo.txt').write_text('EXPERIMENTAL\n')
        (self.artifacts / 'SHA256SUMS').write_text(''.join(
            line + '\n' for line in original.decode().splitlines() if not line.endswith('Image.gz')))
        self.invoke(success=False)
        (self.artifacts / 'SHA256SUMS').write_bytes(original)
        (self.artifacts / 'BuildInfo.txt').unlink()
        (self.artifacts / 'BuildInfo.txt').symlink_to(self.stock)
        self.invoke(success=False)

    def test_kernel_guards(self):
        for invalid in (b'not gzip', gzip.compress(b'not ARM64'), image()[:-3],
                        image() + b'appended device tree'):
            with self.subTest(invalid=invalid[:15]):
                (self.artifacts / 'Image.gz').write_bytes(invalid)
                self.checksums()
                self.invoke(success=False)

    def test_checkout_guards(self):
        (self.arm64 / 'untracked').touch()
        self.invoke(success=False)
        (self.arm64 / 'untracked').unlink()
        binary = bytearray((self.arm64 / 'busybox').read_bytes())
        struct.pack_into('<H', binary, 18, 40)
        (self.arm64 / 'busybox').write_bytes(binary)
        self.invoke(success=False)
        self.commit(self.arm64)
        self.invoke(success=False)  # Clean but different HEAD still fails the pin.
        with mock.patch.object(package, 'ARM64_SHA', self.git(self.arm64, 'rev-parse', 'HEAD')):
            self.invoke(success=False)  # The wrong architecture cannot pass with an updated pin.

    def test_existing_output(self):
        self.output.mkdir()
        marker = self.output / 'keep'
        marker.write_text('untouched')
        with contextlib.redirect_stderr(io.StringIO()):
            result = package.main(['anykernel', '--artifacts', str(self.artifacts), '--output', str(self.output),
                                   '--anykernel', str(self.main), '--anykernel-arm64', str(self.arm64)])
        self.assertEqual(result, 1)
        self.assertEqual(marker.read_text(), 'untouched')

    def assert_odin(self, command='odin'):
        self.invoke(command)
        manifest = self.manifest()
        archive = self.output / manifest['archive']
        self.assertIn('4.19.191_samsung_BakaSU-SUSFS-NoMount', archive.name)
        raw = archive.read_bytes()
        suffix_length = 32 + 2 + len(archive.name[:-4]) + 1
        tarbytes, suffix = raw[:-suffix_length], raw[-suffix_length:]
        self.assertEqual(suffix, f'{hashlib.md5(tarbytes).hexdigest()}  {archive.name[:-4]}\n'.encode())
        self.assertEqual(tarbytes[257:263], b'ustar\x00')
        with tarfile.open(archive) as tar:
            self.assertEqual(tar.getnames(), ['boot.img'])
            rebuilt = tar.extractfile('boot.img').read()
        before, after = package.boot_image(self.stock.read_bytes()), package.boot_image(rebuilt)
        self.assertEqual(before[:2], after[:2])
        self.assertEqual(after[2]['kernel'], (self.artifacts / 'Image.gz').read_bytes())
        self.assertEqual({k: v for k, v in before[2].items() if k != 'kernel'},
                         {k: v for k, v in after[2].items() if k != 'kernel'})
        self.assertFalse(manifest['oem_signature_retained'])
        return manifest

    def make_reference(self, entries=None):
        self.reference = self.root / 'reference.tar'
        if entries is None:
            entries = [('boot.img', self.stock.read_bytes(), tarfile.REGTYPE)]
        with tarfile.open(self.reference, 'w', format=tarfile.USTAR_FORMAT) as tar:
            for name, content, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(content) if kind == tarfile.REGTYPE else 0
                member.linkname = 'outside' if kind == tarfile.SYMTYPE else ''
                tar.addfile(member, io.BytesIO(content) if member.isfile() else None)

    @contextlib.contextmanager
    def reference_pins(self):
        with mock.patch.multiple(package,
                                 REFERENCE_TAR_SHA256=package.sha256(self.reference.read_bytes()),
                                 REFERENCE_BOOT_SHA256=package.sha256(self.stock.read_bytes())):
            yield

    def test_reference_odin_roundtrip(self):
        self.make_reference()
        with self.reference_pins():
            manifest = self.assert_odin('odin-reference')
        self.assertEqual(manifest['format'], 'odin-reference')
        self.assertEqual(manifest['reference_firmware'], 'A042FXXS8EXF2')
        self.assertEqual(manifest['reference_tar_sha256'], package.sha256(self.reference.read_bytes()))
        self.assertEqual(manifest['reference_boot_sha256'], package.sha256(self.stock.read_bytes()))
        self.assertNotIn('stock_boot_sha256', manifest)
        self.assertIsNone(manifest['boot_partition_size'])
        self.assertFalse(manifest['target_partition_size_verified'])
        self.assertFalse(manifest['target_compatibility_verified'])
        self.assertIn('REF-EXF2', manifest['archive'])
        self.assertIn('4.19.191_samsung_BakaSU-SUSFS-NoMount', manifest['archive'])
        self.assertIn('not verified', (self.output / 'README.txt').read_text())
        for name in ('BuildInfo.txt', 'source-revisions.env'):
            self.assertEqual((self.output / name).read_bytes(), (self.artifacts / name).read_bytes())

    def test_reference_digest_guards(self):
        self.make_reference()
        original = self.reference.read_bytes()
        self.invoke('odin-reference', success=False)
        with self.reference_pins():
            self.reference.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            self.invoke('odin-reference', success=False)
            self.reference.write_bytes(original)
            with mock.patch.object(package, 'REFERENCE_BOOT_SHA256', '0' * 64):
                self.invoke('odin-reference', success=False)

    def test_reference_archive_guards(self):
        for entries in ([], [('other.img', boot(), tarfile.REGTYPE)],
                        [('../boot.img', boot(), tarfile.REGTYPE)],
                        [('boot.img', b'', tarfile.SYMTYPE)],
                        [('boot.img', boot(), tarfile.REGTYPE), ('extra', b'', tarfile.REGTYPE)]):
            with self.subTest(entries=[entry[0] for entry in entries]):
                self.make_reference(entries)
                with self.reference_pins():
                    self.invoke('odin-reference', success=False)
        self.reference.write_bytes(b'not a tar archive')
        with self.reference_pins():
            self.invoke('odin-reference', success=False)

    def test_reference_preservation_failures(self):
        self.make_reference()
        with self.reference_pins():
            for fault in ('tool', 'layout', 'ramdisk', 'dtb', 'metadata'):
                with self.subTest(fault=fault), mock.patch.dict(os.environ, A042F_PACKAGE_FAULT=fault):
                    self.invoke('odin-reference', success=False)

    def test_reference_size_policy(self):
        self.make_reference()
        (self.artifacts / 'Image.gz').write_bytes(image(os.urandom(8000)))
        self.checksums()
        self.invoke('odin', success=False)
        with self.reference_pins():
            manifest = self.assert_odin('odin-reference')
        self.assertGreater(manifest['boot_size'], len(self.stock.read_bytes()))
        self.assertIsNone(manifest['boot_partition_size'])
        self.assertFalse(manifest['target_partition_size_verified'])
        self.output = self.root / 'over-ceiling'
        with self.reference_pins(), mock.patch.object(package, 'MAX_REFERENCE_BOOT_BYTES', len(boot())):
            self.invoke('odin-reference', success=False)

    @unittest.skipUnless(os.environ.get('A042F_TEST_MAGISKBOOT'), 'optional native magiskboot not supplied')
    def test_native_reference_roundtrip(self):
        self.tool = Path(os.environ['A042F_TEST_MAGISKBOOT']).resolve(strict=True)
        self.stock.write_bytes(boot() + b'SEANDROIDENFORCE' + bytes(4096))
        self.make_reference()
        with self.reference_pins():
            self.assert_odin('odin-reference')

    def test_odin_roundtrip(self):
        self.assert_odin()

    def test_odin_failures(self):
        for fault in ('tool', 'layout', 'ramdisk', 'dtb', 'metadata'):
            with self.subTest(fault=fault), mock.patch.dict(os.environ, A042F_PACKAGE_FAULT=fault):
                self.invoke('odin', success=False)
        for extra in (('--stock-sha256', '0' * 64), ('--stock-sha256', 'invalid'),
                      ('--firmware', 'A042FA'),
                      ('--firmware', 'A042MXXSGEZF2'), ('--firmware', 'A042F../bad'),
                      ('--boot-partition-size', '1')):
            with self.subTest(extra=extra):
                self.invoke('odin', success=False, extra=extra)

    def test_boot_parser_guards(self):
        for version in (0, 1, 2):
            self.assertEqual(package.boot_image(boot(version))[0], version)
        # Samsung signs the image after the marker; magiskboot keeps only the marker.
        self.assertEqual(package.boot_image(
            boot() + b'SEANDROIDENFORCE' + b'SignerVer02' + bytes(4096))[0], 2)
        mutations = []
        for offset, value in ((40, 3), (36, 3000), (8, 0xffffffff), (1644, 1), (1636, 1)):
            invalid = bytearray(boot())
            struct.pack_into('<I', invalid, offset, value)
            mutations.append(bytes(invalid))
        for invalid in [b'bad', boot()[:-5000], boot() + b'AVBf signature', *mutations]:
            with self.subTest(length=len(invalid)):
                self.stock.write_bytes(invalid)
                self.invoke('odin', success=False)

    def test_size_limit(self):
        (self.artifacts / 'Image.gz').write_bytes(image(bytes(range(256)) * 1000 + os.urandom(5000)))
        self.checksums()
        self.invoke('odin', success=False)
        self.invoke('odin', extra=('--boot-partition-size', str(len(boot()) + 16384)))

    def test_template_guards(self):
        subprocess.run(['sh', '-n', str(package.TEMPLATE)], check=True)
        core = self.root / 'tools'
        core.mkdir()
        (core / 'ak3-core.sh').write_text('''abort() { exit 9; };
SPLITIMG=$PWD; BOOTIMG=fixture;
split_boot() { echo split; }; flash_boot() { echo flash; };
magiskboot() { test -z "$UNPACK_FAIL" || return 1; test "$*" = "unpack -n -h fixture" || return 2; echo raw; };
''')
        template = package.TEMPLATE.read_text()
        properties = ('ro.product.model', 'ro.product.vendor.model', 'ro.product.system.model')
        for model, slot, good, prop in [('SM-A042F', '', True, prop) for prop in properties] + [
                ('SM-A042FN', '', False, properties[0]), ('SM-A042F', '_a', False, properties[0])]:
            shim = 'getprop() { case "$1" in "$MODEL_PROPERTY") echo "$MODEL";; *slot_suffix) echo "$SLOT";; esac; };\n'
            result = subprocess.run(['sh', '-c', shim + template], cwd=self.root, capture_output=True,
                                    text=True, env=dict(os.environ, MODEL=model, SLOT=slot, MODEL_PROPERTY=prop))
            self.assertEqual(result.returncode, 0 if good else 9)
            self.assertEqual(result.stdout, 'split\nraw\nflash\n' if good else '')
        result = subprocess.run(['sh', '-c', shim + template], cwd=self.root, capture_output=True,
                                text=True, env=dict(os.environ, MODEL='SM-A042F', SLOT='',
                                MODEL_PROPERTY=properties[0], UNPACK_FAIL='1'))
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout, 'split\n')

    @unittest.skipUnless(os.environ.get('A042F_TEST_MAGISKBOOT'), 'optional native magiskboot not supplied')
    def test_native_anykernel_sequence(self):
        tools = self.root / 'tools'
        tools.mkdir()
        (tools / 'magiskboot').symlink_to(Path(os.environ['A042F_TEST_MAGISKBOOT']).resolve(strict=True))
        (tools / 'ak3-core.sh').write_text('''AKHOME=$PWD;
BOOTIMG=$AKHOME/stock.img; SPLITIMG=$AKHOME/split;
abort() { echo "$*" >&2; exit 9; };
getprop() { test "$1" = ro.product.model && echo SM-A042F; };
split_boot() {
  mkdir "$SPLITIMG";
  (cd "$SPLITIMG" && magiskboot unpack -h "$BOOTIMG") || exit 1;
};
flash_boot() {
  cp "$AKHOME/Image.gz" "$SPLITIMG/kernel";
  (cd "$SPLITIMG" && magiskboot repack "$BOOTIMG" "$AKHOME/newboot.img") || exit 1;
};
''')
        self.stock.write_bytes(boot() + b'SEANDROIDENFORCE' + bytes(4096))
        (self.root / 'Image.gz').write_bytes((self.artifacts / 'Image.gz').read_bytes())
        result = subprocess.run(['sh', str(package.TEMPLATE)], cwd=self.root, capture_output=True,
                                text=True, env=dict(os.environ, PATCHVBMETAFLAG='false',
                                PATH=str(tools) + os.pathsep + os.environ['PATH']))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = package.boot_image(self.stock.read_bytes())
        after = package.boot_image((self.root / 'newboot.img').read_bytes())
        self.assertEqual(before[:2], after[:2])
        self.assertEqual(after[2]['kernel'], (self.artifacts / 'Image.gz').read_bytes())
        self.assertEqual({k: v for k, v in before[2].items() if k != 'kernel'},
                         {k: v for k, v in after[2].items() if k != 'kernel'})

    @unittest.skipUnless(os.environ.get('A042F_TEST_MAGISKBOOT'), 'optional native magiskboot not supplied')
    def test_native_magiskboot_roundtrip(self):
        self.tool = Path(os.environ['A042F_TEST_MAGISKBOOT']).resolve(strict=True)
        for version in (0, 1, 2):
            with self.subTest(version=version):
                self.stock.write_bytes(boot(version))
                self.output = self.root / f'native-v{version}'
                self.assert_odin()

    @unittest.skipUnless(os.environ.get('A042F_TEST_MAGISKBOOT'), 'optional native magiskboot not supplied')
    def test_native_samsung_trailer(self):
        self.tool = Path(os.environ['A042F_TEST_MAGISKBOOT']).resolve(strict=True)
        self.stock.write_bytes(boot() + b'SEANDROIDENFORCE' + bytes(4096))
        self.assert_odin()


if __name__ == '__main__':
    unittest.main(verbosity=2)
