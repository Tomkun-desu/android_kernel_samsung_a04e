#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
python3 -B - <<'PY'
from pathlib import Path
import hashlib
import json
import os
import subprocess

root = Path.cwd()
temporary = Path(os.environ['RUNNER_TEMP'])
manifest = json.loads((root / '.github/toolchains.json').read_text())
record = {}
for name, spec in manifest.items():
    archive = temporary / ('samsung-' + name + '.tar.gz')
    destination = root / spec['directory']
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run(['curl', '--fail', '--location', '--retry', '3', '--connect-timeout', '30', '--max-time', '600', spec['url'], '-o', str(archive)], check=True)
    digest = hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    subprocess.run(['tar', '-xzf', str(archive), '-C', str(destination)], check=True)
    record[name] = dict(spec, downloaded_sha256=digest.hexdigest())
clang = root / manifest['clang']['directory'] / 'bin/clang'
prefix = root / manifest['binutils']['directory'] / 'bin/aarch64-linux-androidkernel-'
for program in [clang, *(Path(str(prefix) + suffix) for suffix in ['ld', 'ar', 'nm', 'objcopy', 'objdump', 'readelf', 'size', 'strip', 'elfedit'])]:
    if not program.is_file() or not os.access(program, os.X_OK):
        raise SystemExit('Missing executable from pinned toolchain: ' + str(program))
record['clang_version'] = subprocess.check_output([str(clang), '--version'], text=True).strip()
record['linker_version'] = subprocess.check_output([str(prefix) + 'ld', '--version'], text=True).strip()
if 'clang version 11.0.1' not in record['clang_version'] or 'r383902' not in record['clang_version']:
    raise SystemExit('Unexpected compiler: ' + record['clang_version'])
(temporary / 'samsung-toolchain-verification.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record, indent=2))
PY
