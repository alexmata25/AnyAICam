"""Customer download of the AnyAiCam VMS installer (2026-10-01).

The one-time VMS software license is bought for a customer-owned PC (DIY) and
included with every AnyAiCam appliance. A customer owner whose account holds
that license downloads the exact, verified installer from My subscription.

- Storage: object_storage's private 'downloads' category. The package and a
  small catalog record (written last) under vms-installer/. Never served by
  the public /storage/ route; only by the signed-in route below.
- Eligibility: a customer owner with a VMS license on record
  (customer_entitlements.vms_license_capacity > 0). Viewers, partners and
  signed-out visitors are refused; another tenant's license never counts.
- Publishing is a deliberate operator step (python -m customer_downloads
  publish ...), run with the package built and verified by
  installer/build_release_installer.py.
- Release catalog (2026-10-15, software_releases.py): the newest APPROVED
  catalog row per platform is what is served. While no Linux row is
  approved, the Linux download keeps using the latest.json record below
  unchanged (compatibility fallback), so nothing changes in production
  until an administrator approves a catalog row. GET
  /api/customer/downloads/{linux|windows} serves a platform;
  /api/customer/downloads/vms-installer stays as the Linux alias.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from object_storage import get_storage

logger = logging.getLogger(__name__)

CATEGORY = "downloads"
LATEST_KEY = "vms-installer/latest.json"
_PACKAGE_NAME = re.compile(r"^anyaicam-appliance-installer-[0-9A-Za-z.]+-vms-[0-9a-f]{12}\.tar\.gz$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def publish_vms_installer(package: Path, *, commit: str, version: str) -> dict:
    """Store one verified installer and point the catalog at it."""
    if not _PACKAGE_NAME.match(package.name):
        raise ValueError("Not an AnyAiCam installer package name.")
    if not _COMMIT.match(commit) or commit[:12] not in package.name:
        raise ValueError("The commit must be the 40-character VMS commit the package was built from.")
    data = package.read_bytes()
    record = {
        "version": version,
        "commit": commit,
        "filename": package.name,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "released_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "key": f"vms-installer/{commit[:12]}/{package.name}",
    }
    storage = get_storage()
    storage.put(CATEGORY, record["key"], data, content_type="application/gzip")
    storage.put(CATEGORY, LATEST_KEY, json.dumps(record).encode(), content_type="application/json")
    return record


def latest_vms_installer() -> dict | None:
    """The published installer record, or None (never an exception)."""
    try:
        record = json.loads(get_storage().get(CATEGORY, LATEST_KEY).decode("utf-8"))
    except Exception:
        return None
    required = ("version", "commit", "filename", "size_bytes", "sha256", "key")
    return record if isinstance(record, dict) and all(record.get(k) for k in required) else None


def download_eligibility(identity: dict | None) -> tuple[bool, str]:
    if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"} or not identity.get("customer_id"):
        return False, "Sign in to your AnyAiCam customer account."
    if identity.get("role") != "customer_owner":
        return False, "Only the account owner can download the AnyAiCam VMS installer."
    from customer_entitlements import vms_license_capacity
    if vms_license_capacity(identity["customer_id"]) <= 0:
        return False, "A VMS software license is required. It is included with every AnyAiCam appliance."
    return True, ""


def human_size(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def current_installer(platform: str = "linux") -> dict | None:
    """The record a licensed owner downloads for a platform: the newest
    approved catalog row, or -- Linux only, and only while the catalog has
    no approved Linux row -- the latest.json record (unchanged behaviour)."""
    from software_releases import latest_approved
    release = latest_approved(platform)
    if release:
        return {"version": release["version"], "commit": release["build_commit"], "filename": release["filename"],
                "size_bytes": int(release["size_bytes"]), "sha256": release["sha256"], "key": release["storage_key"],
                "released_at": release.get("approved_at"), "platform": platform}
    return latest_vms_installer() if platform == "linux" else None


_MEDIA_TYPES = {"linux": "application/gzip", "windows": "application/vnd.microsoft.portable-executable"}


def _serve_installer(identity: dict | None, platform: str):
    eligible, reason = download_eligibility(identity)
    if not eligible:
        raise HTTPException(status_code=403, detail=reason)
    release = current_installer(platform)
    if not release:
        raise HTTPException(status_code=404, detail="The installer is not available yet.")
    logger.info("customer_download.vms_installer customer_id=%s platform=%s commit=%s", identity.get("customer_id"), platform, release["commit"][:12])
    storage = get_storage()
    if type(storage).__name__ == "S3Storage":
        return RedirectResponse(storage.url(CATEGORY, release["key"], expires_seconds=300), status_code=302)
    return Response(
        storage.get(CATEGORY, release["key"]), media_type=_MEDIA_TYPES[platform],
        headers={"Content-Disposition": f'attachment; filename="{release["filename"]}"',
                 "X-Content-SHA256": release["sha256"], "Cache-Control": "no-store"},
    )


def register_customer_download_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    @app.get("/api/customer/downloads")
    def customer_downloads(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get("role") not in {"customer_owner", "customer_viewer"}:
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        eligible, reason = download_eligibility(identity)
        release = current_installer("linux")
        body = {"eligible": eligible, "reason": reason, "available": bool(release)}
        if eligible and release:
            body["vms_installer"] = {key: release[key] for key in ("version", "commit", "filename", "size_bytes", "sha256", "released_at") if key in release}
        return body

    @app.get("/api/customer/downloads/vms-installer")
    def download_vms_installer(request: Request):
        return _serve_installer(partner_identity(request), "linux")

    @app.get("/api/customer/downloads/{platform}")
    def download_platform_installer(request: Request, platform: str):
        if platform not in _MEDIA_TYPES:
            raise HTTPException(status_code=404, detail="Unknown platform.")
        return _serve_installer(partner_identity(request), platform)


def _main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] != "publish":
        print("usage: python -m customer_downloads publish <package.tar.gz> <40-char VMS commit> <version>")
        return 2
    record = publish_vms_installer(Path(argv[2]), commit=argv[3], version=argv[4])
    print(json.dumps({k: record[k] for k in ("version", "commit", "filename", "size_bytes", "sha256", "released_at")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
