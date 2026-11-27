#!/usr/bin/env python3
"""Bounded source-only checks. Does not edit the source repository."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(tempfile.mkdtemp(prefix='a042f-susfs-check-'))
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts/a042f-susfs-rebase.py'
ASSETS = SCRIPT.with_name('a042f-susfs')
EVIDENCE = HERE / 'evidence'
spec = importlib.util.spec_from_file_location('rebaser', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args])


def store(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def state(root):
    return {str(p.relative_to(root)): [m.sha(p.read_bytes()), p.stat().st_mtime_ns, p.stat().st_mode]
            for p in root.rglob('*') if p.is_file()}


def main():
    manifest = m.load_assets(ASSETS)
    head = git('rev-parse', 'HEAD').decode().strip()
    baseline = {name: (REPO / name).read_bytes() for name in manifest['targets']}
    restored = ['include/net/af_unix.h', 'include/net/dst_ops.h',
                'include/net/sock.h', 'include/linux/oom.h',
                'include/linux/mm.h', 'include/linux/sec_debug.h']
    protected = sorted(set(manifest['targets']) | set(restored) | {'fs/internal.h'})
    main_before = {name: m.sha((REPO / name).read_bytes()) for name in protected}
    assert set(restored).isdisjoint(manifest['targets'])
    EVIDENCE.mkdir(exist_ok=True)
    results = []

    def tree(path):
        inputs = {**baseline, **{n: (REPO / n).read_bytes() for n in manifest['context_paths']}}
        inputs['drivers/kernelsu/probes.h'] = ('\n'.join(manifest['ksu_capabilities']) + '\n').encode()
        for name, data in inputs.items():
            store(path / name, data)
        return path

    def cli(root, raw, helper, output=None, check=False):
        args = ['python3', str(SCRIPT), '--root', str(root), '--patch', str(raw), '--hooks', str(helper)]
        if output:
            args += ['--output', str(output)]
        if check:
            args += ['--check']
        return subprocess.run(args, capture_output=True, text=True,
                              env={**os.environ, 'TMPDIR': str(HERE)}, timeout=60)

    def expect_failure(label, root, raw, helper, needle):
        before = state(root)
        result = cli(root, raw, helper)
        assert result.returncode != 0, label
        assert needle in result.stderr, (label, result.stderr[:1000])
        assert state(root) == before, label
        store(EVIDENCE / f'{label}.stderr', result.stderr.encode())
        results.append({'test': label, 'result': 'PASS', 'exit_code': result.returncode,
                        'unchanged_target_files': len(before)})

    with tempfile.TemporaryDirectory(prefix='tests-', dir=HERE) as tmp:
        work = Path(tmp)
        raw = ASSETS / 'upstream.patch'
        helper = ASSETS / 'upstream-hooks.sh'
        root = tree(work / 'current')
        prep = work / 'baseline-regeneration'
        prep.mkdir()
        delta, before, merged = m.prepare(root, ASSETS, manifest, raw.read_bytes(), helper.read_bytes(), prep)
        assert not delta and merged == baseline and before == baseline
        assert len(manifest['targets']) == 29
        assert 'fs/internal.h' in manifest['context_paths']

        def path_umount(data):
            start = data.index(b'int path_umount(struct path *path, int flags)')
            return data[start:data.index(b'\n}\n', start) + 3]

        native_umount = path_umount(baseline['fs/namespace.c'])
        assert m.sha(native_umount) == '54d25f7870d3e84ba7e09d608e6e09b81253528b0f9fb2e701d921593bf7645d'
        assert path_umount((prep / 'seed/fs/namespace.c').read_bytes()) == native_umount
        internal = (REPO / 'fs/internal.h').read_bytes()
        declaration = b'int path_umount(struct path *path, int flags);'
        assert declaration in internal
        assert m.sha(internal) == '6ed2987775fffaf26b7bd21b31e0718f9c8d83e2ec264a8db4e84879877a5616'
        fdinfo = baseline['fs/notify/fdinfo.c']
        assert b'u32 mask = mark->mask & IN_ALL_EVENTS' in fdinfo
        assert b'mark->ignored_mask' in fdinfo
        assert b'inotify_mark_user_mask' not in fdinfo
        assert b'if (IS_ERR(dpath))' in fdinfo and b'out_path_put:' in fdinfo
        assert b'show_vma_header_prefix_fake' not in baseline['fs/proc/task_mmu.c']
        results.append({'test': 'native-umount-port-and-vendor-semantics', 'result': 'PASS',
                        'native_umount_sha256': m.sha(native_umount), 'target_files': len(baseline)})
        results.append({'test': 'regenerated-baseline-exact-current', 'result': 'PASS',
                        'byte_identical_files': len(merged), 'delta_bytes': len(delta)})
        before_state = state(root)
        result = cli(root, raw, helper)
        assert result.returncode == 0, result.stderr
        same = json.loads(result.stdout)
        assert same['status'] == 'same-paired-content'
        assert state(root) == before_state
        results.append({'test': 'same-pair-noop-including-mtimes', 'result': 'PASS', **same})

        assert manifest['ksu_capabilities'] == ['ksu_handle_setresuid']
        assert b'ksu_hide_setprocattr' not in baseline['security/selinux/hooks.c']
        capability_root = tree(work / 'changed-bakasu-capabilities')
        (capability_root / 'drivers/kernelsu/probes.h').write_text(
            'ksu_handle_setresuid\nksu_hide_setprocattr\n')
        result = cli(capability_root, raw, helper)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['changed_files'] == ['security/selinux/hooks.c']
        assert b'ksu_hide_setprocattr(name, value, size);' in (
            capability_root / 'security/selinux/hooks.c').read_bytes()
        results.append({'test': 'same-pair-rechecks-floating-bakasu-capabilities', 'result': 'PASS'})

        source = raw.read_bytes()
        version = b'+#define SUSFS_VERSION "v2.4.0"'
        payload = b'+\tSUSFS_LOGI("Initializing susfs_extra_works\\n");'
        assert source.count(version) == source.count(payload) == 1
        updated_raw = source.replace(version, b'+#define SUSFS_VERSION "v2.4.1-floating-test"')
        updated_raw = updated_raw.replace(payload,
                        b'+\tSUSFS_LOGI("floating source payload reached fs/susfs.c\\n");\n' + payload)
        # Real regenerated Git patches change blob IDs too. They carry no merge semantics.
        import re
        updated_raw = re.sub(rb'^index [0-9a-f]+\.\.([0-9a-f]+)',
                             lambda match: b'index 0000000..1234567' if b'0000000..' in match[0]
                             else b'index 7654321..1234567', updated_raw, flags=re.M)
        callback = rb'ksu_handle_input_handle_event(\&type, \&code, \&value);'
        updated_helper = helper.read_bytes().replace(callback,
                          callback + b' /* paired-helper-floating-test */')
        assert updated_helper != helper.read_bytes()
        synthetic_raw = EVIDENCE / 'synthetic-source.patch'
        synthetic_helper = EVIDENCE / 'synthetic-hooks.sh'
        store(synthetic_raw, updated_raw)
        store(synthetic_helper, updated_helper)

        result = cli(root, synthetic_raw, synthetic_helper, EVIDENCE / 'synthetic-landed.patch', check=True)
        assert result.returncode == 0, result.stderr
        assert state(root) == before_state
        results.append({'test': 'changed-pair-dry-run-no-writes', 'result': 'PASS', **json.loads(result.stdout)})
        result = cli(root, synthetic_raw, synthetic_helper, EVIDENCE / 'synthetic-landed.patch')
        assert result.returncode == 0, result.stderr
        changed = json.loads(result.stdout)
        assert changed['changed_files'] == ['drivers/input/input.c', 'fs/susfs.c', 'include/linux/susfs.h'], changed
        assert b'floating source payload reached fs/susfs.c' in (root / 'fs/susfs.c').read_bytes()
        assert b'v2.4.1-floating-test' in (root / 'include/linux/susfs.h').read_bytes()
        assert b'paired-helper-floating-test' in (root / 'drivers/input/input.c').read_bytes()
        assert b'ksu_handle_sys_read(fd, &buf, &count);' in (root / 'fs/read_write.c').read_bytes()
        for name, data in baseline.items():
            if name not in changed['changed_files']:
                assert (root / name).read_bytes() == data, name
        results.append({'test': 'real-source-and-paired-helper-landed', 'result': 'PASS',
                         'unchanged_files': len(baseline) - len(changed['changed_files']),
                         'exec_fdinfo_byte_identical': True, **changed})
        expect_failure('updated-tree-requires-fresh-baseline-checkout', root,
                       synthetic_raw, synthetic_helper, 'failed')

        vendor_root = tree(work / 'extra-vendor-change')
        vendor_name = 'fs/namei.c'
        vendor_bytes = b'/* synthetic nonoverlapping vendor extension */\n' + baseline[vendor_name]
        (vendor_root / vendor_name).write_bytes(vendor_bytes)
        result = cli(vendor_root, synthetic_raw, synthetic_helper)
        assert result.returncode == 0, result.stderr
        assert (vendor_root / vendor_name).read_bytes() == vendor_bytes
        assert (vendor_root / 'fs/exec.c').read_bytes() == baseline['fs/exec.c']
        assert (vendor_root / 'fs/notify/fdinfo.c').read_bytes() == baseline['fs/notify/fdinfo.c']
        assert b'floating source payload reached fs/susfs.c' in (vendor_root / 'fs/susfs.c').read_bytes()
        results.append({'test': 'additional-nonoverlapping-vendor-change-preserved', 'result': 'PASS',
                        'vendor_file': vendor_name, 'exec_fdinfo_byte_identical': True})

        root = tree(work / 'conflict')
        conflict_helper = EVIDENCE / 'conflicting-hooks.sh'
        original_gate = b'if (likely(susfs_is_current_proc_no_su()))'
        conflicting = helper.read_bytes().replace(original_gate, b'if (unlikely(susfs_is_current_proc_no_su()))')
        assert conflicting != helper.read_bytes()
        store(conflict_helper, conflicting)
        expect_failure('unknown-exec-hook-conflict', root, synthetic_raw, conflict_helper, 'three-way conflict in fs/exec.c')

        failing_helper = EVIDENCE / 'failing-hooks.sh'
        store(failing_helper, helper.read_bytes() + b'\nexit 42\n')
        expect_failure('helper-exit42-no-stale-fallback', root, synthetic_raw, failing_helper, 'failed (42)')

        broken_patch = EVIDENCE / 'bad-context.patch'
        broken = source.replace(b' \t\t\tif (!user)\n', b' \t\t\tif (impossible_context)\n', 1)
        assert broken != source
        store(broken_patch, broken)
        expect_failure('unapplicable-patch-no-stale-fallback', root, broken_patch, helper, 'failed')

        missing_call = EVIDENCE / 'missing-call-hooks.sh'
        store(missing_call, helper.read_bytes().replace(callback, b'/* no input callback */'))
        expect_failure('helper-silent-missing-call-rejected', root, raw, missing_call, 'required hook disappeared')

        new_scope = EVIDENCE / 'new-scope.patch'
        store(new_scope, source + b'\ndiff --git a/fs/unknown.c b/fs/unknown.c\n')
        expect_failure('unexpected-source-scope-rejected', root, new_scope, helper, 'file scope changed')

    main_after = {name: m.sha((REPO / name).read_bytes()) for name in protected}
    assert main_after == main_before
    results.append({'test': 'main-source-and-OEM-headers-untouched', 'result': 'PASS',
                    'protected_paths': len(protected), 'OEM_header_paths': len(restored),
                    'header_paths_disjoint_from_update_targets': True})
    report = {'kernel_commit': head, 'baseline_manifest_sha256': m.sha((ASSETS / 'baseline.json').read_bytes()),
              'tests': results, 'main_source_before_after_sha256': main_before,
              'baseline_scoped_source_sha256': {name: m.sha(data) for name, data in baseline.items()}}
    store(EVIDENCE / 'verification.json', (json.dumps(report, indent=2, sort_keys=True) + '\n').encode())
    for item in results:
        print(item['result'], item['test'])
    print('Evidence:', EVIDENCE / 'verification.json')


if __name__ == '__main__':
    main()
