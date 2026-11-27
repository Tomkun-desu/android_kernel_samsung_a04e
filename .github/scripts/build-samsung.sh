#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
root="$PWD"
clang="$root/toolchain/clang/host/linux-x86/clang-r383902/bin/clang"
cross="$root/toolchain/gcc/linux-x86/aarch64/aarch64-linux-android-4.9/bin/aarch64-linux-androidkernel-"
test -s Makefile
test -s arch/arm64/configs/a04e_defconfig
test -x "$clang"
test -x "${cross}ld"
test -x "${cross}elfedit"
test ! -e out

export PATH="$(dirname "$clang"):$(dirname "$cross"):$PATH"
export ARCH=arm64
export ANDROID_MAJOR_VERSION=r
export CLANG_TRIPLE=aarch64-linux-gnu-
unset LLVM LLVM_IAS

arguments=(-C "$root" O="$root/out" CC="$clang" CROSS_COMPILE="$cross" KCFLAGS=-w CONFIG_SECTION_MISMATCH_WARN_ONLY=y)
make "${arguments[@]}" a04e_defconfig
make "${arguments[@]}" -j"$(nproc)"
python3 -B .github/scripts/verify-samsung.py --out out --artifacts artifacts
git diff --exit-code -- Makefile build_kernel.sh arch/arm64/configs/a04e_defconfig
