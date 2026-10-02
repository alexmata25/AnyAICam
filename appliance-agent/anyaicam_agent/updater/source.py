"""RDM-1 (Remote Device Management, revised design approved 2026-08-14):
the update source interface -- "where an update manifest and package come
from", kept deliberately separate from how they are authenticated
(updater/verify.py, Group 2) and from how they are installed
(updater/installer.py, a future group).

UpdateSourceProvider is the only interface downstream code (the future
update state machine, Group 6) depends on to find out about and fetch an
update. This module defines the interface, a FakeUpdateSourceProvider
test double, and a production entry-point stub
(get_configured_source()) that always returns None -- there is no real
HTTP/S3/CloudFront/AWS-backed implementation in RDM-1. This is the exact
same fail-closed-by-construction shape app/live_cdn_signing.py's
get_configured_signer() already established for Phase 6b: every real call
path has nowhere to go until a future phase (RDM-3) builds the actual
AWS-backed source, so nothing in RDM-1 can accidentally reach a real
network or AWS resource.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]


class SourceUnavailable(Exception):
    """check_for_manifest() could not reach/query the update source at
    all -- a real network/AWS/HTTP failure. Distinct from "no update
    available", which is a normal, expected None return, not an error."""


class PackageDownloadError(Exception):
    """download_package() failed to retrieve a complete package --
    interrupted transfer, network error, or any other transfer-level
    failure. destination_path is guaranteed to NOT exist when this is
    raised (see UpdateSourceProvider.download_package()'s contract)."""


class PackageNotFound(PackageDownloadError):
    """The source was reachable and the manifest was valid, but the
    specific package object the manifest points to does not exist at the
    source (e.g. a 404). A more specific case of PackageDownloadError --
    callers that only care "did the download fail" can catch the base
    class; callers that want to distinguish "missing" from "interrupted"
    can catch this subclass."""


class UpdateSourceProvider(ABC):
    """The single interface downstream code depends on to find out about
    and fetch an update. Deliberately narrow: exactly the two operations
    a "where do updates come from" implementation must provide. AWS/HTTP/
    CloudFront specifics (RDM-3, not this phase) are free to implement
    this however they like -- callers never see the difference.
    """

    @abstractmethod
    def check_for_manifest(self, current_version: str, target: str, channel: str):
        """Returns (manifest_dict, signature_bytes) describing the latest
        available update for `target`/`channel`, or None if `current_
        version` is already up to date (or nothing has been published).
        manifest_dict/signature_bytes are exactly what
        updater.verify.PackageVerifier.verify_manifest() expects to be
        handed -- this method itself performs no authentication, it only
        retrieves.

        Raises SourceUnavailable if the source could not be reached or
        queried at all.
        """
        raise NotImplementedError

    @abstractmethod
    def download_package(self, manifest_dict: dict, destination_path: PathLike) -> None:
        """Downloads the package manifest_dict describes to
        destination_path.

        Contract every implementation MUST uphold: when this method
        returns normally, destination_path contains the COMPLETE package;
        when it raises, destination_path does NOT exist at all. There is
        no observable partial/truncated state at destination_path itself
        in either case -- implementations achieve this by writing to a
        temporary sibling path and renaming into place only once the
        transfer is fully complete (the same atomic-write-then-rename
        discipline used throughout this codebase, e.g.
        anyaicam_agent/config.py's save_credential()). A crash or
        interruption mid-transfer may leave the temporary sibling path
        behind -- callers/cleanup code must never treat that file as a
        usable package.

        Verifying that the downloaded bytes are actually correct
        (checksum) is explicitly NOT this method's job -- that is
        updater.verify.PackageVerifier.verify_package()'s job, called
        separately by the caller after this method returns successfully.

        Raises PackageDownloadError (or the more specific
        PackageNotFound) on any failure.
        """
        raise NotImplementedError


