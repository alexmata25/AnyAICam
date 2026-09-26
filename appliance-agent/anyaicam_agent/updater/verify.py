"""RDM-1 (Remote Device Management, revised design approved 2026-08-14):
package/manifest verification. Two-layer trust model (revised design Sec
3/5):

  1. The manifest itself is authenticated by an RSA-PKCS1v15+SHA-256
     signature over its OWN canonical serialization, checked against a
     single pinned trusted public key (no rotation, no fetch-at-
     registration bootstrap -- see load_trusted_public_key()'s docstring).
     This mirrors app/live_cdn_signing.py's signing side exactly, just
     inverted (verify instead of sign) and with SHA-256 instead of
     CloudFront's fixed SHA-1.
  2. Only once the manifest is authenticated is its own `sha256` field
     trusted as the correct checksum for the downloaded package bytes --
     verify_package_checksum() itself does no authentication, it merely
     compares two digests.

Every failure path here raises a specific VerificationError subclass --
nothing in this module ever returns a partially-verified or "probably ok"
result. PackageVerifier.__init__() does not fail at construction time if
the trusted key is missing; the key is loaded fresh on every verify call
so a verifier instance can never silently keep using a stale/unavailable
key -- see load_trusted_public_key()'s docstring for why this matters for
fail-closed behaviour.

No network, no AWS, no filesystem writes -- this module only ever reads
the trusted public key file and whatever package file path it is given.
"""

import hashlib
import json
from pathlib import Path
from typing import Union

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .models import Manifest

PathLike = Union[str, Path]

_CHECKSUM_READ_CHUNK_SIZE = 1024 * 1024  # 1 MiB; streamed, never loads a whole package into memory


class VerificationError(Exception):
    """Base for every failure PackageVerifier/this module's functions can
    raise. Catching this one type is enough to know "do not trust this
    update" -- callers that need to distinguish *why* (e.g. the future
    state machine, to choose which UpdateState to record) can catch the
    specific subclass instead."""


class TrustedKeyUnavailable(VerificationError):
    """The pinned trusted public key file is missing, unreadable, not
    valid PEM, or not an RSA public key. This is the fail-closed signal
    for RDM-1's entire trust anchor being absent -- see
    load_trusted_public_key()."""


class ManifestSignatureInvalid(VerificationError):
    """The supplied signature does not verify against the manifest's own
    canonical bytes under the pinned trusted public key -- the manifest
    was tampered with, signed by the wrong key, or the signature/manifest
    pairing does not match."""


class PackageTargetMismatch(VerificationError):
    """The manifest's `target` field does not match this device's own
    target string. An authentic manifest for a DIFFERENT device/target is
    still refused -- authenticity alone is not sufficient."""


class PackageChecksumMismatch(VerificationError):
    """The downloaded package's own SHA-256 digest does not match the
    (already-authenticated) manifest's `sha256` field -- the download is
    corrupt, incomplete, or was substituted after the manifest was
    signed."""


