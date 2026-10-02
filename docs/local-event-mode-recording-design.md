# Local Event-mode recording: audit, design, and implementation (2026-09-20)

## Read-only audit: confirmed root cause

Three independent per-camera async loops run forever, unconditionally,
for every camera, started once at boot:

1. `process_supervisor(camera_number, "live")` → live HLS streaming.
2. `process_supervisor(camera_number, "recording")` → `start_recording()`,
   which runs `ffmpeg ... -f segment -segment_time 300 ...`
   **unconditionally, forever**. This is the exact, sole source of the
   5-minute files, and nothing gated it on activity before this pass.
3. `motion_detector(camera_number)` → an independent frame-diff loop
   that, on a debounced motion event, calls `build_motion_event_clip
   (event_id, camera, start, end)`, which uses `event_clips.
   compute_clip_window()` to extract a short clip (pre-roll + event +
   post-roll) **from whatever continuous segments loop #2 already
   wrote**. The same function is also reused by the AI/YOLO
   classification detection path (`save_yolo_events()`).

`recording_mode`-shaped settings already existed
(`cameras.cloud_recording_mode`) but only ever gated whether an
already-recorded continuous segment gets **uploaded to cloud** —
zero effect on whether it gets **written to local disk** in the first
place. There was no per-camera Local recording mode controlling local
disk behavior at all before this pass.

`event_clips.py` already implemented the pre-roll/post-roll/merge
logic requested here — `compute_clip_window()` (configurable, not a
hardcoded 15s) and `should_merge()` (already wired into the AI-
detection path) — as pure, dependency-free, already-tested functions.
This work extends that existing logic to gate local disk *writing*,
not just clip *extraction*.

## Design

- **New `cameras.local_recording_mode` column** (plus 4 configurable
  seconds fields: pre-roll, post-roll, merge-gap, max-event-length).
  NULL/`'continuous'` = today's exact unchanged behavior; explicit
  `'event'` opts in. Same no-hidden-default convention this codebase
  already uses for `cloud_recording_mode`/`people_counting_enabled`.
- **Continuous mode: completely untouched.** `start_recording()` has
  zero changes. A Continuous-mode camera's code path is identical to
  before this pass, all the way down to the exact same function being
  called.
- **Event mode: a short rolling pre-roll buffer, not a permanent
  continuous recording.** `start_event_recording_buffer()` writes
  `EVENT_BUFFER_SEGMENT_SECONDS` (30s) segments into
  `camera{N}/_event_buffer/` — a subfolder invisible to every existing
  downstream consumer by construction, since `_customer_recording_
  rows()`/`_catalog_local_recordings_for_camera()`/`recording_uploader.py`
  all glob `camera{N}/*.mkv` non-recursively.
- **`event_buffer_janitor()`** — one per camera, started
  unconditionally alongside `motion_detector()` (a cheap no-op for any
  Continuous-mode camera) — deletes buffer segments once they age past
  the configured pre-roll lookback and aren't needed by an in-flight
  event build. **This is the actual mechanism that stops idle-time
  disk accumulation**: an Event-mode camera with no activity for 5
  hours never has more than ~40 seconds of buffer footage on disk at
  any moment.
- **`persist_event_recording()`** — called only for Event-mode
  cameras, alongside (never instead of) the existing customer-facing
  clip build. Extracts the pre-roll+event+post-roll window from the
  short buffer and writes/extends a file in the camera's REAL
  recordings folder using the **exact same filename convention**
  `start_recording()` already uses — completely indistinguishable from
  a Continuous-mode recording to Playback, thumbnails,
  `_customer_recording_rows()`, `retention_worker()`, and Hybrid cloud
  upload. None of that code needed to change.
- **Merging and the safety cap**: `local_recording_policy.
  should_start_new_event_recording()` reuses `event_clips.
  should_merge()` unchanged for the actual merge-gap rule (adjacent/
  overlapping motion extends the same file) and adds the one thing
  that rule doesn't cover: a `max_event_seconds` safety cap so one
  long unbroken activity period still rolls over into a new file
  eventually. This is a ceiling for a pathological case (a camera
  pointed at a busy street), never the normal event-length rule —
  covered by its own dedicated test proving the cap does *not* fire
  for an ordinary 15-second event.
- **Live mode switching**: `process_supervisor()`'s reconnect loop
  re-reads `local_recording_mode` fresh (never cached) on every
  iteration for the "recording" slot, so an admin's mode change takes
  effect the next time that camera's recorder reconnects — no full
  appliance restart required.

## What was implemented and tested (this pass)

- `app/local_recording_policy.py` — pure, dependency-free decision
  logic (buffer retention, merge-vs-new-file with the safety cap).
  11 unit tests, zero ffmpeg/filesystem/DB dependency.
- `cameras.local_recording_mode` + 4 config columns (db_migrations.py).
- `POST /api/admin/cameras/{camera_id}/local-recording-mode`
  (appliance_cloud.py) — same shape/validation/audit-logging pattern as
  the existing `cloud-recording-mode` route. 19 tests.
- `GET /api/appliance/configuration` exposes the new fields; edge_
  camera_sync.py syncs them into the local appliance database. 2 new
  sync tests alongside the 12 existing ones (all still passing).
- `app/main.py`: `start_event_recording_buffer()`,
  `event_buffer_janitor()`, `persist_event_recording()`,
  `_local_recording_settings()`, and the `process_supervisor()`/
  motion-event wiring described above.

98 tests passing across every file touched or added this pass; the one
failure that appears in a combined run
(`test_motion_clip_shortlist_fix.py::test_existing_clip_output_and_
schema_preserved`) is a pre-existing Windows path-separator (`\` vs
`/`) string-comparison artifact in that test itself — confirmed via
`git stash` to fail **identically**, byte-for-byte, with this entire
change stashed out. Not caused by this work.

## What is explicitly NOT done in this pass

- **The AI/YOLO-classification detection path** (`save_yolo_events()`)
  is not yet wired to call `persist_event_recording()` — only the
  primary/basic motion-detection path is. Both paths already converge
  on the same `build_motion_event_clip()` for the customer-facing
  clip; wiring the second path into `persist_event_recording()` too is
  a small, clearly-scoped follow-up (the same few lines already added
  to the basic path), deliberately deferred rather than rushing a
  second edit into an already-large change in the same pass.
- **No deployment to, or validation on, the Ryzen appliance.** This is
  real physical hardware currently recording 5 real cameras for a real
  customer. Per this project's own standing convention (and this
  session's explicit stop condition for "physical hardware testing/
  action required"), that step needs its own explicit go-ahead and is
  not taken automatically just because the code+tests are done.
- No change to `event_clips.py`, `build_motion_event_clip()`, or the
  customer-facing clip/Investigate/thumbnail pipeline at all — those
  are reused completely unmodified, exactly as designed.
