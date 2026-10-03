#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(hostname)" == ip-10-253-77-16 ]] || exit 99
[[ "$(uname -m)" == x86_64 ]] || exit 99
lab=/home/ubuntu/anyaicam-lab
source="$lab/installer/payload/vms"
test -f "$source/requirements.txt"
test -f "$source/requirements-cpu.txt"
trap 'printf "%s\n" "$?" > "$lab/runtime-dependencies.exit"' EXIT
sudo env DEBIAN_FRONTEND=noninteractive apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv ffmpeg nginx openssl wireguard-tools libgl1 libglib2.0-0
python3 -m venv "$lab/runtime"
"$lab/runtime/bin/python" -m pip install --no-cache-dir -r "$source/requirements-cpu.txt"
"$lab/runtime/bin/python" -m pip install --no-cache-dir -r "$source/requirements.txt"
"$lab/runtime/bin/python" -m pip check
"$lab/runtime/bin/python" -m pip freeze > "$lab/runtime-dependencies.freeze"
echo LAB_RUNTIME_DEPENDENCIES_READY
