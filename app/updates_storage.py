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

from object_storage import get_storage

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
        raw = get_storage().get("updates", _catalog_key(target, channel))
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


def publish_release(target: str, channel: str, *, manifest: dict, package_bytes: bytes, signature: bytes) -> dict:
    """Publishes one offline-signed release as this target/channel's
    'latest': the signature must verify under the configured public key,
    and the package must have the manifest's SHA-256 and size. Stores the
    package first and the catalog pointer LAST, so a reader never sees a
    pointer to a missing package."""
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
    storage = get_storage()
    base = _catalog_key(target, channel).rsplit("/", 1)[0]
    update_id = validate_path_segment(str(manifest.get("update_id", "")), "update_id")
    package_key = f"{base}/{update_id}/package.bin"
    storage.put("updates", package_key, package_bytes, content_type="application/octet-stream")
    record = {"manifest": manifest, "package_key": package_key,
              "signature": base64.b64encode(signature).decode("ascii")}
    storage.put("updates", _catalog_key(target, channel), json.dumps(record).encode("utf-8"), content_type="application/json")
    return record
