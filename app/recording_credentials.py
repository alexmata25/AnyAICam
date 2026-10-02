"""R1 (recording-pipeline roadmap, distinct from the earlier Live Phase
1-8 numbering): appliance-facing recording-upload credential issuance.

Mirrors live_relay_s3_prefix()/live_relay_session_policy()/
live_relay_session_name() in appliance_protocol.py exactly, for a
SEPARATE `recordings/` S3 prefix, a SEPARATE feature flag, and a
SEPARATE IAM role from live relay -- so recording upload can be
disabled independently of live relay and vice versa, matching the R1
architecture's own "Pilot rollout & feature flag" decision (a second,
independent flag, not a reuse of the live one).

Deliberately R1 scope only. This module issues short-lived, prefix-
scoped S3 write credentials and nothing else:
  - No recordings catalog table or notification endpoint (R2).
  - No appliance-side uploader that actually calls this endpoint (R3).
  - No customer-facing retrieval, presigned reads, or Playback wiring
    of any kind (R4) -- this module has no read path at all.
  - No retention/lifecycle logic (R5).
  - No analytics association (R6/R7).

The route that uses these helpers (registered in appliance_cloud.py,
alongside the existing live-relay session route it mirrors) is gated
behind ANYAICAM_RECORDING_UPLOAD_ENABLED, defaulting off -- the same
precedent Phase 2 established for live relay ("behind a feature flag,
no media bytes touch this code path even in testing"). No media ever
touches this code path; only STS credentials are issued.

The AWS IAM role/bucket this credential-issuance call assumes are real
and already applied (confirmed live 2026-09-13: role
anyaicam-recording-upload-role, bucket anyaicam-recordings-prod-20260820)
-- docs/r1-recording-iam.md's own "designed, not yet applied" framing
predates that and is stale; do not treat it as current.

2026-09-13: event_media_uploader.py's motion-event thumbnail/clip
upload reuses this exact credential-issuance route and role rather
than getting its own -- the underlying mechanism (issue a short-lived,
prefix-scoped STS session) is identical in shape, and stands up a
second real AWS role for what's still a low-volume, small-object
feature was judged unnecessary complexity. What must NOT be reused is
the *scope*: recording_session_policy() grants a whole camera's
recording prefix, appropriate for bulk/continuous upload but far wider
than event-media ever needs. event_media_session_policy() below grants
only the .../events/* sub-prefix event-media objects actually live
under. The route in appliance_cloud.py picks between the two based on
*which* flag actually authorized the request (RECORDING_UPLOAD_ENABLED
vs ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED) -- when bulk recording is the
authorizer, behavior is completely unchanged from before this addition.
"""

import re
import time

RECORDING_SESSION_DURATION_SECONDS = 900  # matches the live-relay precedent (Phase 1 decision 4)
_SESSION_NAME_SAFE = re.compile(r'[^\w+=,.@-]')


def recording_s3_prefix(customer_id: str, site_id: str, appliance_id: str, camera_id: str) -> str:
    """The tenant-safe key prefix a recording-upload credential is scoped
    to -- same shape as live_relay_s3_prefix(), under a separate
    `recordings/` root so recording and live objects never share a
    lifecycle policy or a credential's write scope."""
    return f'recordings/{customer_id}/{site_id}/{appliance_id}/{camera_id}/'


def recording_session_policy(bucket: str, customer_id: str, site_id: str, appliance_id: str, camera_id: str) -> dict:
    """IAM session policy narrowing an AssumeRole call to exactly one
    camera's own recording prefix -- s3:PutObject only, no ListBucket,
    no GetObject, no DeleteObject. Identical restriction shape to
    live_relay_session_policy()."""
    prefix = recording_s3_prefix(customer_id, site_id, appliance_id, camera_id)
    return {
        'Version': '2012-10-17',
        'Statement': [
            {'Effect': 'Allow', 'Action': 's3:PutObject', 'Resource': f'arn:aws:s3:::{bucket}/{prefix}*'}
        ],
    }


def event_media_session_policy(bucket: str, customer_id: str, site_id: str, appliance_id: str, camera_id: str) -> dict:
    """2026-09-13: event-media (motion-event thumbnail/clip) upload
    reuses this same credential-issuance mechanism and the same
    underlying IAM role as bulk/continuous recording upload, but must
    never be granted that feature's full per-camera write scope --
    event-media objects only ever live at
    {recording_s3_prefix}{date}/events/motion_{event_id}.{mp4,jpg}
    (see appliance_cloud.py's analytics_event_media_available(), which
    already validates every uploaded key against this exact deterministic
    shape). This policy is scoped one level narrower than
    recording_session_policy() -- .../*/events/* rather than .../*  --
    so an event-media credential can never write to what would become a
    bulk-recording object under the same camera prefix, even though both
    currently share one role. s3:PutObject only, no ListBucket, no
    GetObject, no DeleteObject -- same restriction shape as
    recording_session_policy()."""
    prefix = recording_s3_prefix(customer_id, site_id, appliance_id, camera_id)
    return {
        'Version': '2012-10-17',
        'Statement': [
            {'Effect': 'Allow', 'Action': 's3:PutObject', 'Resource': f'arn:aws:s3:::{bucket}/{prefix}*/events/*'}
        ],
    }


def recording_session_name(appliance_id: str, camera_id: str, now: int | None = None) -> str:
    """CloudTrail-readable, sanitized to AWS's RoleSessionName character/
    length rules -- prefixed 'rec-' so recording sessions are
    distinguishable from live-relay sessions in CloudTrail without
    needing a separate role to tell them apart."""
    raw = f'rec-{appliance_id}-{camera_id}-{now or int(time.time())}'
    return _SESSION_NAME_SAFE.sub('-', raw)[:64]
