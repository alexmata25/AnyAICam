# People Counting: frame-acquisition sampling-rate gap (2026-09-22)

Found during real, physical People Counting validation on Ryzen (Living
Room camera, real counting line, real walk-through). This is a
confirmed, reproducible defect, root-caused with direct timing
evidence -- not a walk-test miss and not a bug in the counting math
itself.

## Symptom

A real person walked across a configured counting line and back. Zero
crossing events were recorded (`analytics_events.json` had zero
`people_counting_*` entries). `PEOPLE_COUNTING_DEBUG_CAMERA` diagnostics
(enabled by default for this camera) showed the same real person being
split into **four separate, disconnected tracks** across the walk --
`track=1`, `track=2`, `track=3`, `track=4`, each logged with
`prev_side=None reason=new_track_no_prior_side` -- meaning no track ever
held a confirmed side long enough for a side-change (a counted crossing)
to register.

## Root cause: real sample-to-sample latency is 5-16s, not the configured 1.5s

`people_counting_worker()` (`app/main.py`) reuses `detect_objects_frame()`
-- the exact same YOLO call `ai_person_detector()` makes -- at a
configured `PEOPLE_COUNTING_INTERVAL_SECONDS` (default 1.5s, chosen
explicitly, per that constant's own comment, because "the ordinary 5s
AI_DETECTION_INTERVAL_SECONDS is too coarse for a person to be reliably
tracked across a line at normal walking speed").

`detect_objects_frame()` opens a **brand-new `cv2.VideoCapture()` against
the camera's live HLS `.m3u8` manifest on every single call**, reads one
frame, then releases the capture:

```python
capture = cv2.VideoCapture(str(manifest))
try:
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    ok, frame = capture.read()
finally:
    capture.release()
```

Timed directly on Ryzen, in isolation, with the YOLO model already
warm-cached from a prior call:

```
elapsed (1st call, includes one-time model load): 8.654s
elapsed (2nd call, model already cached):          3.550s
```

3.55s **per call, in isolation, with no other contention** -- already
more than double the configured 1.5s interval. `await
asyncio.sleep(PEOPLE_COUNTING_INTERVAL_SECONDS)` only runs *after* this
call completes, so the real cycle period is `detect_objects_frame()`'s
own cost plus 1.5s, not 1.5s alone. Live, with four other cameras' own
`ai_person_detector()` loops, PPE/LPR/facial sub-analysis, and the
existing recording/encode pipeline all competing for the same 8 CPU
cores, the observed real inter-cycle gaps (measured from `docker logs
-t` timestamps during the walk-test) were **5-16 seconds**, not 1.5s.

Lowering `PEOPLE_COUNTING_INTERVAL_SECONDS` further (down to its
enforced 0.5s minimum) would not help: the sleep is not the bottleneck,
the multi-second `detect_objects_frame()` call itself is.

## Why this breaks counting specifically (not detection generally)

`people_counting.PeopleCounter`'s matching gate
(`max_match_distance=0.20` normalized, or a real box-IoU overlap) is
correct and already proven by 26+ deterministic tests in
`tests/test_people_counting.py` -- for samples arriving close enough
together in time that a walking person's frame-to-frame displacement
stays within that gate. At 5-16s between samples, a person walking at
normal indoor pace can cross the ENTIRE frame between two consecutive
samples, which is by design **not** treated as the same track (correctly
rejecting an association that could just as easily be a different
person) -- so a new, side-less track starts instead, and the real
crossing is never observed as one continuous, confirmed side-change.

This is a sampling-rate/frame-acquisition problem, not a tracking-logic
problem. Widening `max_match_distance` to paper over it was considered
and explicitly rejected: it would let the tracker bridge genuinely large
gaps, which directly weakens the already-tested "two people crossing
close together are both counted" guarantee (a wide-enough gate to
tolerate a multi-second sampling gap is also wide enough to misassociate
two different nearby people).

## What's confirmed working despite this gap

- Cloud entitlement -> local worker activation: live, confirmed
  (`entitlement_changed False -> True` observed in real logs the moment
  the config poll landed).
- Rule loading: the real configured line
  (`x1=0.10 y1=0.50 x2=0.90 y2=0.50 direction=both`, rule id
  `740fd0cfed2f`) loaded correctly, matching what was saved.
- No startup exceptions.
- Thumbnail: a real, confirmed-missing gap (crossing events previously
  had `thumbnail=None` unconditionally, unlike every other analytics
  event type) was found and fixed independently of this report -- see
  commit `fc8ec5a`. Verified via 2 new regression tests
  (`tests/test_people_counting_worker_thumbnail.py`); not yet verified
  against a real crossing since no real crossing has been recorded yet.
- `linked_recording_for()` reuse means People Counting events already
  benefit from this session's real-media-duration fix (no false 5-minute
  assumption) -- inherited, not separately re-verified.

## What's NOT yet validated (blocked on this gap)

One count each direction, no double-count while lingering, real
timestamp/camera association on an actual counted crossing,
Events/Investigate linkage on a real crossing, and count persistence
across a restart -- none of these could be exercised because no real
crossing has yet been counted.

## Recommended fix (not applied tonight, by explicit instruction)

Replace `detect_objects_frame()`'s per-call `cv2.VideoCapture(manifest)`
open with a persistent/faster live-frame source (e.g. a long-lived
capture handle reused across calls, or reading the most recent already-
decoded frame from wherever the live HLS encode/preview pipeline already
holds one, avoiding a fresh HLS demux+decode per sample). This function
is shared by every AI-detection-driven feature validated this session
(People Counting, PPE, LPR, general person/vehicle/facial detection via
`ai_person_detector()`/`save_yolo_events()`), so this is real,
cross-cutting infrastructure work, not a `people_counting.py`-local
change -- it needs its own dedicated pass with regression testing across
all four of those consumers, not a rushed change appended to this
report.

## Current appliance state

Camera 1 (Living Room) remains entitled (`people_counting_enabled=1`)
with the real counting line rule (`740fd0cfed2f`) left in place and
`PEOPLE_COUNTING_ENABLED=true` left on, per explicit instruction --
**People Counting is not being treated as production-ready** pending the
frame-acquisition fix above. No other camera's configuration, and no
shared frame-acquisition code, was touched.
