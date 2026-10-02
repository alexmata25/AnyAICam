# Ryzen recording/playback stuck: root cause and fix (2026-09-21/22)

Investigated live on Ryzen (read-only until the fix below; no product
mode change, no deploy, no intrusion/line-crossing test, no physical
LPR/People Counting test performed). Reported symptoms: current events
arriving with fresh timestamps, live view working, Playback stuck
showing recordings from around Wednesday, recent events often "not
ready"/no playable video, and a suspicion of thumbnail/camera-label
mismatches.

## End-to-end trace, checkpoint by checkpoint

1. **Detection -> local Event-mode persistence -> clip file on Ryzen:
   real, working, confirmed live.** `docker exec anyaicam-vms ls -la
   /app/recordings/camera{1,3}` showed real `.mkv` files with mtimes
   within minutes of "now" for every active camera (camera1 through
   camera23:46, camera3 through 00:10 the same night), and the
   container's own logs showed live `event_recording.triggered`/HLS
   segment activity throughout. **Fresh local video files are being
   written today.** This rules out the detector, ffmpeg, and local
   Event-mode persistence entirely.
2. **Recording catalog row: broken.** The local `recordings` SQL table
   (in `/app/recordings/partner_portal.db`) had not received a single
   new row for ANY camera in over 22 hours (last `created_at` values
   clustered between `2026-09-20T23:52` and `2026-09-21T01:52`),
   despite hundreds of new local files existing past that point. This
   table is the one and only break point.
3. **linked_recording_for(): not reached in a way that matters here.**
   This function scans the local filesystem directly (not the
   `recordings` table), so it can still find a matching file -- but the
   customer-facing Playback/clip-serving API depends on the
   `recordings` table being caught up, which it was not.
4. **Event media/thumbnail:** not implicated -- thumbnails are written
   independently by each detector at the moment of the event, not
   through this table.
5. **Playback URL/API:** reproduced live. Minted a real customer_owner
   session for this appliance's real customer/camera identity and
   called the exact route the Playback page's own JS calls (`GET
   /api/customer/recordings/{camera_id}?date=...&day_start_utc=...&
   day_end_utc=...`). It returned `500 Internal Server Error`.
   `docker logs` showed the underlying exception precisely:
   `sqlite3.OperationalError: database is locked` inside
   `_catalog_local_recordings_for_camera()` (`main.py`).
6. **Customer portal playback:** this is the user-visible failure --
   the browser's fetch to the real Playback API fails, so the page
   never receives fresh data and keeps showing whatever it last
   successfully loaded, which is exactly "stuck showing recordings from
   around Wednesday." The same underlying function is also what a
   fresh event's clip-availability check depends on, explaining "recent
   events often show not ready/no playable video."

## Root cause

`_catalog_local_recordings_for_camera()` backfills the `recordings`
table from local `.mkv` files on every Playback/date/near query. It ran
its entire multi-file backfill scan as **one transaction** --
`database_backend.connect()` only commits once, at the very end of its
`with connection()` block -- held open across a real `ffprobe`
subprocess call for every newly-discovered file (needed for accurate
clip duration).

Ryzen runs several other independent writers against this same SQLite
file every few seconds: motion/AI detection, People Counting, event
recording, analytics sync, HLS segmenting. Once this camera's catalog
fell behind by any real backlog -- confirmed live: over 22 hours, ~900
files per camera -- each attempt to catch up held that single write
transaction open for the entire backlog scan, long enough (SQLite's
`busy_timeout` is 5 seconds; a real multi-hundred-file scan with one
`ffprobe` call per new file routinely runs far longer) that a
collision with one of the other writers became likely, not unlikely.
Because the whole scan was still one uncommitted transaction, that one
collision rolled back **every file already found and inserted earlier
in that same call** -- so the backlog could only ever grow, never
shrink, and the customer's real browser request 500'd instead of the
page silently going stale. This is a genuine, severe, self-reinforcing
failure mode: the bigger the backlog got, the less likely any single
attempt was to ever finish.

## Fix

Each newly-discovered file is now committed the instant it's inserted
(`db.commit()` right after that file's own `INSERT`, not only once at
the very end), with a small bounded retry (3 attempts, short backoff)
on `sqlite3.OperationalError`. A file that still can't be inserted
after retries is logged and skipped for this pass, not raised -- so:

- A lock collision on file N no longer erases files 1..N-1's
  already-durable progress from the same call.
- A single stubbornly-contested file can no longer turn an entire
  customer-facing request into a 500.
- The backlog now shrinks monotonically across repeated calls (each
  Playback page load/reload) instead of being replayed from zero every
  time, which is what actually breaks the "stuck" spiral.

See `app/main.py`'s `_catalog_local_recordings_for_camera()` for the
implementation and its own docstring for the full mechanism.

## Camera-ID/name mismatch check

Investigated directly: Ryzen's local `cameras` table does have several
orphaned placeholder rows (`camera_number IS NULL`, names like "Camera
6"/"Camera 7"/"Camera 8") alongside the 5 real, active cameras -- this
matches the already-documented 12-directories-vs-5-active-cameras
finding from this session's own earlier camera-count verification, not
a new defect. Every real query path that resolves a camera by
`camera_number` (which is how a local event/recording is actually
addressed) filters on that column, and only the 5 real camera rows
have it set -- an orphaned row can never be matched by camera_number,
so it cannot silently substitute for a real camera in this path. No
independent camera-ID/name mismatch bug was found or reproduced; the
"not ready"/mismatched-looking behavior the user saw is fully explained
by the catalog defect above, not a separate identity bug.

## What this does NOT fix or claim

- Not deployed. This is a source-code fix on this branch; Ryzen is
  still running its previously-deployed code and will keep exhibiting
  this exact defect until a real release including this fix is built
  and installed.
- The 22+ hour backlog this investigation found on Ryzen still needs to
  drain once the fix is deployed -- with per-file commits it will now
  make real, permanent progress on every Playback request instead of
  restarting from zero, but a backlog that large will still take
  multiple requests/reloads to fully catch up, not one.
- Local/Hybrid product mode was not touched. No intrusion/line-crossing
  execution was enabled or tested. No physical LPR/People Counting test
  was performed.

## Regression coverage

`app/tests/test_recording_catalog_lock_resilience.py` (3 tests, all
verified to fail against the pre-fix code and pass against the fix):

- a transient lock on one file does not roll back earlier files already
  catalogued in the same call;
- a persistently-locked file is skipped without raising or blocking
  other files in the same call;
- a persistently-locked file is retried and caught up on the very next
  call once contention clears, rather than being lost forever.
