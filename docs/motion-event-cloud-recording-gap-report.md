# Motion-event cloud recording audit

This is a source-only audit of `codex/motion-event-media-cloud-flow`.  No
appliance, shared staging environment, production service, S3 bucket, or AWS
resource was contacted.

## Existing flow

1. `store_motion_event()` creates a local motion event, thumbnail, and bounded
   MP4 clip task.  It mirrors the event into the local analytics-event feed and
   invokes `event_media_uploader.upload_motion_event_media()` in the background.
2. `event_media_uploader.py` obtains the existing short-lived, camera-scoped
   recording credential, uploads the MP4 and optional JPEG directly to S3, and
   synchronizes the event before registering its media metadata.
3. `appliance_cloud.py` stores the event in `detection_events` and its one-to-one
   media record in `detection_event_media`.  Customer, site, appliance, and
   camera identifiers are resolved from the authenticated appliance/camera, not
   from the media payload.
4. Customer event and playback APIs query those rows with customer and
   per-camera permission checks.  Media and thumbnail reads are short-lived
   presigned S3 URLs; thumbnail rendering, event readiness, bounded polling,
   and event-clip playback are already wired into the portal.
5. Recording credentials are `PutObject`-only and limited to one camera's
   `recordings/<customer>/<site>/<appliance>/<camera>/` prefix.

## Isolated fixes in this branch

* The uploader now carries `recording_mode` from the appliance configuration and
  accepts motion-event upload only when it is exactly `motion`.  Continuous,
  disabled, unknown, and unconfigured cameras never request credentials or
  upload event media.
* Local media URL handling now resolves and contains paths below
  `/app/recordings`; a `/recordings/../...` URL cannot cause an arbitrary local
  file to be uploaded.
* The cloud catalog accepts only the deterministic MP4/JPEG keys belonging to
  the event being registered.  It derives the key date from the stored event
  timestamp, so pre-roll across midnight remains valid while client-provided
  timestamps cannot relabel another object as event media.
* New local-only tests cover the media-row lifecycle, idempotency, tenant/camera
  binding, key rejection, pre-roll date handling, entitlement gating, and path
  traversal rejection.

## Remaining operational gaps

These are not safe to resolve from this branch alone and require a separately
authorized infrastructure rollout:

* The production bucket/IAM policy and the three independent feature settings
  (`ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED`, analytics sync, and recording upload)
  must be enabled consistently for a real event to complete.  The source
  defaults all outbound behavior off.
* S3 lifecycle/retention for `detection_event_media` objects is not expressed
  in application code.  The current camera upload credential intentionally has
  no delete permission, so retention must be implemented as an audited bucket
  lifecycle policy or a separately authorized privileged sweeper.  No event
  media is deleted by this branch.
* The uploader makes bounded in-process retries.  It does not yet have a
  durable event-media retry queue for an appliance outage after S3 upload but
  before catalog registration.  Deterministic object keys make a future
  idempotent retry safe, but adding a durable queue needs an explicit lifecycle
  and appliance-storage design.
* A real edge-to-S3 acceptance test, including browser playback and bucket
  lifecycle confirmation, remains required in an isolated environment before
  enabling the flags for customers.
