# Samsung Galaxy A04e SM-A042F

This independent repository starts from the supplied Samsung Android 14
source archives. It preserves the original kernel, official incremental
update, platform sources, build guides and license notices.

## Preserved source history

- `samsung-base`: exact original kernel archive contents and file modes.
- `samsung-stock`: original kernel plus the supplied official update.
- `samsung-platform`: separate platform archive contents and file modes.
- `a04e`: official stock merged into the build-automation history, followed
  by separate clock-lifetime and experimental root-port commits.

The source-input release `samsung-source-inputs-20261127` contains the
original ZIP files, their hashes and the independent tree verification.
Those archives are source code, not installable firmware.

## Verified stock baseline

Commit `949b0f000666943e2b380c91b74823521ea7e6a9` was built successfully in
Actions run `46021933316` using Samsung's documented recipe: `ARCH=arm64`,
`ANDROID_MAJOR_VERSION=r`, `a04e_defconfig`, `KCFLAGS=-w`, and
`CONFIG_SECTION_MISMATCH_WARN_ONLY=y`. The compiler is Clang r383902 with
the Android GCC 4.9 binutils generation recorded in `.github/toolchains.json`.

The stock configuration retains DEFEX and has no root integration. The
downloaded Image, gzip, DTB and modules were independently verified. The
stock build reported 14 section mismatches; its linked map also confirms
that `audio_clks` occupies freed init memory. The custom branch separately
removes init-only annotations from seven retained clock tables. Their seven
probe functions already reside outside init memory and are also checked.

Select `variant: stock` when dispatching the workflow to rebuild the
verified stock commit. Ordinary pushes build the experimental root port.

## Experimental root port

Each build resolves and verifies these branches once:

- `Baka-SU/BakaSU@main`, with full history for its generated version.
- `JackA1ltman/NonGKI_Kernel_Build_2nd@mainline`, fetching the 4.19 patch
  and inline helper from the same resolved commit.
- `maxsteeel/nomount@dev`, with its real commit count passed to Kbuild.

The reviewed baseline is described in `scripts/a042f-susfs/baseline.json`.
The bounded three-way rebaser carries vendor adaptations and local fixes
forward, aborting on conflicts, missing hooks or unexpected file scope.
Helper capability probes come from the actual BakaSU source.

`a04e-root.config` is merged onto the original defconfig. It enables the
SUSFS hook choice and NoMount, disables DEFEX, and leaves SELinux enabled.
The build checks the final configuration, linked feature symbols, Image,
gzip, ELF, DTB and persistent clock addresses. Exact source revisions,
preparation diff, hashes and toolchain evidence accompany the outputs.

The native `path_umount()` backport is retained from Ravindu Deshan's
A04e integration, commit `f02d2fb37ac566f3f62f6330cb00d84fd5b17e60`.
It consumes the path references; callers must not release them again.
Other port checks cover exec callback lifetime, fresh file allocation for
open redirection, error pointers from `d_path()`, the BakaSU read-hook ABI,
and disabled logging macros. Upstream attribution remains in the sources.

## Packages and limits

The AnyKernel artifact has the installer at its ZIP root, uses pinned
ARM64 tools, checks SM-A042F/Android 14 and preserves the installed boot
components while replacing the kernel.

The Odin artifact is explicitly `REF-EXF2-EXPERIMENTAL`. Its ramdisk,
DTB and boot metadata come from the pinned ravindu644 v3 EXF2 reference,
whose ramdisk identifies Android 12. It is not a matching untouched EZF2
boot image. Packaging checks the reference hashes, component preservation,
tar contents and MD5 trailer. Its size ceiling is a resource limit, not a
measurement of the target boot partition.

Compilation and packaging do not establish bootability. Matching EZF2
boot, full AP/CSC details, measured partition capacity and device crash
evidence remain unavailable. The previous bootloop cause is unproven.
No device flashing or NoMount userspace installation is performed here.
