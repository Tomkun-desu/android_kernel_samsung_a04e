properties() { '
kernel.string=SM-A042F 4.19.191 Samsung BakaSU + SUSFS + NoMount (experimental)
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=1
device.name1=a04e
supported.versions=14
'; }

BLOCK=boot;
IS_SLOT_DEVICE=0;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=0;
NO_MAGISK_CHECK=1;

. tools/ak3-core.sh;

model_ok=0;
for property in ro.product.model ro.product.vendor.model ro.product.system.model; do
  [ "$(getprop "$property")" = "SM-A042F" ] && model_ok=1;
done;
[ "$model_ok" = 1 ] || abort "This package requires SM-A042F.";
[ -z "$(getprop ro.boot.slot_suffix)" ] || abort "Slotted devices are unsupported.";

split_boot;
# Restore original compressed components after AnyKernel's default decompression.
(cd "$SPLITIMG" && magiskboot unpack -n -h "$BOOTIMG") || abort "Cannot preserve stock boot components.";
flash_boot;
