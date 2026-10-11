# Edge video pipeline: measured profile, AI on the substream, on-demand HLS, cheaper clips (2026-10-11)

Branch `green/video-pipeline-20261011`, base `2ad5814` (which already moved
motion to the substream, added optional on-demand live HLS and fixed the live
watchdog -- see `docs/perf-substream-hls-20261010.md`). **Not deployed
anywhere.** Nothing here was run on an appliance; every number below is
labelled MEASURED (Docker on the dev PC, or offline on real clips) or
ESTIMATE.

## 1. Problem (measured on the Samsung appliance, one 8MP H.265 camera, no viewer)

| Workload | Cores | Cause |
|---|---|---|
| Live HLS libx264 at 4K | 3.3-3.5, always on | YOLO opened the 4K playlist every ~5 s, so the encoder could never be idle |
| Event clip build | 1.25-1.7 per build | output seeking (`-ss` after `-i`) decoded up to 300 s of 4K HEVC per clip; full-4K x264 encode |
| Motion reader | 0.64 | fixed in 2ad5814 (substream) |
| AI thumbnails | -- | full 3840x2160 JPEGs |

`cameras.resolution` is only a sales label (`'2mp'` default, hard-coded by
partner onboarding, re-written with the plan label by the setup wizard); the
real geometry existed only in the capability record written by the WebRTC
publisher, and nothing in the video pipeline read it.

## 2. Design (one commit per item)

### P1 -- measured geometry and a per-camera processing profile

* `camera_video_probe.py` measures every camera independently of P2P:
  ONVIF media profiles (existing `camera_capabilities.discover`) then
  `ffprobe` on the main stream and on the analytics stream. Results go under
  each stream's `"measured"` key in the capability record; the ONVIF fields
  P2P selection uses are untouched (test proves the P2P choice is identical).
  Re-measured every `ANYAICAM_VIDEO_PROBE_SECONDS` (6 h), after
  `ANYAICAM_VIDEO_PROBE_RETRY_SECONDS` (10 min) on failure, and at once when
  the camera's main URL changes. A failed probe never raises; profile falls
  back to safe defaults. Worker: `main.camera_video_probe_worker()`.
* `video_profile.py`: class (`2mp` <= 2.6 MP, `4mp` <= 4.5, `5mp` <= 6.5,
  `8mp` <= 9.5, else `other`; `unknown`), shape (landscape / portrait /
  square = fisheye / panoramic) and, from MEASURED values only:
  * clip box 1920x1080 (portrait 1080x1920, square 1440x1440, panoramic
    2560x1080), bitrate cap ~2 kbit per 1000 output pixels (800-8000 kbps);
  * live box (same) and a live bitrate cap never above
    `ANYAICAM_LIVE_MAX_BITRATE_KBPS`;
  * analytics stream = smallest stream >= 640 px wide (else main) and the
    analytics frame size (fits 1280x720);
  * thumbnail width `ANYAICAM_THUMBNAIL_MAX_WIDTH` (1280).
  All scale filters are `scale='min(W,iw)':'min(H,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2`:
  downscale-only and aspect-preserving, so an unknown or changed resolution
  can never distort or upscale. Verified with real ffmpeg for 1920x1080,
  2560x1440, 2592x1944, 3840x2160, 4000x3000, 640x360.
* Live HLS x264 encode now scaled into the profile box (4K -> 1080p).
  `ANYAICAM_LIVE_SCALE=full` = previous args exactly. Stream copy never scaled.
* Reporting: VMS writes credential-free `HLS_FOLDER/camera<N>.video.json`; the
  agent's `LocalVmsStatusReader` adds a whitelisted `video` object to the
  camera status it already POSTs to `/api/appliance/cameras`; the cloud stores
  `cameras.measured_width/height/codec/fps/sub_width/sub_height/sub_codec/
  resolution_class/shape/video_at` (additive migration). The class is
  recomputed server-side from pixels. `cameras.resolution` is never written
  by this path, and the label writes (onboarding, wizard PUT) never touch
  `measured_*` (tested). `GET /api/customer/cameras` adds `video_resolution`
  ("3840x2160 H265 (8MP)") and `resolution_source`; the setup wizard shows a
  "Video (measured)" column.

### P2a -- AI detection from the analytics stream

