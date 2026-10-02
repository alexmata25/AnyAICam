# Living Room Analytics Zone Walk Test: physical validation procedure (Ryzen)

Prepared 2026-09-27 on the Dell. Use this later on the Ryzen; nothing here has been run against a camera.
A first walk test on 2026-09-26 already **PASSED** (§6). This procedure is the repeatable version, and it uses the new suppression evidence.

## 1. What the software guarantees (verified by tests, no camera)

- **Suppressed inside, normal outside.** A detection whose **centre** falls inside an enabled exclusion zone is dropped before any analytic sees it:
  - person, vehicle and Smart Motion events and their clips;
  - PPE, facial recognition (it runs on the filtered person crops), LPR, People Counting, and Intrusion/Line Crossing.

  Pixel **Motion** inside the zone is masked as well. Everything outside the zone is unaffected. There is exactly one YOLO call site (`detect_objects_frame`), and no other detection path bypasses the filter. PPE and facial recognition only ever see crops of detections that passed it.
- **Resolution independent.** Zones are stored as fractions of the image. The same zone gives the same decision at 640×360, 1280×720, 1920×1080, 2688×1520, 3840×2160 and 640×480.
- **Reload and restart.**
  - A zone that is drawn, edited or deleted takes effect within **10 seconds**, with no restart (cache window `CACHE_SECONDS`).
  - The edge sync mirrors cloud rules about every 60 seconds.
  - Zones live in the database, so they survive a VMS restart.
- **Bad configuration fails open.** Invalid or empty geometry, fewer than 3 points, non-numeric points, a database error or a missing frame all mean *nothing is excluded*: detection keeps working. A bad zone never disables a good zone on the same camera.
- **Isolation.** Two cameras with different zones never share them, and another customer's camera with the same number is never affected.
- **New evidence, since 2026-09-27.** `GET /api/ai/status` now returns an `exclusion_zones` block for each camera:
  - `zones_active` and the zone points;
  - `suppressed_total` and `suppressed_by_class`;
  - `last_suppressed_at`;
  - up to 5 `last_suppressed` entries, each with class, confidence and normalized centre.

  The VMS log also gets at most one line per camera per minute: `detection_exclusion.suppressed camera=<n> count=<k> total=<t> classes=person`.

  Before this, a suppressed person left no trace, and proving suppression meant re-running YOLO on footage.

**Requirement for §4:** the new evidence needs a VMS build **after f5a6d87**, containing commit "Zone walk evidence…". The Ryzen currently runs f5a6d87, which enforces zones correctly but doesn't report suppression evidence. With f5a6d87 you can still use the event-based method (§5).

## 2. Preconditions

1. The Ryzen VMS is healthy, and the Living Room camera (camera 1) is streaming.
2. Draw the test zone again. The 2026-09-26 zone was deleted after that test.
   - Go to `https://portal-staging.anyaicam.com/customer/cameras/dfba6a63ec/analytics-rules`.
   - Add an **Exclusion** zone over the floor in front of the TV, from the hallway doorway down to the bottom edge. The 2026-09-26 geometry was x 0.339–0.619, y 0.099–0.994.
   - Wait at least 70 seconds for the edge sync and the cache.
3. On the Ryzen, confirm the zone is present (read-only):
   ```
   docker exec -i -w /app anyaicam-vms python3 -c "import detection_exclusion as d; print(d.status(1))"
   ```
   Expect `zones_active: 1`.
4. **Keep the chair beside the sofa empty.** The zone's right edge crosses it, so a seated person drifts in and out.
5. Note a baseline:
   - `suppressed_total` from `/api/ai/status`. Log in first; it's an authenticated route.
   - The current newest Living Room event time.

## 3. The walks (about 30 seconds each; the operator notes start and end times)

| Walk | Where | Expected |
|---|---|---|
| A: inside | From the hallway doorway, walk down the floor one step out from the TV stand, pause 5 seconds, walk back. Stay inside the zone. | **No** person, Smart Motion, PPE or motion event for the walker. `suppressed_total` goes up. |
| B: outside | On the stairs (left of the zone): from the 4th step down to the bottom step, pause 5 seconds facing the camera, walk back up. | A **person event** within about 10 seconds, with thumbnail and clip. Centre x below 0.339. |
| C: after removal | Delete the zone, wait at least 70 seconds, then repeat walk A. | A person event now appears for the walker in front of the TV. |

## 4. Verification (new build, preferred)

- **After walk A:** in `/api/ai/status`, check `cameras[camera==1].exclusion_zones`:
  - `suppressed_total` has increased;
  - `suppressed_by_class.person ≥ 1`;
  - `last_suppressed_at` falls inside walk A's window;
  - the `last_suppressed[].centre` values are inside the zone (x 0.339–0.619).

  The log also shows `detection_exclusion.suppressed camera=1`. No new Living Room event should have its centre inside the zone.
- **After walk B:** there is a new `person` event, and its detection centre `(x + w/2)/W` is below 0.339.
- **After walk C:** there is a person event with its centre inside the former zone, and `zones_active` is 0.

**PASS** when A is suppressed with evidence, B is detected, and C is detected after removal.

## 5. Verification on f5a6d87 (no evidence counters)

This is the method used on 2026-09-26:
1. Check the event list for the walk window. There should be no event for the walker; people on the sofa will keep producing events outside the zone.
2. Save the rolling buffer (`/app/recordings/camera1/_event_buffer`) during walk A. Run unfiltered YOLO on the saved frames to show the walker *was* detectable (for example 0.92 confidence) while no event was created.

## 6. 2026-09-26 result (for reference)

- Inside: suppressed. Unfiltered YOLO on the footage found the walker at 0.92 confidence, and no event was created.
- Outside: detected. Event `bcbd6374149e`, person 0.87, centre x 0.31, with thumbnail and a 7.9-second clip.
- After removal: detected. Event `651077eaf641`, walker in the former zone.
- Notes:
  - The camera's on-screen clock is about 1.5 minutes behind the Ryzen.
  - Event `7cd7208760a8` had no linked recording segment.

## 7. Tests backing this

- `app/tests/test_zone_walk_readiness.py` (new, 21 cases).
- `app/tests/test_detection_exclusion.py` (existing, 13 cases).

All 34 pass.
