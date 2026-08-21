"""Phase 6b (docs/AI_HANDOFF.md Sec 8): CloudFront signed-URL helper for
live-relay segment objects.

sign_segment_url() deliberately does not accept a caller-supplied URL or
resource string -- it builds both the object URL and the signing policy's
Resource internally from validated path components (customer_id, site_id,
appliance_id, camera_id, segment_filename), reusing the same prefix
convention appliance_protocol.live_relay_s3_prefix() already applies for
Phase 1/2's IAM/session-policy scoping. This makes it structurally
impossible for a caller to sign an arbitrary CloudFront URL or arbitrary S3
key through this module -- a function-signature guarantee, not a
caller-discipline convention.

get_configured_signer() assumes the narrowly-scoped, Secrets-Manager-only
signing-key-reader role via STS (least-privilege, per the already-approved
Phase 1 two-role pattern this project already uses for the live-upload
credential in appliance_cloud.py), retrieves the CloudFront private key PEM
from Secrets Manager in the configured secret region, and wraps it into an
rsa_signer callback via cryptography_rsa_signer(). The constructed signer is
cached at module level (thread-safe, lazy, process-lifetime) so that
live_playlist.py's per-poll calls to this function do not pay a fresh
STS + Secrets Manager round trip on every ~2-second playlist poll. Every
failure -- missing configuration, STS failure, Secrets Manager failure,
empty/malformed key, or boto3/cryptography being unavailable -- is caught
and reported as None, never raised and never permanently cached, so every
real call path fails closed by construction and a transient AWS failure
self-heals on the next call instead of requiring a process restart.

CloudFrontSigner requires RSA-PKCS1v15 padding with SHA-1 hashing -- this is
CloudFront's own fixed signing spec (confirmed from
botocore.signers.CloudFrontSigner's own docstring), not a choice made here.
"""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from re import compile as re_compile
from typing import Callable
from urllib.parse import quote

from appliance_protocol import live_relay_s3_prefix

try:
    from botocore.signers import CloudFrontSigner
except ImportError:
    CloudFrontSigner = None

try:
    import boto3
except ImportError:
    boto3 = None

logger = logging.getLogger("anyaicam.live_cdn_signing")

SIGNED_SEGMENT_URL_TTL_SECONDS = 20  # fixed; not caller-configurable

# get_configured_signer()'s AWS configuration. Deliberately separate from
# live_playlist.py's ANYAICAM_CLOUDFRONT_URL/ANYAICAM_CLOUDFRONT_KEY_PAIR_ID
# -- this module only ever produces the rsa_signer callback, never the
# CloudFront domain or key-pair-id used to build/label the signed URL.
SIGNING_KEY_ROLE_ARN_ENV = "ANYAICAM_CLOUDFRONT_SIGNING_KEY_ROLE_ARN"
SIGNING_KEY_SECRET_NAME_ENV = "ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_NAME"
SIGNING_KEY_SECRET_REGION_ENV = "ANYAICAM_CLOUDFRONT_SIGNING_KEY_SECRET_REGION"

_SIGNING_KEY_READER_SESSION_NAME = "anyaicam-cloudfront-signing-key-reader"
_SIGNING_KEY_READER_SESSION_DURATION_SECONDS = 900

# Mirrors the exact shape already used for per-camera-number local playlist
# validation in live_relay_uploader.py (`^camera{N}[0-9_.\-]*\.ts$`),
# generalized to camera\d+ since at this layer the camera binding comes
# from the S3 key prefix (customer/site/appliance/camera_id), not from the
# filename matching one specific camera_number.
_SEGMENT_FILENAME_PATTERN = re_compile(r"^camera\d+[0-9_.\-]*\.ts$")

# Lazily populated by get_configured_signer(); guarded by _signer_cache_lock.
# A failed fetch leaves this None so the next call retries -- never
# permanently cached. Tests reset this directly (see
# test_live_cdn_signing.py's GetConfiguredSignerCacheTests.setUp/tearDown).
_cached_signer: Callable[[bytes], bytes] | None = None
_signer_cache_lock = threading.Lock()