* `analytics_frames.py`: ONE persistent ffmpeg per camera reads the profile's
  analytics stream at `ANYAICAM_AI_READER_FPS` (2) as raw BGR at the profile
  analytics size. The newest frame is shared by `ai_person_detector`, People
  Counting and Smart Rules; `detect_objects_frame()` runs one YOLO pass per
  shared frame (cached by frame sequence). Stalled runs are killed (15 s),
  idle readers stop (120 s), the reader restarts when the measured geometry
  changes. The HLS "touch" now happens only on the HLS path.
* `ANYAICAM_AI_SOURCE=auto` (default): after 3 failed reader runs the camera
  uses the previous live-HLS read for 30 min, then retries. `substream`:
  never falls back. `hls`: previous behaviour exactly.
* LPR / PPE / face crops (`main_frame_grab.py`): when the frame is an
  analytics-stream frame, ONE main-stream frame is fetched on demand -- the
  Event-mode pre-event buffer first, else a single-frame RTSP read (so it
  works in Continuous mode too) -- only when a vehicle/person box is
  actually processed on an entitled camera. `lpr.scan_frame` takes it lazily
  and resolves it at most once per pass, never for a parked vehicle being
  skipped. Cached 2.5 s per camera (one grab serves LPR, PPE and face), 60 s
  back-off after a failed grab. `ANYAICAM_AI_MAIN_GRAB=false` disables it.

### P2b -- on-demand live HLS by default when nothing else needs it

`ANYAICAM_LIVE_HLS_MODE=auto` is the new default: on demand while
`main._live_hls_auto_policy()` holds, otherwise exactly `always`. Re-checked
every 30 s. The policy requires:

1. motion not configured to read HLS (`ANYAICAM_MOTION_SOURCE != hls`);
2. AI frames not configured to read HLS (`ANYAICAM_AI_SOURCE != hls`);
3. a fresh `agent-reads-alive.marker` in the HLS folder, written (every
   60 s) by an appliance agent whose status reader accepts `camera<N>.alive`.
   An older agent never writes it, so its cameras can never be reported
   offline because the encoder went idle.

Per-camera HLS fallbacks (motion or AI reader failures) keep that camera's
encoder running via `hold()`/`touch()`, which are recorded in auto mode even
while it is effectively always-on; when auto switches to on demand every
running encoder gets a full idle period first.

Remaining live-HLS consumers after this branch (base list in
`perf-substream-hls-20261010.md` section 3):

| Consumer | Always on? | Now |
|---|---|---|
| Live supervisor watchdog | its own encoder | unchanged |
| Motion reader | only `MOTION_SOURCE=hls` or per-camera fallback | holds the encoder while it reads HLS |
| Motion colour thumbnail | no | substream when the encoder is idle (2ad5814) |
| AI / People Counting / Smart Rules / LPR / PPE / face frames | only `AI_SOURCE=hls` or per-camera fallback | analytics reader; touches HLS only on the HLS path |
| `_legacy_camera_status` / health monitor / `/ready` | uses `.alive` while idle | unchanged (2ad5814) |
| Agent `LocalVmsStatusReader` (cloud online status) | uses `.alive` while idle | marker gates auto mode |
| Live relay, `/static/hls`, customer live routes, WireGuard proxy, still.jpg | viewer only | demand signals (2ad5814) |

`ANYAICAM_LIVE_HLS_MODE=always` restores the previous behaviour; `on_demand`
forces it regardless of the policy.

### P2c -- event clips

`clip_encode.py`: input seeking (`-ss` before `-i`; a window inside one file
reads it directly, a window across files uses the concat list), event clips
scaled into the profile box as H.264 yuv420p + `+faststart` with a bitrate
cap, and `-threads ANYAICAM_EVENT_CLIP_THREADS` (2) for decode and encode.
Manual (customer-requested) clips get input seeking + thread cap but keep full
resolution unless `ANYAICAM_MANUAL_CLIP_SCALE=profile`.
`ANYAICAM_EVENT_CLIP_ENCODE=legacy` = previous commands exactly;
`ANYAICAM_EVENT_CLIP_SCALE=full` keeps seeking without the downscale.

### P2d -- thumbnails

AI (YOLO), LPR plate-event, People Counting and Smart Rules thumbnails are
saved at most `ANYAICAM_THUMBNAIL_MAX_WIDTH` (1280) wide (INTER_AREA, never
upscaled), boxes drawn in thumbnail coordinates; stored detections keep
analytics-frame coordinates; plate crops stay native. `0` = full frame.

## 3. Flags and defaults

