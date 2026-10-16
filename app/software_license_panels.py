"""My subscription -> Software License, as three separate panels (VMS 1.2.7, owner request 2026-10-15):

A. My Software License      -- what the account owns: type, cameras, status, account.
B. My Registered Appliances -- each appliance: name, connection, installed version (as reported), registration.
C. Software Downloads       -- approved installers only, with the reminder that a running system
                               never needs a download, and the reinstall warnings from Red's review.

The panels render one data contract (Red's GET /api/customer/software-license; LICENSE_REVIEW.md
2026-10-15). Until Red's backend module ``software_license`` provides ``customer_contract()``,
``contract_for()`` builds the same shape from data this page already reads (read-only).
"""
from __future__ import annotations

import re
from datetime import datetime
from html import escape

UNREPORTED_VERSIONS = {"", "unknown", "0.1.0"}
STALE_VERSION_SECONDS = 24 * 3600
# The served installer has no downgrade guard before 1.2.6 (Red's review): warn against reinstalling it.
NO_DOWNGRADE_GUARD_BEFORE = (1, 2, 6)
LICENSE_LABELS = {
    "per_camera_plan": "VMS software license included with your plan",
    "appliance_included": "VMS software license included with your AnyAiCam appliance",
    "one_time_vms_license": "One-time VMS software license (yours to keep)",
    "legacy_slots": "VMS software license from your earlier camera plan",
    "none": "No VMS software license yet",
}


def strict_version(value) -> tuple[int, int, int] | None:
    """major.minor.patch only (an installed version must be fully reported to be compared)."""
    match = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:\b|$)", str(value or ""))
    return tuple(int(part) for part in match.groups()) if match else None


def version_tuple(value) -> tuple[int, ...] | None:
    match = re.match(r"^\s*v?(\d+)\.(\d+)(?:\.(\d+))?", str(value or ""))
    return tuple(int(part or 0) for part in match.groups()) if match else None


def reported_version(value) -> str | None:
    """The version an appliance reported, or None when it never reported a real one."""
    text = str(value or "").strip()
    return None if text.lower() in UNREPORTED_VERSIONS else text


# ------------------------------------------------------------------ the contract

def contract_for(customer_id: str, *, payload: dict | None = None, role: str = "customer_owner") -> dict:
    try:
        import software_license  # Red's backend (e2eec24): downloads come back [] for a viewer
    except ImportError:
        return _local_contract(customer_id, payload)
    return software_license.customer_contract(customer_id, role=role)


def _local_contract(customer_id: str, payload: dict | None) -> dict:
    import customer_downloads
    import customer_entitlements
    import per_camera_billing as billing
    from partner_db import row, rows
    account = row("SELECT id,name FROM customers WHERE id=?", (customer_id,)) or {}
    owned = max((int(e["camera_slot_quantity"] or 0) for e in customer_entitlements.get_entitlements_for_customer(customer_id)
                 if e["product"] == "vms_license" and e["status"] == "active"), default=0)
    capacity = customer_entitlements.vms_license_capacity(customer_id)
    existing = billing.existing_plan(customer_id)
    appliance_paid = row("SELECT id FROM hardware_orders WHERE customer_id=? AND sku LIKE 'AIC-APPLIANCE-%' AND status='paid' LIMIT 1",
                         (customer_id,))
    if payload:
        license_ = {"kind": "per_camera_plan", "cameras": int(payload["camera_quantity"]),
                    "status": "suspended" if payload.get("status") == "suspended" else "active"}
    elif owned:
        license_ = {"kind": "one_time_vms_license", "cameras": owned, "status": "active"}
    elif appliance_paid and capacity:
        license_ = {"kind": "appliance_included", "cameras": capacity, "status": "active"}
    elif existing and existing["kind"] == "legacy":
        license_ = {"kind": "legacy_slots", "cameras": existing["quantity"], "status": existing["status"]}
    elif capacity:
        # Capacity with no license row to name its source (most likely appliance-included slots):
        # a neutral label until Red's backend says where it comes from (Red review, 2026-10-15).
        license_ = {"kind": "legacy_slots", "cameras": capacity, "status": "active", "label": "VMS software license"}
    else:
        license_ = {"kind": "none", "cameras": 0, "status": "none"}
    license_.setdefault("label", LICENSE_LABELS[license_["kind"]])
    appliances = []
    for a in rows("SELECT a.id,a.cloud_id,a.software_version,a.online_status,a.last_check_in,a.activation_status,s.name AS site_name "
                  "FROM appliances a LEFT JOIN sites s ON s.id=a.site_id WHERE a.customer_id=? ORDER BY a.created_at", (customer_id,)):
        version = reported_version(a.get("software_version"))
        status = str(a.get("activation_status") or "")
        appliances.append({
            "appliance_id": a["id"], "cloud_id": a["cloud_id"], "name": a.get("site_name") or a["cloud_id"],
            "connection": "never" if not a.get("last_check_in") else ("online" if a.get("online_status") in ("online", "degraded") else "offline"),
            "last_check_in": a.get("last_check_in"), "installed_version": version, "version_reported_at": a.get("last_check_in") if version else None,
            "registration": "activated" if status == "activated" else ("deactivated" if status in ("deactivated", "revoked") else "claimed_pending"),
        })
    downloads = []
    release = customer_downloads.latest_vms_installer()
    if release:  # the one published (approved) Linux installer record
        downloads.append({"platform": "linux", "version": str(release["version"]), "commit": str(release["commit"]),
                          "filename": str(release["filename"]), "size_bytes": int(release["size_bytes"]), "sha256": str(release["sha256"]),
                          "signed": False, "min_requirements": "Ubuntu 24.04 (64-bit PC), 4+ CPU cores, 8 GB+ memory, 100 GB free disk",
                          "install_flow": "linux", "url": "/api/customer/downloads/vms-installer"})
    return {"account": {"customer_id": customer_id, "name": account.get("name") or ""}, "license": license_,
            "appliances": appliances, "downloads": downloads,
            "notes": {"installer_older_than_installed": installer_older_than_installed(appliances, downloads)}}


