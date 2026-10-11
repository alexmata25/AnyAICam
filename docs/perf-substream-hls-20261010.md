# Edge CPU: motion on the substream, on-demand live HLS (2026-10-10)

> **Superseded in part (2026-10-11):** `docs/video-pipeline-20261011.md` (branch
> `green/video-pipeline-20261011`) moves AI frames off the live HLS playlist,
> makes `ANYAICAM_LIVE_HLS_MODE=auto` the default (on demand when no always-on
> consumer remains), implements the clip input seeking / 1080p option from
> section 4C, and adds measured per-camera video profiles.

Branch `green/perf-substream-hls-20261010` (base `823e983`). Not deployed anywhere.

Line numbers are `app/main.py` at the base commit `823e983` unless marked "head".

## 1. Measured problem (Samsung appliance)

The Samsung appliance has one 8MP camera:

- main stream 101: H.265, 3840x2160, about 20 fps;
- substream 102: H.265, 640x360.

The measurements were taken with no viewers connected.

| Process | CPU (cores) | Where |
|---|---|---|
| Live HLS encoder (libx264 veryfast; H.265 cannot be stream-copied) | 3.26, always on, ~3 restarts / 120 s | `live_video_args()` 16003, `start_live_stream()` 16082, supervisor 17013 |
| Motion reader: decodes the 4K HLS to get 1 fps 160x90 gray | 0.64 | `motion_detector()` 35648, command 35792-35828 |
| Event clip build (concat + libx264 re-encode), in bursts | ~1.2 | `build_motion_event_clip()` 34753 |
| Recorder (main stream, `-c:v copy`) | small | unchanged |
| WebRTC P2P (MediaMTX, substream, on demand) | only while watched | `webrtc_publisher.py`, unchanged |

## 2. Restart churn: root cause

Only two code paths end a live encoder:

1. **The playlist watchdog** (main.py 17160-17199). It calls `process.terminate()` and logs `Camera N live HLS playlist stale for X s; restarting worker.` (17183-17186). It also sets `last_error` to "HLS playlist stopped advancing…".
2. **FFmpeg exiting on its own.** This logs `Camera N live process exited with code C; retrying in 10 seconds.` (17245). `last_error` then holds the redacted FFmpeg stderr tail.

No other code terminates a live encoder. Every other `terminate()`/`kill()` in main.py is the motion reader (36379/36415) or shutdown (41093/41138).

**Defect found (main.py 17171-17179).**

- The watchdog skips its first 30 s, then compares the playlist's mtime to now.
- `-hls_flags append_list` (16091-16127) leaves the previous worker's `camera{N}.m3u8` on disk.
- So a new worker that has not written its first segment within 30 s is judged by its **predecessor's** stale mtime, which is already more than 45 s old. It is killed at the very first check.
- One restart cycle is therefore 30 s run + up to 5 s TERM + 10 s backoff, about 40-45 s. That gives about 3 restarts per 120 s, which is exactly the measured rate.
- The loop sustains itself: every kill leaves another stale playlist.
- A slow start is plausible on this box. One encoder decodes 4K H.265 and encodes 4K x264 on 8 cores at load 12-17. The first 2.8 s segment (`-g 56` at 20 fps) needs more than 30 s whenever the encoder runs below about 0.1x at startup or the RTSP session is slow to open.

A cycle driven only by the watchdog would otherwise be at least about 60 s (2 per 120 s), because an encoder with no playlist at all is not killed before 50 s. An FFmpeg self-exit after about 30 s would also fit 40 s.

**How to confirm on the appliance (read-only; not done in this task):**

```
docker logs <vms> 2>&1 | grep -E "Camera [0-9]+ live (HLS playlist stale|process exited)"
```

- Lines saying `playlist stale for 75-90s` right after restarts confirm the watchdog defect.
- `process exited with code …` lines mean FFmpeg exited on its own. The `[cameraN]` stderr lines before them say why, with credentials masked.

**Fix (head 17212-17229).** A worker's playlist age is now counted from that worker's own start at the earliest, so a new worker gets the full 45 s.

- A truly stalled worker is still restarted, at about 45-50 s.
- The flag `ANYAICAM_LIVE_HLS_WATCHDOG_FROM_WORKER_START` defaults to `true`. Setting it to `false` restores the old rule exactly.
- Tests:
  - `test_watchdog_old_rule_kills_slow_starting_worker_at_first_check` reproduces the 30 s kill.
  - `test_watchdog_counts_from_worker_start` and `test_watchdog_slow_but_working_start_is_not_killed` cover the fix.

