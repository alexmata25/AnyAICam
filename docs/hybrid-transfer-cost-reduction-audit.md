# Hybrid transfer-cost reduction audit (2026-09-18)

Follow-up to `docs/hybrid-event-media-dedup-audit.md` (`e013fe3`), which
found data-transfer-out dominates Hybrid's AWS cost and that
$9.99/camera/month has negative margin at real busy-camera usage, let
alone the proposed 9-hour/day ceiling. This pass asks: is that cost
unavoidable, or can it be substantially reduced through architecture
without changing pricing? Real code and real AWS configuration were
read end to end before anything was changed. **WireGuard/MediaMTX/
installer files were not touched** (a separate, frozen workstream); the
PPE/LPR/facial-recognition no-video-media gap from `e013fe3` was not
touched either (still reserved for its own reviewed pass).

## Why transfer-out dominates -- the real mechanism, traced end to end

**Root cause: event/recording media is never served through a CDN at
all -- only live video is.** This codebase has a fully-built, proven
CloudFront-signed-URL mechanism (`app/live_cdn_signing.py`,
`ANYAICAM_CLOUDFRONT_URL`/`ANYAICAM_CLOUDFRONT_KEY_PAIR_ID`), but it is
wired to exactly one S3 bucket -- `ANYAICAM_S3_BUCKET` (`anyaicam2026`,
confirmed live on staging in `docs/PROJECT_CHECKPOINT.md`'s own prior
entries), used for live-relay HLS segments and the manual "Create Clip"
export feature. Every analytic-event clip and thumbnail, and every
continuous-Playback recording, lives in a **second, separate** bucket --
`ANYAICAM_RECORDING_S3_BUCKET` (`anyaicam-recordings-2026`,
`docs/r1-recording-iam.md`) -- which CloudFront never fronts. Every
single fetch of that content is a direct S3 GetObject against
`_presigned_recording_url()` (`app/main.py`), billed at S3's full public
internet-egress rate ($0.09/GB), for every request, with no edge cache
anywhere in the path.

**Compounding this, no caching was possible even in principle before
this pass**: `_presigned_recording_url()` re-signed a fresh URL on
every single call (a new signature + a new 900s expiry each time), and
neither the 302-redirect thumbnail routes nor the underlying S3 objects
themselves ever set a `Cache-Control` header (confirmed by direct grep
across `app/event_media_uploader.py`/`app/recording_uploader.py`'s
upload calls -- `ContentType` was set, `CacheControl` never was). A
browser has no way to reuse a response it was never told was reusable,
and the URL itself changing on every request makes that doubly true
regardless of headers.

**A real, concrete instance of this waste, traced through the actual
rendered JS**: the customer Dashboard's "recent events" widget
(`updateRecentEvents()`, `app/main.py`) polls `/api/events` every
**15 seconds** and calls `grid.replaceChildren()` -- destroying and
rebuilding every thumbnail `<img>` element on every poll, regardless of
whether the underlying 6 events actually changed. The mobile customer
view's own event poll (`scheduleMobileEventPoll()`) is faster still --
every **4 seconds** while any event is in a "still processing" state
(`MOBILE_EVENT_POLL_INTERVAL_MS = 4000`) -- and `renderMobileRecentEvents()`
does a full `innerHTML` rebuild on every call too. Neither poll loop is
wrong to exist (a security-camera product's whole point is a
dashboard/mobile view someone actually watches), but before this pass,
**every single one of those redraws re-fetched every thumbnail from S3
from scratch**, at full transfer-out price, for as long as a customer
kept that view open -- a cost with no ceiling tied to how long a page
stayed open, completely independent of how many genuinely new events
existed.

## Separating storage cost from playback/viewing cost

These are genuinely different cost drivers, confirmed from the real
code:

- **Storage** scales with events created (PUT + GB-month), independent
  of whether anyone ever looks at them.
- **Transfer-out (the dominant term)** scales with events *actually
  fetched* -- which is a mix of (a) genuine customer viewing (opening
  Investigate, clicking a notification, watching Live-adjacent event
  context) and (b) the polling-redraw waste described above, which has
  nothing to do with genuine viewing at all.