class FakeUpdateSourceProvider(UpdateSourceProvider):
    """Test double -- never touches the network or the filesystem beyond
    the one destination_path a test gives it. Configured entirely via
    constructor flags so tests can deterministically simulate every
    source-level scenario the future state machine's tests will need:
    success, no update available, the source being unreachable, a missing
    package, an interrupted/partial transfer, and in-transit corruption.

    check_calls/download_calls record every call made, in order, so tests
    can assert exactly what a caller asked for without needing a mocking
    framework.
    """

    def __init__(
        self,
        *,
        manifest: Optional[dict] = None,
        signature: bytes = b"",
        package_bytes: bytes = b"",
        fail_check: bool = False,
        fail_download: bool = False,
        missing_package: bool = False,
        interrupt_after_bytes: Optional[int] = None,
        corrupt: bool = False,
    ):
        """
        manifest=None (the default) simulates "no update available" --
        check_for_manifest() returns None, exactly like a real source
        that has nothing new to offer. Set `manifest` (and usually
        `signature`, `package_bytes`) to simulate an update being
        available.

        fail_check simulates the source being unreachable for
        check_for_manifest() (raises SourceUnavailable).

        fail_download simulates a transfer-level failure for
        download_package() with NO bytes written at all (raises
        PackageDownloadError, destination_path never created, not even a
        temporary sibling).

        missing_package simulates the manifest being valid but the
        package object itself missing at the source (raises
        PackageNotFound, same "nothing written" guarantee as
        fail_download).

        interrupt_after_bytes simulates a transfer that starts but never
        completes: exactly that many bytes of package_bytes are written
        to the temporary sibling path, then PackageDownloadError is
        raised WITHOUT renaming into place -- destination_path itself is
        never created, only the (incomplete) temporary sibling is left
        behind, proving callers cannot mistake it for a real package.

        corrupt simulates in-transit bit corruption: the first byte of
        package_bytes is flipped before writing (or a single corrupt
        byte is written if package_bytes is empty), so the bytes that
        land at destination_path do not match what was configured as the
        "real" package_bytes. This module does not itself detect
        corruption -- that is updater.verify.PackageVerifier.
        verify_package()'s job (Group 2, already built); this flag only
        lets tests produce a corrupted download to feed to it.
        """
        self._manifest = manifest
        self._signature = signature
        self._package_bytes = package_bytes
        self.fail_check = fail_check
        self.fail_download = fail_download
        self.missing_package = missing_package
        self.interrupt_after_bytes = interrupt_after_bytes
        self.corrupt = corrupt
        self.check_calls = []
        self.download_calls = []

    def check_for_manifest(self, current_version: str, target: str, channel: str):
        self.check_calls.append((current_version, target, channel))
        if self.fail_check:
            raise SourceUnavailable("fake source: simulated check failure.")
        if self._manifest is None:
            return None
        return self._manifest, self._signature

    def download_package(self, manifest_dict: dict, destination_path: PathLike) -> None:
        destination_path = Path(destination_path)
        self.download_calls.append((manifest_dict.get("update_id"), destination_path))

        if self.fail_download:
            raise PackageDownloadError("fake source: simulated transfer failure.")
        if self.missing_package:
            raise PackageNotFound(
                f"fake source: package for update_id {manifest_dict.get('update_id')!r} not found."
            )

        payload = self._corrupted_payload() if self.corrupt else self._package_bytes

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")

        if self.interrupt_after_bytes is not None:
            temporary.write_bytes(payload[: self.interrupt_after_bytes])
            raise PackageDownloadError("fake source: simulated interruption mid-transfer.")

        temporary.write_bytes(payload)
        temporary.replace(destination_path)  # atomic; destination_path only ever appears fully-written

    def _corrupted_payload(self) -> bytes:
        if not self._package_bytes:
            return b"\x00"
        corrupted = bytearray(self._package_bytes)
        corrupted[0] ^= 0xFF
        return bytes(corrupted)


def get_configured_source() -> Optional[UpdateSourceProvider]:
    """Production entry point (not implemented in this phase). Always
    returns None -- never raises -- so every caller has one uniform
    fail-closed signal to act on. Real HTTP/S3/CloudFront/AWS-backed
    retrieval of manifests and packages is deferred to RDM-3, which
    creates the actual distribution AWS resources; until then, every real
    call path fails closed by construction, not by coincidence -- exactly
    mirroring app/live_cdn_signing.py's get_configured_signer().
    """
    return None