def installer_older_than_installed(appliances: list[dict], downloads: list[dict]) -> bool:
    """True when an appliance reports a newer full version (major.minor.patch) than every offered
    installer. "Not reported" is unknown, never "older" (Red review 2026-10-15)."""
    installed = [strict_version(a.get("installed_version")) for a in appliances]
    offered = [strict_version(d.get("version")) for d in downloads]
    newest_installed = max((v for v in installed if v), default=None)
    newest_offered = max((v for v in offered if v), default=None)
    return bool(newest_installed and newest_offered and newest_offered < newest_installed)


# ------------------------------------------------------------------ the panels

def _ago(stamp) -> str:
    try:
        seconds = (datetime.now() - datetime.fromisoformat(str(stamp)[:26])).total_seconds()
    except (TypeError, ValueError):
        return ""
    if seconds < 120:
        return "just now"
    if seconds < 7200:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def _row(label: str, value: str, row_id: str = "") -> str:
    attr = f' id="{row_id}"' if row_id else ""
    return f'<div class="health-row"{attr}><span>{label}</span><span>{value}</span></div>'


def license_panel(contract: dict) -> str:
    license_ = contract["license"]
    # "pending" (Red, 2026-10-15): a paid appliance order whose license activates when the appliance is set up.
    status = {"active": "Active", "suspended": "Paused (billing problem)", "pending": "Starts when your appliance is set up",
              "none": "—"}.get(license_["status"], escape(str(license_["status"])))
    if license_["cameras"]:
        cameras = f'Up to {int(license_["cameras"])} camera{"" if int(license_["cameras"]) == 1 else "s"}'
    else:
        cameras = "Set when your appliance is set up" if license_["status"] == "pending" else "—"
    body = (_row("License type", escape(license_["label"]), "sl-type") + _row("Licensed cameras", cameras, "sl-cameras")
            + _row("Status", status, "sl-status") + _row("Account", escape(contract["account"].get("name") or "Your AnyAiCam account"), "sl-account"))
    note = ('<p class="health-detail">Your license belongs to your AnyAiCam account. It stays the same when you reinstall '
            'or update the software.</p>' if license_["kind"] != "none" else
            '<p class="health-detail">A VMS software license is included with every AnyAiCam appliance and per-camera plan.</p>')
    return f'<section class="panel" style="margin-top:14px" id="sl-license"><h2>My Software License</h2>{body}{note}</section>'


