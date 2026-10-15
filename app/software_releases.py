"""Release catalog for the AnyAiCam VMS installers customers download
(owner-approved "Software License" backend, 2026-10-15).

Before this module the customer download was one mutable pointer
(customer_downloads.LATEST_KEY, ``downloads/vms-installer/latest.json``)
with no history, no approval state, no platform and no signature field.
This catalog keeps one row per built installer:

    draft  --approve-->  approved  --withdraw-->  withdrawn
      \\-----------------withdraw-------------------^

* Only an ``approved`` row is ever shown or served to a customer. A draft
  (e.g. a 1.2.7 build that has not been approved yet) and a withdrawn row
  never are.
* Windows rows are served only when ``signed`` is true; the unsigned public
  Windows 1.3.0 build (aedecb6) is refused outright (NEVER_LIST).
* The installer bytes live in object storage (the private 'downloads'
  category, key ``software-releases/<platform>/<commit12>/<filename>``).
  Creating a draft re-verifies the SHA-256 and size of the local file (or,
  without a local file, of the stored object); approving re-hashes the
  stored object again before the state changes, streaming it in chunks.
* Every draft/approve/withdraw writes an audit_logs row.
* Admin API: global administrators only (website_partner._require_global_admin).
  Operator CLI: ``python -m software_releases draft|approve|withdraw|list``.

Compatibility: while the catalog has NO approved Linux row, the Linux
download keeps using customer_downloads.latest_vms_installer() (the
latest.json pointer) exactly as before, so production behaviour does not
change until an administrator approves a catalog row. Approving the first
Linux row switches the Linux download to the catalog; withdrawing every
approved Linux row switches it back to latest.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from object_storage import safe_key
from partner_db import audit, connection, row, rows

CATEGORY = "downloads"
KEY_PREFIX = "software-releases"
PLATFORMS = ("linux", "windows")
STATUSES = ("draft", "approved", "withdrawn")
INSTALL_FLOWS = ("terminal_claim", "desktop_link", "windows_setup")
DEFAULT_MIN_REQUIREMENTS = {
    "linux": "Ubuntu 24.04 (64-bit PC, 4+ CPU cores, 8 GB+ memory, 100 GB free disk)",
    "windows": "Windows 10/11 (64-bit), administrator rights",
}
# Builds that must never be listed, whatever a row says: the unsigned public
# Windows 1.3.0 (owner rule, LICENSE_REVIEW Q9).
NEVER_LIST = (("windows", "1.3.0", "aedecb6"),)

_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAMES = {
    "linux": re.compile(r"^anyaicam-appliance-installer-(?P<version>[0-9A-Za-z.]+)-vms-(?P<short>[0-9a-f]{12})\.tar\.gz$"),
    "windows": re.compile(r"^AnyAiCam-VMS-Setup-(?P<version>[0-9A-Za-z.]+)-(?P<short>[0-9a-f]{7,12})\.exe$"),
}
_CONTENT_TYPES = {"linux": "application/gzip", "windows": "application/vnd.microsoft.portable-executable"}
_CHUNK = 1024 * 1024
MAX_TEXT = 300


class ReleaseError(ValueError):
    """A release request that must be refused (the message is safe to show)."""


def _now() -> str:
    return datetime.now().isoformat()


def version_tuple(version) -> tuple | None:
    if not isinstance(version, str) or not _VERSION.match(version):
        return None
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (4 - len(parts))


def storage_key(platform: str, commit: str, filename: str) -> str:
    return f"{KEY_PREFIX}/{platform}/{commit[:12]}/{filename}"


def default_install_flow(platform: str, version: str) -> str:
    if platform == "windows":
        return "windows_setup"
    parsed = version_tuple(version)
    # 1.2.6 introduced the desktop "Link this appliance" page; earlier Linux
    # packages link with the terminal claim (LICENSE_REVIEW Q7).
    return "desktop_link" if parsed and parsed >= (1, 2, 6, 0) else "terminal_claim"


def never_listed(platform: str, version: str, commit: str) -> bool:
    return any(platform == p and version == v and str(commit or "").startswith(c) for p, v, c in NEVER_LIST)


def _validate(*, platform, version, commit, filename, sha256=None, size_bytes=None, install_flow=None, signed=False) -> None:
    if platform not in PLATFORMS:
        raise ReleaseError("platform must be linux or windows.")
    if not _VERSION.match(str(version or "")):
        raise ReleaseError("version must look like 1.2.7.")
    if not _COMMIT.match(str(commit or "")):
        raise ReleaseError("commit must be the full 40-character commit the installer was built from.")
    match = _NAMES[platform].match(str(filename or ""))
    if not match or not commit.startswith(match.group("short")) or match.group("version") != version:
        raise ReleaseError("The file name is not an AnyAiCam installer name for this platform, version and commit.")
    if sha256 is not None and not _SHA256.match(str(sha256)):
        raise ReleaseError("sha256 must be 64 lowercase hex characters.")
    if size_bytes is not None and (not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0):
        raise ReleaseError("size_bytes must be a positive whole number.")
    if install_flow is not None and install_flow not in INSTALL_FLOWS:
        raise ReleaseError("install_flow must be one of " + ", ".join(INSTALL_FLOWS) + ".")
    if never_listed(platform, version, commit):
        raise ReleaseError("This build may never be offered to customers.")
    if platform == "windows" and not signed:
        raise ReleaseError("A Windows installer must be digitally signed before it can be catalogued.")


def get_storage():
    """The downloads storage -- the same backend customer_downloads serves
    from (one place to stub in tests)."""
    import customer_downloads
    return customer_downloads.get_storage()


def _digest_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _digest_stored(key: str) -> tuple[str, int] | None:
    """SHA-256 and size of a stored object, streamed; None when it is missing."""
    storage = get_storage()
    try:
        if hasattr(storage, "path"):  # LocalStorage
            path = storage.path(CATEGORY, key)
            return _digest_file(path) if path.is_file() else None
        if hasattr(storage, "client") and hasattr(storage, "bucket"):  # S3Storage: stream, never buffer ~900 MB
            body = storage.client.get_object(Bucket=storage.bucket, Key=safe_key(CATEGORY, key))["Body"]
            digest, size = hashlib.sha256(), 0
            for chunk in iter(lambda: body.read(_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
            return digest.hexdigest(), size
        data = storage.get(CATEGORY, key)
        return hashlib.sha256(data).hexdigest(), len(data)
    except Exception:
        return None


def _upload(path: Path, key: str, platform: str) -> None:
    storage = get_storage()
    if hasattr(storage, "client") and hasattr(storage, "bucket"):
        storage.client.upload_file(str(path), storage.bucket, safe_key(CATEGORY, key),
                                   ExtraArgs={"ContentType": _CONTENT_TYPES[platform]})
    else:
        storage.put(CATEGORY, key, path.read_bytes(), content_type=_CONTENT_TYPES[platform])


def _public(record: dict | None) -> dict | None:
    if not record:
        return None
    result = dict(record)
    result["signed"] = bool(result.get("signed"))
    return result


def get_release(release_id: str) -> dict | None:
    return _public(row("SELECT * FROM software_releases WHERE id=?", (release_id,)))


def list_releases(platform: str | None = None) -> list[dict]:
    if platform:
        found = rows("SELECT * FROM software_releases WHERE platform=? ORDER BY created_at DESC", (platform,))
    else:
        found = rows("SELECT * FROM software_releases ORDER BY created_at DESC")
    return [_public(item) for item in found]


def create_draft(*, platform: str, version: str, commit: str, actor: dict, package: Path | None = None,
                 filename: str | None = None, sha256: str | None = None, size_bytes: int | None = None,
                 signed: bool = False, signer_subject: str | None = None, min_requirements: str | None = None,
                 install_flow: str | None = None, notes: str | None = None) -> dict:
    """Records a verified installer as a DRAFT (never shown to customers).

    With ``package`` (a local file): the file is hashed, compared with any
    expected sha256/size_bytes given, then stored. Without it: the object
    must already be stored at storage_key() and its hash/size must match."""
    signed = bool(signed)
    if package is not None:
        package = Path(package)
        filename = package.name
    _validate(platform=platform, version=version, commit=commit, filename=filename, sha256=sha256,
              size_bytes=size_bytes, install_flow=install_flow, signed=signed)
    key = storage_key(platform, commit, filename)
    if package is not None:
        if not package.is_file():
            raise ReleaseError("The package file does not exist.")
        actual_sha, actual_size = _digest_file(package)
        if (sha256 and sha256 != actual_sha) or (size_bytes and size_bytes != actual_size):
            raise ReleaseError("The package does not match the expected SHA-256/size; nothing was stored.")
        sha256, size_bytes = actual_sha, actual_size
    else:
        if not sha256 or not size_bytes:
            raise ReleaseError("sha256 and size_bytes are required when no package file is given.")
        stored = _digest_stored(key)
        if stored is None:
            raise ReleaseError("The installer is not in storage at " + key + ".")
        if stored != (sha256, size_bytes):
            raise ReleaseError("The stored installer does not match the given SHA-256/size.")
    if row("SELECT id FROM software_releases WHERE platform=? AND version=? AND build_commit=?", (platform, version, commit)):
        raise ReleaseError("This platform, version and commit is already in the catalog.")
    if package is not None:
        _upload(package, key, platform)
    release_id = uuid.uuid4().hex
    now = _now()
    requirements = (min_requirements or DEFAULT_MIN_REQUIREMENTS[platform])[:MAX_TEXT]
    flow = install_flow or default_install_flow(platform, version)
    with connection() as db:
        db.execute(
            "INSERT INTO software_releases(id,product,platform,arch,version,build_commit,filename,size_bytes,sha256,storage_key,"
            "signed,signer_subject,min_requirements,install_flow,status,created_by,created_at,notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (release_id, "vms", platform, "x86_64", version, commit, filename, size_bytes, sha256, key, 1 if signed else 0,
             (signer_subject or None) and str(signer_subject)[:MAX_TEXT], requirements, flow, "draft",
             actor.get("email") or "unknown", now, (notes or None) and str(notes)[:MAX_TEXT]),
        )
    audit(actor, "software_release.drafted", "software_release", release_id,
          {"platform": platform, "version": version, "commit": commit, "sha256": sha256, "size_bytes": size_bytes, "signed": signed})
    return get_release(release_id)


def approve(release_id: str, *, actor: dict) -> dict:
    """draft -> approved, after re-hashing the stored object."""
    release = get_release(release_id)
    if not release:
        raise LookupError("No such release.")
    if release["status"] != "draft":
        raise ReleaseError(f"Only a draft can be approved (this release is {release['status']}).")
    _validate(platform=release["platform"], version=release["version"], commit=release["build_commit"],
              filename=release["filename"], sha256=release["sha256"], size_bytes=int(release["size_bytes"]),
              install_flow=release["install_flow"], signed=release["signed"])
    stored = _digest_stored(release["storage_key"])
    if stored != (release["sha256"], int(release["size_bytes"])):
        audit(actor, "software_release.approve_refused", "software_release", release_id, {"reason": "stored_object_mismatch"})
        raise ReleaseError("The stored installer is missing or no longer matches its recorded SHA-256/size; not approved.")
    now = _now()
    with connection() as db:
        changed = db.execute("UPDATE software_releases SET status='approved',approved_by=?,approved_at=? WHERE id=? AND status='draft'",
                             (actor.get("email") or "unknown", now, release_id)).rowcount
    if not changed:
        raise ReleaseError("The release changed while it was being approved.")
    audit(actor, "software_release.approved", "software_release", release_id,
          {"platform": release["platform"], "version": release["version"], "commit": release["build_commit"], "sha256": release["sha256"]})
    return get_release(release_id)


def withdraw(release_id: str, *, actor: dict, reason: str = "") -> dict:
    """draft/approved -> withdrawn (final; the stored object is kept)."""
    release = get_release(release_id)
    if not release:
        raise LookupError("No such release.")
    if release["status"] == "withdrawn":
        raise ReleaseError("This release is already withdrawn.")
    with connection() as db:
        db.execute("UPDATE software_releases SET status='withdrawn',withdrawn_by=?,withdrawn_at=?,withdraw_reason=? WHERE id=?",
                   (actor.get("email") or "unknown", _now(), str(reason or "")[:MAX_TEXT] or None, release_id))
    audit(actor, "software_release.withdrawn", "software_release", release_id,
          {"previous_status": release["status"], "reason": str(reason or "")[:MAX_TEXT]})
    return get_release(release_id)


def latest_approved(platform: str) -> dict | None:
    """The newest APPROVED release for a platform that may be offered, or None."""
    try:
        candidates = rows("SELECT * FROM software_releases WHERE platform=? AND status='approved'", (platform,))
    except Exception:  # catalog table not migrated yet
        return None
    offerable = [item for item in candidates
                 if version_tuple(item.get("version")) is not None
                 and not never_listed(item["platform"], item["version"], item["build_commit"])
                 and (platform != "windows" or bool(item.get("signed")))]
    if not offerable:
        return None
    offerable.sort(key=lambda item: (version_tuple(item["version"]), str(item.get("approved_at") or "")))
    return _public(offerable[-1])


def _fallback_linux() -> dict | None:
    """The pre-catalog latest.json record (customer_downloads), shaped like a
    catalog row. Used ONLY while no Linux row is approved."""
    import customer_downloads
    record = customer_downloads.latest_vms_installer()
    if not record:
        return None
    version = str(record.get("version") or "")
    return {"id": None, "platform": "linux", "version": version, "build_commit": record["commit"],
            "filename": record["filename"], "size_bytes": int(record["size_bytes"]), "sha256": record["sha256"],
            "storage_key": record["key"], "signed": False, "min_requirements": DEFAULT_MIN_REQUIREMENTS["linux"],
            "install_flow": "terminal_claim" if version_tuple(version) is None else default_install_flow("linux", version),
            "status": "approved", "source": "latest_json", "released_at": record.get("released_at")}


def current_release(platform: str) -> dict | None:
    """What a licensed owner downloads for a platform right now."""
    if platform not in PLATFORMS:
        return None
    release = latest_approved(platform)
    if release:
        release["source"] = "catalog"
        return release
    return _fallback_linux() if platform == "linux" else None


def download_entry(release: dict) -> dict:
    """One item of the customer contract's ``downloads`` list."""
    return {"platform": release["platform"], "version": release["version"], "commit": release["build_commit"],
            "filename": release["filename"], "size_bytes": int(release["size_bytes"]), "sha256": release["sha256"],
            "signed": bool(release.get("signed")), "min_requirements": release.get("min_requirements") or "",
            "install_flow": release.get("install_flow") or "", "url": f"/api/customer/downloads/{release['platform']}"}


