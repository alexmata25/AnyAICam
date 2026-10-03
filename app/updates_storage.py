"""Server-side storage/catalog for RDM-1 signed appliance updates,
backing GET /api/appliance/updates/latest (registered in
appliance_cloud.py). This is the other half of an already-built,
already-tested client: appliance-agent/anyaicam_agent/updater/models.py
(Manifest), .../updater/verify.py (RSA-PKCS1v15+SHA-256 signature
verification against a pinned public key), and .../updater/s3_source.py
(the exact response shape this module's route must produce) all exist
and were built against this contract; nothing on the server side
registered the endpoint or produced a catalog entry for them to consume
-- see docs/cloud-product-architecture-plan.md for how this gap was
found.

Catalog storage: one JSON pointer record per target/channel, plus the
package bytes, both under object_storage's 'updates' category. No
catalog entry for a given target/channel is the normal, safe-by-default
state for every appliance until a release is deliberately published for
it via publish_release() -- get_latest_release() returning None means
GET /api/appliance/updates/latest must answer no_update_available, never
a 404 or an unhandled error (that 404/traceback loop is the actively
reported bug this module fixes).
"""
import base64
import hashlib
import json
import os
import re
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from object_storage import LocalStorage, get_storage, safe_key

# ---------------------------------------------------------------- update storage (2026-10-03)
#
# Software Update packages and catalog records can live in S3 while the rest
# of the application keeps its general backend (ANYAICAM_STORAGE_BACKEND --
# thumbnails, clips, documents, downloads ... are unaffected).
#
#   ANYAICAM_UPDATE_STORAGE_BACKEND   unset/"default": the general backend
#                                     (unchanged behaviour); "local"; or "s3"
#   ANYAICAM_UPDATE_S3_BUCKET         bucket for "s3" (default ANYAICAM_S3_BUCKET)
#   ANYAICAM_UPDATE_S3_REGION         region (default ANYAICAM_S3_REGION, then AWS_REGION, then us-east-1)
#
# Objects are written only under the "updates/" prefix. Credentials come from
# the standard AWS chain (the instance role); none are configured here.
# Releases are signed offline, so no private key is involved anywhere.
UPDATE_BACKEND_ENV = "ANYAICAM_UPDATE_STORAGE_BACKEND"


class UpdateStorageError(Exception):
    """Update storage is misconfigured or unavailable. Callers fail safe:
    nothing is published, no release is offered, no download link is served."""


class UpdateS3Storage:
    """S3 storage for the 'updates' category only (prefix updates/)."""

    CATEGORY = "updates"

    def __init__(self, *, client=None):
        self.bucket = (os.environ.get("ANYAICAM_UPDATE_S3_BUCKET") or os.environ.get("ANYAICAM_S3_BUCKET") or "").strip()
        if not self.bucket:
            raise UpdateStorageError("ANYAICAM_UPDATE_STORAGE_BACKEND=s3 needs a bucket (ANYAICAM_UPDATE_S3_BUCKET or ANYAICAM_S3_BUCKET).")
        self.region = (os.environ.get("ANYAICAM_UPDATE_S3_REGION") or os.environ.get("ANYAICAM_S3_REGION")
                       or os.environ.get("AWS_REGION") or "us-east-1").strip()
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as error:
                raise UpdateStorageError("boto3 is required for S3 update storage.") from error
            endpoint = os.environ.get("ANYAICAM_S3_ENDPOINT", "").strip() or None
            client = boto3.client("s3", region_name=self.region, endpoint_url=endpoint,
                                  config=Config(signature_version="s3v4"))
        self.client = client

    def _key(self, category: str, key: str) -> str:
        if category != self.CATEGORY:
            raise ValueError("Update storage only holds the 'updates' category.")
        return safe_key(category, key)

    def put(self, category, key, data, content_type=None):
        object_key = self._key(category, key)
        self.client.put_object(Bucket=self.bucket, Key=object_key, Body=data,
                               ContentType=content_type or "application/octet-stream")
        return {"key": object_key, "size": len(data), "backend": "s3"}

    def get(self, category, key):
        return self.client.get_object(Bucket=self.bucket, Key=self._key(category, key))["Body"].read()

    def delete(self, category, key):
        self.client.delete_object(Bucket=self.bucket, Key=self._key(category, key))

    def url(self, category, key, expires_seconds=900):
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": self._key(category, key)},
                                                  ExpiresIn=expires_seconds)


def get_update_storage():
    """The storage Software Update uses. Raises UpdateStorageError for an
    unknown setting or an incomplete S3 configuration (never falls back to
    a different backend silently)."""
    backend = os.environ.get(UPDATE_BACKEND_ENV, "").strip().lower()
    if backend in ("", "default"):
        return get_storage()
    if backend == "local":
        return LocalStorage()
    if backend == "s3":
        return UpdateS3Storage()
    raise UpdateStorageError(f"{UPDATE_BACKEND_ENV}={backend!r} is not supported (use 's3', 'local' or leave unset).")


