#!/usr/bin/env python3
"""Run the root build orchestration with host command stubs, never a kernel build."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]


class BuildContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='a04e-build-contract-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ['.github/scripts/build-root.sh', 'arch/arm64/configs/a04e-root.config']:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, target)
        self.write('out/a042f-sources.env', 'NOMOUNT_COMMITS=123\n')
        self.write('toolchain/clang/host/linux-x86/clang-r383902/bin/clang', '#!/bin/sh\nexit 0\n', executable=True)
        cross = 'toolchain/gcc/linux-x86/aarch64/aarch64-linux-android-4.9/bin/aarch64-linux-androidkernel-'
        self.write(cross + 'elfedit', '#!/bin/sh\nexit 0\n', executable=True)
        self.write('scripts/kconfig/merge_config.sh', '#!/bin/sh\ncat "$5" >> out/.config\n')
        self.write('scripts/check-a042f-clocks.py', 'from pathlib import Path\nassert Path("out/System.map").is_file()\n')
        self.write('.github/scripts/verify-samsung.py', 'from pathlib import Path\nassert Path("out/System.map").is_file()\n')
        self.write('bin/git', '#!/bin/sh\n[ "$1" = diff ] && [ "$2" = --exit-code ]\n', executable=True)
        self.write('bin/make', '''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path.cwd()
with (root / 'commands.jsonl').open('a') as stream:
    stream.write(json.dumps({'args': sys.argv[1:], 'arch': os.environ.get('ARCH'),
                             'android': os.environ.get('ANDROID_MAJOR_VERSION')}) + '\\n')
if 'a04e_defconfig' in sys.argv:
    (root / 'out/.config').write_text('CONFIG_ARM64=y\\n')
elif 'olddefconfig' not in sys.argv:
    (root / 'out/System.map').write_text('fixture\\n')
''', executable=True)

    def write(self, name, text, executable=False):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if executable:
            path.chmod(0o755)

    def run_build(self):
        return subprocess.run(['bash', '.github/scripts/build-root.sh'], cwd=self.root,
                              env={**os.environ, 'PATH': str(self.root / 'bin') + ':' + os.defpath},
                              capture_output=True, text=True, timeout=20)

    def test_binutils_only_build_preserves_samsung_flags(self):
        import json
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [json.loads(row) for row in (self.root / 'commands.jsonl').read_text().splitlines()]
        self.assertEqual(len(commands), 3)
        self.assertIn('a04e_defconfig', commands[0]['args'])
        self.assertIn('olddefconfig', commands[1]['args'])
        for command in commands:
            self.assertEqual((command['arch'], command['android']), ('arm64', 'r'))
            for setting in ['KCFLAGS=-w', 'CONFIG_SECTION_MISMATCH_WARN_ONLY=y', 'NOMOUNT_COMMITS=123']:
                self.assertIn(setting, command['args'])

    def test_invalid_nomount_count_stops_before_make(self):
        self.write('out/a042f-sources.env', 'NOMOUNT_COMMITS=invalid\n')
        self.assertNotEqual(self.run_build().returncode, 0)
        self.assertFalse((self.root / 'commands.jsonl').exists())

    def test_existing_configuration_is_rejected(self):
        self.write('out/.config', 'old configuration\n')
        self.assertNotEqual(self.run_build().returncode, 0)
        self.assertEqual((self.root / 'out/.config').read_text(), 'old configuration\n')

    def test_dropped_feature_stops_before_compilation(self):
        self.write('scripts/kconfig/merge_config.sh', '#!/bin/sh\nexit 0\n')
        self.assertNotEqual(self.run_build().returncode, 0)
        self.assertFalse((self.root / 'out/System.map').exists())


if __name__ == '__main__':
    unittest.main()
