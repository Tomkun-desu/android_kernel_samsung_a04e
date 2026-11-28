#!/usr/bin/env python3
"""Stage experimental SM-A042F packages; never flash or fetch firmware."""

import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import zlib


ANYKERNEL_SHA = '41f04bdb4ee30a44e429c2c1131d5e8bb32b15f1'
ARM64_SHA = 'cc7d2cee9b1b4298f6ab8666eb4486dea342748c'
BINARIES = ('busybox', 'fec', 'httools_static', 'lptools_static',
            'magiskboot', 'magiskpolicy', 'snapshotupdater_static')
TEMPLATE = Path(__file__).resolve().parents[1] / 'packaging/a042f/anykernel.sh'
ARTIFACTS = ('Image.gz', 'BuildInfo.txt', 'source-revisions.env')
REFERENCE_URL = ('https://github.com/ravindu644/android_kernel_a042f/releases/download/'
                 'v3/KernelSU-Next-SM-A042F.tar')
REFERENCE_TAR_SHA256 = 'e30a4c925b8d11591d91e210221ebfdab21a9f4ec690dababee4951b0ef100a8'
REFERENCE_BOOT_SHA256 = '8dc9c9351393feddf77a2b3991b0a2544c063794a5e5645bfd86c0d5cb2687f9'
MAX_REFERENCE_BOOT_BYTES = 128 * 1024 * 1024  # Resource ceiling, not a partition-capacity claim.


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def regular(root, name):
    parts = PurePosixPath(name).parts
    require(parts and not name.startswith('/') and
            all(part not in ('.', '..') for part in parts) and
            PurePosixPath(name).as_posix() == name, f'Unsafe path: {name}')
    path = root
    for part in parts:
        path = path / part
        require(not path.is_symlink(), f'Symlink not allowed: {path}')
    require(path.is_file(), f'Missing regular file: {path}')
    return path


def artifacts(root):
    root = Path(root).resolve(strict=True)
    checks = regular(root, 'SHA256SUMS').read_text().splitlines()
    verified = {}
    for line in checks:
        match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *]([A-Za-z0-9_./-]+)', line)
        require(match, 'Malformed SHA256SUMS line')
        digest, name = match.groups()
        require(name not in verified and name != 'SHA256SUMS', 'Duplicate/self checksum')
        data = regular(root, name).read_bytes()
        require(sha256(data) == digest.lower(), f'Checksum mismatch: {name}')
        if name in ARTIFACTS:
            require(data, f'Empty artifact: {name}')
            verified[name] = data
        else:
            verified[name] = None
    require(all(name in verified for name in ARTIFACTS), 'Required artifact not checksummed')
    with gzip.GzipFile(fileobj=io.BytesIO(verified['Image.gz'])) as stream:
        header = stream.read(64)
        require(len(header) == 64 and header[56:60] == b'ARM\x64', 'Not an ARM64 Image')
        while stream.read(1024 * 1024):
            pass  # Consume to EOF to verify gzip integrity without retaining the raw kernel.
    return {name: verified[name] for name in ARTIFACTS}


def run(command, cwd=None, env=None):
    result = subprocess.run([str(arg) for arg in command], cwd=cwd, env=env,
                            capture_output=True, text=True, timeout=120)
    require(result.returncode == 0,
            f'{command[0]} {command[1]} failed: {result.stderr[-2000:]}')
    return result.stdout.strip()


def checkout(path, pin, names):
    root = Path(path).resolve(strict=True)
    git = ['git', '-C', root]
    require(run(git + ['rev-parse', '--show-toplevel']) == str(root), 'Not a checkout root')
    require(run(git + ['rev-parse', 'HEAD']) == pin, 'Unexpected AnyKernel checkout pin')
    require(not run(git + ['status', '--porcelain', '--untracked-files=all', '--ignored']),
            'AnyKernel checkout must be clean (including untracked/ignored files)')
    run(git + ['ls-files', '--error-unmatch', '--', *names])
    return root


@contextmanager
def staging(output):
    output = Path(os.path.abspath(output))
    require(not os.path.lexists(output), f'Output already exists: {output}')
    with tempfile.TemporaryDirectory(prefix='.a042f-package-', dir=output.parent) as work:
        payload = Path(work) / 'package'
        payload.mkdir()
        yield payload, Path(work)
        require(not os.path.lexists(output), f'Output already exists: {output}')
        payload.rename(output)


