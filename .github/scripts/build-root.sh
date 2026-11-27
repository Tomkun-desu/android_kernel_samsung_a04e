#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
root="$PWD"
clang="$root/toolchain/clang/host/linux-x86/clang-r383902/bin/clang"
cross="$root/toolchain/gcc/linux-x86/aarch64/aarch64-linux-android-4.9/bin/aarch64-linux-androidkernel-"
fragment=arch/arm64/configs/a04e-root.config
sources=out/a042f-sources.env
test -s "$sources"
test -s "$fragment"
test -x "$clang"
test -x "${cross}elfedit"
test ! -e out/.config

export PATH="$(dirname "$clang"):$(dirname "$cross"):$PATH"
export ARCH=arm64
export ANDROID_MAJOR_VERSION=r
export CLANG_TRIPLE=aarch64-linux-gnu-
unset LLVM LLVM_IAS
nomount_commits="$(sed -n 's/^NOMOUNT_COMMITS=//p' "$sources")"
[[ "$nomount_commits" =~ ^[1-9][0-9]*$ ]]
arguments=(-C "$root" O="$root/out" CC="$clang" CROSS_COMPILE="$cross" KCFLAGS=-w CONFIG_SECTION_MISMATCH_WARN_ONLY=y NOMOUNT_COMMITS="$nomount_commits")
make "${arguments[@]}" a04e_defconfig
bash scripts/kconfig/merge_config.sh -m -O out out/.config "$fragment"
make "${arguments[@]}" olddefconfig
while IFS= read -r setting; do
    case "$setting" in
        CONFIG_*=y|'# CONFIG_'*' is not set') grep -Fxq "$setting" out/.config ;;
    esac
done < "$fragment"
make "${arguments[@]}" -j"$(nproc)"
python3 -B scripts/check-a042f-clocks.py out/System.map
python3 -B .github/scripts/verify-samsung.py --out out --artifacts artifacts --root
git diff --exit-code -- Makefile build_kernel.sh arch/arm64/configs/a04e_defconfig
