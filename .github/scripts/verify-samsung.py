#!/usr/bin/env python3
"""Verify and collect Samsung stock or experimental root build outputs."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_outputs(out, rooted=False):
    image = out / 'arch/arm64/boot/Image'
    compressed = image.with_name('Image.gz')
    vmlinux = out / 'vmlinux'
    for path in [image, compressed, vmlinux, out / 'System.map', out / '.config']:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError('Missing build output: ' + str(path))
    with image.open('rb') as stream:
        header = stream.read(64)
    if len(header) != 64 or header[56:60] != b'ARM\x64':
        raise ValueError('Image does not have the ARM64 Linux boot header')
    with vmlinux.open('rb') as stream:
        elf = stream.read(64)
    if len(elf) != 64 or elf[:6] != b'\x7fELF\x02\x01' or struct.unpack_from('<H', elf, 18)[0] != 183:
        raise ValueError('vmlinux is not a little-endian ARM64 ELF64 binary')
    digest = hashlib.sha256()
    with gzip.open(compressed, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    if digest.hexdigest() != sha256(image):
        raise ValueError('Image.gz does not decompress to the built Image')
    with image.open('rb') as stream:
        data = stream.read()
    banners = [part.split(b'\0', 1)[0].split(b'\n', 1)[0].decode(errors='replace') for part in data.split(b'Linux version ')[1:]]
    banners = ['Linux version ' + text for text in banners if text.startswith('4.19.191')]
    if len(banners) != 1:
        raise ValueError('Expected one Linux 4.19.191 banner: ' + repr(banners))
    config = (out / '.config').read_text().splitlines()
    required = ['CONFIG_ARM64=y', 'CONFIG_MACH_MT6765=y', 'CONFIG_HQ_PROJECT_HS04=y', 'CONFIG_CC_IS_CLANG=y', 'CONFIG_KERNEL_GZIP=y', 'CONFIG_SECTION_MISMATCH_WARN_ONLY=y']
    if rooted:
        required.extend(line for line in Path('arch/arm64/configs/a04e-root.config').read_text().splitlines() if line)
        required.extend(['CONFIG_64BIT=y', 'CONFIG_THREAD_INFO_IN_TASK=y', 'CONFIG_SECURITY_SELINUX=y'])
    else:
        required.append('CONFIG_SECURITY_DEFEX=y')
    missing = [line for line in required if line not in config]
    if missing:
        raise ValueError('Missing stock configuration: ' + repr(missing))
    if not rooted and any(line.startswith(('CONFIG_KSU=', 'CONFIG_KSU_SUSFS=', 'CONFIG_NOMOUNT=')) and line.endswith(('=y', '=m')) for line in config):
        raise ValueError('The stock baseline unexpectedly enables root integration')
    if rooted:
        symbols = {fields[2]: fields[1] for row in (out / 'System.map').read_text().splitlines() if len(fields := row.split()) == 3}
        required_symbols = ['ksu_handle_execveat', 'ksu_handle_post_execveat_sucompat', 'ksu_handle_sys_reboot', 'susfs_init', 'susfs_set_priority_uid_filter', 'nomount_init', 'nm_key_preparse', 'nm_hijacked_lookup', 'path_umount']
        missing_symbols = [name for name in required_symbols if symbols.get(name) not in ('T', 't')]
        if symbols.get('nm_key_type') not in ('D', 'd'):
            missing_symbols.append('nm_key_type')
        if missing_symbols:
            raise ValueError('Missing linked root symbols: ' + repr(missing_symbols))
    dtbs = sorted((out / 'arch/arm64/boot/dts/mediatek').glob('mt6765_a04e*.dtb'))
    if not dtbs:
        raise ValueError('No A04e device trees were built')
    for dtb in dtbs:
        raw = dtb.read_bytes()
        if len(raw) < 40:
            raise ValueError('Truncated device tree: ' + str(dtb))
        magic, total, structure, strings, reserve, version, compatible, cpu, string_size, structure_size = struct.unpack_from('>10I', raw)
        if magic != 0xd00dfeed or total != len(raw) or version < 17 or compatible > 17 or structure + structure_size > total or strings + string_size > total or reserve >= total:
            raise ValueError('Invalid device-tree header: ' + str(dtb))
    return {'banner': banners[0], 'configuration_assertions': required, 'linked_root_symbols': required_symbols + ['nm_key_type'] if rooted else [], 'dtbs': [str(p.relative_to(out)) for p in dtbs], 'linked_vmlinux': {'bytes': vmlinux.stat().st_size, 'sha256': sha256(vmlinux), 'format': 'ELF64 little-endian AArch64'}, 'modules': [str(p.relative_to(out)) for p in sorted(out.rglob('*.ko'))]}


def collect(out, artifacts, verified, rooted=False):
    artifacts.mkdir(parents=True, exist_ok=False)
    targets = [('arch/arm64/boot/Image', 'Image'), ('arch/arm64/boot/Image.gz', 'Image.gz'), ('System.map', 'System.map'), ('.config', 'kernel.config')]
    targets.extend((name, 'dtb/' + Path(name).name) for name in verified['dtbs'])
    targets.extend((name, 'modules/' + name) for name in verified['modules'])
    for source, destination in targets:
        path = artifacts / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out / source, path)
    toolchains = Path(os.environ['RUNNER_TEMP']) / 'samsung-toolchain-verification.json'
    shutil.copyfile(toolchains, artifacts / toolchains.name)
    record = {
        'kernel_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'sources': json.loads(Path('.github/samsung-sources.json').read_text()),
        'recipe': {'defconfig': 'a04e_defconfig', 'ARCH': 'arm64', 'ANDROID_MAJOR_VERSION': 'r', 'KCFLAGS': '-w', 'CONFIG_SECTION_MISMATCH_WARN_ONLY': 'y'},
        'verification': verified,
        'device_tested': False,
        'flashable_package': False,
    }
    if rooted:
        sources = dict(line.split('=', 1) for line in (out / 'a042f-sources.env').read_text().splitlines())
        for name in ['BAKASU', 'NOMOUNT']:
            actual = subprocess.check_output(['git', '-C', 'KernelSU' if name == 'BAKASU' else 'NoMount', 'rev-parse', 'HEAD'], text=True).strip()
            if actual != sources[name + '_SHA']:
                raise ValueError('Prepared source revision changed: ' + name)
        record['floating_sources'] = sources
        preparation = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'])
        record['working_tree_diff_sha256'] = hashlib.sha256(preparation).hexdigest()
        (artifacts / 'source-preparation.patch').write_bytes(preparation)
        shutil.copyfile(out / 'a042f-sources.env', artifacts / 'source-revisions.env')
        (artifacts / 'BuildInfo.txt').write_text(
            'Samsung Galaxy A04e SM-A042F, experimental BakaSU/SUSFS/NoMount build.\n'
            'Base: supplied official Samsung Android 14 kernel plus the official EZE1 update.\n'
            'Kernel commit: ' + record['kernel_commit'] + '\n'
            'Recipe: a04e_defconfig + a04e-root.config, ARCH=arm64, ANDROID_MAJOR_VERSION=r, '
            'Clang r383902, Android GCC 4.9 binutils, KCFLAGS=-w, CONFIG_SECTION_MISMATCH_WARN_ONLY=y.\n'
            'Resolved floating sources and paired SUSFS hashes: source-revisions.env.\n'
            'DEFEX disabled by the root fragment; SELinux remains enabled.\n'
            'No device, boot, userspace NoMount or installed firmware compatibility test performed.\n')
    (artifacts / 'build-info.json').write_text(json.dumps(record, indent=2) + '\n')
    sums = [sha256(path) + '  ' + path.relative_to(artifacts).as_posix() for path in sorted(artifacts.rglob('*')) if path.is_file()]
    (artifacts / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
    print(json.dumps(record, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--root', action='store_true')
    args = parser.parse_args()
    collect(args.out, args.artifacts, check_outputs(args.out, args.root), args.root)


if __name__ == '__main__':
    main()
