# Motion-event cloud recording implementation status

## Added locally

* `event_media_outbox.py` persists an atomic, deduplicated local retry record
  before the first cloud attempt. Retrying the deterministic object key is
  idempotent; successful registration removes the record.
* `event_media_policy.py` limits motion plans to 7, 14, or 30-day retention
  and six hours of event clips per camera per calendar day. The cloud-side
  catalog enforces the camera's `motion` mode and this policy.
* The existing delete-only retention sweep now includes event-media objects and
  deletes their catalog rows only after the object deletion succeeds.

## Required runtime consistency

All of these must be true for a controlled edge test; defaults remain off:

1. Edge: `ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED=true`,
   `ANYAICAM_ANALYTICS_SYNC_ENABLED=true`,
   `ANYAICAM_RECORDING_UPLOAD_ENABLED=true`, a valid cloud URL and appliance
   credential.
2. Cloud: analytics sync and recording upload enabled, recording bucket/region
   configured, and an STS upload role restricted to the individual camera
   prefix.
3. Data: active plan with `recording_mode=motion` and retention 7/14/30,
   camera `cloud_recording_mode=motion`, and a synced detection event.
4. Retention: separate delete-only lifecycle role plus
   `ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=true` only after isolated
   delete verification. A bucket lifecycle rule remains a backstop, never the
   per-customer policy engine.

## Still required before real edge-to-S3 validation

* Verify the now-wired retry worker on a disposable appliance: it starts only
  when the event-media flag is enabled, scans at the configured interval, and
  resumes the persistent outbox after a restart.
* Create isolated bucket, upload/read/delete roles, and a lifecycle backstop;
  prove each role has only its intended S3 action.
* Use one disposable appliance identity/camera and a non-customer test plan to
  validate clip upload, thumbnail, catalog row, customer-scoped playback,
  retry after a forced control-plane failure, quota rejection, and retention.
* Confirm alerting, metering, monitoring, and customer-facing plan terms before
  enabling a paid plan. No billing or Stripe mapping is changed here.
