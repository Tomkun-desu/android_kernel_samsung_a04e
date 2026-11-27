#!/usr/bin/env python3
"""Exercise port regressions with host stubs; no device behavior is implied."""

from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def read(name):
    return (ROOT / name).read_text()


class RootRegressions(unittest.TestCase):
    def compile(self, source, execute=False):
        compiler = shutil.which('cc')
        self.assertIsNotNone(compiler, 'Host C compiler is required for regression checks')
        with tempfile.TemporaryDirectory(prefix='a04e-root-regression-') as temporary:
            target = Path(temporary) / 'check'
            arguments = [compiler, '-std=gnu11', '-Werror=implicit-function-declaration', '-x', 'c', '-', '-o', str(target)]
            if not execute:
                arguments.append('-fsyntax-only')
            result = subprocess.run(arguments, input=source, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            if execute:
                result = subprocess.run([str(target)], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_redirect_restarts_with_an_unopened_file(self):
        text = read('fs/namei.c')
        start = text.index('static struct file *path_openat(')
        function = text[start:text.index('\n}', start) + 2]
        self.compile(r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#define CONFIG_KSU_SUSFS_OPEN_REDIRECT 1
#define __O_TMPFILE 1
#define O_PATH 2
#define FMODE_OPENED 4
#define LOOKUP_OPEN 8
#define LOOKUP_CREATE 16
#define LOOKUP_EXCL 32
#define LOOKUP_RCU 64
#define EOPENSTALE 500
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define likely(x) (x)
#define unlikely(x) (x)
#define WARN_ON(x) assert(!(x))
struct inode { int unused; };
struct dentry { struct inode *d_inode; };
struct path { struct dentry *dentry; };
struct filename { int unused; };
struct nameidata { int dfd; unsigned flags; struct filename *name; struct path path; };
struct file { unsigned f_flags, f_mode; int target; };
struct open_flags { unsigned open_flag; };
static struct inode inode;
static struct dentry dentry = { &inode };
static struct filename original_name, redirect_name;
static int redirect, allocations, fail_allocation, opened, closed, names_released;
static int target, fail_redirect_open;
#define SUSFS_IS_INODE_OPEN_REDIRECT_WITHOUT_UID_CHECK(i) (redirect)
static void *current_cred(void) { return NULL; }
static struct file *alloc_empty_file(unsigned flags, void *cred) {
    if (++allocations == fail_allocation) return ERR_PTR(-ENOMEM);
    struct file *file = calloc(1, sizeof(*file));
    assert(file); file->f_flags = flags; return file;
}
static void fput(struct file *file) { assert(file && !IS_ERR(file)); ++closed; free(file); }
static int do_tmpfile(struct nameidata *nd, unsigned flags, const struct open_flags *op, struct file *file) {
    file->f_mode = FMODE_OPENED; return 0;
}
static int do_o_path(struct nameidata *nd, unsigned flags, struct file *file) {
    file->f_mode = FMODE_OPENED; return 0;
}
static const char *path_init(struct nameidata *nd, unsigned flags) {
    nd->path.dentry = &dentry; return "fixture";
}
static int link_path_walk(const char *s, struct nameidata *nd) { return 0; }
static int do_last(struct nameidata *nd, struct file *file, const struct open_flags *op) {
    assert(!(file->f_mode & FMODE_OPENED));
    ++opened;
    if (target && fail_redirect_open) return -EACCES;
    file->f_mode |= FMODE_OPENED; file->target = target; return 0;
}
static const char *trailing_symlink(struct nameidata *nd) { return "fixture-link"; }
static void terminate_walk(struct nameidata *nd) {}
static void restore_nameidata(void) {}
static void set_nameidata(struct nameidata *nd, int fd, struct filename *name) {
    nd->name = name; target = 1;
}
static struct filename *susfs_open_redirect_spoof_do_sys_openat(struct inode *inode) {
    return &redirect_name;
}
static void putname(struct filename *name) { assert(name == &redirect_name); ++names_released; }
''' + function + r'''
static void scenario(int enabled, int flags, int allocation_failure, int open_failure) {
    redirect = enabled; allocations = opened = closed = names_released = target = 0;
    fail_allocation = allocation_failure; fail_redirect_open = open_failure;
    struct nameidata nd = { .dfd = -100, .name = &original_name };
    struct open_flags op = { .open_flag = flags };
    struct file *file = path_openat(&nd, &op, 0);
    if (allocation_failure || open_failure) {
        assert(IS_ERR(file));
        assert(PTR_ERR(file) == (allocation_failure ? -ENOMEM : -EACCES));
        assert(closed == (allocation_failure == 1 ? 0 : open_failure ? 2 : 1));
    } else {
        assert(!IS_ERR(file));
        assert(file->f_mode & FMODE_OPENED);
        if (!flags) assert(file->target == enabled);
        assert(opened == (flags ? 0 : enabled ? 2 : 1));
        assert(closed == (enabled && !flags ? 1 : 0));
        fput(file);
    }
    assert(nd.name == &original_name);
    assert(names_released == (enabled && !flags && allocation_failure != 1 ? 1 : 0));
}
int main(void) {
    scenario(0, 0, 0, 0);
    scenario(1, 0, 0, 0);
    scenario(1, 0, 1, 0);
    scenario(1, 0, 2, 0);
    scenario(1, 0, 0, 1);
    scenario(1, O_PATH, 0, 0);
    scenario(1, __O_TMPFILE, 0, 0);
    return 0;
}
''', execute=True)

    def test_fdinfo_rejects_error_pointers_before_path_lookup(self):
        text = read('fs/proc/fd.c')
        region = text[text.index('dpath = d_path('):]
        region = region[:region.index('kern_path(')]
        self.assertRegex(region, r'if\s*\(IS_ERR\(dpath\)\)\s*\{?\s*goto out_kfree;')

    def test_sys_read_matches_bakasu_signature(self):
        text = read('fs/read_write.c')
        declaration = re.search(r'extern[^;]+\bksu_handle_sys_read\([^;]+;', text)
        self.assertIsNotNone(declaration)
        self.compile('#include <stddef.h>\n#define __user\n'
                     'int ksu_handle_sys_read(unsigned int, char **, size_t *);\n'
                     + declaration[0] + '\n')
        self.assertIn('ksu_handle_sys_read(fd, &buf, &count);', text)

    def test_ratelimited_logs_compile_when_logging_is_disabled(self):
        text = read('fs/susfs.c')
        start = text.index('#ifdef CONFIG_KSU_SUSFS_ENABLE_LOG')
        definitions = text[start:text.index('#endif', start) + len('#endif')]
        self.compile(definitions + '\nvoid probe(void) { SUSFS_LOGI_RL("%d", 1); SUSFS_LOGE_RL("%d", 2); }\n')


if __name__ == '__main__':
    unittest.main()
