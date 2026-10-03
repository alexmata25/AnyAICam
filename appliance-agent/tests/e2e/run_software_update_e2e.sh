#!/usr/bin/env bash
# Disposable Software Update E2E: a throwaway container (--rm), no network,
# the repository mounted read-only. Uses a locally available image that has
# python3 + cryptography + openssl (the VMS image built from this repo's
# Dockerfile works). Nothing outside the container is touched.
#
#   appliance-agent/tests/e2e/run_software_update_e2e.sh [image]
set -euo pipefail
IMAGE="${1:-anyaicam-vms:golden-rc4}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
exec docker run --rm --network none --user 0 --entrypoint python3 \
    -v "$REPO:/src:ro" "$IMAGE" /src/appliance-agent/tests/e2e/software_update_e2e.py