def canonical_manifest_bytes(manifest_dict: dict) -> bytes:
    """The exact byte sequence that is signed/verified for a manifest:
    JSON with sorted keys and no incidental whitespace, so two processes
    (the signer, and every verifier) always compute identical bytes for
    the same logical manifest regardless of dict insertion order or
    formatting choices made when the manifest was authored.
    """
    return json.dumps(manifest_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_trusted_public_key(path: PathLike) -> rsa.RSAPublicKey:
    """Loads and returns the single pinned RSA public key RDM-1 trusts,
    from the PEM file at `path`.

    This is RDM-1's entire trust anchor (revised design Sec 3): a public
    key provisioned onto the device out-of-band (e.g. at image build/
    enrollment time), read fresh on every call. There is deliberately no
    "fetch the trusted key once at registration" alternative in RDM-1 --
    that would make a compromised/spoofed registration flow able to plant
    an attacker's key as trusted, which a pre-provisioned pinned file
    cannot be tricked into doing over the network. Key rotation is out of
    scope for RDM-1 and would need its own separately authorized design.

    Raises TrustedKeyUnavailable -- never returns None, never lets a raw
    OSError/ValueError escape -- if the file is missing, unreadable, not
    valid PEM, or not an RSA public key. Every caller therefore has one
    uniform fail-closed signal: no trusted key means no verification is
    even attempted, ever.
    """
    try:
        key_bytes = Path(path).read_bytes()
    except OSError as error:
        raise TrustedKeyUnavailable(f"trusted public key file is unreadable: {error}") from error

    try:
        public_key = serialization.load_pem_public_key(key_bytes)
    except (ValueError, UnsupportedAlgorithm, TypeError) as error:
        raise TrustedKeyUnavailable(f"trusted public key file is not a valid PEM public key: {error}") from error

    if not isinstance(public_key, rsa.RSAPublicKey):
        raise TrustedKeyUnavailable("trusted public key is not an RSA public key.")

    return public_key


def verify_manifest_signature(manifest_dict: dict, signature: bytes, public_key: rsa.RSAPublicKey) -> None:
    """Verifies `signature` against manifest_dict's canonical bytes under
    `public_key`, using RSA-PKCS1v15 padding with SHA-256 -- the exact
    mirror-image of the padding/hash choice app/live_cdn_signing.py's
    cryptography_rsa_signer() uses on the signing side (that module uses
    SHA-1 only because CloudFront's own signing spec fixes it; this one is
    free to use SHA-256).

    Raises ManifestSignatureInvalid on any verification failure. Returns
    None (no value) on success -- the caller already has manifest_dict.
    """
    message = canonical_manifest_bytes(manifest_dict)
    try:
        public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as error:
        raise ManifestSignatureInvalid("manifest signature verification failed.") from error


def sha256_of_file(path: PathLike) -> str:
    """Streams the file at `path` in fixed-size chunks and returns its
    SHA-256 digest as a lowercase hex string. Never loads the whole file
    into memory -- update packages are not assumed to be small."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHECKSUM_READ_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_package_checksum(path: PathLike, expected_sha256: str) -> None:
    """Raises PackageChecksumMismatch unless the SHA-256 of the file at
    `path` equals `expected_sha256` (case-insensitive hex comparison).
    Callers must only ever pass an `expected_sha256` that has already been
    authenticated (i.e. came from a Manifest that passed
    verify_manifest_signature()) -- this function itself performs no
    authentication, only a digest comparison.
    """
    actual = sha256_of_file(path)
    if actual.lower() != str(expected_sha256).lower():
        raise PackageChecksumMismatch(
            f"package checksum mismatch: expected {expected_sha256}, got {actual}."
        )


class PackageVerifier:
    """The single entry point RDM-1's future state machine (Group 6) calls
    to authenticate one update end-to-end: manifest signature, then
    structural validation, then target match, then package checksum --
    always in that order, since each later step trusts data only the
    earlier steps have already authenticated.

    Holds no key material itself -- only the path to the pinned public key
    file, re-read via load_trusted_public_key() on every call. This means
    a verifier instance can be constructed once at agent startup and
    reused for every update check without ever risking the use of a
    key that was cached before the file existed, or before it was
    corrected after an operator fixed a provisioning mistake.
    """

    def __init__(self, trusted_public_key_file: PathLike):
        self._trusted_public_key_file = trusted_public_key_file

    def verify_manifest(self, manifest_dict: dict, signature: bytes) -> Manifest:
        """Verifies `signature` over manifest_dict using the pinned
        trusted key, and ONLY THEN structurally validates manifest_dict
        into a Manifest -- in that order, so a structurally-plausible but
        unsigned/mis-signed manifest is never even parsed into a trusted
        object.

        Raises TrustedKeyUnavailable if the pinned key cannot be loaded,
        ManifestSignatureInvalid if the signature does not verify, or
        ValueError (from Manifest.from_dict) if the now-authenticated
        content is structurally malformed -- the last case is left as a
        distinct exception type deliberately: it means the trusted signer
        itself produced a malformed manifest, a different failure mode
        than "this manifest is not trustworthy".
        """
        public_key = load_trusted_public_key(self._trusted_public_key_file)
        verify_manifest_signature(manifest_dict, signature, public_key)
        return Manifest.from_dict(manifest_dict)

    def target_matches(self, manifest: Manifest, expected_target: str) -> bool:
        """True iff the (already-authenticated) manifest's target field
        equals `expected_target`. Pure comparison, no I/O, no exception on
        mismatch -- callers that need fail-closed behaviour on a mismatch
        use verify() below, which raises PackageTargetMismatch."""
        return manifest.target == expected_target

    def verify_package(self, manifest: Manifest, package_path: PathLike) -> None:
        """Checksums the downloaded bytes at `package_path` against
        manifest.sha256. Only meaningful to call with a Manifest that has
        already passed verify_manifest() -- this method trusts
        manifest.sha256 completely, since authenticating that value is
        exactly what verify_manifest() already did. Raises
        PackageChecksumMismatch on any digest mismatch."""
        verify_package_checksum(package_path, manifest.sha256)

    def verify(
        self,
        manifest_dict: dict,
        signature: bytes,
        package_path: PathLike,
        expected_target: str,
    ) -> Manifest:
        """Full pipeline: signature -> structural validation -> target
        match -> package checksum. Returns the trusted Manifest only if
        every step succeeds; raises the specific VerificationError
        subclass (or ValueError, see verify_manifest()'s docstring) for
        whichever step failed first. Nothing is returned, and no partial
        result is exposed, on any failure.
        """
        manifest = self.verify_manifest(manifest_dict, signature)
        if not self.target_matches(manifest, expected_target):
            raise PackageTargetMismatch(
                f"manifest target {manifest.target!r} does not match this device's target {expected_target!r}."
            )
        self.verify_package(manifest, package_path)
        return manifest
