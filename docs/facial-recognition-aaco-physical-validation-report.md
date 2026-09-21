# Facial Recognition / AACO physical validation (2026-09-22)

Real, physical end-to-end validation on Ryzen (Living Room camera, real
enrolled person, real relay rule). One real infrastructure defect was
found and fixed along the way (see below); the recognition result
itself is a documented engine limitation, not a bug.

## Setup performed

- `ANYAICAM_FACIAL_RECOGNITION_ENABLED`, `ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED`,
  `ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED` enabled on both Ryzen and
  staging (all three were off by default).
- `facial_recognition` entitlement granted to Living Room (`dfba6a63ec`)
  via the real `customer_analytics_panel.assign_entitlement()` path
  (required creating one `analytics_subscriptions` row first --
  licensing is enforced even for this feature).
- `door_access_enabled=1`, `door_relay_channel=1` set on Living Room.
- One real `facial_rules` row: `known_person` trigger, mock relay,
  `dry_run=0`, 30s cooldown.
- Alejandro Mata enrolled as a real person via the real customer-portal
  enrollment flow (not fabricated) -- 7 reference embeddings, quality
  1.0, synced from staging to Ryzen via `facial_embedding_sync.sync_facial_directory()`.

## Defect found and fixed: People Counting was starving the AI detection pipeline

While diagnosing why facial recognition wasn't firing, found that
`people_counting_worker()` called `detect_objects_frame()` -- the same
appliance-wide YOLO call `ai_person_detector()` wraps in
`ai_inference_semaphore` -- without acquiring that semaphore itself.
Measured live: over one ~7-minute Living Room walk-test,
`people_counting_worker()` ran 215 inference cycles while
`ai_person_detector()`'s own qualifying scans for that camera -- the
path facial recognition, PPE, and LPR all depend on -- dropped to 2.

Fixed (commit `494ebb0`) by wrapping that call site in the same
semaphore. Regression test added (`test_people_counting_and_ai_person_
detector_cannot_run_inference_concurrently`, confirmed to fail against
the pre-fix code and pass after). This did not touch People Counting's
geometry/rule, the tracker's matching tolerance, facial-recognition
thresholds, or `detect_objects_frame()`'s own per-call cost.

After the fix, a real 4-minute post-restart window showed the
correctness half working (no more concurrent access) but exposed a
second, related symptom: `people_counting_worker()` still requests the
shared semaphore far more often than any single camera's normal
detector, so appliance-wide `ai_person_detector()` throughput dropped
to zero across all 5 cameras during that window -- fair queuing, but
severe under-provisioning. Fixing that throughput/fairness problem
would be a broader shared-camera performance change, explicitly out of
scope tonight. Living Room's People Counting was temporarily disabled
(entitlement flag only, rule/geometry untouched) to unblock the facial
recognition test, then restored afterward -- see `docs/people-counting-
sampling-rate-gap-report.md` for the fuller writeup of this shared root
cause.

## Recognition result: pipeline correct, engine confidence insufficient at this face size

With Living Room's People Counting paused, `ai_person_detector()`
resumed firing normally and a real face-recognition attempt ran.
Matcher output, inspected directly against a live frame (bypassing scan
scheduling entirely, per explicit instruction to inspect before
changing any threshold):

```
face detected: yes (bbox 58x58px in a 1280x720 frame -- small/distant)
best candidate: person_398ce56f84dde26aa7dc (Alejandro Mata) -- correct
similarity: 0.1415
```

The matcher correctly selected the right enrolled person as the best
candidate out of the 7 loaded embeddings -- enrollment, cloud/edge sync,
and the matching math are all proven correct end-to-end. Similarity
(0.14, and 0.333 on an earlier real attempt) is far below the
recognition threshold (`FACIAL_MIN_CONFIDENCE`/`settings["min_confidence"]`,
0.6), so `classify_match()` correctly reported `unknown` both times --
never a false positive, never a false claim of identity.

This is `facial_recognition.py`'s own documented Phase 1 limitation, not
a defect: `HaarEmbeddingFaceEngine` is a classical CPU-only intensity-
vector baseline (explicitly "not claimed to match production deep-
learning face recognition accuracy" per that module's own docstring),
measurably sensitive to face size, pose, and distance. A small/distant
face in frame produces a real but weak similarity score. The threshold
was **not** lowered tonight, per explicit instruction.

## Full checklist result

| Item | Result |
|---|---|
| Face detection fires | Confirmed -- Haar cascade finds a real face region |
| Known vs. unknown handling | Confirmed correct -- below-threshold candidate reported as `unknown`, never a false "known" |
| Confidence/threshold behavior | Confirmed correct -- 0.14/0.33 similarity, 0.6 threshold, correctly rejected both times |
| Camera/timestamp association | Confirmed -- `camera_id=dfba6a63ec`, `event_timestamp` matches the real scan |
| Events/Investigate linkage | Inherited from this session's earlier `linked_recording_for()` fix (real media duration, not the old 300s assumption) -- not independently re-verified against a `known` match since none occurred |
| Thumbnail/metadata | `facial_events.face_thumbnail_path` populated with a real per-face crop JPG on the one recorded event |
| Duplicate suppression | `facial_recognition.DuplicateSuppressor` (30s debounce, keyed by camera+identity or camera+position-bucket for unknowns) reviewed by code inspection -- correct design, not exercised by a sustained real presence tonight |
| Mock facial-access rule execution | Confirmed -- rule evaluated, correctly did NOT activate (see below) |
| No real hardware relay action | Guaranteed structurally -- `relay_control.get_provider()` always returns `MockRelayProvider`; there is no hardware-backed provider anywhere in this codebase |
| Resource health | Normal throughout (7.2G memory available, load average consistent with the night's baseline) |

## AACO authorization result

`door_access_events` recorded the real outcome for the one match:
`authorization_result='unknown_person'`, `relay_result='skipped'`,
`success=0` -- the mock relay correctly did **not** fire, because an
unrecognized face is never authorized regardless of any relay rule
existing. This is exactly the required behavior: no automatic
unlock/activation happens unless the strict, real authorization path
(a `known`/`watchlist` match against an applicable rule) allows it.

## Conclusion

**Facial Recognition/AACO: pipeline validated end-to-end (detection,
enrollment sync, matching, event recording, thumbnail, authorization
gating, mock-only relay). The current Phase 1 Haar-based engine is not
reliable enough at small/distant face size for production recognition
confidence** -- a real customer would need to stand closer/more face-on
to this camera for a reliable "known" match, or a future engine swap
(`ANYAICAM_FACE_ENGINE=onnx|arcface`, already supported as a drop-in via
the existing `FaceEngine` interface) would be the real fix, not a
threshold change.

## Current appliance state

- Living Room: `facial_recognition` entitlement, door access, and the
  mock `known_person` relay rule all left in place, unchanged.
- Living Room: `people_counting_enabled` restored to its prior enabled
  value; the counting-line rule/geometry was never touched.
- All five cameras remain in Event mode.
- Env flags left on: `ANYAICAM_FACIAL_RECOGNITION_ENABLED`,
  `ANYAICAM_FACIAL_ACCESS_CONTROL_ENABLED`,
  `ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED` (Ryzen and staging).
