#!/usr/bin/env bash
# Prepared for the simulator VM only. Never targets a real camera.
set -euo pipefail
exec ffmpeg -hide_banner -loglevel warning -re -f lavfi \
  -i 'testsrc2=size=1280x720:rate=15' -an \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -profile:v baseline \
  -g 30 -keyint_min 30 -sc_threshold 0 \
  -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/testcamera
