# Disposable Motion Cloud E2E harness

Run only from a temporary directory with synthetic MP4 input, a temporary
SQLite database, localhost cloud process, and a new validation S3 bucket.

## Runtime flags

```text
ANYAICAM_RUNTIME_ROLE=edge
ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED=true
ANYAICAM_ANALYTICS_SYNC_ENABLED=true
ANYAICAM_RECORDING_UPLOAD_ENABLED=true
ANYAICAM_EVENT_MEDIA_OUTBOX_FILE=<temporary>/outbox.json
ANYAICAM_STATE_DIR=<temporary>/state
ANYAICAM_CLOUD_URL=http://127.0.0.1:<temporary-port>
ANYAICAM_EVENT_MEDIA_RETRY_SECONDS=30
ANYAICAM_EVENT_MEDIA_RETRY_MAX_SECONDS=3600
```

The local cloud process uses a separate SQLite database seeded only with:
`validation-customer`, `validation-site`, `validation-appliance`,
`validation-camera`, one `motion` plan at each 7/14/30-day value, and an
authorized synthetic customer identity. The camera must have
`cloud_recording_mode=motion`.

## Synthetic fixture and assertions

Generate a 30-second H.264/AAC MP4 from an FFmpeg test source, segment it as a
completed local recording, and use an in-memory synthetic motion frame.

1. Call `store_motion_event`; assert local JPEG and bounded MP4 exist.
2. Assert the outbox contains exactly one event before cloud completion.
3. Configure temporary STS upload credentials and assert one MP4 and one JPEG
   object under the synthetic camera prefix.
4. Force one S3 `PutObject` failure with a temporary deny on the deterministic
   event key; remove the deny; assert the edge retry worker completes it.
5. Force one localhost media-registration 503 after upload; assert retry
   registers the same deterministic key and produces one media row.
6. Restart the edge process; assert the persisted outbox resumes without a
   duplicate object or media row.
7. Verify the synthetic customer receives a presigned URL only for its own
   camera/event; cross-camera/customer lookup returns 403.
8. Seed 21,600 seconds for that camera/day and assert the next media request is
   rejected. Repeat retention tests with event timestamps older than 7, 14,
   and 30 days using only the temporary delete role.

## AWS resources and teardown

Create one uniquely named bucket plus temporary upload/read/delete roles. Each
role has exactly one S3 action on one synthetic camera prefix; no bucket list
permission. After tests, delete all objects, bucket, inline policies, and
roles, then verify `HeadBucket` is 404 and every `GetRole` is `NoSuchEntity`.

## Stop conditions

Stop before creation if the validation profile is unavailable, the generated
resource name collides, a policy addresses a non-validation prefix, FFmpeg is
not available in the disposable runtime, or any log contains a credential.
