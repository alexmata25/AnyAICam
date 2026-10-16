"""My subscription -> Software License: the data behind the section
(owner-approved backend, 2026-10-15; LICENSE_REVIEW.md section 10).

customer_contract(customer_id) / GET /api/customer/software-license returns,
for the signed-in customer's OWN account only:

    account     -- id and name
    license     -- customer_entitlements.vms_license_summary(): the same
                   number the account's appliances receive signed
    appliances  -- the account's registered appliances, with connection,
                   registration and the installed version the appliance
                   itself reported over its authenticated heartbeat
    downloads   -- approved installers (software_releases) for a licensed
                   owner; [] for a viewer or an unlicensed account
    notes       -- installer_older_than_installed: an offered installer is
                   older than a version an appliance reports (do not
                   "reinstall" it over that appliance; LICENSE_REVIEW Q2/Q6)

Installed versions (record_reported_version): written ONLY from the
authenticated appliance heartbeat and POST /api/appliance/version. A label
like "1.2.6+01d1470ceacc" (or structured release_version/build_id fields
from newer agents) is parsed; "0.1.0" (the agent's package fallback),
"Unknown", "Not installed" and "" are not a version. The legacy
appliances.software_version column is still written by those routes for
the pages that read it.

Read-only for the customer: nothing here changes a license, entitlement,
claim, appliance identity, recording or subscription.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request

from partner_db import row, rows

# Same threshold as the admin dashboard's offline sweep (appliance_cloud,
# partner/appliance-dashboard): no check-in for 3 minutes is offline.
ONLINE_THRESHOLD_SECONDS = 180
NOT_A_VERSION = {"", "0.1.0", "unknown", "not installed"}
CUSTOMER_ROLES = ("customer_owner", "customer_viewer")
_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}){1,3}$")
_BUILD = re.compile(r"^[0-9a-f]{7,40}$")
_PLATFORMS = ("linux", "windows")


# ------------------------------------------------------------------ versions

def version_tuple(version) -> tuple | None:
    if not isinstance(version, str) or not _VERSION.match(version):
        return None
    parts = tuple(int(part) for part in version.split("."))
    return parts + (0,) * (4 - len(parts))


def parse_version_label(label) -> tuple[str | None, str | None]:
    """'1.2.6+01d1470ceacc' -> ('1.2.6', '01d1470ceacc'); '1.2.6' -> ('1.2.6', None);
    '0.1.0' / 'Unknown' / '' / anything unparseable -> (None, None)."""
    text = str(label if label is not None else "").strip()
    if text.lower() in NOT_A_VERSION:
        return None, None
    version, _, build = text.partition("+")
    version = version.strip()
    if version.lower() in NOT_A_VERSION or not _VERSION.match(version):
        return None, None
    build = build.strip().lower()
    return version, (build if _BUILD.match(build) else None)


def parse_reported_version(payload: dict) -> tuple[bool, str | None, str | None, str | None]:
    """(reported, version, build, platform) from an appliance payload.
    reported is False when the payload carries no version at all (an older
    payload shape): the stored values are then left as they are."""
    if not isinstance(payload, dict):
        return False, None, None, None
    platform = str(payload.get("platform") or "").strip().lower()
    platform = platform if platform in _PLATFORMS else None
    structured = payload.get("release_version")
    if isinstance(structured, str) and structured.strip():
        version, _ = parse_version_label(structured)
        build = str(payload.get("build_id") or "").strip().lower()
        return True, version, (build if version and _BUILD.match(build) else None), platform
    if "software_version" not in payload and "release_version" not in payload:
        return False, None, None, platform
    version, build = parse_version_label(payload.get("software_version"))
    return True, version, build, platform


def record_reported_version(db, appliance_id: str, payload: dict, now: str) -> None:
    """Called from the AUTHENTICATED appliance routes only, inside their own
    connection. A usable report sets version/build/time; an unusable one
    (0.1.0, Unknown, ...) clears version/build so nothing stale is shown."""
    reported, version, build, platform = parse_reported_version(payload)
    if not reported:
        if platform:
            db.execute("UPDATE appliances SET reported_platform=? WHERE id=?", (platform, appliance_id))
        return
    db.execute("UPDATE appliances SET reported_version=?,reported_commit=?,reported_platform=COALESCE(?,reported_platform),"
               "version_reported_at=? WHERE id=?", (version, build, platform, now, appliance_id))


def installed_version_of(appliance: dict) -> str | None:
    """The appliance's installed release version, or None when unknown.
    Appliances that have not reported since the reported_* columns existed
    fall back to their legacy heartbeat label, accepted only in the strict
    '<version>+<build>' form the release marker produces."""
    if appliance.get("version_reported_at"):
        version = appliance.get("reported_version")
        return version if version_tuple(version) else None
    from software_update import split_release_label
    version, _build = split_release_label(str(appliance.get("software_version") or ""))
    return version or None


# ------------------------------------------------------------------ appliances

def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:  # the cloud writes naive local time; compare like with like
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def connection_state(appliance: dict, *, now: datetime | None = None) -> str:
    if appliance.get("state") == "revoked" or appliance.get("online_status") == "revoked":
        return "offline" if appliance.get("last_check_in") else "never"
    seen = _parse_time(appliance.get("last_check_in"))
    if seen is None:
        return "offline" if appliance.get("last_check_in") else "never"
    age = ((now or datetime.now()) - seen).total_seconds()
    return "online" if age <= ONLINE_THRESHOLD_SECONDS else "offline"


def registration_state(appliance: dict) -> str:
    if appliance.get("state") == "revoked" or appliance.get("activation_status") == "released":
        return "deactivated"
    credentials = rows("SELECT created_at,last_used_at FROM appliance_credentials WHERE appliance_id=? AND revoked_at IS NULL",
                       (appliance["id"],))
    if not credentials:
        if appliance.get("activation_status") in ("activated", "linked") or appliance.get("credential_revoked_at"):
            return "deactivated"  # it had a credential; every one is revoked
        return "claimed_pending"
    import appliance_readiness
    if appliance_readiness.enrollment_ready(appliance["id"]) or appliance.get("activation_status") in ("activated", "linked"):
        return "activated"
    return "claimed_pending"


def customer_appliances(customer_id: str) -> list[dict]:
    """The registered appliances of THIS customer only."""
    found = rows("SELECT a.*, s.name AS site_name FROM appliances a LEFT JOIN sites s ON s.id=a.site_id "
                 "WHERE a.customer_id=? ORDER BY a.created_at, a.cloud_id", (customer_id,))
    now = datetime.now()
    result = []
    for item in found:
        site = str(item.get("site_name") or "").strip()
        result.append({
            "appliance_id": item["id"], "cloud_id": item["cloud_id"],
            "name": f"AnyAiCam appliance · {site}" if site else "AnyAiCam appliance",
            "connection": connection_state(item, now=now), "last_check_in": item.get("last_check_in") or None,
            "installed_version": installed_version_of(item),
            "version_reported_at": item.get("version_reported_at") or None,
            "registration": registration_state(item),
            "platform": item.get("reported_platform") or None,
        })
    return result


# ------------------------------------------------------------------ contract

def installer_older_than_installed(downloads: list[dict], appliances: list[dict]) -> bool:
    """True when an offered installer is older than a version an appliance
    reports for the same platform (or an appliance whose platform is not
    reported). Unknown versions are never 'older'."""
    for download in downloads:
        offered = version_tuple(download.get("version"))
        if offered is None:
            continue
        for appliance in appliances:
            installed = version_tuple(appliance.get("installed_version"))
            platform = appliance.get("platform")
            if installed is not None and (platform in (None, download["platform"])) and offered < installed:
                return True
    return False


def customer_contract(customer_id: str, *, role: str = "customer_owner") -> dict:
    """The Software License section for one account. ``role`` is the
    caller's role on that account; downloads are listed only for a licensed
    owner (the same rule as customer_downloads.download_eligibility)."""
    import customer_entitlements
    import software_releases
    customer = row("SELECT id,name FROM customers WHERE id=?", (customer_id,)) or {}
    summary = customer_entitlements.vms_license_summary(customer_id)
    appliances = customer_appliances(customer_id)
    downloads = software_releases.offered_downloads() if role == "customer_owner" and summary["cameras"] > 0 else []
    return {
        "account": {"customer_id": customer_id, "name": customer.get("name") or ""},
        "license": {key: summary[key] for key in ("kind", "cameras", "status", "label", "usable_cameras")},
        "appliances": [{key: value for key, value in item.items() if key != "platform"} for item in appliances],
        "downloads": downloads,
        "notes": {"installer_older_than_installed": installer_older_than_installed(downloads, appliances)},
    }


def register_software_license_routes(app: FastAPI) -> None:
    from partner_portal import partner_identity

    @app.get("/api/customer/software-license")
    def customer_software_license(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity:
            raise HTTPException(status_code=401, detail="Authentication required.")
        if identity.get("role") not in CUSTOMER_ROLES or not identity.get("customer_id"):
            raise HTTPException(status_code=403, detail="Customer Portal sign-in required.")
        # Always the caller's own account: there is no customer_id parameter.
        return customer_contract(identity["customer_id"], role=identity["role"])
