# Hybrid event-media deduplication audit and pricing-margin validation (2026-09-18)

Requested before finalizing Hybrid pricing: does AnyAiCam store duplicate
physical video when a single camera produces overlapping analytic events
(e.g. a Person event and a PPE event at the same moment), and does the
working $9.99/camera/month price have sufficient margin against a
9-hour/day (270-hour/month) unique-video allowance. This document is the
real, code-and-data-grounded answer, not an assumption.

## Phase 1 -- audit: does duplication exist today?

**Short answer: not the way the motivating example described, but a real,
narrower instance of it does exist elsewhere -- and a complementary,
opposite-shaped gap exists too.**

### The Person + PPE example specifically: no duplication, because PPE never gets video today

Read end-to-end (`app/event_media_uploader.py`, `app/main.py`'s
`save_yolo_events()`): a single YOLO scan can produce several analytic
event records (Person, PPE, LPR "plate", `facial_recognition`) from one
frame, but only ONE of them -- the scan's own `primary_class_name` (the
first qualifying detected class) -- ever gets a real clip build +
`upload_motion_event_media()` call. PPE, LPR, and `facial_recognition`
events are each written as their own independent analytics-history
record (correct, unchanged) but currently call neither
`upload_motion_event_media()` nor `register_shared_event_media()` --
they have **zero** `detection_event_media` rows, not a duplicate one.

Confirmed against real production data on staging (customer
`d75bdbecdd4887de4d2b89a9fcea9092`, the real five-camera fleet):

| event_type | total | with media | root | shared | no media |
|---|---:|---:|---:|---:|---:|
| ppe | 613 | 0 | 0 | 0 | 613 |
| person | 5359 | 2269 | 2269 | 0 | 3090 |
| smart_motion | 1006 | 305 | 6 | 299 | 701 |
| car | 20284 | 7746 | 7746 | 0 | 12538 |
| truck | 11512 | 1561 | 1561 | 0 | 9951 |

100% of real PPE events (613/613) have no media at all. There is no
second S3 object to deduplicate for this pairing -- there is no second
object, period.

This is a real, separate value gap (see "Complementary finding" below),
not the duplication risk originally asked about.

### Where real duplication *does* exist: raw Basic Motion vs. an independently-triggered AI-classified event

`store_motion_event()` (the raw pixel-diff Basic Motion path) and
`save_yolo_events()` (the YOLO AI-classification path) are two
independent, asynchronously-scheduled detection loops. When a real
person/vehicle triggers both within a few seconds of each other, **each
uploads its own separate clip** for what is very likely the same
physical moment -- confirmed live:

Querying the same real fleet's `detection_events`/`detection_event_media`
for root-media pairs on the same camera, of different event_type, with
different (non-shared) `s3_key`, within the existing 8-second merge-gap
(`event_clips.DEFAULT_MERGE_GAP_SECONDS`):

- **191 duplicate-clip pairs** found (`motion` vs. an AI-classified type,
  mostly `person`, gaps of 1.1-8.0s).
- Approximate duplicate bytes: **851.9 MB**, or **2.67%** of this
  sample's total root media bytes (31.96 GB across 12,855 real root
  clips, 2026-09-13 through 2026-09-18).

This is real, measured, low-but-nonzero waste. It is architecturally
distinct from the Person+PPE example: it's two *independent detectors*
each reacting to the same real-world moment, not two *analytic
categories derived from one detection pass*.

