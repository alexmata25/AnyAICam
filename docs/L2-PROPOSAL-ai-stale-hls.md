# L-2 proposal: stop AI analytics on a disconnected camera's stale live playlist (NOT applied)

Yellow, 2026-10-17. Branch `claude/yellow-ai-stale-hls-l2-20261017` (on 7804f3d) holds **tests only**; no production code.

## Why it happens (source, 7804f3d; identical in RC3 01d1470)
1. AI frames come from the persistent main-stream reader (`ANYAICAM_AI_SOURCE=main`, the default). Its
   `latest()` already refuses frames older than `ANYAICAM_AI_FRAME_MAX_AGE_SECONDS` (3 s).
2. When that reader cannot reach the camera, `analytics_frames.uses_hls()` falls back to the camera's live HLS playlist
   for a while.
3. `main._read_hls_analytics_frame()` (main.py:37062) checks playlist freshness **only when live HLS is on-demand**.
   In the default "always" mode (the Ryzen) it checks only that the playlist exists. It then opens it with
   `cv2.VideoCapture` and returns the first frame of the old segments still on disk.
4. So every AI pass (every 5 s) "succeeds" on frozen video: YOLO, people counting and LPR all run, and LPR also decodes
   the stale recording buffer (that part is L-1, fixed in 29b7b9b).

Observed on the Ryzen, 2026-10-17 00:35–00:45Z:
- after cameras 2 and 3 stopped, the Python VMS ran at 360–450 % CPU with only cameras 1 and 5 streaming;
- before the repair attempt, the same two cameras ran at 27–50 %.

## Smallest safe correction (proposed)
In `_read_hls_analytics_frame`, after the existing "playlist exists" check, require freshness in every mode:
```python
HLS_ANALYTICS_MAX_AGE_SECONDS = float(os.environ.get("ANYAICAM_HLS_ANALYTICS_MAX_AGE_SECONDS", "15"))
...
if not live_hls_demand.playlist_fresh(manifest, max_age=HLS_ANALYTICS_MAX_AGE_SECONDS):
    return None, None, {"ok": False, "error": "Live video is not arriving from this camera.", "detections": []}
```
- It reuses the existing `live_hls_demand.playlist_fresh()`.
- Live segments are 2 s (`-hls_time 2`), so a live playlist is rewritten about every 2 s: a 15 s bound is a 7× margin.
- The on-demand "starting" path (10 s) is unchanged.

## What it does not change
- Live view: the playlist route and live workers are untouched.
- Recording and playback: they don't use this reader.
- The main-stream reader path: it already has a 3 s freshness rule.
- Reconnection: as soon as the live worker writes the playlist again, the next AI pass reads it.

The worker already reports a pass that returns `ok: False` as "waiting" (main.ai_person_detector), so the camera health
shows analytics waiting rather than running on stale video.

## Tests prepared (`app/tests/test_ai_stale_hls_playlist.py`, real ffmpeg HLS)
- A live playlist still feeds detection. Must pass now and after.
- **A stale playlist (10 min old) is not analysed.** Marked `xfail(strict=True)`: it demonstrates today's bug and flips to
  passing with the fix.
- Detection resumes when the playlist is fresh again (reconnection). Must pass now and after.
- A missing playlist is still reported as "not ready". Must pass now and after.

## Risk
- Low. Worst case, a camera whose live worker stalls for over 15 s while its main-stream reader is also down gets no AI
  until video flows again, which is the correct behaviour.
- Remaining exposure without L-2: any unreachable camera with a leftover playlist keeps the CPU busy indefinitely (up to
  several cores on the Ryzen). L-1 alone removes the full-resolution PNG decodes but not the YOLO/LPR passes on the
  stale frame.
