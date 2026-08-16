#!/usr/bin/env python3
"""RDM-2 Group 2G: operator publisher CLI for software updates.

Runs OUTSIDE the cloud backend's own runtime, using a SEPARATE read/
write S3 identity from the cloud backend's own read/presign-only role
(this script relies entirely on boto3's own standard credential chain
-- env vars/profile/instance role -- it does not manage IAM itself; see
docs/AI_HANDOFF.md RDM-2 Group 2G for the approved IAM design, applied
in a later, separately-authorized infra step, not by this script).

Signs the manifest with an operator-supplied RSA private key -- a local
PEM file path given explicitly via --private-key. This phase does NOT
integrate with AWS Secrets Manager or any other key-management
infrastructure; the operator is solely responsible for that file's
custody.

Reuses appliance-agent's OWN Manifest/canonical_manifest_bytes()
directly (via a sys.path insertion, matching this repo's already-
established test-file convention) rather than re-implementing the
manifest/signing shape a second time -- a signing/verification scheme
that silently drifted out of sync between publisher and device would be
far more dangerous than the small, deliberate duplication of the S3
path-segment grammar below (which has nothing to do with cryptographic
correctness, only with safe key construction, and must be duplicated
because tools/, app/, and appliance-agent/ are three separate
deployables with no shared runtime import path between them).

Publish sequence (atomic-current-pointer discipline):
  1. Validate target/channel/version against the same safe path-segment
     grammar app/updates_storage.py and appliance-agent's s3_source.py
     independently enforce.
  2. Compute the REAL sha256 of the package file being published --
     never trust an operator-supplied --sha256 override over the actual
     bytes; refuse if the two disagree.
  3. Compare the version being published against whatever latest.json
     currently points to (if anything) -- refuse a non-newer version.
  4. Refuse to overwrite an already-published (immutable) version's
     manifest or package object.
  5. Sign the manifest.
  6. Upload the package object.
  7. Upload the versioned manifest object.
  8. Only THEN overwrite latest.json -- the ONE mutable object, written
     last, so a crash/failure at any earlier step never leaves
     latest.json pointing at a version whose package doesn't exist yet.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APPLIANCE_AGENT_DIR = REPO_ROOT / "appliance-agent"
if str(APPLIANCE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(APPLIANCE_AGENT_DIR))

from anyaicam_agent.updater.models import Manifest  # noqa: E402
from anyaicam_agent.updater.verify import canonical_manifest_bytes  # noqa: E402

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


# Mirrors app/updates_storage.py's and appliance-agent's
# updater/s3_source.py's own _validate_path_segment() -- see each
# module's docstring for why this must be duplicated rather than shared.
def _validate_path_segment(value, field_name):
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise ValueError(f"{field_name} must be a non-empty string matching {_SAFE_SEGMENT.pattern!r}.")
    if value in (".", ".."):
        raise ValueError(f"{field_name} must not be '.' or '..'.")
    return value


def _manifest_key(target, channel, version="latest"):
    return f"manifests/{target}/{channel}/{version}.json"


def _package_key(target, channel, version):
    return f"packages/{target}/{channel}/{version}.tar"


def _parse_version(version):
    """Same dotted-integer scheme appliance-agent's state_machine.py
    uses for upgrade comparison -- duplicated here for the same
    no-shared-import-path reason as the path-segment grammar above."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as error:
        raise ValueError(f"version {version!r} is not a dotted-integer version string.") from error


class PublishConflict(Exception):
    """Refused to overwrite an already-published immutable object, or to
    publish a version that is not newer than what's currently published."""


def _s3_error_code(error):
    response = getattr(error, "response", None)
    return response.get("Error", {}).get("Code") if isinstance(response, dict) else None


def _refuse_non_newer_publish(s3_client, bucket, target, channel, version):
    try:
        response = s3_client.get_object(Bucket=bucket, Key=_manifest_key(target, channel, "latest"))
    except s3_client.exceptions.NoSuchKey:
        return  # nothing published yet -- any version is fine
    except Exception as error:  # noqa: BLE001 -- fallback for a generic ClientError carrying the same code
        if _s3_error_code(error) in ("NoSuchKey", "404"):
            return
        raise
    current = json.loads(response["Body"].read().decode("utf-8"))
    current_version = current.get("manifest", {}).get("version")
    if current_version and _parse_version(version) <= _parse_version(current_version):
        raise PublishConflict(
            f"version {version!r} is not newer than the currently published {current_version!r}."
        )