| Flag | Default | Rollback value |
|---|---|---|
| `ANYAICAM_VIDEO_PROBE_SECONDS` / `_RETRY_SECONDS` / `_TIMEOUT_SECONDS` | 21600 / 600 / 15 | (probe is read-only; no rollback needed) |
| `ANYAICAM_LIVE_SCALE` | `profile` | `full` |
| `ANYAICAM_CLIP_MAX_LONG_SIDE` / `_SHORT_SIDE`, `ANYAICAM_LIVE_MAX_LONG_SIDE` / `_SHORT_SIDE`, `ANYAICAM_SQUARE_MAX_SIDE`, `ANYAICAM_PANORAMIC_MAX_LONG_SIDE`, `ANYAICAM_ANALYTICS_MAX_LONG_SIDE` / `_SHORT_SIDE` | 1920/1080, 1920/1080, 1440, 2560, 1280/720 | -- |
| `ANYAICAM_AI_SOURCE` | `auto` | `hls` |
| `ANYAICAM_AI_READER_FPS` | 2 | -- |
| `ANYAICAM_AI_READER_STALL_SECONDS` / `_IDLE_SECONDS` / `_MAX_FAILURES` / `_RETRY_SECONDS` | 15 / 120 / 3 / 1800 | -- |
| `ANYAICAM_AI_FRAME_MAX_AGE_SECONDS` / `ANYAICAM_AI_FIRST_FRAME_WAIT_SECONDS` | 3 / 8 | -- |
| `ANYAICAM_AI_MAIN_GRAB` (+ `_CACHE_SECONDS` 2.5, `_TIMEOUT_SECONDS` 10, `_BACKOFF_SECONDS` 60) | `true` | `false` (crops from the analytics frame) |
| `ANYAICAM_LIVE_HLS_MODE` | `auto` (was `always`) | `always` |
| `ANYAICAM_EVENT_CLIP_ENCODE` | `optimized` | `legacy` |
| `ANYAICAM_EVENT_CLIP_SCALE` / `ANYAICAM_MANUAL_CLIP_SCALE` | `profile` / `full` | `full` / `full` |
| `ANYAICAM_EVENT_CLIP_THREADS` | 2 | `0` (ffmpeg default) |
| `ANYAICAM_THUMBNAIL_MAX_WIDTH` | 1280 | `0` |

Unrecognised values fall back to the previous behaviour (`hls`, `always`,
`legacy`).

**Full env-only rollback to 2ad5814 behaviour:**
`ANYAICAM_AI_SOURCE=hls ANYAICAM_LIVE_HLS_MODE=always ANYAICAM_LIVE_SCALE=full
ANYAICAM_EVENT_CLIP_ENCODE=legacy ANYAICAM_THUMBNAIL_MAX_WIDTH=0
ANYAICAM_AI_MAIN_GRAB=false` (+ `ANYAICAM_MOTION_SOURCE=hls` for the
pre-2ad5814 motion reader). The probe worker and the cloud columns stay
(read-only measurement, additive schema).

**Git rollback:** `git revert` the commits newest first (886b15c, 4ee1ffb,
f8ff3a0, 65576b5, 9e6e0d8, b58950c). Each commit is independent except: P2a
uses the P1 profile, P2b's policy reads P2a's `ai_frames`, P2c/P2d use the
profile. The cloud migration is additive (NULL columns), so a reverted cloud
keeps working with them present; an old appliance agent ignores nothing
(it simply never sends `video`), and a new agent against an old cloud sends
an extra `video` key the old route ignores.

## 4. Validation results

### 4.1 AI accuracy on real clips (MEASURED offline; NOT a substream measurement)

20 event clips of the protected test camera (S3, read-only; deleted after
the run), all H.264 3840x2160 20 fps, 12-49 s. 473 frames (1 per second).
YOLOv8n `imgsz=640` on CPU, threshold 0.35 (production `YOLO_CONFIDENCE`),
person = `person`, vehicle = car/truck/bus/motorcycle/bicycle; a match is the
same group with IoU >= 0.5 (low-res boxes mapped back x6).

| Test input vs reference (a) = the 4K frame | TP | FN (missed) | extra | recall vs 4K | precision vs 4K | conf delta mean / median / p10 / p90 |
|---|---|---|---|---|---|---|
| (b) 640x360 INTER_AREA downscale | 637 | 1 | 185 | 99.84 % | 77.5 % | -0.023 / -0.010 / -0.122 / +0.071 |
| (c) 640x360 + H.265 512 kbps re-encode (substream-like) | 637 | 1 | 189 | 99.84 % | 77.1 % | -0.023 / -0.007 / -0.136 / +0.077 |

