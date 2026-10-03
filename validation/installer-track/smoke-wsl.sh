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
# Synthetic boundary; no disk filling/mounting. GNU df -B1G rounds up.
bytes=$((99 * 1024 * 1024 * 1024 + 1))
rounded=$(((bytes + 1024*1024*1024 - 1) / (1024*1024*1024)))
if (free_gb_root() { echo "$rounded"; }; total_gb_root() { echo 180; }; storage_preflight clean) >"$temp/rounding.log" 2>&1; then
  printf 'STORAGE_ROUNDING_FINDING: %s bytes (<100 GiB) reported as %s passes current integer check\n' "$bytes" "$rounded"
fi
printf 'No installer, service, firewall, user or package-manager mutation executed.\n'