def finish(payload, manifest):
    (payload / 'package-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    files = sorted(path for path in payload.rglob('*') if path.is_file())
    (payload / 'SHA256SUMS').write_text(''.join(
        f'{sha256(path.read_bytes())}  {path.relative_to(payload).as_posix()}\n' for path in files))


def anykernel(args, data, manifest):
    scripts = ('META-INF/com/google/android/update-binary',
               'META-INF/com/google/android/updater-script', 'tools/ak3-core.sh', 'LICENSE')
    source = checkout(args.anykernel, ANYKERNEL_SHA, scripts)
    arm64 = checkout(args.anykernel_arm64, ARM64_SHA, BINARIES)
    files = {name: regular(source, name).read_bytes() for name in scripts}
    for name in BINARIES:
        binary = regular(arm64, name).read_bytes()
        require(len(binary) >= 64 and binary[:6] == b'\x7fELF\x02\x01' and
                struct.unpack_from('<H', binary, 18)[0] == 183, f'Not AArch64 ELF: {name}')
        files['tools/' + name] = binary
    files['anykernel.sh'] = TEMPLATE.read_bytes()
    files.update(data)
    manifest.update(format='anykernel', upstreams={'anykernel': ANYKERNEL_SHA,
                                                 'anykernel_arm64': ARM64_SHA})
    with staging(args.output) as (payload, _):
        for name, content in files.items():
            target = payload / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o755 if name.startswith('tools/') or
                         name.endswith(('update-binary', 'anykernel.sh')) else 0o644)
        finish(payload, manifest)


def boot_image(data):
    """Parse only plain Android boot v0-v2; reject unknown wrappers/trailers/padding."""
    require(len(data) >= 1632 and data[:8] == b'ANDROID!', 'Not a plain Android boot image')
    u32 = lambda offset: struct.unpack_from('<I', data, offset)[0]
    version, page = u32(40), u32(36)
    require(version in (0, 1, 2), 'Only boot header versions 0-2 are supported')
    length = (1632, 1648, 1660)[version]
    require(len(data) >= length and length <= page <= 65536 and page & (page - 1) == 0,
            'Invalid boot header/page size')
    require(u32(8) > 0, 'Empty boot kernel')
    sizes = [('kernel', u32(8)), ('ramdisk.cpio', u32(16)), ('second', u32(24))]
    if version:
        require(u32(1644) == length, 'Invalid declared header size')
        sizes.append(('recovery_dtbo', u32(1632)))
    if version == 2:
        sizes.append(('dtb', u32(1648)))
    offset = page
    require(not any(data[length:page]), 'Unsupported nonzero header padding')
    components = {}
    for name, size in sizes:
        if name == 'recovery_dtbo':
            declared = struct.unpack_from('<Q', data, 1636)[0]
            require(declared == (offset if size else 0), 'Invalid recovery DTBO offset')
        end = offset + size
        require(end <= len(data), f'Truncated boot component: {name}')
        components[name] = data[offset:end]
        aligned = (end + page - 1) // page * page
        require(not any(data[end:min(aligned, len(data))]), 'Unsupported component padding')
        offset = aligned
    trailer = data[offset:]
    samsung = b'SEANDROIDENFORCE'
    marker = samsung if trailer.startswith(samsung) else b''
    require(marker or not any(trailer), 'Unsupported signed/footer/trailing data')
    metadata = bytearray(data[:length])
    metadata[8:12] = bytes(4)  # Kernel size and boot ID are regenerated by magiskboot.
    metadata[576:608] = bytes(32)
    if version:
        metadata[1636:1644] = bytes(8)  # Recovery offset moves with kernel page count.
    return version, bytes(metadata) + marker, components