* Reference detections: 638 persons, **0 vehicles** -- these clips contain no
  vehicles, so vehicle recall on the substream is **NOT VALIDATED** on real
  data.
* The single miss (both b and c): `motion_61f8371f2a93.mp4` t=24 s, a
  person at 4K confidence 0.376 (just above threshold), box 679x1558 px at 4K
  = 113x260 px at 640; the low-res pass saw it at 0.186 / 0.114.
* All 185/189 "extra" low-res detections are objects the 4K pass also
  found: 172/174 the same box with 4K confidence below 0.35, 13/15 an
  overlapping box (IoU 0.3-0.5). **None** has no 4K counterpart, so the
  lower "precision" is threshold/box jitter, not new false positives. (YOLO
  letterboxes a 4K frame to 640 with linear interpolation; an INTER_AREA
  downscale is a cleaner image, which plausibly explains the higher
  confidence on several boxes.)
* Persons in this scene are large (the miss was 113x260 px at 640 width);
  small, distant persons are not represented -- recall for those on a
  640x360 substream is untested.
* **The real camera substream is a different encode** (camera-side H.265 at
  its own bitrate/GOP, possibly sharpened differently). (c) is only a proxy.
  Substream accuracy is NOT VERIFIED until the on-appliance soak (section 6).
* LPR/PPE/face keep full resolution through the main-stream grab; their
  accuracy is unchanged by design but the grab is ~1-2 s after the analytics
  frame (box mapped with a 15 % margin) -- also to be verified in the soak.

### 4.2 Event timing (MEASURED, synthetic RTSP via MediaMTX in Docker)

One ffmpeg publishes main 1280x720 + sub 640x360 (H.264, GOP 2 s, same lavfi
clock) with a full-frame flash at t = 10/30/50 s. Lag = wall time a frame
showing the flash arrives - (publisher start + offset); +-0.1-0.3 s start
uncertainty.

| Path | Lag per event (s) | Event timestamp error |
|---|---|---|
| NEW AI reader (substream, 2 fps) | 1.00, 1.01, 1.00 | ~1.0 s (frame time = receipt time) |
| NEW motion reader (substream, 1 fps) -- 2ad5814 | 2.25, 2.26, 2.25 | ~2.3 s |
| OLD motion reader (live HLS) | 3.33, 6.09, 6.01 | ~6 s (uncompensated) |
| OLD AI read (cv2 on live HLS) | 11.36, 11.46, 11.40 | estimate via detection_timing was +2.0 to +2.8 s off |

* With a 5 s pre-roll the new paths start the clip ~2.7-4 s BEFORE the real
  onset; the old HLS motion path started it ~1 s AFTER.
* Clip window (pytest `test_event_clip_input_seek.py`, real HEVC
  recordings with filename timestamps, window spanning two 30 s files): the
  event's flash frame sits at 5.00 s +-0.1 in the built clip; clip duration
  10.0 +-0.3 s; H.264 yuv420p, moov first. Input-seek frame selection is
  frame-identical (decoded MD5) to the old output-seek selection for a single
  file, a two-file concat window and an unindexed in-progress recording.

### 4.3 CPU (MEASURED in Docker on the dev PC, 16 threads -- NOT the appliance)

4K HEVC stream (from a real test-camera clip, 10 Mbps, GOP 2 s, 20 fps) and a
640x360 HEVC 512 kbps substream looped through MediaMTX; each streaming
workload 60 s; CPU = user+sys of the process.