## 3. Consumers of the live HLS output (`/app/static/hls`, `camera{N}.m3u8`, `camera{N}_*.ts`)

### Always running (background, no viewer)

| # | Consumer | file:line (base) | Without a running encoder |
|---|---|---|---|
| 1 | Live supervisor watchdog | main.py 17160-17199 | restarts the encoder |
| 2 | `motion_detector()` (ffmpeg on the playlist) | main.py 35648 / 35729 | motion stops |
| 3 | `create_motion_thumbnail()`: color frame from the playlist | main.py 34173 / 34236 | gray fallback thumbnail |
| 4 | `detect_objects_frame()` (`cv2.VideoCapture(playlist)`) and `_hls_read_frame_time()` | main.py 36618 / 36690 / 36764, 36603 | analytics blind, or a stale frame re-read |
| 4a | `ai_person_detector()`: YOLO every 5 s for every camera. `AI_PERSON_DETECTION_ENABLED` defaults to true and there is no per-camera gate | main.py 39010, started 40492 | |
| 4b | LPR `run_lpr_scan()` uses that frame (full-res option comes from `_event_buffer`, not HLS) | main.py 38524; lpr.py 496-530 | |
| 4c | PPE / facial recognition crops in `save_yolo_events()` | main.py 37370, 38196, 38260 | |
| 4d | `people_counting_worker()` | main.py 38723 / 38848 | |
| 4e | Customer analytics / Smart Rules worker | customer_analytics_rule_worker.py 322-344 | |
| 5 | `_legacy_camera_status()`: "online" when the playlist mtime is under 15 s. Feeds `health_monitor()` (39806; stream_offline alerts), `/ready` `cameras_online` (14822/15029), site monitoring summary (130237) | main.py 52775-52811 | camera shown offline, false alerts |
| 6 | Appliance agent `LocalVmsStatusReader`: playlist mtime under 20 s. Drives cloud `appliance_camera_status.online`, `camera_offline` warning / degraded, customer status, AACO, notifications | appliance-agent/anyaicam_agent/camera_binding.py 363-395 | camera shown offline in the cloud |
| 7 | `hls_segment_sweeper_worker` (orphan `.ts` cleanup) | hls_segment_sweeper.py 58-106 | unaffected (cleans up) |

### Only on viewer / user request

| # | Consumer | file:line (base) |
|---|---|---|
| 8 | Live relay uploader (uploads only for cameras with an active cloud relay) | live_relay_uploader.py 306-420, 472; started main.py 40611 |
| 9 | `/static` mount serving `/static/hls/*` (no-cache middleware 41773). Pages: home `connectCamera` (73060), camera detail (49374), analytics rule preview (79613), playback live monitors `attachStream` (147625) | main.py 41999 |
| 10 | Customer live routes: `…/live/playlist.m3u8` (`local_playlist()`, returns an empty playlist when stale), `…/live/segments/{name}`, `…/live/still.jpg` (newest local `.ts`). Callers: live_view_page.py 1025/1873, customer_analytics_rules.py 345, facial_recognition_ui.py 385 | live_playlist.py 185-261; local_live_hls.py 20-55 |
| 11 | WireGuard live proxy (fetches `/static/hls/...` through the tunnel) | live_view_wireguard.py 209-250 |

### Incidental, not runtime readers

- Optional backups `include_hls` (main.py 25945-25972).
- Release readiness folder check (27601).
- Offline source patchers `v1_1_2_hls_latency_patch.py` and `v1_1_2_preflight_hotfix.py`.
- Installer excludes.
- `recordings_guard.py` 27 (extension list).

### Not HLS consumers

- WebRTC/MediaMTX (pulls RTSP).
- Event clips (recordings).
- `/api/snapshots`.
- `latest-thumbnail` (S3).
- AACO, except through the status path in items 5-6.

## 4. Design

### A. Motion reader on the substream (`app/motion_source.py`)

**Source.** `ANYAICAM_MOTION_SOURCE=auto|substream|hls`.

- `auto` (default): the camera's substream when it has a distinct one, else the HLS playlist.
- `substream`: always the substream when one exists.
- `hls`: the old command, unchanged.

**Command.** `ffmpeg -loglevel error -rtsp_transport tcp -fflags nobuffer -i <sub> -an -vf fps=1,scale=160:90,format=gray -f rawvideo pipe:1`. This is the same filter as before. With `hls`, the command is byte-for-byte the old one (`test_hls_command_is_the_previous_command_exactly`).