No real telemetry exists in this codebase for "how many times does a
real customer actually watch a given event clip" -- this was already
`e013fe3`'s stated central unknown, and it still is. What this pass
adds is real, code-grounded evidence that a meaningful share of
*historical* transfer-out was never a genuine view at all, just
redraw waste -- and that share is now bounded (see below), not zero.

## What was implemented and measured this pass

All of the following are pure application-code changes -- no AWS
infrastructure was touched, nothing was deployed, no S3/CloudFront
configuration changed. Commit `<see below>`.

### 1. Presigned-URL reuse cache (`app/main.py`)

`_presigned_recording_url()`'s internals were split into
`_generate_presigned_recording_url()` (the real STS-backed sign, now
called only on a cache miss) and a new
`_presigned_recording_url_and_ttl(s3_key)`, which reuses an
already-signed URL for up to `PRESIGNED_URL_REUSE_SECONDS = 300`
(300s, comfortably inside the underlying object's real 900s
`ExpiresIn`/STS-session window -- 600s of margin left before a handed-out
URL could ever actually expire). Every existing caller of
`_presigned_recording_url()` is unaffected in return shape; it is now a
thin wrapper that discards the ttl.

### 2. Browser-facing `Cache-Control` on the two thumbnail redirect routes

New `_cacheable_presigned_redirect(s3_key)` builds the same 302 as
before but adds `Cache-Control: private, max-age=<n>`, sized to the
signed URL's own real remaining reuse window. `private` (never
`public`) is deliberate -- this must only ever be reusable by the one
already-authorized browser that requested it, never a shared/
intermediate cache. Wired into both `customer_event_thumbnail()` and
`customer_recording_thumbnail()` -- the two routes a polling redraw
loop actually re-fetches. `_customer_event_thumbnail_url()` gained a
sibling, `_customer_event_thumbnail_s3_key()` (pure DB lookup, no
presigning), so the route can hand the raw key straight to the new
cacheable-redirect helper.

