"""Windows motion-reader resource measurement (Blue, 2026-10-21; test tooling only, never packaged).

Per camera, the VMS runs a recorder (stream copy) and a live HLS encoder (libx264 veryfast); the motion detector adds a
1 fps 160x90 grey reader. The Windows correction (D2) reads a single-stream camera's MAIN stream over RTSP; a camera
with a substream reads the substream (as before). This measures the CPU of each process group for N synthetic cameras
(main 1920x1080 15 fps H.264, sub 640x360 15 fps), for: no motion reader / reader on the main stream / on the substream.
(The old HLS-playlist reader is not measured: on Windows it freezes the playlist and stops decoding, see D2.)

    python benchmark-motion-reader.py --ffmpeg <ffmpeg.exe> --mediamtx <mediamtx.exe> [--cameras 1 2 4] [--seconds 30]
Writes a Markdown table to stdout. Needs psutil (the bundled runtime has it).
"""
import argparse
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import psutil

PORT = 18654


def start(command):
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cameras_up(ffmpeg, mediamtx, count, folder):
    config = folder / "cameras.yml"
    config.write_text("\n".join(["logLevel: warn", "api: no", "metrics: no", "pprof: no", "playback: no", "rtsp: yes",
                                 f"rtspAddress: 127.0.0.6:{PORT}", "rtspTransports: [tcp]", "rtmp: no", "hls: no", "webrtc: no",
                                 "srt: no", "moq: no", "paths:", "  all_others:"]) + "\n", encoding="ascii")
    procs = [start([mediamtx, str(config)])]
    time.sleep(2)
    for n in range(1, count + 1):
        for name, size in ((f"cam{n}main", "1920x1080"), (f"cam{n}sub", "640x360")):
            procs.append(start([ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15",
                                "-pix_fmt", "yuv420p", "-g", "30", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                                "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.6:{PORT}/{name}"]))
    time.sleep(5)
    return procs


def vms_pipeline(ffmpeg, n, folder, reader):
    main = f"rtsp://127.0.0.6:{PORT}/cam{n}main"
    sub = f"rtsp://127.0.0.6:{PORT}/cam{n}sub"
    hls = folder / f"hls{n}"
    rec = folder / f"rec{n}"
    hls.mkdir(exist_ok=True)
    rec.mkdir(exist_ok=True)
    groups = {
        "recorder": start([ffmpeg, "-rtsp_transport", "tcp", "-i", main, "-map", "0:v:0", "-c:v", "copy", "-f", "segment", "-segment_time", "300",
                           "-reset_timestamps", "1", "-strftime", "1", str(rec / "c_%Y-%m-%d_%H-%M-%S.mkv")]),
        "live": start([ffmpeg, "-rtsp_transport", "tcp", "-i", main, "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                       "-g", "56", "-keyint_min", "28", "-sc_threshold", "0", "-vf",
                       "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
                       "-maxrate", "800k", "-bufsize", "1600k", "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                       "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
                       "-hls_segment_filename", str(hls / "c_%09d.ts"), str(hls / "c.m3u8")]),
    }
    if reader != "none":
        url = main if reader == "main" else sub
        groups["motion"] = subprocess.Popen([ffmpeg, "-loglevel", "error", "-rtsp_transport", "tcp", "-fflags", "nobuffer", "-i", url, "-an",
                                             "-vf", "fps=1,scale=160:90,format=gray", "-f", "rawvideo", "pipe:1"],
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return groups


def measure(groups_per_camera, seconds):
    handles = {}
    for groups in groups_per_camera:
        for kind, proc in groups.items():
            handles.setdefault(kind, []).append(psutil.Process(proc.pid))
    drains = []
    for groups in groups_per_camera:
        motion = groups.get("motion")
        if motion is not None:
            import threading
            def drain(p=motion):
                while p.stdout.read(14400):
                    pass
            t = threading.Thread(target=drain, daemon=True); t.start(); drains.append(t)
    time.sleep(8)  # warm-up
    for procs in handles.values():
        for p in procs:
            p.cpu_percent(None)
    time.sleep(seconds)
    result = {kind: sum(p.cpu_percent(None) for p in procs if p.is_running()) for kind, procs in handles.items()}
    memory = sum(p.memory_info().rss for procs in handles.values() for p in procs if p.is_running()) / 2 ** 20
    return result, memory


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--mediamtx", required=True)
    ap.add_argument("--cameras", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()
    logical = psutil.cpu_count()
    print(f"Host: {logical} logical CPUs. CPU % = percent of ONE logical CPU (100 = one full core thread), summed per group.\n")
    print("| cameras | motion reader | recorders % | live encoders % | motion readers % | motion % per camera | RSS MB (all VMS ffmpeg) |")
    print("|---|---|---|---|---|---|---|")
    for count in args.cameras:
        folder = Path(tempfile.mkdtemp(prefix="motionbench-"))
        cams = cameras_up(args.ffmpeg, args.mediamtx, count, folder)
        try:
            for reader in ("none", "main", "substream"):
                groups = [vms_pipeline(args.ffmpeg, n, folder, reader) for n in range(1, count + 1)]
                try:
                    cpu, memory = measure(groups, args.seconds)
                finally:
                    for g in groups:
                        for p in g.values():
                            p.kill()
                    time.sleep(2)
                motion = cpu.get("motion", 0.0)
                print(f"| {count} | {reader} | {cpu['recorder']:.0f} | {cpu['live']:.0f} | {motion:.0f} | {motion / count:.1f} | {memory:.0f} |", flush=True)
        finally:
            for p in cams:
                p.kill()
            time.sleep(2)
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