def odin(args, data, manifest):
    reference = args.command == 'odin-reference'
    if reference:
        reference_tar = Path(args.reference_tar).read_bytes()
        require(sha256(reference_tar) == REFERENCE_TAR_SHA256, 'Reference tar SHA256 mismatch')
        with tarfile.open(fileobj=io.BytesIO(reference_tar), mode='r:') as tar:
            members = tar.getmembers()
            require(len(members) == 1 and members[0].name == 'boot.img' and
                    members[0].isfile() and 0 < members[0].size <= MAX_REFERENCE_BOOT_BYTES,
                    'Reference tar must contain only a regular boot.img')
            stock = tar.extractfile(members[0]).read()
        require(sha256(stock) == REFERENCE_BOOT_SHA256, 'Reference boot SHA256 mismatch')
        limit = MAX_REFERENCE_BOOT_BYTES
        archive_name = 'AP_SM-A042F_REF-EXF2_4.19.191_samsung_BakaSU-SUSFS-NoMount_experimental.tar.md5'
        manifest.update(format='odin-reference', reference_firmware='A042FXXS8EXF2',
                        reference_url=REFERENCE_URL, reference_tar_sha256=sha256(reference_tar),
                        reference_boot_sha256=sha256(stock), reference_boot_size=len(stock),
                        boot_partition_size=None, packaging_max_boot_bytes=limit,
                        target_partition_size_verified=False, target_compatibility_verified=False)
    else:
        require(re.fullmatch(r'[0-9a-fA-F]{64}', args.stock_sha256), 'Invalid stock SHA256')
        require(re.fullmatch(r'A042F[A-Z0-9]{6,}', args.firmware), 'Invalid A042F firmware/AP identifier')
        stock = Path(args.stock_boot).read_bytes()
        require(sha256(stock) == args.stock_sha256.lower(), 'Stock boot SHA256 mismatch')
        limit = args.boot_partition_size if args.boot_partition_size is not None else len(stock)
        require(limit >= len(stock), 'Partition limit must cover the original boot image')
        archive_name = f'AP_{args.firmware}_4.19.191_samsung_BakaSU-SUSFS-NoMount_experimental.tar.md5'
        manifest.update(format='odin', firmware=args.firmware, stock_boot_sha256=sha256(stock),
                        stock_boot_size=len(stock), boot_partition_size=limit)
    version, metadata, components = boot_image(stock)
    tool = Path(args.magiskboot).resolve(strict=True)
    env = dict(os.environ, PATCHVBMETAFLAG='false')
    with staging(args.output) as (payload, work):
        original, rebuilt = work / 'stock.img', work / 'boot.img'
        original.write_bytes(stock)
        unpack = work / 'unpack'
        unpack.mkdir()
        run([tool, 'unpack', '-n', '-h', original], cwd=unpack, env=env)
        allowed = {'header', 'kernel', 'kernel_dtb', 'ramdisk.cpio', 'second',
                   'dtb', 'extra', 'recovery_dtbo'}
        require(all(p.name in allowed and p.is_file() and not p.is_symlink()
                    for p in unpack.iterdir()), 'Unexpected magiskboot unpack layout')
        require((unpack / 'header').is_file() and (unpack / 'kernel').is_file(),
                'magiskboot omitted kernel/header')
        (unpack / 'kernel').write_bytes(data['Image.gz'])
        run([tool, 'repack', original, rebuilt], cwd=unpack, env=env)
        newboot = rebuilt.read_bytes()
        newversion, newmetadata, newcomponents = boot_image(newboot)
        require(version == newversion and metadata == newmetadata, 'Boot metadata changed')
        require(all(newcomponents[name] == content for name, content in components.items()
                    if name != 'kernel'), 'Stock boot component changed')
        # magiskboot may reattach an existing appended DTB; never drop or replace it.
        kernel_dtb = (unpack / 'kernel_dtb').read_bytes() if (unpack / 'kernel_dtb').exists() else b''
        require(newcomponents['kernel'] == data['Image.gz'] + kernel_dtb,
                'Repacked kernel differs from supplied Image.gz/stock appended DTB')
        require(len(newboot) <= limit, 'Repacked boot exceeds ' +
                ('reference package resource ceiling' if reference else 'boot partition size limit'))
        archive = payload / archive_name
        with tarfile.open(archive, 'w', format=tarfile.USTAR_FORMAT) as tar:
            info = tarfile.TarInfo('boot.img')
            info.size, info.mode = len(newboot), 0o644
            with rebuilt.open('rb') as stream:
                tar.addfile(info, stream)
        digest = hashlib.md5(archive.read_bytes()).hexdigest()
        with archive.open('ab') as stream:
            stream.write(f'{digest}  {archive.name[:-4]}\n'.encode('ascii'))
        manifest.update(boot_header_version=version, boot_sha256=sha256(newboot), boot_size=len(newboot),
                        magiskboot_sha256=sha256(tool.read_bytes()), archive=archive.name,
                        oem_signature_retained=False, firmware_identity_verified=False)
        if reference:
            for name in ('BuildInfo.txt', 'source-revisions.env'):
                (payload / name).write_bytes(data[name])
            (payload / 'README.txt').write_text(
                'Experimental SM-A042F boot-only Odin package.\n'
                'Extract the tar.md5 from the downloaded artifact ZIP for Odin AP.\n'
                'Only the kernel is replaced. Ramdisk, DTB and boot metadata come from\n'
                'ravindu644/android_kernel_a042f release v3, firmware A042FXXS8EXF2.\n'
                'The reference ramdisk identifies Android 12; this is not an EZF2 stock boot.\n'
                'Target compatibility and boot partition capacity are not verified.\n'
                'The package size ceiling is a resource limit, not a measured partition size.\n'
                'Never boot tested. No vbmeta patch is included. Device/root/SUSFS testing is required.\n'
                'Keep an exact stock boot and a working recovery route before testing.\n')
        finish(payload, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('anykernel', 'odin', 'odin-reference'):
        command = commands.add_parser(name)
        command.add_argument('--artifacts', required=True)
        command.add_argument('--output', required=True)
        if name == 'anykernel':
            command.add_argument('--anykernel', required=True)
            command.add_argument('--anykernel-arm64', required=True)
        elif name == 'odin':
            for option in ('stock-boot', 'stock-sha256', 'firmware', 'magiskboot'):
                command.add_argument('--' + option, required=True)
            command.add_argument('--boot-partition-size', type=int)
        else:
            command.add_argument('--reference-tar', required=True)
            command.add_argument('--magiskboot', required=True)
    args = parser.parse_args(argv)
    try:
        data = artifacts(args.artifacts)
        manifest = dict(device='SM-A042F', experimental=True, device_tested=False,
                        kernel_sha256=sha256(data['Image.gz']),
                        build_info_sha256=sha256(data['BuildInfo.txt']),
                        source_revisions_sha256=sha256(data['source-revisions.env']))
        (anykernel if args.command == 'anykernel' else odin)(args, data, manifest)
    except (ValueError, OSError, EOFError, zlib.error, tarfile.TarError,
            subprocess.SubprocessError) as error:
        print(f'Packaging failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
