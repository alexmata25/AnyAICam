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


def publish_release(target: str, channel: str, *, manifest: dict, package_bytes: bytes) -> dict:
    """Publishes one release: stores the package bytes and then the
    catalog pointer (which must be written LAST, so a reader can never
    observe a catalog entry whose package object doesn't exist yet) as
    this target/channel's new 'latest'. A full release-publishing CLI
    (signing, versioning, rollout control) is out of scope for this
    repair pass -- the actively reported bug is the total absence of a
    catalog/endpoint, not the absence of tooling to operate one; this
    function exists so that absence is directly testable."""
    storage = get_storage()
    base = _catalog_key(target, channel).rsplit("/", 1)[0]
    package_key = f"{base}/{validate_path_segment(str(manifest.get('version', '')), 'version')}/package.bin"
    storage.put("updates", package_key, package_bytes, content_type="application/octet-stream")
    record = {"manifest": manifest, "package_key": package_key}
    storage.put("updates", _catalog_key(target, channel), json.dumps(record).encode("utf-8"), content_type="application/json")
    return record


def load_signing_key():
    """Loads the RSA private key used to sign manifests, from the PEM
    file at ANYAICAM_UPDATE_SIGNING_KEY_FILE. Returns None (never raises)
    if unset, missing, or invalid -- callers must treat that as "signing
    unavailable" and fail closed (503) rather than serve an unsigned or
    partially-signed manifest. Mirrors the fail-closed posture of the
    device-side counterpart, updater/verify.py's
    load_trusted_public_key(), just inverted (private key, loaded fresh
    on every call for the same reason: a verifier/signer must never keep
    using a key that was cached before the file existed or before an
    operator corrected a provisioning mistake)."""
    path = os.environ.get("ANYAICAM_UPDATE_SIGNING_KEY_FILE", "").strip()
    if not path:
        return None
    try:
        return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None


def canonical_manifest_bytes(manifest_dict: dict) -> bytes:
    """Byte-for-byte the same canonicalization as updater/verify.py's own
    canonical_manifest_bytes() -- signer and verifier must agree exactly,
    or every signature this server issues would fail verification on the
    device."""
    return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_manifest(manifest_dict: dict, private_key) -> bytes:
    """RSA-PKCS1v15+SHA-256 over canonical_manifest_bytes() -- the exact
    padding/hash pair updater/verify.py's verify_manifest_signature()
    checks against (deliberately SHA-256, not live_cdn_signing.py's
    CloudFront-fixed SHA-1; see that module's own verify-side docstring
    for why the two differ)."""
    return private_key.sign(canonical_manifest_bytes(manifest_dict), padding.PKCS1v15(), hashes.SHA256())