def offered_downloads() -> list[dict]:
    return [download_entry(release) for release in (current_release(platform) for platform in PLATFORMS) if release]


# ------------------------------------------------------------------ admin API

def register_software_release_routes(app: FastAPI) -> None:
    from website_partner import _require_global_admin

    def _actor(identity: dict) -> dict:
        return {"email": identity.get("email"), "role": "administrator"}

    def _refuse(error: Exception):
        if isinstance(error, LookupError):
            raise HTTPException(status_code=404, detail=str(error)) from error
        raise HTTPException(status_code=409 if "already" in str(error) or "Only a draft" in str(error) else 400,
                            detail=str(error)) from error

    @app.get("/api/admin/software-releases")
    def admin_list_releases(request: Request, platform: str = "") -> dict:
        _require_global_admin(request)
        return {"releases": list_releases(platform or None),
                "offered": {name: (current_release(name) or {}).get("version") for name in PLATFORMS}}

    @app.post("/api/admin/software-releases")
    def admin_create_release(request: Request, payload: dict) -> dict:
        identity = _require_global_admin(request)
        size = payload.get("size_bytes")
        try:
            return create_draft(
                platform=str(payload.get("platform") or ""), version=str(payload.get("version") or ""),
                commit=str(payload.get("commit") or ""), filename=str(payload.get("filename") or ""),
                sha256=str(payload.get("sha256") or "") or None, size_bytes=size if isinstance(size, int) else None,
                signed=payload.get("signed") is True, signer_subject=payload.get("signer_subject"),
                min_requirements=payload.get("min_requirements"), install_flow=payload.get("install_flow"),
                notes=payload.get("notes"), actor=_actor(identity))
        except (ReleaseError, LookupError) as error:
            _refuse(error)

    @app.post("/api/admin/software-releases/{release_id}/approve")
    def admin_approve_release(request: Request, release_id: str) -> dict:
        identity = _require_global_admin(request)
        try:
            return approve(release_id, actor=_actor(identity))
        except (ReleaseError, LookupError) as error:
            _refuse(error)

    @app.post("/api/admin/software-releases/{release_id}/withdraw")
    def admin_withdraw_release(request: Request, release_id: str, payload: dict | None = None) -> dict:
        identity = _require_global_admin(request)
        try:
            return withdraw(release_id, actor=_actor(identity), reason=str((payload or {}).get("reason") or ""))
        except (ReleaseError, LookupError) as error:
            _refuse(error)


