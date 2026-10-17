#!/bin/bash
# Finalize the 1.2.7 download pages (Option A, owner decision 2026-10-16: https://anyaicam.com/downloads/linux/1.2.7/,
# the same website path as 1.2.4). PREPARED, NOT RUN: only after the FINAL installer passes acceptance, it has been
# uploaded there with the owner's approval, and the owner approves publication.
# Runs only after the owner approves the S3/CloudFront setup (aws-downloads/APPROVAL-PACKAGE.md), the resources exist,
# and aws-downloads/upload_and_verify.sh has passed for that host. The host is never assumed; it must be passed in.
#   bash finalize_download_host.sh https://<approved host> <installer size>   (use the host the owner approved)
set -euo pipefail
HOST="${1:?approved https download host, e.g. https://<host> (no path)}"
[[ "$HOST" =~ ^https://[a-z0-9.-]+$ ]] || { echo "STOP: host must be https://<hostname> with no path"; exit 1; }
cd "$(dirname "$0")"
# The installer values are read from the frozen pending export (refresh_for_candidate.sh wrote its .sha256).
SHAFILE=$(ls prepared/public_html/anyaicam-appliance-installer-1.2.7-vms-*.tar.gz.sha256)
SHA=$(cut -d' ' -f1 "$SHAFILE"); FILE=$(basename "$SHAFILE" .sha256)
SIZE="${2:?verified installer size in bytes (from Red)}"
URL="$HOST/linux/1.2.7/$FILE"
# The installer must already be served there, exactly (upload_and_verify.sh checked it; re-check size and type).
curl -sfI "$URL" | tee /tmp/aac-final-head.txt | grep -qi "content-length: $SIZE" || { echo "STOP: $URL is not serving the verified installer"; exit 1; }
py -3.11 build_127_pages.py --filename "$FILE" --sha256 "$SHA" --size "$SIZE" --url-base "$HOST/linux/1.2.7" --state published
py -3.11 build_127_home.py --filename "$FILE" --sha256 "$SHA" --size "$SIZE" --url-base "$HOST/linux/1.2.7" --state published
py -3.11 verify_publishable.py          # must now report: values complete
ARCH="anyaicam-website-1.2.7-${FILE#anyaicam-appliance-installer-1.2.7-vms-}"; ARCH="${ARCH%.tar.gz}-final.tar"
tar --sort=name --mtime='2026-10-16 00:00Z' --owner=0 --group=0 --numeric-owner -cf "export/$ARCH" -C prepared public_html
(cd export && sha256sum "$ARCH" > "$ARCH.sha256")
(cd prepared/public_html && sha256sum * > ../../export/public_html.final.SHA256SUMS)
echo "Rebuilt for $URL. Next: local page test, Codex website contract, then the owner's publication approval."
