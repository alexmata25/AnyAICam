"""RDM-2 Group 2G: the dedicated S3 helper for software update
manifests/packages -- private-S3 + presigned GET.

Deliberately NOT app/object_storage.py: that module is scoped to
customer-content categories (ALLOWED_CATEGORIES) on the shared
ANYAICAM_S3_BUCKET, used for snapshots/thumbnails/clips/documents/
partner-materials. Update packages are far more security-sensitive than
those categories -- a compromised read/write path here means arbitrary
code execution on every appliance, not exposure of a customer's video
clip. A separate module, on a SEPARATE dedicated bucket
(ANYAICAM_UPDATES_S3_BUCKET), with its OWN narrowly-scoped IAM identity
(read/presign only -- see the module-level docstring below), keeps that
blast radius contained and the IAM surface easy to audit.

S3 IS the sole source of truth for what is currently published -- there
is no database table backing this (see docs/AI_HANDOFF.md RDM-2 Group
2G for the full "why no DB table" reasoning). This module only ever
READS: latest.json / {version}.json manifests, and generates presigned
GET URLs for package objects. It never writes -- publishing is
tools/publish_update.py's job, using a completely separate read/write
identity (see that module's own docstring).

Object layout (approved design):
  manifests/{target}/{channel}/latest.json      <- the mutable "current" pointer
  manifests/{target}/{channel}/{version}.json   <- immutable per-version manifest
  packages/{target}/{channel}/{version}.tar     <- immutable per-version package
"""

import json
import os
import re

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# Mirrors appliance-agent/anyaicam_agent/updater/s3_source.py's and
# tools/publish_update.py's own _validate_path_segment() -- no shared
# import path between this package, the separate appliance-agent
# package, and the standalone publisher script, so all three copies
# must be kept in sync by hand. This is the MOST important of the three
# enforcement points: target/channel here come from an external
# (though authenticated) HTTP request, not this process's own trusted
# config.
def _validate_path_segment(value, field_name):
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise ValueError(f"{field_name} must be a non-empty string matching {_SAFE_SEGMENT.pattern!r}.")
    if value in (".", ".."):
        raise ValueError(f"{field_name} must not be '.' or '..'.")
    return value


UPDATES_S3_BUCKET = os.getenv("ANYAICAM_UPDATES_S3_BUCKET", "").strip()
UPDATES_S3_REGION = os.getenv("ANYAICAM_UPDATES_S3_REGION", os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))).strip()
UPDATES_PRESIGNED_TTL_SECONDS = int(os.getenv("ANYAICAM_UPDATES_PRESIGNED_TTL_SECONDS", "1800"))


class UpdatesStorageError(Exception):
    """Base for every failure this module can raise -- an S3-level
    problem (auth, network, unexpected response), never a "no update
    available" outcome, which is represented by ManifestNotFound below."""


class ManifestNotFound(UpdatesStorageError):
    """No manifest is currently published for this target/channel (no
    latest.json object exists) -- a normal, expected outcome, not an
    error the caller should treat as a storage failure."""


class NotConfigured(UpdatesStorageError):
    """ANYAICAM_UPDATES_S3_BUCKET is not set -- this device-management
    feature has no bucket to read from yet. Distinct from
    ManifestNotFound: this means the FEATURE itself has no backing
    storage configured at all, not merely "nothing published yet"."""


def _client():
    if not UPDATES_S3_BUCKET:
        raise NotConfigured("ANYAICAM_UPDATES_S3_BUCKET is not configured.")
    try:
        import boto3
    except ImportError as error:
        raise UpdatesStorageError("Install boto3 to enable the update source.") from error
    return boto3.client("s3", region_name=UPDATES_S3_REGION)


def _manifest_key(target: str, channel: str, version: str = "latest") -> str:
    _validate_path_segment(target, "target")
    _validate_path_segment(channel, "channel")
    if version != "latest":
        _validate_path_segment(version, "version")
    return f"manifests/{target}/{channel}/{version}.json"


def _package_key(target: str, channel: str, version: str) -> str:
    _validate_path_segment(target, "target")
    _validate_path_segment(channel, "channel")
    _validate_path_segment(version, "version")
    return f"packages/{target}/{channel}/{version}.tar"


def get_latest_manifest(target: str, channel: str) -> dict:
    """Returns the parsed JSON body of manifests/{target}/{channel}/
    latest.json -- {"manifest": {...}, "signature": "<base64>"},
    exactly the envelope shape Group 2C's install_update payload and
    Group 2D's report payload already use, for consistency across every
    manifest-carrying wire contract in this project.

    Raises ManifestNotFound if nothing has ever been published for this
    target/channel. Raises NotConfigured if the bucket itself isn't set
    up. Raises UpdatesStorageError for any other S3-level failure.
    """
    client = _client()
    key = _manifest_key(target, channel, "latest")
    try:
        response = client.get_object(Bucket=UPDATES_S3_BUCKET, Key=key)
    except client.exceptions.NoSuchKey:
        raise ManifestNotFound(f"No published manifest for target={target!r} channel={channel!r}.")
    except Exception as error:
        # Defensive fallback: some botocore paths raise a generic
        # ClientError with Error.Code=='NoSuchKey'/'404' rather than the
        # service-generated NoSuchKey exception class caught above.
        error_response = getattr(error, "response", None)
        error_code = error_response.get("Error", {}).get("Code") if isinstance(error_response, dict) else None
        if error_code in ("NoSuchKey", "404"):
            raise ManifestNotFound(f"No published manifest for target={target!r} channel={channel!r}.") from error
        raise UpdatesStorageError(f"Could not read published manifest: {error}") from error
    try:
        return json.loads(response["Body"].read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as error:
        raise UpdatesStorageError(f"Published manifest is not valid JSON: {error}") from error


def presign_package_url(target: str, channel: str, version: str, ttl_seconds: int = None) -> str:
    """Returns a presigned GET URL for packages/{target}/{channel}/
    {version}.tar. ttl_seconds defaults to UPDATES_PRESIGNED_TTL_SECONDS
    (configurable, 1800s/30min default) -- the appliance never receives
    any AWS credential, only this URL."""
    client = _client()
    key = _package_key(target, channel, version)
    ttl = ttl_seconds if ttl_seconds is not None else UPDATES_PRESIGNED_TTL_SECONDS
    return client.generate_presigned_url("get_object", Params={"Bucket": UPDATES_S3_BUCKET, "Key": key}, ExpiresIn=ttl)