**Why this is not fixed in this pass, deliberately:** the existing
sharing primitive (`register_shared_event_media()` /
`appliance_cloud.py`'s `analytics_event_media_shared()`) is intentionally
hardcoded to one pairing only -- child `event_type='smart_motion'`,
parent `event_type='motion'` (`_resolve_parent_motion_event()`,
`app/appliance_cloud.py` line ~152). Extending it to also allow a
`motion` child to share from an AI-classified parent means widening a
multi-tenant media-access authorization boundary, plus building a new,
*tight*-window correlation (the existing `smart_motion.classify_motion()`
uses a 45-second correlation window -- correct for its own
"is this real activity" purpose, but far too loose to safely identify a
clip whose *video window* actually overlaps; sharing against a
45-second-old, non-overlapping clip would show the customer the wrong
footage for an event, a correctness bug, not an optimization). Both of
those are real, separable pieces of work deserving their own reviewed
pass rather than being rushed through inside this audit. A concrete spec
for that follow-up:

1. Add a tight (e.g. <=13-16s, matching the real pre/post-roll window)
   per-camera "most recent AI-classified event with a confirmed root
   clip" tracker, separate from `smart_motion.classify_motion()`'s
   existing loose 45s tracker.
2. When `store_motion_event()` finds a match, call
   `register_shared_event_media()` against it instead of independently
   building/uploading -- same call shape already proven for
   Smart Motion.
3. Widen `_resolve_parent_motion_event()`/`analytics_event_media_shared()`
   to accept an AI-classified parent type for a `motion` child, alongside
   the existing `motion` parent / `smart_motion` child pairing -- same
   ownership/tenant/camera checks, just a wider event_type allowlist.
4. Real tests proving: correct sharing when windows genuinely overlap,
   correct independent upload when they don't (no regression to
   existing coverage-per-event), and no cross-tenant/cross-camera leak.

### Complementary finding: PPE/LPR/facial_recognition could share the primary clip at zero additional storage cost

Because these event types already fire *within the same scan* as the
primary AI-classified event (same `event_group_id`, same physical
window, same `now` timestamp) -- unlike the motion-vs-AI case above,
there is no window-matching ambiguity here at all. Wiring PPE/LPR/
`facial_recognition` to call `register_shared_event_media(parent_local_
event_id=event_group_id)` whenever `event_clip_path is not None` for
that scan would be safe, correct, and free (zero additional S3
PutObject) -- and is closer to the user's own stated "Desired
architecture" than the motion-vs-AI fix is. It requires the *same*
cloud-side authorization widening described above (the child event_type
constraint at `appliance_cloud.py` line ~1097 rejects anything but
`smart_motion` today). Left undone for the same reason: a multi-tenant
authorization boundary change deserves its own reviewed pass. Two new
regression tests (`app/tests/test_ppe_lpr_facial_event_media_gap_audit.py`)
characterize the current, confirmed-correct-for-now behavior so a future
fix changes it on purpose.

## Phase 2 -- implementation

Not built this pass -- see the two specs above. Both are additive,
narrow widenings of an already-proven mechanism, not new architecture;
both are ready to implement in a focused follow-up once reviewed.

## Phase 3 -- cooldown/merge window for rapid repeated detections