**Finding the substream URL** (`motion_substream_url()`, head main.py 35640). It reuses what the codebase already has:

1. The stored capability record (`camera_capabilities.load()`, ONVIF profiles written by the WebRTC publisher). It is used only if it describes the camera's current main URI. The smallest sub stream at least 160 px wide wins, and streams marked `verified: false` are skipped.
2. Otherwise, the URL convention `webrtc_publisher.substream_candidates()` (`…/Channels/101` becomes `…/102`; `subtype=0` becomes `1`).

The camera's own credentials (from `camera_url()`) are re-applied only to the URL handed to FFmpeg. FFmpeg's stderr is discarded, the command is never logged, and every log line names only the camera number and source kind. The existing "retrying" log also passes through `_redact_stream_credentials()`.

**Fallback.**

- A substream run that ends with fewer than 30 frames, or delivers no frame for `ANYAICAM_MOTION_SUBSTREAM_STALL_SECONDS` (30), is a failure.
- In `auto`, after `ANYAICAM_MOTION_SUBSTREAM_MAX_FAILURES` (3) consecutive failures, the camera reads HLS for `ANYAICAM_MOTION_SUBSTREAM_RETRY_SECONDS` (1800), then tries the substream again.
- A camera without a distinct substream, or one that is not configured, always uses HLS.

**Detection math does not depend on resolution.**

- The reader reads exactly 160×90 bytes per frame, and the zone/exclusion masks are 160×90 (`_compare_motion_frames()`).
- Thresholds are fixed (`>=20` per-pixel delta, score threshold `4.0*(1.5-s/100)`, `0.02 <= changed_ratio <= MOTION_MAX_CHANGED_RATIO`) and computed on those 160x90 frames.
- `scale=160:90` forces that size from any input.
- `previous_frame` is reset when the source changes or the substream stalls, so frames from two different sources are never compared.

**Differences to watch for (in the soak test):**

- **Timing.** Substream frames are near real time. HLS frames were about 6 s late (FFmpeg starts a live playlist 3 segments back; see detection_timing.py), and motion events were stamped with no compensation. Event start and end times, and therefore clip windows and pre-roll, now line up with the real motion, about 6 s earlier than before. This is an improvement, but it is visible.
- **Noise.** A 640x360 H.265 substream downscaled 4x has slightly more compression noise per output pixel than a 4K stream downscaled 24x. The `>=20` delta threshold should absorb it. Compare the motion-event rate (pass criterion below).
- **Aspect ratio.** A camera whose substream has a different aspect ratio (for example 4:3 D1 on a 16:9 main) is still squashed to 160x90, the same full field of view, so the normalized zones still map correctly.

### B. On-demand live HLS (`app/live_hls_demand.py`)

**Modes.** `ANYAICAM_LIVE_HLS_MODE=always|on_demand`. The default is **always** (see "Default" below). `ANYAICAM_LIVE_HLS_IDLE_SECONDS` defaults to 60, with a minimum of 10.

**Demand signals**, all no-ops in `always`:

- **HTTP.** Middleware touch on any `/static/hls/cameraN.m3u8` or `cameraN_*.ts` request (head main.py 41715). This covers the legacy pages and the WireGuard proxy.
- **Customer live routes.** `local_live_hls.local_playlist()` and `segment_path()` touch, and so does the local `still.jpg` path. The customer live page already polls the playlist every 2 s for up to 45 s until it lists segments (live_view_page.py 1149-1163). So the first viewer's first poll starts the encoder and a later poll gets video, with no error.
- **Live relay.** `live_relay_uploader.active_camera_numbers` is registered as a demand provider (head main.py 40478).
- **Analytics.** `detect_objects_frame()` touches. In `on_demand` it never analyses a stale playlist; it returns "Live HLS stream is starting." until the playlist is fresh (10 s).
- **Motion.** While the motion reader reads HLS (fallback or `hls`), it holds a lease on the encoder.

**Supervisor** (head main.py 17020-17039 and the loop).

- With no demand it waits in state `idle` without starting FFmpeg, re-checking every 1 s.
- When the encoder has been unneeded for IDLE_SECONDS, it TERMs the encoder. This is not a failure: no reconnect count, no error, no copy-fallback count, and no 10 s backoff.
- After an idle stop, the playlist's mtime is set back by one hour. Every freshness check then treats it as stale at once. The content is kept, so `append_list` continues the segment numbering and no segment name is reused, which matters for the relay uploader's de-duplication.

**Status.**

