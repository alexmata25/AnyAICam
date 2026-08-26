"""RDM-2 (device-side integration, Group 2G): the real UpdateSourceProvider
implementation -- the cloud's authenticated manifest endpoint plus a
direct, unauthenticated GET against whatever presigned S3 URL that
endpoint returns.

This device NEVER receives an AWS credential of any kind, and never
constructs an S3 key or talks to S3 directly for the MANIFEST -- it only
ever calls the existing, already-authenticated PortalClient against the
same cloud API every other endpoint uses (GET /api/appliance/updates/
latest), and then does a plain, unauthenticated HTTP GET against the
presigned URL that response contains (S3's own presigned-URL scheme
carries its own one-time authorization in the URL itself -- this module
does not, and must not, attach the appliance's own portal credential to
that second request).

target/channel are validated against the same safe path-segment grammar
the cloud endpoint/storage helper and the publisher tool independently
enforce (see each module's own _validate_path_segment()) -- there is no
shared import path between this package and app/ or tools/, so the
three copies must be kept in sync by hand; this one exists so a
locally-misconfigured config.update_target/update_channel can never
produce a malformed request even before the cloud gets a chance to
reject it.
"""

import base64
import binascii
import re
import urllib.error
import urllib.request
from pathlib import Path

from ..portal import PortalError
from .source import PackageDownloadError, PackageNotFound, SourceUnavailable, UpdateSourceProvider

# Mirrors app/updates_storage.py's and tools/publish_update.py's own
# _validate_path_segment() -- see each for why this exact grammar.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MiB, streamed -- never loads a whole package into memory
_DOWNLOAD_TIMEOUT_SECONDS = 30


def _validate_path_segment(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise ValueError(
            f"{field_name} must be a non-empty string matching {_SAFE_SEGMENT.pattern!r}."
        )
    if value in (".", ".."):
        raise ValueError(f"{field_name} must not be '.' or '..'.")
    return value


class ManifestSource(UpdateSourceProvider):
    """The real production UpdateSourceProvider (Group 2G). `client` is
    the SAME already-activated PortalClient service.py already uses for
    everything else -- no new authentication mechanism, no new base URL.
    """

    def __init__(self, client):
        self._client = client

    def check_for_manifest(self, current_version: str, target: str, channel: str):
        _validate_path_segment(target, "target")
        _validate_path_segment(channel, "channel")
        try:
            response = self._client.request(
                "GET", f"/api/appliance/updates/latest?target={target}&channel={channel}"
            )
        except PortalError as error:
            raise SourceUnavailable(str(error)) from error
        if not isinstance(response, dict) or response.get("status") == "no_update_available":
            return None
        manifest_dict = response.get("manifest")
        signature_b64 = response.get("signature")
        package_url = response.get("package_url")
        if not isinstance(manifest_dict, dict) or not isinstance(signature_b64, str) or not isinstance(package_url, str):
            raise SourceUnavailable("Update endpoint returned a malformed response.")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise SourceUnavailable(f"Update endpoint returned a malformed signature: {error}") from error
        # package_url travels alongside manifest_dict as an in-memory-only
        # attribute lookup for download_package() -- never persisted,
        # never part of the authenticated Manifest object itself (a
        # presigned URL is short-lived and endpoint-specific, not a
        # durable fact about the version the way sha256/platform are).
        self._last_package_url = package_url
        return manifest_dict, signature

    def download_package(self, manifest_dict: dict, destination_path) -> None:
        package_url = getattr(self, "_last_package_url", None)
        if not package_url:
            raise PackageDownloadError("No presigned package URL is available for this manifest.")
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination_path.with_suffix(destination_path.suffix + ".tmp")
        try:
            request = urllib.request.Request(package_url, method="GET")
            with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
                with open(temporary, "wb") as handle:
                    while True:
                        chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
        except urllib.error.HTTPError as error:
            temporary.unlink(missing_ok=True)
            if error.code == 404:
                raise PackageNotFound(f"Package not found at presigned URL (404): {error}") from error
            raise PackageDownloadError(f"Package download failed with HTTP {error.code}: {error}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise PackageDownloadError(f"Package download failed: {error}") from error
        temporary.replace(destination_path)  # atomic; destination_path only ever appears fully-written


def make_manifest_source(client) -> ManifestSource:
    """Small factory-style constructor, mirroring make_restart_signal()/
    make_health_check()'s own naming convention -- service.py calls this,
    never constructs ManifestSource directly."""
    return ManifestSource(client)