def _require_nonempty_str(value, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string.")
    return value


def _validate_segment_filename(segment_filename) -> str:
    _require_nonempty_str(segment_filename, "segment_filename")
    if "/" in segment_filename or "\\" in segment_filename:
        raise ValueError("segment_filename must not contain path separators.")
    if not _SEGMENT_FILENAME_PATTERN.fullmatch(segment_filename):
        raise ValueError(
            "segment_filename does not match the canonical live-segment filename pattern."
        )
    return segment_filename


def sign_segment_url(
    *,
    cloudfront_base_url: str,
    customer_id: str,
    site_id: str,
    appliance_id: str,
    camera_id: str,
    segment_filename: str,
    key_id: str,
    rsa_signer: Callable[[bytes], bytes],
    now: datetime | None = None,
) -> str:
    """Builds and signs the CloudFront URL for exactly one live-relay
    segment object.

    Every path component is validated (non-empty string) and individually
    percent-encoded via urllib.parse.quote(..., safe="") before being joined
    into the object URL -- encoding each component independently, rather
    than encoding an already-joined key string, means a component value
    that happened to contain "/" or another reserved character can never be
    misread as introducing an extra path segment or escaping the intended
    prefix.

    segment_filename is validated against _SEGMENT_FILENAME_PATTERN before
    anything is built or signed; invalid shapes raise ValueError
    immediately, before any signing is attempted.

    A drift guard reconstructs the raw (unencoded) key independently and
    compares it against appliance_protocol.live_relay_s3_prefix()'s own
    output for the same inputs -- if that function's layout ever changes
    without this module being updated to match, this raises loudly instead
    of silently producing a wrongly-scoped URL.

    Uses a CUSTOM CloudFront policy (CloudFrontSigner.build_policy()), not a
    canned one, so the resulting policy's Resource field is exactly the one
    object URL built above -- auditable directly in the policy JSON.

    Expiry is always exactly `(now or the current UTC time) +
    SIGNED_SEGMENT_URL_TTL_SECONDS` -- there is no expires_at parameter, so
    no caller can request a longer-lived signature than the fixed 20-second
    window. `now` exists only so tests can supply a deterministic clock.

    Raises:
        ValueError: any component is empty/wrong-type, or segment_filename
            doesn't match the canonical pattern.
        RuntimeError: botocore's CloudFrontSigner is unavailable in this
            environment, or the drift guard above trips.

    Never returns an unsigned URL; never falls back to an unscoped resource.
    """
    if CloudFrontSigner is None:
        raise RuntimeError("botocore.signers.CloudFrontSigner is not available.")

    base_url = _require_nonempty_str(cloudfront_base_url, "cloudfront_base_url").rstrip("/")
    customer_id = _require_nonempty_str(customer_id, "customer_id")
    site_id = _require_nonempty_str(site_id, "site_id")
    appliance_id = _require_nonempty_str(appliance_id, "appliance_id")
    camera_id = _require_nonempty_str(camera_id, "camera_id")
    segment_filename = _validate_segment_filename(segment_filename)
    key_id = _require_nonempty_str(key_id, "key_id")
    if not callable(rsa_signer):
        raise ValueError("rsa_signer must be a callable.")

    raw_prefix = live_relay_s3_prefix(customer_id, site_id, appliance_id, camera_id)
    raw_key = f"{raw_prefix}{segment_filename}"
    if raw_key != f"live/{customer_id}/{site_id}/{appliance_id}/{camera_id}/{segment_filename}":
        raise RuntimeError("live_relay_s3_prefix() layout has diverged from this module's assumptions.")

    encoded_key = "/".join(
        quote(part, safe="")
        for part in ("live", customer_id, site_id, appliance_id, camera_id, segment_filename)
    )
    object_url = f"{base_url}/{encoded_key}"

    current = now or datetime.now(timezone.utc)
    expires_at = current + timedelta(seconds=SIGNED_SEGMENT_URL_TTL_SECONDS)

    signer = CloudFrontSigner(key_id, rsa_signer)
    policy = signer.build_policy(object_url, expires_at)
    return signer.generate_presigned_url(object_url, policy=policy)


def cryptography_rsa_signer(private_key) -> Callable[[bytes], bytes]:
    """Wraps an in-memory `cryptography` RSA private key object into the
    (bytes) -> bytes callback botocore.signers.CloudFrontSigner expects.
    Where `private_key` comes from is entirely the caller's concern -- this
    function performs no I/O, reads no file, and touches no secret store.
    The `cryptography` import is deferred to inside this function so that
    importing this module, or using sign_segment_url() with some other
    signer callback, never requires `cryptography` to be installed.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def _sign(message: bytes) -> bytes:
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    return _sign


def _fetch_signer_from_aws() -> Callable[[bytes], bytes] | None:
    """Assumes the signing-key-reader role via STS, retrieves the
    CloudFront private key PEM from Secrets Manager in the configured
    secret region, and wraps it via cryptography_rsa_signer(). Never
    raises: every failure is caught, logged with a static message only
    (no exception args, no secret/credential value ever interpolated into
    the log call), and reported as None. Called at most once per process
    unless every prior call has failed -- see get_configured_signer().
    """
    role_arn = os.environ.get(SIGNING_KEY_ROLE_ARN_ENV, "").strip()
    secret_name = os.environ.get(SIGNING_KEY_SECRET_NAME_ENV, "").strip()
    secret_region = os.environ.get(SIGNING_KEY_SECRET_REGION_ENV, "").strip()
    if not (role_arn and secret_name and secret_region):
        return None
    if boto3 is None:
        return None

    try:
        assumed = boto3.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=_SIGNING_KEY_READER_SESSION_NAME,
            DurationSeconds=_SIGNING_KEY_READER_SESSION_DURATION_SECONDS,
        )
        reader_credentials = assumed["Credentials"]
        secret = boto3.client(
            "secretsmanager",
            region_name=secret_region,
            aws_access_key_id=reader_credentials["AccessKeyId"],
            aws_secret_access_key=reader_credentials["SecretAccessKey"],
            aws_session_token=reader_credentials["SessionToken"],
        ).get_secret_value(SecretId=secret_name)
    except Exception:
        # Static message only -- never log the exception's str()-formatted
        # args with credential/secret values interpolated in, and never
        # pass reader_credentials/secret into this call.
        logger.exception("live_cdn_signing.signing_key_fetch_failed")
        return None

    key_pem = secret.get("SecretString") or ""
    if not key_pem.strip():
        return None

    try:
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    except Exception:
        logger.exception("live_cdn_signing.signing_key_parse_failed")
        return None
    finally:
        key_pem = None  # best-effort only; Python strings/bytes aren't securely erasable.

    return cryptography_rsa_signer(private_key)


def get_configured_signer() -> Callable[[bytes], bytes] | None:
    """Production entry point. Returns a cached rsa_signer callback backed
    by the real CloudFront private key, or None if signing is not
    configured or could not be obtained -- never raises, so every caller
    has one uniform fail-closed signal to act on.

    Thread-safe, lazy, process-lifetime caching: the first successful call
    fetches from AWS (STS assume-role + Secrets Manager GetSecretValue) and
    caches the resulting callable; every later call in this process reuses
    it without another AWS round trip, which matters because
    live_playlist.py calls this on every playlist poll (~every 2 seconds
    per active viewer). A failed fetch is never cached -- the next call
    retries from scratch, so a transient AWS failure self-heals without
    requiring a process restart. Rotating the underlying secret takes
    effect only after the process restarts/redeploys (the cache has no
    TTL); that is an accepted tradeoff, not an oversight, given the
    alternative is a fresh AWS round trip on every live-view poll.
    """
    global _cached_signer
    if _cached_signer is not None:
        return _cached_signer
    with _signer_cache_lock:
        if _cached_signer is None:
            _cached_signer = _fetch_signer_from_aws()
        return _cached_signer