- An idle encoder's playlist is stale by design. While the camera is demonstrably reachable (its recorder is running, or the motion reader got a substream frame in the last 20 s), the supervisor refreshes `HLS_FOLDER/camera{N}.alive` every 5 s, both while idle and while the encoder is starting.
- `_legacy_camera_status()` (head 52745) and the agent's `LocalVmsStatusReader` accept that file. The agent change in `camera_binding.py` ships in the appliance-agent package and must be deployed together with the VMS before `on_demand` is turned on.
- In `always` the file is never written, and the local status ignores it.

**Motion thumbnails.** In `on_demand`, when the playlist is not fresh, the color thumbnail is taken from the substream (same field of view, 640 px wide, 20 s timeout). If that fails, the existing gray fallback is used. In `always` nothing changes (`test_create_motion_thumbnail_always_mode_reads_hls_as_before`).

**Legacy local pages.** Home, camera detail, analytics rule preview and playback monitors used to stay dead after a fatal hls.js error. They now retry (destroy, then re-attach after 3 s, at most 20 times). This is the only change that is not behind a flag. It applies only after a fatal player error, which previously left the tile dead.

**Default: `always`.** After A, the motion reader no longer needs HLS. But YOLO person/vehicle detection runs every 5 s on every camera by default, with no per-camera gate (`AI_PERSON_DETECTION_ENABLED=true`), and LPR, PPE, face, people counting and Smart Rules read the same frames. On an appliance where `ai_person_detector()` is active, `on_demand` correctly keeps the encoder running, so it saves nothing there. Moving those readers to another source is out of scope: LPR and face need resolution, and the substream is 640x360. `on_demand` is therefore opt-in for appliances whose cameras run only motion (and AI is disabled or unavailable), until the analytics frame source is migrated.

### C. Event clip build: analysis only (not changed)

`build_motion_event_clip()` (34753-34916) and `build_manual_clip()` (60074, command 60356-60368) run `-f concat -i list -ss offset -t dur -c:v libx264 -preset veryfast`.

- `-ss` comes **after** `-i` (34892), so it seeks on the output. FFmpeg decodes, and throws away, every frame from the start of the first 300 s recording segment up to the event.
- For a 4K H.265 recording that can be up to 5 minutes of 4K HEVC decode per clip. This is probably most of the ~1.2-core bursts.

Options, best first:

1. **Input seeking:** `-ss offset` before `-i`. The concat demuxer seeks to the keyframe before `offset`; FFmpeg's default `-accurate_seek` then trims exactly. Decode drops to (GOP + clip length). This needs a test with growing and multi-file windows, and timestamps checked on the Samsung's mkv segments. **Recommended next change.**
2. **Downscale clips to 1080p** (`-vf scale=-2:1080`): x264 cost is about 4x lower, and cloud transfer and storage drop too. This is a product decision.
3. **`-preset superfast` / `ultrafast`**: 20-40 % less encode CPU, larger files.
4. **`-threads 2` and `nice`** for the clip encode: same total CPU, but bursts no longer starve live/recording. `event_clip_encode_semaphore` already serializes clips.
5. **Stream copy** when the recording is H.264: no encode at all, but keyframe-aligned cuts. Not for H.265, which many browsers cannot play in MP4.

## 5. Flags and defaults

| Flag | Default | Effect | Old behaviour |
|---|---|---|---|
| `ANYAICAM_MOTION_SOURCE` | `auto` | substream for motion when the camera has one, HLS otherwise or after failures | `hls` |
| `ANYAICAM_MOTION_SUBSTREAM_MAX_FAILURES` | 3 | failures before `auto` switches to HLS | n/a |
| `ANYAICAM_MOTION_SUBSTREAM_RETRY_SECONDS` | 1800 (min 60) | HLS period before retrying the substream | n/a |
| `ANYAICAM_MOTION_SUBSTREAM_STALL_SECONDS` | 30 (min 5) | no frame for this long = failed run | n/a |
| `ANYAICAM_LIVE_HLS_MODE` | `always` | `on_demand` starts and stops the encoder by demand | `always` |
| `ANYAICAM_LIVE_HLS_IDLE_SECONDS` | 60 (min 10) | idle time before the encoder stops (`on_demand`) | n/a |
| `ANYAICAM_LIVE_HLS_WATCHDOG_FROM_WORKER_START` | `true` | playlist age counted from this worker's start at the earliest | `false` |

An unrecognised value means the old behaviour: `hls` / `always`.

## 6. Expected CPU savings (ESTIMATES, not measured)

