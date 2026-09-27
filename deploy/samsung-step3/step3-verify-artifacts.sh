#!/usr/bin/env bash
# Samsung Step 3 -- verify the release artifacts and load the image.
# Safe to run while 0.9.0 is live: it only reads files and ADDS an image
# (docker load never touches the running container or anyaicam-vms:latest).
# Usage: sudo bash step3-verify-artifacts.sh /path/to/samsung-step3
set -euo pipefail
DIR="${1:?usage: $0 <artifact directory>}"
IMAGE_TGZ="anyaicam-vms-f5a6d875c5cc-linux-amd64.tar.gz"
IMAGE_SHA="aaa33eebf12a7d1b0d34d6dbbcf7925b888fb95c60570832ed3ef96555eb4826"
IMAGE_ID="sha256:d5cd5fc263445dd18ce75e06bbc053feec935bb31f45ec0f53ab934babe5054b"
INSTALLER_TGZ="anyaicam-appliance-installer-1.1.0-vms-f5a6d875c5cc.tar.gz"
INSTALLER_SHA="96e08e123f463033751ec0a20aa3a160aff4e18a8c6a93912cc27daa4d8f29b6"
COMMIT="f5a6d875c5cc9bd313e85ac7a330359f39255150"

fail() { echo "FAIL: $*" >&2; exit 1; }
cd "$DIR"
echo "$IMAGE_SHA  $IMAGE_TGZ" | sha256sum -c - || fail "image tarball checksum mismatch"
echo "$INSTALLER_SHA  $INSTALLER_TGZ" | sha256sum -c - || fail "installer checksum mismatch"

if docker image inspect anyaicam-vms:f5a6d87 >/dev/null 2>&1; then
  echo "anyaicam-vms:f5a6d87 already present; not reloading"
else
  gunzip -c "$IMAGE_TGZ" | docker load
fi
got_id="$(docker image inspect anyaicam-vms:f5a6d87 --format '{{.Id}}')"
got_arch="$(docker image inspect anyaicam-vms:f5a6d87 --format '{{.Os}}/{{.Architecture}}')"
got_commit="$(docker image inspect anyaicam-vms:f5a6d87 --format '{{index .Config.Labels "anyaicam.commit"}}')"
[ "$got_id" = "$IMAGE_ID" ] || fail "image ID $got_id != $IMAGE_ID"
[ "$got_arch" = "linux/amd64" ] || fail "architecture $got_arch"
[ "$got_commit" = "$COMMIT" ] || fail "image commit label $got_commit"
echo "PASS: anyaicam-vms:f5a6d87 = $IMAGE_ID ($got_arch, commit $got_commit)"

# Unpack the installer (only its verified agent payload is used in Step 3).
mkdir -p "$DIR/anyaicam-release-f5a6d87"
tar xzf "$INSTALLER_TGZ" -C "$DIR/anyaicam-release-f5a6d87"
grep -q "\"vms_release_commit\": \"$COMMIT\"" "$DIR/anyaicam-release-f5a6d87/release-manifest.json" \
  || grep -q "$COMMIT" "$DIR/anyaicam-release-f5a6d87/release.env" || fail "installer is not commit $COMMIT"
test -f "$DIR/anyaicam-release-f5a6d87/payload/agent/scripts/install.sh" || fail "agent payload missing"
echo "PASS: installer unpacked; agent payload present at $DIR/anyaicam-release-f5a6d87/payload/agent"
