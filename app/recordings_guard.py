"""What the /recordings static mount may serve (2026-10-01 security fix).

/recordings is a StaticFiles mount of the whole /app/recordings directory,
and its only gate was "signed in". That directory also holds the
application's own data: audit_log.jsonl, billing_invoices.json,
license_state.json, the email-preview mailbox (password-reset and
invitation links), enrolled face crops (aac_faces), database backups,
object storage (storage/ -- including the licensed customer installer) and
more. Any signed-in customer of any account could fetch those files by name.

On the cloud the mount serves nothing at all (see recordings_path_allowed).
On an appliance it serves only the recorded media the app itself links to --
camera<N>/ recordings, media/ (motion, AI and snapshot images) and clips/
(event clips) -- and only media file types. Everything else is a 404, the
same answer as a file that does not exist. Per-tenant authorization of the
media itself is unchanged (separate, existing concern).
"""
from __future__ import annotations

import os
import re

from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

_TOP_LEVEL = re.compile(r"^(camera\d+|media|clips)$")
MEDIA_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".m3u8", ".ts", ".m4s",
                              ".jpg", ".jpeg", ".png", ".webp"})


def cloud_runtime() -> bool:
    return os.environ.get("ANYAICAM_RUNTIME_ROLE", "edge").strip().lower() == "cloud"


def recordings_path_allowed(path: str) -> bool:
    """`path` is the mount-relative path (either separator).

    On the cloud nothing is served: the cloud keeps no customer media under
    /app/recordings (it lives in S3 / on the customer's appliance and is
    reached only through tenant-checked routes such as
    /api/customer/events/{camera}/{event}/media/url), and a path like
    camera1/... says nothing about which customer it belongs to, so serving
    it would let one customer guess another's file. On an appliance (one
    customer) recorded media stays available to the local pages."""
    if cloud_runtime():
        return False
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) < 2 or any(p == ".." or p.startswith(".") for p in parts):
        return False
    if not _TOP_LEVEL.match(parts[0]):
        return False
    name = parts[-1].lower()
    dot = name.rfind(".")
    return dot > 0 and name[dot:] in MEDIA_EXTENSIONS


# Signed-in responses must never be kept by a shared cache: Cloudflare caches
# by file extension (.mp4, .jpg, .tar.gz ...) and would otherwise serve one
# person's signed-in response to anyone who asks for the same URL -- seen
# live on staging (cf-cache-status: HIT, signed out) on 2026-10-01.
PRIVATE_NO_STORE = {"Cache-Control": "private, no-store", "CDN-Cache-Control": "no-store"}


class RecordingsStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        if not recordings_path_allowed(path):
            raise HTTPException(status_code=404, headers=dict(PRIVATE_NO_STORE))
        response = await super().get_response(path, scope)
        response.headers.update(PRIVATE_NO_STORE)
        return response