| Change | Before (measured) | After (estimate) | Saving (estimate) |
|---|---|---|---|
| A: motion on the 640x360 H.265 substream | 0.64 core | 0.03-0.08 core (20 fps 360p HEVC decode + 1 fps scale) | **≈0.55-0.6 core**, always |
| Watchdog fix (no restart churn) | about 3 restarts / 120 s; each restart re-opens RTSP, re-inits the 4K decoder and encoder, and loses about 10 s of live video | steady encoder | small CPU (≈0.1-0.3 core of startup overhead, estimate); removes live gaps and log noise |
| B: `on_demand` with no viewer and no HLS analytics | 3.26 cores | 0, plus an alive-file touch | **≈3.2 cores** while idle; nothing on cameras where YOLO/LPR/people counting/Smart Rules run |
| B: while someone watches | 3.26 | 3.26 for the viewing time plus 60 s | 0 |
| Clip option 1 (not implemented) | ~1.2-core bursts | most of the decode-before-offset removed | burst length and size cut sharply (estimate) |

On the measured Samsung (one camera, no viewer): A alone gives about 0.6 core. A + B (`on_demand`, AI detection off or unavailable) gives about 3.8 cores of 8.

## 7. Rollback

**Environment only (container restart, no code change):**

- `ANYAICAM_MOTION_SOURCE=hls`: the motion reader runs the old command against the HLS playlist, with no source logs.
- `ANYAICAM_LIVE_HLS_MODE=always` (or unset): the encoder always runs, nothing is touched, held or idle-stopped, and no `.alive` file is written. Leftover `.alive` files go stale after 15-20 s and are ignored.
- `ANYAICAM_LIVE_HLS_WATCHDOG_FROM_WORKER_START=false`: the previous watchdog rule.

**Code:** `git revert <commit>`, or redeploy the previous `app/` and `appliance-agent/` files.

- The new modules `motion_source.py` and `live_hls_demand.py` are imported by main.py, local_live_hls.py and live_playlist.py, so revert those files together.
- The agent change is backward compatible: it only adds an "or" on a file that the old VMS never writes. It can stay even if the VMS is rolled back.

## 8. Sustained performance test plan (2 h per scenario, on a lab appliance first)

**Setup.**

- Same camera and settings as the measurement: 8MP H.265 main + 640x360 substream.
- Record before and after on the same build, flags only.
- Phase 1: `ANYAICAM_MOTION_SOURCE=auto` with `LIVE_HLS_MODE=always`.
- Phase 2: also `ANYAICAM_LIVE_HLS_MODE=on_demand`. Only if `AI_PERSON_DETECTION_ENABLED=false` or YOLO is unavailable, and only with the matching appliance-agent.

**Sample every 10 s:**

- `uptime` (load average);
- `top -b -n1` / `pidstat -C ffmpeg 10` (cores per ffmpeg role);
- ffmpeg process start times (restart count);
- `docker logs` lines `motion detector source`, `motion substream failed`, `live HLS playlist stale`, `process exited`, `not needed for`;
- `/api/cameras/status`;
- cloud camera status.

**Scenarios (2 h each):**

1. **Idle:** no viewer, a quiet scene.
2. **Events:** scripted motion (walk-through every 5 min, 24 events).
3. **WebRTC viewer:** a viewer is connected for the full 2 h.
4. **HLS viewer:** local or relay HLS live page for 2 h, plus 10 open/close cycles at the end with 2 min idle between them (`on_demand`).

**Pass criteria (all scenarios):**

- **Motion-event rate:** same as the HLS baseline over the same 2 h of scripted motion. ±10 % on the event count, and all 24 scripted events detected. Times may be about 6 s earlier.
- **Recordings:** continuous. No gap over 15 s in the `camera{N}` segments; the recorder process never restarts because of this change.
- **Clips:** every event clip is created, uploaded (cloud plans), and plays in Chrome and Safari.
- **Live works:**
  - WebRTC tile in under 5 s.
  - HLS live page shows video in under 15 s from a cold encoder (`on_demand`), and with no visible error.
  - The camera is never shown offline (local `/api/cameras/status`, cloud status, no `stream_offline` or `camera_offline` alert).
- **Load:** load average below 6 (1-min, sampled) for the whole 2 h.
- **Restart churn:** no ffmpeg restart churn.
  - Live encoder restarts are 0 in `always` (except camera faults).
  - In `on_demand`, starts and stops only follow viewer open/close.
  - Motion substream fallbacks: 0.
- **Logs:** no credentials in any log (grep for the camera password and `://user:` patterns).
