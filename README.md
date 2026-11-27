# Samsung Galaxy A04e kernel

Independent SM-A042F kernel repository imported from the Samsung source archives supplied by the owner. Kernel version: Linux 4.19.191. Device configuration: `a04e_defconfig`, ARM64, MT6765 / HS04.

## Sources

`.github/samsung-sources.json` identifies the original downloads by SHA256. The `samsung-source-inputs-20261127` release stores the full source ZIP and the official incremental update. These are source archives, not flashable firmware.

The kernel source stays at the repository root, following Samsung's layout. The import keeps original file contents, executable permissions, symbolic links, copyright notices and licenses.

- `samsung-base`: complete kernel source and Samsung's original `README_Kernel.txt`.
- `samsung-stock`: the base plus the 62-file EZE1 update, whose guide requests the EXJ3 base.
- `samsung-platform`: the separately supplied Android platform source and its original guide.
- `a04e`: the stock kernel with repository documentation and GitHub Actions automation.

## Build

The first build follows `README_Kernel.txt`: Clang r383902, the source's Android GCC 4.9 binutils, `ARCH=arm64`, `ANDROID_MAJOR_VERSION=r`, `a04e_defconfig`, `KCFLAGS=-w` and `CONFIG_SECTION_MISMATCH_WARN_ONLY=y`. Toolchains are downloaded on the GitHub runner. Kernel builds run in GitHub Actions.

Samsung's warning flags are retained for this baseline. They suppress compiler warnings and make section mismatches nonfatal; a successful build does not clear those issues.

The baseline retains Samsung's configuration, including DEFEX. BakaSU, SUSFS and NoMount integration belongs in subsequent, separately reviewable commits after the stock build is checked.

Build outputs include the kernel, device trees, configuration, symbol map and a provenance manifest. Compilation does not establish that an image boots on the target firmware. No device flashing is automated.

## Provenance

The downloads were supplied by the owner as Samsung Open Source packages. Recorded checksums identify the supplied bytes; no Samsung-published checksum or signature was available for independent comparison. The full ZIP name alone does not establish an exact installed firmware revision.

The incremental archive's original guide and both full-package guides are preserved under `.github/source-guides/`. The archives retain their own source licenses; this repository does not replace those licenses.
