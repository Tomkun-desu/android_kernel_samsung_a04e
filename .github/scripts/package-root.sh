#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
fetch_anykernel() {
    local revision="$1" destination="$2"
    [[ "$revision" =~ ^[0-9a-f]{40}$ ]]
    git init -q "$destination"
    for attempt in 1 2 3; do
        if git -C "$destination" fetch --depth=1 https://github.com/osm0sis/AnyKernel3.git "$revision"; then
            break
        fi
        test "$attempt" -lt 3
    done
    git -C "$destination" checkout --detach -q "$revision"
    test "$(git -C "$destination" rev-parse HEAD)" = "$revision"
}
fetch_anykernel 41f04bdb4ee30a44e429c2c1131d5e8bb32b15f1 "$RUNNER_TEMP/AnyKernel3"
fetch_anykernel cc7d2cee9b1b4298f6ab8666eb4486dea342748c "$RUNNER_TEMP/AnyKernel3-arm64"
python3 -B scripts/package-a042f.py anykernel \
    --artifacts artifacts --anykernel "$RUNNER_TEMP/AnyKernel3" \
    --anykernel-arm64 "$RUNNER_TEMP/AnyKernel3-arm64" --output package-anykernel
sh -n package-anykernel/anykernel.sh
(cd package-anykernel && sha256sum -c SHA256SUMS)

curl --fail --location --retry 3 --max-time 180 \
    --proto '=https' --proto-redir '=https' \
    https://github.com/topjohnwu/Magisk/releases/download/v31.1/Magisk-v31.1.apk \
    -o "$RUNNER_TEMP/Magisk-v31.1.apk"
echo "89cefac0a318afa39fb3880c45c87b036c05bc1ebf02a7a715b7f8d7c26ac6e7  $RUNNER_TEMP/Magisk-v31.1.apk" | sha256sum -c -
unzip -p "$RUNNER_TEMP/Magisk-v31.1.apk" lib/x86_64/libmagiskboot.so > "$RUNNER_TEMP/a042f-magiskboot"
chmod 755 "$RUNNER_TEMP/a042f-magiskboot"
A042F_TEST_MAGISKBOOT="$RUNNER_TEMP/a042f-magiskboot" python3 -B scripts/tests/test-a042f-package.py
curl --fail --location --retry 3 --max-time 180 --max-filesize 67108864 \
    --proto '=https' --proto-redir '=https' \
    https://github.com/ravindu644/android_kernel_a042f/releases/download/v3/KernelSU-Next-SM-A042F.tar \
    -o "$RUNNER_TEMP/a042f-reference.tar"
python3 -B scripts/package-a042f.py odin-reference --artifacts artifacts \
    --reference-tar "$RUNNER_TEMP/a042f-reference.tar" \
    --magiskboot "$RUNNER_TEMP/a042f-magiskboot" --output package-odin-reference
(cd package-odin-reference && sha256sum -c SHA256SUMS)