def package_download_url(release: dict, expires_seconds: int = 300) -> str:
    """An absolute http(s) link an appliance can download the package from
    (a presigned S3 URL). A relative path -- what the local backend
    returns, behind a locked-down route -- is useless to an appliance, so it
    is refused rather than served."""
    url = get_update_storage().url("updates", release["package_key"], expires_seconds=expires_seconds)
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise UpdateStorageError("Update packages are not downloadable from this storage backend; set "
                                 f"{UPDATE_BACKEND_ENV}=s3.")
    return url

# Mirrors appliance-agent/anyaicam_agent/updater/s3_source.py's own
# _validate_path_segment() exactly (same grammar, same reasoning) --
# there is no shared import path between this package and the agent's,
# so the two copies must be kept in sync by hand, same as that module's
# own docstring already notes for its own relationship to the publisher
# tool.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_path_segment(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value) or value in {".", ".."}:
        raise ValueError(f"{field_name} must be a non-empty string matching {_SAFE_SEGMENT.pattern!r}.")
    return value


def _catalog_key(target: str, channel: str) -> str:
    return f"{validate_path_segment(target, 'target')}/{validate_path_segment(channel, 'channel')}/latest.json"


def get_latest_release(target: str, channel: str) -> dict | None:
    """Returns {'manifest': ..., 'package_key': ...} for the currently
    published release on this target/channel, or None if none has ever
    been published -- the common, safe-by-default case for every
    appliance until a release is deliberately pushed. Any storage-backend
    error (missing object, unreachable S3, malformed JSON) is treated the
    same way: no release, never an exception the route would have to
    turn into a 500."""
    try:
        raw = get_update_storage().get("updates", _catalog_key(target, channel))
        record = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("manifest"), dict) or not record.get("package_key"):
        return None
    return record


def load_trusted_public_key():
    """The release-signing PUBLIC key (ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE),
    used only to check that what is published was signed offline by the
    owner. Software Update (2026-10-03): the cloud never holds the private
    key and never signs anything -- it stores and serves the offline
    signature. None when unset/unreadable (publishing then fails closed)."""
    path = os.environ.get("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE", "").strip()
    if not path:
        return None
    try:
        return serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError, TypeError):
        return None


def canonical_manifest_bytes(manifest_dict: dict) -> bytes:
    """Byte-for-byte the canonicalization the appliance verifies
    (updater/verify.py, system/apply_release.py) and the offline signer
    signs (installer/sign_update_release.py)."""
    return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")


class PublishError(Exception):
    pass


def verify_release(target: str, channel: str, *, manifest: dict, package_bytes: bytes, signature: bytes) -> None:
    """Every check publish_release() makes, without storing anything: the
    signature verifies under the configured PUBLIC key, and the package has
    the manifest's SHA-256 and size, target and channel. Raises PublishError."""
    public_key = load_trusted_public_key()
    if public_key is None:
        raise PublishError("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE is not configured; refusing to publish.")
    try:
        public_key.verify(signature, canonical_manifest_bytes(manifest), padding.PKCS1v15(), hashes.SHA256())
    except Exception as error:
        raise PublishError("The manifest signature does not verify; refusing to publish.") from error
    if hashlib.sha256(package_bytes).hexdigest() != str(manifest.get("sha256", "")).lower():
        raise PublishError("The package does not match the manifest's SHA-256.")
    if len(package_bytes) != int(manifest.get("package_size_bytes", -1)):
        raise PublishError("The package does not match the manifest's size.")
    if manifest.get("target") != target or manifest.get("channel") != channel:
        raise PublishError("The manifest is for a different target/channel.")


def publish_release(target: str, channel: str, *, manifest: dict, package_bytes: bytes, signature: bytes) -> dict:
    """Publishes one offline-signed release as this target/channel's
    'latest' after verify_release(). Stores the package first and the
    catalog pointer LAST, so a reader never sees a pointer to a missing
    package."""
    verify_release(target, channel, manifest=manifest, package_bytes=package_bytes, signature=signature)
    try:
        storage = get_update_storage()
    except UpdateStorageError as error:
        raise PublishError(f"Update storage is not usable: {error}") from error
    base = _catalog_key(target, channel).rsplit("/", 1)[0]
    update_id = validate_path_segment(str(manifest.get("update_id", "")), "update_id")
    package_key = f"{base}/{update_id}/package.bin"
    storage.put("updates", package_key, package_bytes, content_type="application/octet-stream")
    record = {"manifest": manifest, "package_key": package_key,
              "signature": base64.b64encode(signature).decode("ascii")}
    storage.put("updates", _catalog_key(target, channel), json.dumps(record).encode("utf-8"), content_type="application/json")
    return record