| Workload | Old | New | Change |
|---|---|---|---|
| Live HLS encoder, no viewer | 1.89 cores (4K x264) | **0** (auto, idle) | -1.89 |
| Live HLS encoder, while watched | 1.89 cores | 1.18 cores (1080p x264) | -38 % |
| AI frame source | 0.10 core (cv2 4K HLS read every 5 s) **+ the forced encoder** | 0.034 core (substream reader 2 fps) | -0.07 core and the encoder can stop |
| AI frame source, camera without a substream | same | 0.43 core (main-stream reader 2 fps, 4K decode) | -- |
| Motion reader (2ad5814) | 0.285 core | 0.034 core | -0.25 |
| YOLO per scan (imgsz 640, 2 torch threads) | 0.30 CPU-s (4K frame) | 0.29 CPU-s (640 frame) | ~same (YOLO resizes to 640 anyway) |
| Event clip, 10 s window at 30 s into a 300 s segment | 44.8 CPU-s, 11.2 s wall, 4.07 MB | 9.5 CPU-s, 3.8 s, 0.81 MB | -79 % |
| ... at 150 s | 123.6 CPU-s, 21.1 s | 11.0 CPU-s, 4.4 s | -91 % |
| ... at 280 s | 191.5 CPU-s, 34.6 s | 9.5 CPU-s, 3.8 s | -95 % |
| (input seek only, still 4K, at 150 s) | -- | 30.1 CPU-s | seek alone -76 % |
| Main-stream single-frame grab (LPR/PPE/face, only with a box) | -- (HLS frame / event buffer) | 1.2 CPU-s, ~2.0 s wall per grab | new cost, on demand |

Grab variants measured: `-skip_frame nokey` halves CPU (0.55 s) but waits
~4 s; a 1 MB probe saves no CPU. The default command was kept.

### 4.4 Appliance estimate (ESTIMATE, scaled -- not measured)

The Samsung measured 3.3 cores for the same 4K encode the dev PC does in
1.89, so dev-PC numbers are scaled x1.75 here.

| No viewer, AI on, one 8MP camera | 2ad5814 (measured on appliance) | This branch (estimate) |
|---|---|---|
| Live HLS encoder | 3.3-3.5 | 0 with the new agent (auto); ~2.1 while someone watches HLS |
| AI frame read | ~0.18 + forced encoder | ~0.06 |
| Event clip build | 1.25-1.7 cores for 20-60 s per clip | ~10-20 CPU-s per clip (~5-6 s at 2 threads) |
| Thumbnails | 3840x2160 JPEG (~1-2 MB) | 1280x720 / 640x360 |
| **Steady-state saving** | | **~3.4 cores** of 8 when idle, plus much shorter clip bursts |

### 4.5 Test suites, base 2ad5814 vs head (Docker `anyaicam-citest:035eaa1`)

| Suite | Base 2ad5814 | Head |
|---|---|---|
| 76 related app test files (clip, motion, LPR, capabilities, thumbnails, HLS, detection, people counting, rules, WebRTC, timing, AI, PPE, appliance cloud, edge sync, live relay/playlist, recording uploader, status, Smart Motion, event media, playback, stationary, exclusion, partner workspace) | 1128 passed, 2 failed, 4 skipped | same 76 files + the 5 new test files + 4 of the extra files below in one run: 1299 passed, 2 failed, 4 skipped |
| 7 extra files (live copy/transport, talk-down, camera migration, multi-appliance isolation, camera count, cameras tab) | 106 passed | 110 passed (+1 new live-scale test, +3 customer-surface tests) |
| appliance-agent `tests/` | 922 passed, 4 failed, 4 skipped | 922 passed, 4 failed, 4 skipped |

