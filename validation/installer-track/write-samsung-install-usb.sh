#!/usr/bin/env bash
set -Eeuo pipefail
media=/home/alejandro-mata/anyaicam-clean-test-media-20261003
iso="$media/ubuntu-24.04.4-desktop-amd64.iso"
usb_id=/dev/disk/by-id/usb-General_UDisk_2408200641348662589024-0:0
expected_sha=3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e
die() { echo "STOP: $*" >&2; exit 1; }
verify_target() {
  [[ "$(hostname)" == anyaicam-samsung ]] || die "Wrong hostname"
  [[ "$(cat /sys/class/dmi/id/sys_vendor)" == "SAMSUNG ELECTRONICS CO., LTD." ]] || die "Wrong hardware vendor"
  [[ "$(cat /sys/class/dmi/id/product_name)" == 930MBE ]] || die "Wrong hardware model"
  [[ "$(lsblk -dn -o SERIAL /dev/nvme0n1 | xargs)" == 193077800308 ]] || die "Unexpected system disk"
  [[ "$(findmnt -n -o SOURCE /)" == /dev/nvme0n1p2 ]] || die "Unexpected root filesystem"
  [[ -b "$usb_id" ]] || die "Expected USB is absent"
  usb="$(readlink -f "$usb_id")"
  [[ "$usb" != /dev/nvme0n1 ]] || die "USB resolves to system disk"
  [[ "$(lsblk -dn -o TYPE "$usb" | xargs)" == disk ]] || die "USB target is not a whole disk"
  [[ "$(lsblk -dn -o TRAN "$usb" | xargs)" == usb ]] || die "Not a USB disk"
  [[ "$(lsblk -dn -o SERIAL "$usb" | xargs)" == 2408200641348662589024 ]] || die "USB serial changed"
  [[ "$(lsblk -bdn -o SIZE "$usb" | xargs)" == 31457280000 ]] || die "USB size changed"
}
verify_target
if [[ "$EUID" != 0 ]]; then
  echo "Preparing Ubuntu installation USB on Samsung."
  echo "Only USB serial 2408200641348662589024 (31.5 GB) will be erased."
  echo "The Samsung system disk is NOT written by this script."
  echo "Waiting for the authenticated ISO download and checksum verification..."
  for ((n=0;n<180;n++)); do
    [[ -f "$iso" ]] && break
    sleep 5
  done
  [[ -f "$iso" ]] || die "Verified ISO did not become ready within 15 minutes"
  echo "Enter the Samsung administrator password HERE, never in chat."
  exec sudo -- bash "$0" --write-verified-usb
fi
[[ "${1:-}" == --write-verified-usb ]] || die "Explicit media-write argument is required"
verify_target
[[ -f "$iso" ]] || die "ISO absent"
printf '%s  %s\n' "$expected_sha" "$iso" | sha256sum --check --strict -
# Unmount only the exact authorized USB's filesystems.
mapfile -t partitions < <(lsblk -ln -o PATH "$usb")
for partition in "${partitions[@]}"; do
  if findmnt -rn -S "$partition" >/dev/null; then
    umount -- "$partition"
  fi
done
verify_target
echo "Writing verified Ubuntu ISO to authorized USB: $usb"
dd if="$iso" of="$usb" bs=4M conv=fsync status=progress
sync
cmp -n "$(stat -c %s "$iso")" "$iso" "$usb"
printf 'USB_WRITE_VERIFIED hostname=%s usb=%s serial=%s iso_sha256=%s\n' "$(hostname)" "$usb" 2408200641348662589024 "$expected_sha" | tee "$media/usb-write-verified.txt"
echo "USB verified. The system disk has NOT been erased."
echo "Leave the USB inserted. Use the Samsung physical console to boot this USB."
echo "Select Ubuntu installer > Erase disk and install Ubuntu ONLY for"
echo "SN520 SDAPNUW-512G-1004, serial 193077800308, capacity 512 GB."
echo "Do not select the 31.5 GB UDisk. Disconnect any other storage first."
read -r -p "Press Enter to close this window. "