# ------------------------------------------------------------------ operator CLI

def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m software_releases")
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="store a verified installer as a draft")
    draft.add_argument("platform", choices=PLATFORMS)
    draft.add_argument("package", type=Path)
    draft.add_argument("commit")
    draft.add_argument("version")
    draft.add_argument("--actor", required=True, help="operator email recorded in the audit log")
    draft.add_argument("--sha256", help="expected SHA-256 from the build manifest (re-verified)")
    draft.add_argument("--size-bytes", type=int, help="expected size from the build manifest (re-verified)")
    draft.add_argument("--signed", action="store_true", help="the Windows installer is Authenticode-signed and verified")
    draft.add_argument("--signer-subject")
    draft.add_argument("--min-requirements")
    draft.add_argument("--install-flow", choices=INSTALL_FLOWS)
    for name in ("approve", "withdraw"):
        command = sub.add_parser(name)
        command.add_argument("release_id")
        command.add_argument("--actor", required=True)
        if name == "withdraw":
            command.add_argument("--reason", default="")
    sub.add_parser("list")
    args = parser.parse_args(argv[1:])
    try:
        if args.command == "list":
            print(json.dumps(list_releases(), indent=2))
            return 0
        actor = {"email": args.actor, "role": "operator_cli"}
        if args.command == "draft":
            result = create_draft(platform=args.platform, version=args.version, commit=args.commit, actor=actor,
                                  package=args.package, sha256=args.sha256, size_bytes=args.size_bytes, signed=args.signed,
                                  signer_subject=args.signer_subject, min_requirements=args.min_requirements,
                                  install_flow=args.install_flow)
        elif args.command == "approve":
            result = approve(args.release_id, actor=actor)
        else:
            result = withdraw(args.release_id, actor=actor, reason=args.reason)
    except (ReleaseError, LookupError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps({k: result[k] for k in ("id", "platform", "version", "build_commit", "filename", "size_bytes", "sha256", "status")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
