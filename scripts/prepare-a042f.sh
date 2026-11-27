#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
root="$PWD"
rm -f out/a042f-sources.env
for source in KernelSU NoMount; do
    test -e "$source/.git" || {
        echo 'Initialize BakaSU and NoMount first: git submodule update --init --recursive' >&2
        exit 1
    }
done
bakasu_url=https://github.com/Baka-SU/BakaSU.git
susfs_url=https://github.com/JackA1ltman/NonGKI_Kernel_Build_2nd.git
nomount_url=https://github.com/maxsteeel/nomount.git
bakasu="$(git ls-remote "$bakasu_url" refs/heads/main | awk 'NR == 1 { print $1 }')"
susfs="$(git ls-remote "$susfs_url" refs/heads/mainline | awk 'NR == 1 { print $1 }')"
nomount="$(git ls-remote "$nomount_url" refs/heads/dev | awk 'NR == 1 { print $1 }')"
for sha in "$bakasu" "$susfs" "$nomount"; do
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || {
        echo 'Could not resolve a floating source revision' >&2
        exit 1
    }
done

float_source() {
    local path="$1" url="$2" revision="$3"
    git -C "$path" diff --quiet
    git -C "$path" diff --cached --quiet
    if [ "$(git -C "$path" rev-parse --is-shallow-repository)" = true ]; then
        git -C "$path" fetch --unshallow --tags "$url" "$revision"
    else
        git -C "$path" fetch --tags "$url" "$revision"
    fi
    git -C "$path" checkout --detach "$revision"
    test "$(git -C "$path" rev-parse HEAD)" = "$revision"
    test "$(git -C "$path" rev-parse --is-shallow-repository)" = false
}
float_source KernelSU "$bakasu_url" "$bakasu"
float_source NoMount "$nomount_url" "$nomount"
test "$(readlink drivers/kernelsu)" = ../KernelSU/kernel
test "$(readlink fs/nomount)" = ../NoMount/kernel/src
test -s drivers/kernelsu/Kconfig
test -s fs/nomount/Kconfig

scratch="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/a042f-sources.XXXXXX")"
trap 'rm -rf -- "$scratch"' EXIT
base="https://raw.githubusercontent.com/JackA1ltman/NonGKI_Kernel_Build_2nd/$susfs/Patches"
curl --fail --location --retry 3 "$base/Patch/susfs_patch_to_4.19.patch" -o "$scratch/susfs.patch"
curl --fail --location --retry 3 "$base/susfs_inline_hook_patches.sh" -o "$scratch/hooks.sh"
python3 scripts/a042f-susfs-rebase.py --root "$root" --patch "$scratch/susfs.patch" --hooks "$scratch/hooks.sh"

version="$(sed -n 's/^#define SUSFS_VERSION "\([^"]*\)"$/\1/p' include/linux/susfs.h)"
if ! [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9.+_-]*$ ]]; then
    echo 'WARNING: SUSFS version label unavailable; recording the resolved source SHA' >&2
    version="git-${susfs:0:12}"
fi
mkdir -p out
cat > out/a042f-sources.env <<EOF
BAKASU_SHA=$bakasu
BAKASU_URL=$bakasu_url
BAKASU_REF=main
BAKASU_COMMITS=$(git -C KernelSU rev-list --count HEAD)
SUSFS_SHA=$susfs
SUSFS_URL=$susfs_url
SUSFS_REF=mainline
SUSFS_VERSION=$version
SUSFS_PATCH_SHA256=$(sha256sum "$scratch/susfs.patch" | awk '{print $1}')
SUSFS_HOOKS_SHA256=$(sha256sum "$scratch/hooks.sh" | awk '{print $1}')
NOMOUNT_SHA=$nomount
NOMOUNT_URL=$nomount_url
NOMOUNT_REF=dev
NOMOUNT_COMMITS=$(git -C NoMount rev-list --count HEAD)
EOF
cat out/a042f-sources.env
