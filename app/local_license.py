"""Local Recording device-bound licence ("Local ID") -- core (2026-09-28).

Implements the cryptographic core of docs/local-recording-licensing-design.md,
which is independent of the still-open product/infrastructure decisions
(where the issuing service lives; what the product does when verification
fails). Nothing here enforces anything or is called at startup yet --
wiring enforcement waits for those decisions.

- A device fingerprint is SHA-256 over stable local characteristics
  (/etc/machine-id, root volume UUID, primary MAC, appliance_id). Only the
  hash is ever stored or sent -- never the raw values.
- The licensing service signs {local_id, license_key, fingerprint_hash,
  product, issued_at} with its Ed25519 private key (never shipped).
- Every startup can verify fully offline with the embedded PUBLIC key:
  the signature must be valid and the fingerprint must equal this
  machine's freshly computed one. No fuzzy matching -- hardware changes go
  through the support transfer workflow.
- Failure reasons match the design's audit vocabulary:
  certificate_missing / certificate_corrupt / signature_invalid /
  fingerprint_mismatch (plus wrong_product).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PRODUCT = "local"
CERT_VERSION = 1
_FIELDS = ("version", "local_id", "license_key", "fingerprint_hash", "product", "issued_at")


# ---------------------------------------------------------------- fingerprint

def compute_fingerprint(machine_id: str, volume_uuid: str, mac_address: str, appliance_id: str) -> str:
    """SHA-256 over the four inputs (normalized, length-prefixed so no two
    different input sets can concatenate to the same bytes)."""
    parts = [str(v or "").strip().lower() for v in (machine_id, volume_uuid, mac_address, appliance_id)]
    if not any(parts):
        raise ValueError("no fingerprint inputs available")
    material = b"".join(len(p.encode()).to_bytes(4, "big") + p.encode() for p in parts)
    return hashlib.sha256(b"anyaicam-local-id-v1\x00" + material).hexdigest()


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _root_volume_uuid() -> str:
    """UUID of the filesystem mounted at / (Linux), or ''."""
    try:
        device = ""
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) > 4 and fields[4] == "/":
                device = line.split(" - ", 1)[1].split()[1]
                break
        if not device:
            return ""
        device_real = os.path.realpath(device)
        by_uuid = Path("/dev/disk/by-uuid")
        for entry in by_uuid.iterdir() if by_uuid.is_dir() else []:
            if os.path.realpath(entry) == device_real:
                return entry.name
    except OSError:
        return ""
    return ""


def _primary_mac() -> str:
    """The first physical interface's MAC (sorted by name, skipping
    loopback/virtual), or ''."""
    net = Path("/sys/class/net")
    try:
        for name in sorted(p.name for p in net.iterdir()):
            if name == "lo" or name.startswith(("docker", "veth", "br-", "wg", "tailscale", "virbr")):
                continue
            if not (net / name / "device").exists():
                continue  # virtual interface
            mac = _read(str(net / name / "address"))
            if mac and mac != "00:00:00:00:00:00":
                return mac
    except OSError:
        return ""
    return ""


class FingerprintUnavailable(RuntimeError):
    pass


def collect_fingerprint(appliance_id: str) -> str:
    """This machine's fingerprint hash (raw inputs never leave this call).

    Must run on the HOST (the appliance agent), not inside the VMS
    container: verified on the real Ryzen, the container has no
    /etc/machine-id, an overlay root with no volume UUID and no physical
    NICs -- a fingerprint computed there would bind to appliance_id alone
    and change whenever the container is rebuilt. Refuses rather than
    returning such a weak fingerprint."""
    machine_id, volume_uuid, mac = _read("/etc/machine-id"), _root_volume_uuid(), _primary_mac()
    if not machine_id or not (volume_uuid or mac):
        raise FingerprintUnavailable("host fingerprint inputs unavailable (running inside a container?)")
    return compute_fingerprint(machine_id, volume_uuid, mac, appliance_id)


# ---------------------------------------------------------------- keys

def generate_signing_keypair() -> tuple[bytes, bytes]:
    """(private_pem, public_pem) for the licensing service. Ops helper;
    the private key must never be committed or shipped."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem, public_pem


def _load_private(private_pem: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("licensing key must be Ed25519")
    return key


def _load_public(public_pem: bytes) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("licensing public key must be Ed25519")
    return key


# ---------------------------------------------------------------- certificates

def _canonical(fields: dict) -> bytes:
    return json.dumps({k: fields[k] for k in _FIELDS}, sort_keys=True, separators=(",", ":")).encode("utf-8")


def new_license_key() -> str:
    raw = secrets.token_hex(10).upper()
    return "-".join(raw[i:i + 5] for i in range(0, 20, 5))


def issue_certificate(private_pem: bytes, *, license_key: str, fingerprint_hash: str,
                      local_id: str | None = None, now: datetime | None = None) -> dict:
    """Service side: a signed certificate binding license_key to one device."""
    if not license_key or len(fingerprint_hash or "") != 64:
        raise ValueError("license_key and a 64-hex fingerprint_hash are required")
    fields = {
        "version": CERT_VERSION,
        "local_id": local_id or f"LID-{uuid.uuid4().hex[:16].upper()}",
        "license_key": license_key,
        "fingerprint_hash": fingerprint_hash.lower(),
        "product": PRODUCT,
        "issued_at": (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat(),
    }
    signature = _load_private(private_pem).sign(_canonical(fields))
    return {"certificate": fields, "signature": base64.b64encode(signature).decode("ascii")}


@dataclass(frozen=True)
class Verification:
    valid: bool
    reason: str  # ok | certificate_missing | certificate_corrupt | signature_invalid | fingerprint_mismatch | wrong_product
    local_id: str | None = None


def verify_certificate(document, public_pem: bytes, current_fingerprint: str) -> Verification:
    """Device side, fully offline."""
    if not document:
        return Verification(False, "certificate_missing")
    try:
        if isinstance(document, (bytes, str)):
            document = json.loads(document)
        fields = document["certificate"]
        signature = base64.b64decode(document["signature"], validate=True)
        canonical = _canonical(fields)
    except (ValueError, KeyError, TypeError):
        return Verification(False, "certificate_corrupt")
    try:
        _load_public(public_pem).verify(signature, canonical)
    except InvalidSignature:
        return Verification(False, "signature_invalid", fields.get("local_id"))
    if fields.get("product") != PRODUCT or fields.get("version") != CERT_VERSION:
        return Verification(False, "wrong_product", fields.get("local_id"))
    if fields.get("fingerprint_hash") != str(current_fingerprint or "").lower():
        return Verification(False, "fingerprint_mismatch", fields.get("local_id"))
    return Verification(True, "ok", fields.get("local_id"))


def load_and_verify(path: str | Path, public_pem: bytes, appliance_id: str) -> Verification:
    """Convenience for a future startup check: read the certificate file
    and verify it against this machine."""
    try:
        document = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return Verification(False, "certificate_missing")
    except OSError:
        return Verification(False, "certificate_corrupt")
    return verify_certificate(document, public_pem, collect_fingerprint(appliance_id))


def save_certificate(path: str | Path, document: dict) -> None:
    """Write with 0600 permissions (integrity comes from the signature)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(document, indent=2), encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)