**Already built, already working -- no new mechanism needed.**
`event_clips.should_merge()` (an 8-second `DEFAULT_MERGE_GAP_SECONDS`,
chosen deliberately per its own docstring to "bridge a person briefly
leaving frame and re-entering... without merging two genuinely unrelated
visits") is already wired into `save_yolo_events()` via
`ai_event_clip_windows` -- a lingering subject across consecutive YOLO
scan ticks does NOT get a new clip built/uploaded per tick; the window
merges into the prior one. This is proven live by the real data itself:
`smart_motion` shows 6 independent root uploads against 299 shared
registrations in the sampled window -- the merge/sharing machinery is
demonstrably active and effective, not theoretical.

`store_motion_event()`'s own raw Basic Motion path fires once per
motion *episode* (a `start_time`/`end_time` pair already spanning the
whole episode, supplied by the upstream motion detector), not once per
frame, so it does not need its own separate per-call merge gate for the
same reason a single `save_yolo_events()` call already doesn't re-fire
mid-episode.

## Phase 4 -- stress test and cost validation

### Real-fleet data already spans the requested load range

Rather than only synthetic data, the real five-camera fleet's own
current traffic already covers 200/500/1000 events/camera/day:

| camera | real events/camera/day (root clips) | real unique video hours/day |
|---|---:|---:|
| dc7a226120 (vehicle-facing) | 1062.6 | 2.996 |
| 5c689a0c0e (vehicle-facing) | 1209.9 | 3.433 |
| dfba6a63ec | 494.8 | 1.424 |
| 41dc80c85e | 251.1 | 0.724 |
| 55bdd715ea | 20.6 | 0.202 |

Real inputs used below: avg root clip **2.486 MB**, avg duration
**10.40s**, effective bitrate **~1.91 Mbps** (0.239 MB/s), measured
across 12,855 real root clips over a 4.23-day window. Thumbnail size is
not separately tracked in `detection_event_media` (only clip
`size_bytes` is) -- estimated at ~60 KB/event below, a minor addendum
next to the clip itself.

**Region**: `us-east-1` (confirmed from this codebase's own config
template). S3 pricing below is standard public `us-east-1` pricing at
the time of writing -- confirm against the current AWS pricing page or
an actual Cost Explorer invoice before finalizing, these are not fetched
live.

### Cost model, scaled from real measured clip size, at 200/500/1000 events/camera/day

30-day month, rolling 30-day retention (steady-state stored volume, not
cumulative-forever):

| events/camera/day | monthly events | storage (GB) | storage $ | PUT $ | GET $ (3 views/event) | transfer-out $ (1x view) | transfer-out $ (2x view) | **total $ (1x view)** | **total $ (2x view)** |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 200 | 6,000 | 14.92 | 0.34 | 0.06 | 0.01 | 1.34 | 2.68 | **1.75** | **3.09** |
| 500 | 15,000 | 37.29 | 0.86 | 0.15 | 0.02 | 3.36 | 6.71 | **4.39** | **7.74** |
| 1000 | 30,000 | 74.58 | 1.72 | 0.30 | 0.04 | 6.71 | 13.42 | **8.77** | **15.48** |

(Storage $0.023/GB, PUT $0.005/1000, GET $0.0004/1000, transfer-out
$0.09/GB -- standard tier-1 `us-east-1` rates, free tier ignored as not
realistic at fleet scale.)

**The dominant cost driver by far is data-transfer-out, and it is
directly proportional to how many times a customer actually views/
downloads each clip -- a number this audit does not have real telemetry
for.** The table above brackets it at 1x (watched once) and 2x
(watched, then re-watched once); real behavior could be higher or lower.
This is the single most important unknown for finalizing margin, and is
worth instrumenting (S3 access logs or a lightweight playback-view
counter) before the price is locked in.

### The 9-hour/day (270-hour/month) theoretical ceiling

At the real measured bitrate (0.239 MB/s):

- 270 hours/month = 972,000 seconds -> **232.3 GB/month** stored if a
  customer actually consumed the full advertised allowance every single
  day.
- Storage: 232.3 GB x $0.023 = **$5.34/month**
- Transfer-out (1x view): 232.3 GB x $0.09 = **$20.91/month**
- Transfer-out (2x view): **$41.81/month**
- **Total at the ceiling: ~$26.25-$47.06/camera/month**, dwarfing
  storage cost -- transfer, not storage, is what actually breaks the
  budget at the ceiling.

### Margin conclusion against $9.99/camera/month

- At the real, currently-observed busiest cameras in the live fleet
  (~1000-1210 events/day, ~3-3.4 hours/day actually used -- well under
  the 9-hour ceiling), estimated AWS cost is **$8.77-$15.48/camera/month**
  depending on the (unmeasured) view-count assumption -- **at or above
  $9.99 even at real, already-observed traffic**, not a hypothetical
  worst case.
- At the full advertised 9-hour/day ceiling, estimated AWS cost is
  **$26-$47/camera/month** -- **$9.99 has substantial negative margin**
  if a customer's real usage approaches what's actually being sold.
- The confirmed real duplication found in Phase 1 (~2.67% of root
  bytes) is a real but *minor* contributor to this gap next to the
  view-count sensitivity above -- fixing it (see Phase 2 spec) helps,
  but does not on its own close a multi-dollar gap between $9.99 and a
  $26-47 ceiling cost.

**Recommendation, not a decision made here:** either the advertised
9-hour/day ceiling needs to come down materially, the price needs to go
up, or real per-clip view/download counts need to be measured before
trusting the 1x-view column -- ideally more than one of these. This is
the real data; the pricing call itself belongs to the user.

## Files touched this pass

- `app/tests/test_ppe_lpr_facial_event_media_gap_audit.py` (new): two
  characterization tests proving the confirmed PPE-gets-no-media
  behavior described above, so a future fix changes it intentionally.
- This document.

No production code changed. No WireGuard/MediaMTX/installer files
touched (separate, parallel workstream).
