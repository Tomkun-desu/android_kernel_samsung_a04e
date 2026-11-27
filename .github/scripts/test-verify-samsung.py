#!/usr/bin/env python3
"""Check output rejection cases and the Samsung build command contract."""

import gzip
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('verify_samsung', SCRIPTS / 'verify-samsung.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='samsung-output-test-')
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.image = self.out / 'arch/arm64/boot/Image'
        self.image.parent.mkdir(parents=True)
        raw = bytearray(512)
        raw[56:60] = b'ARM\x64'
        banner = b'Linux version 4.19.191-synthetic-fixture\0'
        raw[128:128 + len(banner)] = banner
        self.image.write_bytes(raw)
        self.image.with_name('Image.gz').write_bytes(gzip.compress(raw))
        elf = bytearray(64)
        elf[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', elf, 18, 183)
        (self.out / 'vmlinux').write_bytes(elf)
        (self.out / 'System.map').write_text('ffffff8008080000 T _text\n')
        self.config = '\n'.join(['CONFIG_ARM64=y', 'CONFIG_MACH_MT6765=y', 'CONFIG_HQ_PROJECT_HS04=y', 'CONFIG_CC_IS_CLANG=y', 'CONFIG_KERNEL_GZIP=y', 'CONFIG_SECURITY_DEFEX=y', 'CONFIG_SECTION_MISMATCH_WARN_ONLY=y']) + '\n'
        (self.out / '.config').write_text(self.config)
        self.dtb = self.out / 'arch/arm64/boot/dts/mediatek/mt6765_a04e_fixture.dtb'
        self.dtb.parent.mkdir(parents=True)
        self.dtb.write_bytes(struct.pack('>10I', 0xd00dfeed, 64, 56, 64, 40, 17, 16, 0, 0, 8) + bytes(24))

    def test_valid_synthetic_outputs(self):
        result = verify.check_outputs(self.out)
        self.assertEqual(result['banner'], 'Linux version 4.19.191-synthetic-fixture')
        self.assertEqual(len(result['dtbs']), 1)
        self.assertEqual(result['modules'], [])

    def test_kernel_architecture_and_compression_are_checked(self):
        self.image.with_name('Image.gz').write_bytes(gzip.compress(b'wrong kernel'))
        with self.assertRaisesRegex(ValueError, 'does not decompress'):
            verify.check_outputs(self.out)
        data = bytearray(self.image.read_bytes())
        data[56:60] = b'FAIL'
        self.image.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'ARM64 Linux boot header'):
            verify.check_outputs(self.out)

    def test_wrong_linked_architecture_is_rejected(self):
        data = bytearray((self.out / 'vmlinux').read_bytes())
        struct.pack_into('<H', data, 18, 62)
        (self.out / 'vmlinux').write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'ARM64 ELF64'):
            verify.check_outputs(self.out)

    def test_stock_configuration_and_root_absence_are_checked(self):
        (self.out / '.config').write_text(self.config.replace('CONFIG_SECURITY_DEFEX=y', '# CONFIG_SECURITY_DEFEX is not set'))
        with self.assertRaisesRegex(ValueError, 'Missing stock configuration'):
            verify.check_outputs(self.out)
        (self.out / '.config').write_text(self.config + 'CONFIG_KSU=y\n')
        with self.assertRaisesRegex(ValueError, 'unexpectedly enables root'):
            verify.check_outputs(self.out)

    def test_missing_and_corrupt_device_trees_are_rejected(self):
        self.dtb.write_bytes(bytes(64))
        with self.assertRaisesRegex(ValueError, 'Invalid device-tree'):
            verify.check_outputs(self.out)
        self.dtb.unlink()
        with self.assertRaisesRegex(ValueError, 'No A04e device trees'):
            verify.check_outputs(self.out)

    def test_root_configuration_requires_linked_functions_and_key_type(self):
        fragment = (SCRIPTS.parents[1] / 'arch/arm64/configs/a04e-root.config').read_text()
        config = self.config.replace('CONFIG_SECURITY_DEFEX=y\n', '') + fragment
        config += 'CONFIG_64BIT=y\nCONFIG_THREAD_INFO_IN_TASK=y\nCONFIG_SECURITY_SELINUX=y\n'
        (self.out / '.config').write_text(config)
        symbols = ['ksu_handle_execveat', 'ksu_handle_post_execveat_sucompat', 'ksu_handle_sys_reboot', 'susfs_init', 'susfs_set_priority_uid_filter', 'nomount_init', 'nm_key_preparse', 'nm_hijacked_lookup', 'path_umount']
        rows = [f'ffffff8008080000 T {name}\n' for name in symbols]
        (self.out / 'System.map').write_text(''.join(rows) + 'ffffff8008090000 D nm_key_type\n')
        result = verify.check_outputs(self.out, rooted=True)
        self.assertEqual(set(result['linked_root_symbols']), set(symbols + ['nm_key_type']))
        (self.out / 'System.map').write_text(''.join(rows) + 'ffffff8008090000 U nm_key_type\n')
        with self.assertRaisesRegex(ValueError, 'Missing linked root symbols'):
            verify.check_outputs(self.out, rooted=True)


class BuildContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='samsung-build-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        target = self.root / '.github/scripts/build-samsung.sh'
        target.parent.mkdir(parents=True)
        shutil.copyfile(SCRIPTS / 'build-samsung.sh', target)
        self.script = target
        (self.root / 'arch/arm64/configs').mkdir(parents=True)
        (self.root / 'arch/arm64/configs/a04e_defconfig').write_text('fixture\n')
        (self.root / 'Makefile').write_text('fixture\n')
        for name in ['toolchain/clang/host/linux-x86/clang-r383902/bin/clang', 'toolchain/gcc/linux-x86/aarch64/aarch64-linux-android-4.9/bin/aarch64-linux-androidkernel-ld', 'toolchain/gcc/linux-x86/aarch64/aarch64-linux-android-4.9/bin/aarch64-linux-androidkernel-elfedit']:
            self.executable(self.root / name, '#!/bin/sh\nexit 0\n')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.executable(self.bin / 'make', '''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
p = Path('commands.jsonl')
with p.open('a') as f:
    f.write(json.dumps({'arguments': sys.argv[1:], 'ARCH': os.environ.get('ARCH'), 'ANDROID_MAJOR_VERSION': os.environ.get('ANDROID_MAJOR_VERSION'), 'LLVM': os.environ.get('LLVM')}) + '\\n')
if os.environ.get('FAIL_CONFIGURE') and 'a04e_defconfig' in sys.argv:
    sys.exit(23)
''')
        self.executable(self.bin / 'git', '#!/bin/sh\nexit 0\n')
        self.executable(self.bin / 'python3', '#!/bin/sh\ntouch verifier-was-called\n')
        self.executable(self.bin / 'nproc', '#!/bin/sh\necho 2\n')

    def executable(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        path.chmod(0o755)

    def invoke(self, **extra):
        environment = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'], LLVM='1', **extra)
        result = subprocess.run(['bash', str(self.script)], cwd=self.root, env=environment, capture_output=True, text=True, timeout=15)
        commands = [json.loads(s) for s in (self.root / 'commands.jsonl').read_text().splitlines()]
        return result, commands

    def test_readme_flags_and_source_toolchain_are_used(self):
        result, commands = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0]['arguments'][-1], 'a04e_defconfig')
        self.assertEqual(commands[1]['arguments'][-1], '-j2')
        for command in commands:
            self.assertEqual(command['ARCH'], 'arm64')
            self.assertEqual(command['ANDROID_MAJOR_VERSION'], 'r')
            self.assertIsNone(command['LLVM'])
            self.assertIn('KCFLAGS=-w', command['arguments'])
            self.assertIn('CONFIG_SECTION_MISMATCH_WARN_ONLY=y', command['arguments'])
            self.assertTrue(any(s.startswith('CC=') and s.endswith('/clang-r383902/bin/clang') for s in command['arguments']))
            self.assertTrue(any(s.startswith('CROSS_COMPILE=') and s.endswith('/aarch64-linux-androidkernel-') for s in command['arguments']))
        self.assertTrue((self.root / 'verifier-was-called').is_file())

    def test_configuration_failure_stops_compilation(self):
        result, commands = self.invoke(FAIL_CONFIGURE='1')
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(commands), 1)
        self.assertFalse((self.root / 'verifier-was-called').exists())


if __name__ == '__main__':
    unittest.main()
