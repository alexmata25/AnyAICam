# AnyAiCam LPR Phase 1

## Scope

`app/lpr.py` is a pure CPU-only edge event-contract module. It has no camera,
network, cloud, GPU, OpenCV, YOLO, or OCR-library dependency. A future
appliance integration supplies frames, optional vehicle candidates, a plate
localizer, and an OCR adapter.

Pipeline: frame → optional vehicle candidate → plate localizer → OCR adapter
→ normalization → confidence threshold → camera-local dedupe → LPR event.

The module never unlocks a door, calls a relay, queues an RDM command, or
performs any access-control action.

## Entitlements

An event is emitted only when that exact camera has an active
`camera_analytics_entitlements` row with `analytic_key="lpr"`. Capacity remains
the responsibility of the existing `analytics_subscriptions.licensed_quantity`
and `assign_entitlement()` path. A site purchase is not a site-wide enablement.

## Event contract

`LprEvent` contains camera ID, deterministic local event ID, timestamp, raw
OCR plate text, normalized plate text, confidence, OCR engine/version, optional
opaque snapshot/crop references, and optional candidate metadata. Its
`analytics_payload()` is compatible with the existing authenticated
`POST /api/appliance/analytics/{camera_id}/events` contract:

* `event_type` is `lpr`.
* `camera_id` is intentionally only in the authenticated URL path; cloud code
  resolves customer/site/appliance/camera ownership server-side.
* Plate details remain under `detections`, which is stored as `detections_json`.
* The deterministic ID supports safe retry/idempotency with the existing
  `(camera_id, local_event_id)` uniqueness rule.

## Normalization and confidence

Normalization uppercases, trims, removes separators/punctuation, retains
alphanumeric characters, and rejects empty or out-of-range output. It never
substitutes ambiguous characters such as `O` and `0`.

Confidence must be a finite numeric value in `[0, 1]`. Events below the
configured `min_confidence` are rejected. Defaults: plate length 3–12,
confidence 0.60, debounce 30 seconds.

## Dedupe and media safety

The debounce key is `(camera_id, normalized_plate)`. A matching plate seen by a
different camera is never suppressed; the same camera is eligible again when
the debounce window expires.

Snapshot and plate-crop values are opaque single-segment keys only. URLs,
slashes, backslashes, traversal sequences, and arbitrary filesystem paths are
rejected. Phase 1 does not publish media URLs or upload files.

## Phase 2 validation

Use a validated CPU plate-localization model plus a CPU OCR adapter behind the
existing protocols. Test only with explicit appliance authorization. Benchmark
sample rate, vehicle prefilter accuracy, OCR latency, false positives, event
dedupe, and snapshot/media mapping before claiming supported camera capacity.

Recommended candidates for evaluation are a lightweight OpenCV-compatible plate
detector and PaddleOCR CPU, but neither is bundled or required in Phase 1.

## Phase 2 controlled-validation record

The first compatible, **validation-only** CPU stack on the authorized Ryzen
test appliance is pinned as follows:

* `paddlepaddle==3.2.0`
* `paddleocr==3.3.3`
* detector: `PP-OCRv5_mobile_det`
* recognizer: `latin_PP-OCRv5_mobile_rec`

This is not an application dependency, installer dependency, or production
runtime requirement.  The combination was installed only in a disposable
`/tmp` environment, where a synthetic `ABC1234` image completed CPU inference
without the earlier oneDNN/PIR error.  It must remain behind `OcrAdapter` and a
future optional edge runner; `app/lpr.py` must not import PaddleOCR, OpenCV,
or a camera client.

`lpr_validation.py` defines the stdout contract for a future explicitly
authorized edge-validation runner.  It emits a single JSON line after all
work is complete, rather than relying on intermixed OCR-library progress
output.  That line includes candidate count, crop dimensions, raw and
normalized OCR text, confidence, and detector/OCR/total latency.  It excludes
RTSP URLs, credentials, images, filesystem paths, cloud-upload data, and any
relay/action command.

The current state is paused awaiting natural driveway traffic.  A parked-car
sample is only a static sanity check, never an accuracy, capacity, duplicate,
or moving-vehicle benchmark.  Cloud upload, notifications, and relay/access
actions remain off.