The failures are identical on base and head (pre-existing):
`test_lpr_real_world.py::test_glyph_reader_handles_tilted_and_slanted_plates[-15--0.2]`,
`test_webrtc_publisher.py::test_recording_and_live_hls_still_use_the_main_stream_only`
(a source-grep test already broken by 2ad5814's `motion_substream_url`), and
in the agent `test_agent_file_helpers.py` (3) and
`test_capacity_accept.py` (1).

## 5. Deployment steps (when approved -- NOT done)

1. Cloud first: deploy the cloud build (additive `cameras.measured_*`
   migration runs at start; `/api/appliance/cameras` accepts `video`).
2. Appliance (lab first, then Samsung): install VMS + appliance agent from
   the same release (the agent writes the marker and reports `video`).
   Phase 1 env: `ANYAICAM_LIVE_HLS_MODE=always` for the first soak (AI on the
   substream, clips/thumbnails/live scale on, encoder still always on).
   Phase 2: unset it (auto) once phase 1 passes.
3. Check: `docker logs <vms> | grep -E "measured video|AI frames:|Live HLS \(auto\)|AI frame reader failed|not needed for"`,
   cloud `cameras.measured_*` populated, `/api/customer/cameras` shows
   `video_resolution`.

## 6. 2 h soak plan (per scenario; lab appliance first, then Samsung)

As in `perf-substream-hls-20261010.md` section 8 (idle, scripted events
every 5 min, WebRTC viewer, HLS viewer + 10 open/close cycles), plus:

* **AI parity:** same 2 h of scripted walk-throughs (and a vehicle pass if
  the camera sees a driveway) with `ANYAICAM_AI_SOURCE=hls` and then `auto`:
  person events within +-10 %, every scripted pass detected, AI event
  timestamps ~1 s after the real motion (not ~11 s), clip shows the person
  entering.
* **LPR/PPE/face (entitled camera):** plate reads per vehicle pass not lower
  than with `hls`; `main_frame_grabber` grabs only while vehicles/persons
  are present (count RTSP sessions / log lines); no grabs for a parked car
  after its attempts are used up.
* **CPU:** per-process cores (`pidstat -C ffmpeg 10`, python): live encoder
  0 while idle (auto), AI reader < 0.1 core, clip builds short bursts; load
  average < 6.
* **Profile:** `camera<N>.video.json` and cloud `measured_*` = 3840x2160 H265 /
  640x360 H265; change the camera's main resolution once during the soak and
  confirm re-measure within the probe period (or after a restart) and that
  clips/live follow without distortion.
* **Status:** camera never shown offline (local, cloud), no stream_offline /
  camera_offline alerts while the encoder is idle.
* **Logs:** no credentials (`://user:` patterns, the camera password).
* **Camera RTSP sessions:** motion reader + AI reader on the substream,
  recorder on main, WebRTC on demand, main grabs on demand -- confirm the
  camera's session limit is not reached.

## 7. Risks and open questions

**Risks**

* **Substream AI accuracy is not verified on the real substream** (4.1 is a
  proxy; no vehicles in the sample; no small/distant persons). Rollback:
  `ANYAICAM_AI_SOURCE=hls`.
* **More RTSP sessions per camera:** the AI reader is a second substream
  session next to the motion reader (+ recorder on main, WebRTC on demand,
  short main grabs). A camera with a low session limit may refuse one; the AI
  reader then falls back to HLS after 3 failures (auto). Merging motion and
  AI into one substream reader is a follow-up.
* **Main-stream grab cost/latency:** ~1.2 CPU-s and ~2 s per grab (4K HEVC,
  dev PC); the crop is taken 1-2 s after the analytics frame, so a fast
  vehicle can move out of the mapped box (15 % margin). Grabs only happen when
  LPR actually reads a vehicle (max 4 attempts per position) or PPE/face crop a
  person, cached 2.5 s, 60 s back-off on failure.
* **Live view is now 1080p for 4K cameras** (`ANYAICAM_LIVE_SCALE=profile`).
  At the 4 Mbps live cap this is sharper than 4K was, but it is a visible
  product change; `full` restores 4K.
* **Event clips are now 1080p** (4K before): smaller uploads/storage, but
  less detail for zooming into an event clip. Manual exports stay full
  resolution. Product decision to confirm.
* **auto live HLS needs the new agent:** without the marker the encoder
  stays always on (safe, no saving). An agent rolled back leaves the marker
  to go stale within 15 min, after which the VMS goes back to always on.
  First viewer of an idle camera waits for the encoder to start (live page
  already polls up to 45 s).
* **Capability record shared with WebRTC:** the probe worker and
  `webrtc_publisher.p2p_source_for` both save the record; a lost write is
  re-measured on the next 60 s loop (measurement) or re-verified on the next
  restart (P2P flags). P2P selection fields are never changed by the probe.
* **AI frame timestamps** are receipt times (~1 s after reality, measured);
  `detection_event_span` still counts the clip pre-roll from the previous
  scan, so clips start earlier than before, not later.
* The working tree of the clone uses CRLF (core.autocrlf=true); all committed
  blobs are LF as in the repository.

**Open questions (owner)**

1. Accept 1080p event clips and 1080p live HLS for 4K cameras by default?
2. Should manual (customer-requested) clips also be scaled (`ANYAICAM_MANUAL_CLIP_SCALE=profile`)?
3. Analytics stream minimum width is 640 (task spec); cameras whose only
   substream is 352x240/480x270 fall back to decoding the MAIN stream for AI
   (0.43 core per 4K camera measured). Lower the minimum to 480?
4. Cloud UI: `measured_*` is stored and returned by `/api/customer/cameras`
   and shown in the setup wizard; partner/admin camera pages still show the
   plan label. Extend?
5. Phase 2 of the soak (`ANYAICAM_LIVE_HLS_MODE` unset = auto) only after
   phase 1 passes, and only with the matching appliance-agent installed.
