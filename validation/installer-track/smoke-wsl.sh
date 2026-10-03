#!/usr/bin/env bash
# Read-only host checks + temporary-directory storage cases. NEVER runs install.
set -euo pipefail
SOURCE="$1"
source "$SOURCE/install.sh"
load_release_metadata
verify_installer_payload "$SOURCE/artifact-files.json"
printf 'VERSION=%s VMS=%s INSTALLER=%s MEDIAMTX=%s\n' "$INSTALLER_VERSION" "$VMS_RELEASE_COMMIT" "$INSTALLER_SOURCE_COMMIT" "$MEDIAMTX_INCLUDED"
for script in "$SOURCE"/*.sh; do bash -n "$script"; done
if [[ $EUID -eq 0 ]]; then preflight_checks; fi
printf 'ROOT_AVAILABLE_BYTES=%s ROOT_AVAILABLE_ROUNDED_GIB=%s\n' \
  "$(df --output=avail -B1 / | tail -1 | tr -d ' ')" "$(free_gb_root)"
temp="$(mktemp -d)"
trap 'rm -rf "$temp"' EXIT
CONFIG_DIR="$temp/config"
log() { printf '%s\n' "$*"; }
for threshold in 99 100 101; do
  if (free_gb_root() { echo "$threshold"; }; total_gb_root() { echo 180; }; storage_preflight clean) >"$temp/$threshold.log" 2>&1; then result=0; else result=$?; fi
  printf 'CLEAN_STORAGE_%s_GIB_EXIT=%s\n' "$threshold" "$result"
  [[ "$threshold" == 99 && $result -ne 0 || "$threshold" != 99 && $result -eq 0 ]]
done
# Assert production df-byte parsing never rounds up; no mounts or disk filling.
for bytes in 106300440577 107374182399 107374182400 107374182401; do
  if (df() { printf 'Avail\n%s\n' "$bytes"; }; total_gb_root() { echo 180; }; storage_preflight clean) >"$temp/bytes.log" 2>&1; then result=0; else result=$?; fi
  printf 'CLEAN_STORAGE_%s_BYTES_EXIT=%s\n' "$bytes" "$result"
  [[ "$bytes" -lt 107374182400 && $result -ne 0 || "$bytes" -ge 107374182400 && $result -eq 0 ]]
done
printf 'No installer, service, firewall, user or package-manager mutation executed.\n'
