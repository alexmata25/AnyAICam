#!/bin/bash
# Rebuild the prepared (PENDING-state) website export for a 1.2.7 candidate installer, from Red's verified values.
# Never publishes, never sets a host. Usage:
#   bash refresh_for_candidate.sh <full-commit-sha> <installer-sha256> <size-bytes> <release-folder>
set -euo pipefail
cd "$(dirname "$0")"
COMMIT="${1:?full commit sha}"; SHA="${2:?installer sha256}"; SIZE="${3:?size in bytes}"; DIR="${4:?release folder with the installer}"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ && "$SHA" =~ ^[0-9a-f]{64}$ && "$SIZE" =~ ^[0-9]+$ ]] || { echo "STOP: bad values"; exit 1; }
SHORT="${COMMIT:0:12}"
FILE="anyaicam-appliance-installer-1.2.7-vms-$SHORT.tar.gz"
# 1. Re-check the installer independently; Red's .sha256 must match it byte for byte.
[ "$(stat -c %s "$DIR/$FILE")" = "$SIZE" ] || { echo "STOP: size differs"; exit 1; }
[ "$(sha256sum "$DIR/$FILE" | cut -d' ' -f1)" = "$SHA" ] || { echo "STOP: SHA-256 differs"; exit 1; }
# 2. Rebuild every prepared page in the pending state (no host).
rm -f prepared/public_html/anyaicam-appliance-installer-1.2.7-vms-*.tar.gz.sha256
py -3.11 build_127_pages.py --filename "$FILE" --sha256 "$SHA" --size "$SIZE"
py -3.11 build_127_home.py --filename "$FILE" --sha256 "$SHA" --size "$SIZE"
py -3.11 build_127_signup_links.py
py -3.11 build_127_header.py
py -3.11 build_127_pricing.py   # 2026-10-17 prices + free phone support (exact-match edits; stops on any mismatch)
cmp "prepared/public_html/$FILE.sha256" "$DIR/$FILE.sha256" || { echo "STOP: .sha256 differs from Red's"; exit 1; }
for f in prepared/public_html/*.html; do grep -q "vms-$SHORT" "$f" || [[ "$f" =~ (plans|build-your-system)\.html$ ]] || { echo "STOP: $f lacks the new installer"; exit 1; }; done
py -3.11 verify_publishable.py | grep -E "old price|phone support|19\.99" && { echo "STOP: pricing check failed"; exit 1; }
# 3. Archive + checksums (deterministic), and the manifest.
rm -f export/anyaicam-website-1.2.7-*-pending.tar export/anyaicam-website-1.2.7-*-pending.tar.sha256
ARCH="anyaicam-website-1.2.7-$SHORT-pending.tar"
tar --sort=name --mtime='2026-10-16 00:00Z' --owner=0 --group=0 --numeric-owner -cf "export/$ARCH" -C prepared public_html
(cd export && sha256sum "$ARCH" > "$ARCH.sha256")
(cd prepared/public_html && sha256sum * > ../../export/public_html.SHA256SUMS)
{
  echo "# AnyAiCam website export for VMS 1.2.7 candidate $COMMIT (prepared, NOT published; release state = PENDING), Green $(date -Is)."
  echo "# Installer referenced: $FILE, SHA-256 $SHA, $SIZE bytes."
  echo "# Download host: PENDING owner approval of S3/CloudFront (export/EXPORT-README.md). Not publishable until finalize_download_host.sh."
  echo "# Verify the archive (from export/):  sha256sum -c $ARCH.sha256"
  echo "# Verify each file (from prepared/public_html):  sha256sum -c ../../export/public_html.SHA256SUMS"
  cat "export/$ARCH.sha256"
  sed 's/^/# file: /' export/public_html.SHA256SUMS
} > WEBSITE-EXPORT-SHA256SUMS.txt
(cd export && sha256sum -c "$ARCH.sha256") && (cd prepared/public_html && sha256sum -c ../../export/public_html.SHA256SUMS >/dev/null)
py -3.11 verify_publishable.py || true   # expected: blocked on host + pending state only
echo "REFRESHED for $COMMIT: $ARCH $(cut -d' ' -f1 "export/$ARCH.sha256")"