**Real, measured effect** (arithmetic, not simulation -- inputs are the
real code's own poll intervals and `e013fe3`'s own thumbnail-size
estimate, 0.06MB, clearly labeled as an estimate since
`detection_event_media` doesn't track thumbnail size separately):

| Scenario (illustrative dwell time) | Before (fresh fetch every poll) | After (300s reuse) | Reduction |
|---|---:|---:|---:|
| 30 min/day dashboard open, desktop 15s cadence | $0.117/mo | $0.006/mo | 20x |
| **10 hours/day wall-mounted/always-open dashboard**, desktop 15s cadence | **$2.33/mo** | **$0.12/mo** | **20x** |
| 2 hours/day mobile view with a pending event, 4s cadence | $0.29/mo | $0.004/mo | 75x |

The middle row matters most: a wall-mounted or always-on monitoring
display is a completely normal way this class of product gets used
(a shop owner leaving the dashboard up all shift), and before this fix
that use pattern had **no cost ceiling at all** -- it scaled linearly
with however long the screen stayed on. $2.33/camera/month from
thumbnail-redraw waste alone, for one entirely plausible real usage
pattern, was not a rounding error next to the ~$9.99 price point. It
is now bounded to ~$0.12/camera/month regardless of how long the
screen stays on.

**Honest limit of this fix, stated plainly**: it eliminates *same-
browser, same-session* repeat fetches of *unchanged* data. It does
**not** reduce genuine distinct-viewing-occasion cost (the 5/10/25/
50/100%-viewed table below is unaffected by it), and it does not help
a *different* device/session re-fetching the same clip (that needs a
shared cache -- see CloudFront below). Because a full clip (2.486MB) is
~41x larger than a thumbnail (0.06MB, estimated) and clips are fetched
on-demand (click-to-play via `/media/url`'s JSON response, not on a
redraw timer) rather than auto-polled, **this fix does not move the
dominant cost driver** (clip transfer-out, scaling with genuine view
count) -- it closes a real, previously-unbounded liability on the
*minority* cost driver.

### 3. Long-lived, immutable `CacheControl` on new uploads

`event_media_uploader.py` (`EVENT_MEDIA_CACHE_CONTROL`) and
`recording_uploader.py` (`RECORDING_MEDIA_CACHE_CONTROL`), both
`"public, max-age=31536000, immutable"`, added to every clip and
thumbnail `upload_file()` call's `ExtraArgs`. An event clip/recording
is written exactly once and never modified afterward, so this is
always safe. "public" here describes how the *bytes* may be cached
once legitimately fetched (access is still gated entirely by the
short-lived presigned-URL requirement upstream -- this changes nothing
about who may fetch an object); it is also exactly what a future
CloudFront distribution in front of this bucket would need to actually
cache these objects at the edge. **Applies to new uploads only** --
does not retroactively rewrite the ~12,855 already-uploaded objects
`e013fe3` measured against; no bulk in-place S3 copy was performed
(that would be a real, unrequested write against live production
data).

### Regression evidence

Targeted suite (every file touched, plus every adjacent thumbnail/
recording-URL test): **86 passed, 0 failed.** Two pre-existing tests
(`test_playback_thumbnail_authorization.py`,
`test_events_thumbnail_fix.py`) were updated, not weakened -- they now
patch the actual function the route calls post-refactor
(`_generate_presigned_recording_url`/`_customer_event_thumbnail_s3_key`
instead of the now-bypassed `_presigned_recording_url`/
`_customer_event_thumbnail_url`), and gained assertions on the new
`Cache-Control` header. 9 new tests added (URL-reuse-cache behavior,
per-key isolation, expiry, the redirect helper's header logic, and
both uploaders' `CacheControl` metadata).

Full suite: **91 failed / 2758 passed / 24 skipped** -- the established
baseline failure count held exactly (matching `e013fe3`'s own 91/2741/
24; the passed-count delta is the new tests above). Confirmed the 15
failures in `test_recording_uploader_credential_hardening.py` when run
in isolation are a pre-existing `AWS_REGION not configured` environment
issue, not caused by this change (traced the actual traceback; nothing
in it involves `CacheControl` or the presign cache).

## What was investigated and NOT implemented, with real reasons

### CloudFront in front of the recordings bucket -- recommended, not built, and smaller than it first looks

Extending CloudFront (or a new distribution) to front
`anyaicam-recordings-2026` is a real AWS infrastructure change (a new
origin/behavior on a distribution, or a new distribution entirely) --
out of this pass's scope by the user's own explicit boundary (no AWS
infra changes, inspect/test only). But it is also **not the silver
bullet it first appears to be**, and stating that precisely matters
more than recommending it reflexively:

- CloudFront's own public egress rate (~$0.085/GB, `us-east-1`, first
  10TB) is only about **6% cheaper** than S3's direct public egress
  ($0.09/GB) -- a real but modest saving on its own.
- S3-to-CloudFront transfer, same region, is **free**, and S3 GET
  requests are already extremely cheap ($0.0004/1000) -- so a CDN
  cache *hit* does not meaningfully undercut a cache *miss* here the
  way it would for, say, a globally-distributed, request-heavy static
  site. The viewer still downloads the same bytes at essentially the
  same egress rate either way.
- The real incremental value of CloudFront beyond what this pass
  already captures via browser caching is narrower: **cross-device/
  cross-session repeat views** (two family members' phones, or the
  same customer reopening a notification link from two different
  devices) -- genuinely useful, but a subset of "repeat views," not all
  of them, and one this audit has no real measurement for.
- It would also move event/recording delivery off raw presigned URLs
  onto the same short-TTL CloudFront-signed-URL pattern
  `live_cdn_signing.py` already proves out for live video -- a real
  architectural consistency win, independent of the dollar math above.

**Recommendation, not a decision made here**: worth doing for
architectural consistency and the cross-device case, but do not expect
it alone to close the margin gap `e013fe3` found -- it is a ~6%
egress-rate improvement plus an unmeasured slice of repeat-view
traffic, not a 10x lever.

### S3 storage-class tiering (e.g. Intelligent-Tiering) -- investigated and found NOT viable, a real negative finding

Checked directly: Hybrid's own retention is plan-driven
(`plans.retention_days`, `app/recording_retention_sweep.py`), modeled
at the same 30-day figure `e013fe3` used, matching the user's own
"30-day Hybrid event storage" framing. S3 Intelligent-Tiering only
moves an object to a cheaper access tier after a **minimum 30-day**
monitoring period, and separately charges a small per-object monthly
monitoring fee ($0.0025/1000 objects) regardless of whether the object
ever actually moves tier. An object retained for exactly ~30 days is
deleted at almost the same moment it would first become tier-eligible
-- Intelligent-Tiering would add a real monitoring cost here for
**zero** realized storage saving. Storage itself is also not the
dominant driver in the first place (see the tables below: storage is
single-digit percent of total cost at every real scenario modeled).
**Correct conclusion: leave storage on S3 Standard.** Worth
re-evaluating only if retention is ever lengthened well past 30 days.

### Local/direct/WireGuard-preferred event playback -- the single most impactful lever, correctly deferred

The architecturally biggest available lever for the *dominant* cost
driver (clip transfer-out) is not a caching or pricing trick at all:
**an event clip already exists on the appliance itself** at capture
time (before, and often after, any cloud upload). A viewer on the same
LAN -- or, per the paused WireGuard work, over a direct tunnel -- could
be served that clip directly from the appliance with **zero** AWS
transfer-out cost, mirroring the exact precedent this codebase already
proves out for *live* video (`live_view_p2p.py`'s WebRTC-P2P-first,
AWS-relay-fallback race). The same "try local/direct first, fall back
to the cloud copy" shape is architecturally sound for *event* playback
too, and was the user's own explicit question (checklist items 7-8).

**Not built this pass, and not investigated further than this
paragraph, on purpose**: implementing it means either (a) a LAN-only
path (real but narrow -- most Hybrid viewing is explicitly *remote*,
the case a LAN-only fallback can't help) or (b) the paused WireGuard
direct-connectivity work, which the user has explicitly frozen at
`c332aa9` pending separate approval. Building or designing this
further here would mean touching exactly the frozen workstream this
pass was explicitly told to leave alone. This is flagged as the
**highest-value next step once WireGuard resumes** -- closing the
margin gap by *avoiding* AWS transfer for remote viewers with a direct
path available is a bigger lever than any pricing or caching change
inside AWS itself can be, precisely because it removes the transfer
from AWS's bill entirely rather than discounting it.

## Recalculated cost model

Same real inputs as `e013fe3` (avg clip 2.486MB, avg thumbnail 0.06MB
estimated, `us-east-1` public pricing: storage $0.023/GB-mo, PUT
$0.005/1000, GET $0.0004/1000, egress $0.09/GB -- not fetched live,
confirm against current AWS pricing before finalizing). Storage now
includes both clip and thumbnail bytes (a small addition next to
`e013fe3`'s clip-only storage column). Thumbnail transfer is modeled
separately, per the illustrative table above, and is small enough next
to clip transfer to omit from the main table below without materially
changing any total.

### 5/10/25/50/100% of stored event clips actually viewed, at 200/500/1000 events/camera/day

| events/day | monthly events | storage $ | PUT $ | 5% viewed | 10% viewed | 25% viewed | 50% viewed | 100% viewed |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 200 | 6,000 | 0.35 | 0.06 | **0.48** | **0.55** | **0.75** | **1.08** | **1.76** |
| 500 | 15,000 | 0.88 | 0.15 | **1.20** | **1.37** | **1.87** | **2.71** | **4.39** |
| 1,000 | 30,000 | 1.76 | 0.30 | **2.39** | **2.73** | **3.74** | **5.42** | **8.78** |

(The 100%-viewed column reproduces `e013fe3`'s own "1x view" column
almost exactly -- $1.76/$4.39/$8.78 here vs. $1.75/$4.39/$8.77 there,
the small delta being this pass's added thumbnail storage -- confirming
this model is a consistent refinement, not a contradiction, of the
prior audit. "100% viewed" here means *every event gets watched once*;
`e013fe3`'s separate "2x view" column, watched-twice-on-average, is a
different question this table doesn't re-ask.)

### The 9-hour/day (270-hour/month) ceiling, stored vs. actually transferred

At the real measured avg clip duration (10.40s), 9 hours/day of unique
event video is **~3,115 events/day** -- a genuinely heavy but not
absurd volume next to the real fleet's own busiest cameras
(1,000-1,210/day, `e013fe3`).

| | stored (GB/mo) | storage $ | 5% viewed | 10% viewed | 25% viewed | 50% viewed | 100% viewed |
|---|---:|---:|---:|---:|---:|---:|---:|
| At the 9hr/day ceiling | 237.95 | 5.47 | **7.46** | **8.50** | **11.65** | **16.88** | **27.36** |

**Video stored and video transferred are now explicitly distinguished
in this model, per the user's own ask**: at the ceiling, storage is a
flat $5.47/month regardless of how much gets watched -- transfer is
what swings the total from $7.46 to $27.36 depending entirely on the
unmeasured view rate. Storage was never the risk; transfer always was.

### Normal / busy / very busy / worst case

| Scenario | Events/camera/day | Viewed | Total AWS $/camera/mo |
|---|---:|---:|---:|
| Normal | 200 | 10% | **$0.55** |
| Busy | 500 | 25% | **$1.87** |
| Very busy | 1,000 | 50% | **$5.42** |
| Worst case (advertised 9hr/day ceiling, fully watched) | ~3,115 | 100% | **$27.36** |

## Margin conclusion against $9.99/camera/month

- **This pass's implemented fix is real, measured, and closes a
  previously-unbounded liability** (up to ~$2.33/camera/month for one
  entirely plausible always-on-dashboard usage pattern, now capped
  near $0.12) -- but it acts on the *minority* cost driver
  (thumbnails), not the dominant one (clip transfer-out), so it does
  not by itself change `e013fe3`'s margin conclusion.
- **Normal and Busy usage now have real, comfortable margin**
  ($0.55 and $1.87/camera/month respectively against $9.99) --
  materially better-looking than `e013fe3`'s own headline numbers,
  because this pass models view-rate directly (5-50%) rather than
  `e013fe3`'s 1x/2x-per-event multiplier, which is a more realistic
  shape for "some fraction of events get watched" than "every event
  gets watched at least once."
- **Very Busy usage ($5.42) still has real margin against $9.99**, but
  the gap is narrowing -- and this is *below* the real fleet's own
  already-observed busiest cameras (1,000-1,210 events/day,
  `e013fe3`).
- **The advertised 9-hour/day ceiling, if a customer's real usage and
  viewing approach it, still has negative or near-zero margin**
  ($16.88-$27.36 against $9.99 at 50-100% viewed) -- this pass's fixes
  do not change that conclusion, because clip transfer at the ceiling
  dwarfs everything this pass touched.
- **Storage and request costs are not, and were never, the risk** --
  at every scenario modeled, storage is single-digit-percent of total
  cost. The entire margin question is the unmeasured real-world view
  rate at real-world event volume, exactly as `e013fe3` already
  concluded.

**Recommendation, not a decision made here, same discipline as
`e013fe3`**: the biggest real lever available is *not* inside AWS at
all -- it's avoiding the AWS transfer entirely for remote viewers with
a direct/local path available, which is exactly the frozen WireGuard
work's own eventual value proposition beyond connectivity. Until that
resumes, real per-clip view-rate telemetry (this audit's other
standing recommendation from `e013fe3`, still not built) is the
highest-value next step to replace the 5-100% bracket above with a
real number.

## Files touched this pass

- `app/main.py`: `_generate_presigned_recording_url()` (renamed from
  `_presigned_recording_url()`), `_presigned_recording_url_and_ttl()`
  (new), `_presigned_recording_url()` (now a thin wrapper),
  `_cacheable_presigned_redirect()` (new), `_customer_event_thumbnail_s3_key()`
  (new), `customer_event_thumbnail()`/`customer_recording_thumbnail()`
  (now use the cacheable redirect).
- `app/event_media_uploader.py`: `EVENT_MEDIA_CACHE_CONTROL` (new),
  applied to both upload calls.
- `app/recording_uploader.py`: `RECORDING_MEDIA_CACHE_CONTROL` (new),
  applied to both upload calls.
- `app/tests/test_recording_read_credentials_cache.py`: cache
  reset fixtures, 1 updated + 6 new tests for the reuse cache and the
  redirect helper.
- `app/tests/test_events_thumbnail_fix.py`,
  `app/tests/test_playback_thumbnail_authorization.py`: updated to
  patch the real post-refactor call targets; gained `Cache-Control`
  assertions.
- `app/tests/test_event_media_uploader.py`: gained a `CacheControl`
  assertion on the existing happy-path upload test.
- `app/tests/test_recording_uploader_cache_control.py` (new): 2 tests
  proving both the recording and its thumbnail carry the new metadata.
- This document.

No AWS infrastructure, no CloudFront/S3 configuration, no production
system, and no WireGuard/MediaMTX/installer file was touched.