def _refuse_overwrite(s3_client, bucket, key):
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
    except s3_client.exceptions.NoSuchKey:
        return  # doesn't exist yet -- safe to publish
    except Exception as error:  # noqa: BLE001 -- fallback for a generic ClientError carrying the same code
        if _s3_error_code(error) in ("404", "NoSuchKey"):
            return
        raise
    raise PublishConflict(f"{key} already exists -- published versions are immutable, refusing to overwrite.")


def _load_private_key(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key_bytes = Path(path).read_bytes()
    private_key = serialization.load_pem_private_key(key_bytes, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("Private key file is not an RSA private key.")
    return private_key


def publish(
    *, s3_client, bucket, target, channel, version, update_id, package_path,
    platform, architecture, private_key_path, expected_sha256=None, issued_at=None,
):
    """Core publish logic -- s3_client is injected so this is fully
    testable without any real AWS access. Returns the published
    envelope {'manifest': {...}, 'signature': '<base64>'}.

    Raises ValueError for a structural/grammar/digest-mismatch problem
    (nothing durable touched), or PublishConflict for an attempted
    overwrite or non-newer-version publish (also nothing durable
    touched -- both checks run BEFORE any upload).
    """
    _validate_path_segment(target, "target")
    _validate_path_segment(channel, "channel")
    _validate_path_segment(version, "version")

    package_path = Path(package_path)
    package_bytes = package_path.read_bytes()
    sha256 = hashlib.sha256(package_bytes).hexdigest()
    if expected_sha256 is not None and expected_sha256.lower() != sha256.lower():
        raise ValueError(
            f"--sha256 {expected_sha256!r} does not match the actual package digest {sha256!r}."
        )

    manifest_dict = {
        "update_id": update_id, "version": version, "sha256": sha256,
        "target": target, "platform": platform, "architecture": architecture,
        "channel": channel, "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
        "package_size_bytes": len(package_bytes),
    }
    manifest = Manifest.from_dict(manifest_dict)  # structural validation, reused from RDM-1

    _refuse_non_newer_publish(s3_client, bucket, target, channel, version)
    _refuse_overwrite(s3_client, bucket, _package_key(target, channel, version))
    _refuse_overwrite(s3_client, bucket, _manifest_key(target, channel, version))

    private_key = _load_private_key(private_key_path)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    signature = private_key.sign(canonical_manifest_bytes(manifest.as_dict()), padding.PKCS1v15(), hashes.SHA256())
    signature_b64 = base64.b64encode(signature).decode("ascii")

    envelope = {"manifest": manifest.as_dict(), "signature": signature_b64}
    envelope_bytes = json.dumps(envelope).encode("utf-8")

    s3_client.put_object(Bucket=bucket, Key=_package_key(target, channel, version), Body=package_bytes, ContentType="application/x-tar")
    s3_client.put_object(Bucket=bucket, Key=_manifest_key(target, channel, version), Body=envelope_bytes, ContentType="application/json")
    s3_client.put_object(Bucket=bucket, Key=_manifest_key(target, channel, "latest"), Body=envelope_bytes, ContentType="application/json", CacheControl="no-cache")

    return envelope


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish a signed AnyAiCam appliance software update.")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "us-east-1"))
    parser.add_argument("--target", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--update-id", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--sha256", default=None, help="Optional expected package sha256 -- refuses to publish if it disagrees with the actual computed digest.")
    args = parser.parse_args(argv)

    import boto3
    s3_client = boto3.client("s3", region_name=args.region)
    envelope = publish(
        s3_client=s3_client, bucket=args.bucket, target=args.target, channel=args.channel,
        version=args.version, update_id=args.update_id, package_path=args.package,
        platform=args.platform, architecture=args.architecture,
        private_key_path=args.private_key, expected_sha256=args.sha256,
    )
    print(json.dumps(envelope, indent=2))


if __name__ == "__main__":
    main()
