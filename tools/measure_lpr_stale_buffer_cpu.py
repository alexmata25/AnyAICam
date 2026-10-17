"""Controlled CPU measurement for L-1 (stale LPR buffer decoding).

Builds real 30-second H.264 Matroska buffer segments with ffmpeg (1080p and
1440p, like the driveway cameras), then simulates SIMULATED_SECONDS of AI
passes for one stopped camera -- one lpr.latest_full_resolution_frame() call
every AI_INTERVAL seconds, the cache always expired -- once with the old
behaviour (no age bound) and once with the fix. CPU is the children's
user+system time (the ffmpeg decodes) plus this process's own (PNG decode).

  python tools/measure_lpr_stale_buffer_cpu.py      (run from app/ with lpr importable)
"""
import os
import resource
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import lpr  # noqa: E402

AI_INTERVAL = 5.0          # AI_DETECTION_INTERVAL_SECONDS default
SIMULATED_SECONDS = 300    # five minutes of a stopped camera


def make_segment(folder, width, height):
    path = os.path.join(folder, f"buf3_{width}x{height}.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=15",
                    "-t", "30", "-c:v", "libx264", "-preset", "veryfast", "-g", "30", path], check=True)
    return path


def cpu():
    me, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime


def run(folder, max_age):
    lpr._full_frame_cache.clear()
    lpr.LPR_FULL_RES_FRAMES = True
    lpr.LPR_FULL_RES_MAX_AGE_SECONDS = max_age
    wall = time.time() + 600  # every segment is ten minutes old: a stopped camera
    passes = int(SIMULATED_SECONDS / AI_INTERVAL)
    start_cpu, start = cpu(), time.monotonic()
    decoded = 0
    for tick in range(passes):
        frame = lpr.latest_full_resolution_frame(3, folder, now=tick * AI_INTERVAL, wall_now=wall)
        decoded += frame is not None
    return passes, decoded, cpu() - start_cpu, time.monotonic() - start


def main():
    for width, height in ((1920, 1080), (2560, 1440)):
        with tempfile.TemporaryDirectory() as folder:
            make_segment(folder, width, height)
            passes, decoded, cpu_old, wall_old = run(folder, 1e12)
            passes2, decoded2, cpu_new, wall_new = run(folder, 10.0)
            per_pass = cpu_old / passes
            print(f"{width}x{height}: {passes} AI passes over {SIMULATED_SECONDS}s for one stopped camera")
            print(f"  before fix: {decoded} full-res decodes, {cpu_old:.2f} CPU-s ({per_pass * 1000:.0f} ms per pass, "
                  f"{cpu_old / SIMULATED_SECONDS * 100:.1f}% of one core on average)")
            print(f"  after fix:  {decoded2} decodes, {cpu_new:.3f} CPU-s")


if __name__ == "__main__":
    main()
