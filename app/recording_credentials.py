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

The actual AWS IAM role this credential-issuance call assumes does not
exist yet -- see docs/r1-recording-iam.md for the role design and the
illustrative (not executed) AWS CLI commands to create it. Creating
real IAM resources requires elevated AWS credentials this application
deliberately does not have (its own EC2 instance role is scoped to
assume only specific, already-approved role ARNs -- by design, the
same restriction that already applies to live relay).
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


def recording_session_name(appliance_id: str, camera_id: str, now: int | None = None) -> str:
    """CloudTrail-readable, sanitized to AWS's RoleSessionName character/
    length rules -- prefixed 'rec-' so recording sessions are
    distinguishable from live-relay sessions in CloudTrail without
    needing a separate role to tell them apart."""
    raw = f'rec-{appliance_id}-{camera_id}-{now or int(time.time())}'
    return _SESSION_NAME_SAFE.sub('-', raw)[:64]