def appliances_panel(contract: dict) -> str:
    items = contract.get("appliances") or []
    if not items:
        return ('<section class="panel" style="margin-top:14px" id="sl-appliances"><h2>My Registered Appliances</h2>'
                '<p class="health-detail" id="sl-no-appliances">No appliances are registered to this account yet. When your AnyAiCam '
                'appliance arrives, or after you install AnyAiCam on your own PC, link it on <a href="/customer/claim-appliance">'
                'Claim an appliance</a>.</p></section>')
    rows_html = ""
    for item in items:
        connection = {"online": "Online", "offline": f"Offline · last seen {_ago(item.get('last_check_in'))}".rstrip(" ·"),
                      "never": "Not connected yet"}[item["connection"]]
        version = item.get("installed_version")
        if not version:
            version_text = "Not reported"
        else:
            age = _ago(item.get("version_reported_at"))
            try:
                stale = (datetime.now() - datetime.fromisoformat(str(item.get("version_reported_at"))[:26])).total_seconds() > STALE_VERSION_SECONDS
            except (TypeError, ValueError):
                stale = True
            version_text = (f"Last reported VMS {escape(version)} ({age})" if stale else f"VMS {escape(version)} · reported {age}")
        registration = {"activated": "Activated", "claimed_pending": "Waiting to finish activation", "deactivated": "Deactivated"}[item["registration"]]
        rows_html += (f'<div class="sl-appliance" data-cloud-id="{escape(item["cloud_id"], quote=True)}">'
                      f'<h3 style="margin:10px 0 4px">{escape(item["name"])} <span class="health-detail">({escape(item["cloud_id"])})</span></h3>'
                      + _row("Connection", connection) + _row("Installed version", version_text) + _row("Registration", registration) + "</div>")
    return f'<section class="panel" style="margin-top:14px" id="sl-appliances"><h2>My Registered Appliances</h2>{rows_html}</section>'


def downloads_panel(contract: dict, *, steps_html=None) -> str:
    downloads = contract.get("downloads") or []
    running = any(a.get("registration") == "activated" for a in contract.get("appliances") or [])
    reminder = ('<p class="notice" id="sl-no-download-needed"><strong>You don&rsquo;t need to download anything if your AnyAiCam is '
                'already installed and running.</strong> Software updates are installed only when you choose to install them. '
                'Download the installer only to set up a new PC, or if AnyAiCam support asks you to reinstall.</p>')
    if not downloads:
        return (f'<section class="panel" style="margin-top:14px" id="sl-downloads"><h2>Software Downloads</h2>{reminder}'
                '<p class="health-detail" id="sl-no-installer">The installer download will appear here when an approved release is available.</p></section>')
    cards = ""
    for item in downloads:
        os_name = {"linux": "Linux", "windows": "Windows"}.get(item["platform"], escape(item["platform"]))
        warning = ""
        version = version_tuple(item["version"])
        if version and version < NO_DOWNGRADE_GUARD_BEFORE:
            warning = (f'<p class="notice warn" id="sl-downgrade-warning">This is the AnyAiCam {escape(item["version"])} installer. '
                       'If your appliance already runs a newer version, do not reinstall it with this installer.</p>')
        elif contract.get("notes", {}).get("installer_older_than_installed"):
            warning = ('<p class="notice warn" id="sl-downgrade-warning">Your appliance runs a newer version than this installer. '
                       'Do not reinstall it with this installer.</p>')
        size = f'{int(item["size_bytes"]) / (1024 * 1024):.1f} MB'
        steps = steps_html(item) if steps_html else ""
        cards += (f'<div class="sl-download" data-platform="{escape(item["platform"], quote=True)}">'
                  f'<div class="health-row"><span>AnyAiCam VMS {escape(item["version"])} for {os_name}'
                  f'<br><span class="health-detail">{escape(item["min_requirements"])} &middot; {size}</span></span>'
                  f'<a class="ghost-button" href="{escape(item["url"], quote=True)}" download>Download installer</a></div>{warning}'
                  '<details style="margin-top:10px"><summary>Installation and setup instructions</summary>'
                  f'<p class="health-detail" style="margin:8px 0 0">Build {escape(item["commit"][:7])}<br>SHA-256 '
                  f'<code style="word-break:break-all">{escape(item["sha256"])}</code></p>{steps}</details></div>')
    lead = reminder if running else ('<p class="health-detail">Install AnyAiCam on your PC, then link it to this account on '
                                     '<a href="/customer/claim-appliance">Claim an appliance</a>.</p>' + reminder)
    return f'<section class="panel" style="margin-top:14px" id="sl-downloads"><h2>Software Downloads</h2>{lead}{cards}</section>'


def panels_html(customer_id: str, *, is_owner: bool, payload: dict | None, mode: str | None) -> str:
    contract = contract_for(customer_id, payload=payload, role="customer_owner" if is_owner else "customer_viewer")
    html = license_panel(contract) + appliances_panel(contract)
    import customer_entitlements
    if is_owner and customer_entitlements.vms_license_capacity(customer_id) > 0:  # the download route's own eligibility
        import sys
        main = sys.modules.get("main")
        steps = (lambda item: main._installer_steps_html(mode, item["filename"], item["sha256"])) if main and hasattr(main, "_installer_steps_html") else None
        html += downloads_panel(contract, steps_html=steps)
    return html
