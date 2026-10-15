"""Owner-only AWS cost and storage reporting.

The dashboard reads persisted, read-only AWS Cost Explorer and S3 Inventory
snapshots. It deliberately does not call ListObjects from a browser request or
store AWS credentials. Collection is a separate, opt-in CLI task.
"""
from __future__ import annotations

import json
import math
import os
from datetime import timedelta
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from database_backend import connect


GB_BYTES = 1_000_000_000  # AWS S3 billing uses decimal GB, not GiB.
S3_STANDARD_US_EAST_1_USD_PER_GB_MONTH = Decimal("0.023")
MONTH_DAYS = Decimal("30")


def _configured_owner_email() -> str:
    return str(os.getenv("ANYAICAM_PLATFORM_OWNER_EMAIL") or "").strip().casefold()


def owner_identity(request: Request) -> dict | None:
    """Return a live-confirmed, designated platform-owner identity only.

    A generic local Admin Portal administrator or partner-scoped administrator
    is insufficient. The deployment must opt in with its existing designated
    platform-owner setting and the account must still have a live global grant.
    """
    expected = _configured_owner_email()
    if not expected:
        return None
    try:
        from partner_portal import partner_identity
        identity = partner_identity(request)
        if not identity or str(identity.get("role") or "").casefold() != "administrator":
            return None
        email = str(identity.get("email") or "").strip().casefold()
        if not email or email != expected:
            return None
        from appliance_identity import has_global_administrator_grant
        from partner_db import connection
        with connection() as db:
            if not has_global_administrator_grant(db, email=email):
                return None
        return identity
    except Exception:
        # Authorization lookup failures fail closed.
        return None


def require_owner(request: Request) -> dict:
    identity = owner_identity(request)
    if not identity:
        raise HTTPException(status_code=403, detail="Platform owner access is required.")
    return identity


def owner_dashboard_entry(request: Request) -> str:
    """Small link for the existing Admin Portal; no customer-site changes."""
    if not owner_identity(request):
        return ""
    return (
        '<article class="admin-command-card"><h2>AWS Cost Dashboard</h2>'
        '<p>Owner-only view of account costs, customer storage estimates, and retention evidence.</p>'
        '<a class="action-button" href="/admin/aws-costs">Open cost dashboard</a></article>'
    )


def parse_amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Cost amount must be numeric.")
    if not amount.is_finite():
        raise ValueError("Cost amount must be finite.")
    return amount


def resolve_recording_context(row: Mapping) -> dict:
    """Resolve current camera mode/retention from the VMS billing and policy sources."""
    paid_active = str(row.get("paid_plan_status") or "").casefold() == "active"
    paid_plan = str(row.get("paid_plan_key") or "").casefold() if paid_active else ""
    legacy_mode = str(row.get("legacy_recording_mode") or "").strip().casefold()
    camera_cloud_mode = str(row.get("cloud_recording_mode") or "").strip().casefold()
    if paid_plan == "hybrid":
        recording_mode = "Hybrid"
    elif paid_plan in {"basic_local", "ai_local"}:
        recording_mode = "Local"
    elif legacy_mode == "motion" or camera_cloud_mode == "motion":
        recording_mode = "Hybrid"
    elif camera_cloud_mode in {"continuous", "cloud", "cloud_continuous"}:
        recording_mode = "Cloud continuous"
    elif row.get("local_recording_mode"):
        recording_mode = "Local"
    else:
        recording_mode = None

    retention_days = None
    # The per-camera cloud mode is also an operational signal for existing
    # installations whose billing row is represented by the legacy schema.
    # This resolves configured retention only; it never grants or asserts a
    # purchased entitlement. A currently active local plan takes precedence.
    hybrid_retention_applies = recording_mode == "Hybrid" and (
        (paid_plan == "hybrid" and bool(row.get("cloud_event_storage")))
        or (not paid_active and (legacy_mode == "motion" or camera_cloud_mode == "motion"))
    )
    if hybrid_retention_applies:
        candidate = row.get("policy_retention_days")
        if candidate is None and paid_plan == "hybrid":
            candidate = row.get("cloud_event_retention_days") or 14
        if candidate is None and legacy_mode == "motion":
            candidate = row.get("legacy_retention_days")
        try:
            retention_days = int(candidate) if candidate is not None else None
        except (TypeError, ValueError, OverflowError):
            retention_days = None
    return {"recording_mode": recording_mode, "retention_days": retention_days}


def estimate_storage_cost(bytes_by_class: Mapping[str, int], rates: Mapping[str, Decimal] | None = None) -> dict:
    """Estimate a month of storage at current snapshot size.

    Classes without a supplied rate remain unpriced; they are never silently
    treated as zero cost. Rates are USD per decimal GB-month.
    """
    rates = rates or {"STANDARD": S3_STANDARD_US_EAST_1_USD_PER_GB_MONTH}
    total_bytes = 0
    total_cost = Decimal("0")
    unknown: list[str] = []
    class_costs: dict[str, float | None] = {}
    for raw_class, raw_bytes in bytes_by_class.items():
        storage_class = str(raw_class or "STANDARD").strip().upper()
        try:
            size = int(raw_bytes)
        except (TypeError, ValueError, OverflowError):
            unknown.append(storage_class)
            class_costs[storage_class] = None
            continue
        if size < 0:
            raise ValueError("Stored bytes cannot be negative.")
        total_bytes += size
        rate = rates.get(storage_class)
        if rate is None:
            unknown.append(storage_class)
            class_costs[storage_class] = None
            continue
        cost = Decimal(size) / Decimal(GB_BYTES) * parse_amount(rate)
        total_cost += cost
        class_costs[storage_class] = float(cost.quantize(Decimal("0.000001")))
    return {
        "bytes": total_bytes,
        "decimal_gb": total_bytes / GB_BYTES,
        "monthly_cost_usd": None if unknown else float(total_cost.quantize(Decimal("0.000001"))),
        "class_costs_usd": class_costs,
        "unpriced_storage_classes": sorted(set(unknown)),
        "estimate": True,
        "rate_basis": "S3 Standard us-east-1 list rate; snapshot-size estimate, not billed GB-months",
    }


def calculate_projection(
    camera_count: int,
    retention_days: int,
    gb_per_camera_day: float,
    storage_usd_per_gb_month: float,
    put_requests_per_camera_day: int | None = None,
    usd_per_1000_puts: float | None = None,
    get_requests_per_camera_day: int | None = None,
    usd_per_1000_gets: float | None = None,
) -> dict:
    if isinstance(camera_count, bool) or int(camera_count) != camera_count or camera_count < 1 or camera_count > 100_000:
        raise ValueError("Camera count must be an integer from 1 to 100,000.")
    if retention_days not in (7, 14, 30):
        raise ValueError("Retention must be 7, 14, or 30 days.")
    values = [gb_per_camera_day, storage_usd_per_gb_month]
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in values):
        raise ValueError("Storage inputs must be finite and non-negative.")
    retained_gb = Decimal(str(gb_per_camera_day)) * retention_days * int(camera_count)
    storage_cost = retained_gb * Decimal(str(storage_usd_per_gb_month))
    request_cost: Decimal | None = None
    if put_requests_per_camera_day is not None or get_requests_per_camera_day is not None:
        for count in (put_requests_per_camera_day or 0, get_requests_per_camera_day or 0):
            if isinstance(count, bool) or int(count) != count or count < 0:
                raise ValueError("Request counts must be non-negative integers.")
        if usd_per_1000_puts is None or usd_per_1000_gets is None:
            raise ValueError("Supply both request prices when estimating request costs.")
        prices = (usd_per_1000_puts, usd_per_1000_gets)
        if any(not math.isfinite(float(value)) or float(value) < 0 for value in prices):
            raise ValueError("Request prices must be finite and non-negative.")
        request_cost = (
            Decimal(int(camera_count) * 30 * (put_requests_per_camera_day or 0)) * Decimal(str(usd_per_1000_puts)) / 1000
            + Decimal(int(camera_count) * 30 * (get_requests_per_camera_day or 0)) * Decimal(str(usd_per_1000_gets)) / 1000
        )
    total = storage_cost + (request_cost or Decimal("0"))
    return {
        "camera_count": int(camera_count),
        "retention_days": retention_days,
        "retained_gb": float(retained_gb),
        "monthly_storage_usd": float(storage_cost.quantize(Decimal("0.000001"))),
        "monthly_request_usd": None if request_cost is None else float(request_cost.quantize(Decimal("0.000001"))),
        "monthly_total_usd": float(total.quantize(Decimal("0.000001"))),
        "assumptions": ["30-day billing month", "steady-state retention", "customer-supplied average GB/camera/day", "excludes taxes, data transfer, and shared infrastructure"],
    }


def normalize_cost_explorer_response(response: Mapping, dimension_name: str = "RECORD_TYPE") -> list[dict]:
    """Normalize Cost Explorer GetCostAndUsage rows; no client is made here."""
    normalized = []
    for period in response.get("ResultsByTime") or []:
        month = str((period.get("TimePeriod") or {}).get("Start") or "")[:7]
        if len(month) != 7:
            continue
        estimated = bool(period.get("Estimated", True))
        for group in period.get("Groups") or []:
            keys = list(group.get("Keys") or [])
            metrics = group.get("Metrics") or {}
            cost = metrics.get("UnblendedCost") or {}
            currency = str(cost.get("Unit") or "USD").upper()
            if currency != "USD":
                raise ValueError("Cost Explorer returned a non-USD amount; it cannot be stored as USD.")
            try:
                amount = parse_amount(cost.get("Amount", "0"))
            except ValueError:
                continue
            normalized.append({
                "month": month,
                "service": str(keys[0] if keys else "Unknown"),
                "dimension_name": str(dimension_name).upper(),
                "dimension_value": str(keys[1] if len(keys) > 1 else ""),
                "record_type": str(keys[1] if len(keys) > 1 and str(dimension_name).upper() == "RECORD_TYPE" else ""),
                "usage_type": str(keys[1] if len(keys) > 1 and str(dimension_name).upper() == "USAGE_TYPE" else ""),
                "amount_usd": float(amount),
                "estimated": estimated,
                "currency": str(cost.get("Unit") or "USD"),
                "source": "cost_explorer",
            })
    return normalized


def classify_usage_type(usage_type: str) -> str:
    """Best-effort label for raw AWS usage-type rows; keep the raw label too."""
    value = str(usage_type or "").casefold()
    if "datatransfer" in value or "data transfer" in value or "bytes" in value and "out" in value:
        return "data transfer (usage-type classification)"
    if any(token in value for token in ("request", "requests", "put", "get", "tier1", "tier2")):
        return "requests (usage-type classification)"
    if any(token in value for token in ("timedstorage", "storage", "byte-hour", "gb-month", "gb-month")):
        return "storage (usage-type classification)"
    return "other/unclassified usage"


def aggregate_inventory_rows(rows: Iterable[Mapping], prefix_to_camera: Mapping[str, Mapping], expected_bucket: str | None = None) -> list[dict]:
    """Aggregate an S3 Inventory CSV by camera and storage class.

    ``prefix_to_camera`` is built from the VMS camera records and the exact
    recording_s3_prefix() contract. All object versions are counted if the
    report includes versions; delete markers are excluded because they do not
    consume object payload storage. Inventory keys are used only in memory.
    """
    prefixes = sorted(((str(prefix), data) for prefix, data in prefix_to_camera.items()), key=lambda item: len(item[0]), reverse=True)
    aggregate: dict[tuple[str, str, str, str, str], dict] = {}
    for row in rows:
        if expected_bucket and str(row.get("Bucket", row.get("bucket", ""))) != expected_bucket:
            continue
        key = unquote(str(row.get("Key", row.get("key", ""))))
        if not key:
            continue
        delete_marker = str(row.get("IsDeleteMarker", row.get("is_delete_marker", "false"))).strip().lower()
        if delete_marker in {"true", "1", "yes"}:
            continue
        target = next(((prefix, camera) for prefix, camera in prefixes if key.startswith(prefix)), None)
        if target is None:
            continue
        prefix, camera = target
        try:
            size = int(row.get("Size", row.get("size", 0)))
        except (TypeError, ValueError):
            continue
        if size < 0:
            continue
        storage_class = str(row.get("StorageClass", row.get("storage_class", "STANDARD")) or "STANDARD").strip().upper()
        date_value = str(row.get("LastModifiedDate", row.get("last_modified", "")) or "")
        customer_id = str(camera.get("customer_id") or "")
        site_id = str(camera.get("site_id") or "")
        appliance_id = str(camera.get("appliance_id") or "")
        camera_id = str(camera.get("camera_id") or "")
        key_tuple = (customer_id, site_id, appliance_id, camera_id, storage_class)
        item = aggregate.setdefault(key_tuple, {
            "customer_id": customer_id, "site_id": site_id, "appliance_id": appliance_id,
            "camera_id": camera_id, "storage_class": storage_class, "bytes": 0,
            "object_count": 0, "oldest_upload_at": None, "newest_upload_at": None,
            "recording_mode": camera.get("recording_mode"), "retention_days": camera.get("retention_days"),
            "prefix": prefix,
        })
        item["bytes"] += size
        item["object_count"] += 1
        if date_value:
            item["oldest_upload_at"] = min(filter(None, (item["oldest_upload_at"], date_value)))
            item["newest_upload_at"] = max(filter(None, (item["newest_upload_at"], date_value)))
    return list(aggregate.values())


def evaluate_retention(configured_days: int | None, camera_prefix: str, rules: Iterable[Mapping], oldest_upload_at: str | None, as_of: datetime | None = None) -> dict:
    """Compare saved retention and lifecycle evidence without claiming deletion."""
    if configured_days is None:
        configured = "unknown"
    else:
        configured = int(configured_days)
    matching: list[Mapping] = []
    complex_filter = False
    for rule in rules:
        if str(rule.get("status", "Enabled")).casefold() != "enabled":
            continue
        raw_filter = rule.get("filter_json") or rule.get("filter") or {}
        if isinstance(raw_filter, str):
            try:
                raw_filter = json.loads(raw_filter)
            except (ValueError, TypeError):
                complex_filter = True
                continue
        if raw_filter and set(raw_filter) - {"Prefix"}:
            complex_filter = True
            continue
        prefix = rule.get("prefix")
        if prefix is None:
            prefix = raw_filter.get("Prefix", "") if isinstance(raw_filter, Mapping) else ""
        if camera_prefix.startswith(str(prefix or "")):
            matching.append(rule)
    expiry = [int(rule["expiration_days"]) for rule in matching if rule.get("expiration_days") is not None]
    if expiry:
        lifecycle_days: int | None = min(expiry)
        policy_status = "matches" if configured_days == lifecycle_days else "mismatch"
    elif complex_filter:
        lifecycle_days = None
        policy_status = "unverified_complex_filter"
    elif matching:
        lifecycle_days = None
        policy_status = "no_current_version_expiration"
    else:
        lifecycle_days = None
        policy_status = "no_applicable_expiration_rule"
    old_object = None
    if lifecycle_days is not None and oldest_upload_at:
        try:
            parsed = datetime.fromisoformat(str(oldest_upload_at).replace("Z", "+00:00"))
            now = as_of or datetime.now(timezone.utc)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            old_object = (now - parsed).total_seconds() > (lifecycle_days + 2) * 86400
        except (ValueError, TypeError):
            old_object = None
    return {
        "configured_days": configured,
        "lifecycle_expiration_days": lifecycle_days,
        "policy_status": policy_status,
        "old_upload_beyond_lifecycle_plus_48h_observed": old_object,
        "actual_deletion_verified": False,
        "note": "Lifecycle configuration and upload timestamps do not prove that an object was deleted or that its capture time matches its upload time.",
    }


def _record_view(identity: dict) -> None:
    try:
        from partner_db import audit
        audit(identity, "aws_cost_dashboard.view", "aws_cost_dashboard", "account", {"source": "admin_portal"})
    except Exception as error:
        raise HTTPException(status_code=503, detail="The access audit could not be recorded.") from error


def dashboard_summary(as_of: date | None = None) -> dict:
    """Build owner-facing data only from persisted application records."""
    as_of = as_of or date.today()
    with connect() as db:
        cost_rows = [dict(row) for row in db.execute(
            "SELECT month,service,dimension_name,dimension_value,amount_usd,estimated,source,captured_at "
            "FROM aws_cost_monthly_snapshots ORDER BY month DESC,service,dimension_name,dimension_value"
        ).fetchall()]
        camera_rows = [dict(row) for row in db.execute(
            "SELECT c.id AS camera_id,c.customer_id,c.site_id,c.appliance_id,c.name AS camera_name,c.resolution,c.status,"
            "c.cloud_recording_mode,c.local_recording_mode,cp.retention_days AS policy_retention_days,"
            "(SELECT p.recording_mode FROM plans p WHERE p.customer_id=c.customer_id ORDER BY p.created_at DESC LIMIT 1) AS legacy_recording_mode,"
            "(SELECT p.retention_days FROM plans p WHERE p.customer_id=c.customer_id ORDER BY p.created_at DESC LIMIT 1) AS legacy_retention_days,"
            "v2.plan_key AS paid_plan_key,v2.status AS paid_plan_status,v2.cloud_event_storage,v2.cloud_event_retention_days "
            "FROM cameras c LEFT JOIN customer_cloud_policy cp ON cp.customer_id=c.customer_id "
            "LEFT JOIN camera_plan_entitlements_v2 v2 ON v2.customer_id=c.customer_id "
            "ORDER BY c.customer_id,c.site_id,c.appliance_id,c.id"
        ).fetchall()]
        customer_rows = [dict(row) for row in db.execute("SELECT id,name FROM customers ORDER BY name,id").fetchall()]
        site_rows = [dict(row) for row in db.execute("SELECT id,name,customer_id FROM sites").fetchall()]
        appliance_rows = [dict(row) for row in db.execute("SELECT id,customer_id,site_id,software_version,online_status FROM appliances").fetchall()]
        history_start = (as_of - timedelta(days=89)).isoformat()
        usage_rows = [dict(row) for row in db.execute(
            "SELECT u.snapshot_date,u.customer_id,u.site_id,u.appliance_id,u.camera_id,u.storage_class,u.bytes,u.object_count,"
            "u.oldest_upload_at,u.newest_upload_at,u.recording_mode,u.retention_days,u.source,u.captured_at "
            "FROM aws_camera_usage_snapshots u WHERE (u.snapshot_date>=? AND u.snapshot_date<=?) OR u.snapshot_date=("
            "SELECT MAX(v.snapshot_date) FROM aws_camera_usage_snapshots v WHERE v.camera_id=u.camera_id "
            "AND v.storage_class=u.storage_class AND v.source=u.source AND v.snapshot_date<?) "
            "ORDER BY u.snapshot_date DESC,u.camera_id,u.storage_class",
            (history_start, as_of.isoformat(), history_start),
        ).fetchall()]
        lifecycle_rows = [dict(row) for row in db.execute(
            "SELECT l.snapshot_at,l.bucket_name,l.rule_id,l.status,l.prefix,l.expiration_days,l.filter_json,l.source "
            "FROM aws_lifecycle_rule_snapshots l WHERE l.snapshot_at=(SELECT MAX(x.snapshot_at) FROM aws_lifecycle_rule_snapshots x WHERE x.bucket_name=l.bucket_name) "
            "ORDER BY l.bucket_name,l.rule_id"
        ).fetchall()]

    customers = {str(row["id"]): {"customer_id": str(row["id"]), "customer_name": row.get("name"), "appliance_ids": set(), "camera_count": 0, "stored_camera_prefix_count": 0, "cameras": []} for row in customer_rows}
    sites = {str(row["id"]): row for row in site_rows}
    appliances = {str(row["id"]): row for row in appliance_rows}
    latest_usage_date: dict[str, str] = {}
    for row in usage_rows:
        camera_id = str(row["camera_id"])
        latest_usage_date.setdefault(camera_id, str(row["snapshot_date"]))
    usage_by_camera: dict[str, list[dict]] = {}
    daily_by_camera: dict[str, dict[str, dict]] = {}
    for row in usage_rows:
        if str(row["snapshot_date"]) == latest_usage_date.get(str(row["camera_id"])):
            usage_by_camera.setdefault(str(row["camera_id"]), []).append(row)
        day = str(row["snapshot_date"])
        daily = daily_by_camera.setdefault(str(row["camera_id"]), {}).setdefault(day, {"bytes": 0, "object_count": 0})
        daily["bytes"] += int(row["bytes"])
        daily["object_count"] += int(row["object_count"])
    latest_lifecycle = {}
    for row in lifecycle_rows:
        latest_lifecycle.setdefault(str(row["bucket_name"]), (str(row["snapshot_at"]), []))
        if latest_lifecycle[str(row["bucket_name"])][0] == str(row["snapshot_at"]):
            latest_lifecycle[str(row["bucket_name"])][1].append(row)
    bucket = str(os.getenv("ANYAICAM_RECORDING_S3_BUCKET") or "").strip()
    rule_rows = latest_lifecycle.get(bucket, (None, []))[1] if bucket else []

    customer_usage: dict[str, dict] = {}
    camera_result = []
    for camera in camera_rows:
        camera_id = str(camera["camera_id"])
        customer_id = str(camera.get("customer_id") or "")
        site_id = str(camera.get("site_id") or "")
        appliance_id = str(camera.get("appliance_id") or "")
        record = customers.setdefault(customer_id, {"customer_id": customer_id, "customer_name": None, "appliance_ids": set(), "camera_count": 0, "stored_camera_prefix_count": 0, "cameras": []})
        if appliance_id:
            record["appliance_ids"].add(appliance_id)
        record["camera_count"] += 1
        snapshots = usage_by_camera.get(camera_id, [])
        context = resolve_recording_context(camera)
        record["stored_camera_prefix_count"] += int(bool(snapshots))
        bytes_by_class: dict[str, int] = {}
        for item in snapshots:
            storage_class = str(item["storage_class"])
            bytes_by_class[storage_class] = bytes_by_class.get(storage_class, 0) + int(item["bytes"])
        objects = sum(int(item["object_count"]) for item in snapshots) if snapshots else None
        storage = estimate_storage_cost(bytes_by_class) if snapshots else None
        daily_history = [
            {"snapshot_date": day, "bytes": value["bytes"], "decimal_gb": value["bytes"] / GB_BYTES, "object_or_version_count": value["object_count"]}
            for day, value in sorted(daily_by_camera.get(camera_id, {}).items())
        ]
        growth_bytes = daily_history[-1]["bytes"] - daily_history[-2]["bytes"] if len(daily_history) > 1 else None
        growth_days = (date.fromisoformat(daily_history[-1]["snapshot_date"]) - date.fromisoformat(daily_history[-2]["snapshot_date"])).days if len(daily_history) > 1 else None
        prefix = f"recordings/{customer_id}/{site_id}/{appliance_id}/{camera_id}/" if customer_id and site_id and appliance_id else ""
        oldest = min((str(item["oldest_upload_at"]) for item in snapshots if item.get("oldest_upload_at")), default=None)
        newest = max((str(item["newest_upload_at"]) for item in snapshots if item.get("newest_upload_at")), default=None)
        captured = max((str(item["captured_at"]) for item in snapshots), default=None)
        snapshot_mode = next((item.get("recording_mode") for item in snapshots if item.get("recording_mode")), None)
        snapshot_retention_days = next((item.get("retention_days") for item in snapshots if item.get("retention_days") is not None), None)
        mode = context["recording_mode"] or snapshot_mode
        retention_days = context["retention_days"] if context["recording_mode"] else snapshot_retention_days
        retention = evaluate_retention(retention_days, prefix, rule_rows, oldest) if snapshots and prefix else {
            "configured_days": retention_days if snapshots else None,
            "lifecycle_expiration_days": None,
            "policy_status": "lifecycle_not_measured" if snapshots else "usage_not_measured",
            "old_upload_beyond_lifecycle_plus_48h_observed": None,
            "actual_deletion_verified": False,
            "note": "No complete usage/lifecycle evidence is available.",
        }
        camera_record = {
            "customer_id": customer_id or None,
            "site_id": site_id or None,
            "site_name": sites.get(site_id, {}).get("name"),
            "appliance_id": appliance_id or None,
            "appliance_version": appliances.get(appliance_id, {}).get("software_version"),
            "camera_id": camera_id,
            "camera_name": camera.get("camera_name"),
            "camera_status": camera.get("status"),
            "registered": True,
            "cloud_recording_mode": camera.get("cloud_recording_mode"),
            "local_recording_mode": camera.get("local_recording_mode"),
            "snapshot_recording_mode": snapshot_mode,
            "snapshot_retention_days": snapshot_retention_days,
            "resolution": camera.get("resolution"),
            "recording_mode": mode,
            "configured_retention_days": retention_days,
            "snapshot_date": latest_usage_date.get(camera_id),
            "bytes_by_storage_class": bytes_by_class if snapshots else None,
            "decimal_gb": storage["decimal_gb"] if storage else None,
            "object_or_version_count": objects,
            "estimated_monthly_storage_usd": storage["monthly_cost_usd"] if storage else None,
            "unpriced_storage_classes": storage["unpriced_storage_classes"] if storage else [],
            "oldest_upload_at": oldest,
            "newest_upload_at": newest,
            "last_inventory_captured_at": captured,
            "daily_storage_history": daily_history,
            "storage_growth_since_previous_snapshot_bytes": growth_bytes,
            "storage_growth_period_days": growth_days,
            "retention": retention,
        }
        camera_result.append(camera_record)
        record["cameras"].append(camera_record)
        bucket_cost = customer_usage.setdefault(customer_id, {"known_storage_bytes": 0, "storage_cost_known": Decimal("0"), "unpriced": False, "measured_camera_ids": set()})
        if storage:
            bucket_cost["known_storage_bytes"] += storage["bytes"]
            bucket_cost["measured_camera_ids"].add(camera_id)
            if storage["monthly_cost_usd"] is None:
                bucket_cost["unpriced"] = True
            else:
                bucket_cost["storage_cost_known"] += Decimal(str(storage["monthly_cost_usd"]))

    # Retain visibility of inventory prefixes from a camera that was later
    # soft-removed. These rows still represent stored media and cost, but are
    # explicitly marked as historical and are not included in registered
    # camera counts or presented as currently assigned cameras.
    registered_camera_ids = {str(camera["camera_id"]) for camera in camera_result}
    for camera_id, snapshots in usage_by_camera.items():
        if camera_id in registered_camera_ids or not snapshots:
            continue
        source = snapshots[0]
        customer_id = str(source.get("customer_id") or "")
        site_id = str(source.get("site_id") or "")
        appliance_id = str(source.get("appliance_id") or "")
        record = customers.setdefault(customer_id, {"customer_id": customer_id, "customer_name": None, "appliance_ids": set(), "camera_count": 0, "stored_camera_prefix_count": 0, "cameras": []})
        if appliance_id:
            record["appliance_ids"].add(appliance_id)
        record["stored_camera_prefix_count"] += 1
        bytes_by_class: dict[str, int] = {}
        for item in snapshots:
            storage_class = str(item["storage_class"])
            bytes_by_class[storage_class] = bytes_by_class.get(storage_class, 0) + int(item["bytes"])
        storage = estimate_storage_cost(bytes_by_class)
        objects = sum(int(item["object_count"]) for item in snapshots)
        daily_history = [
            {"snapshot_date": day, "bytes": value["bytes"], "decimal_gb": value["bytes"] / GB_BYTES, "object_or_version_count": value["object_count"]}
            for day, value in sorted(daily_by_camera.get(camera_id, {}).items())
        ]
        growth_bytes = daily_history[-1]["bytes"] - daily_history[-2]["bytes"] if len(daily_history) > 1 else None
        growth_days = (date.fromisoformat(daily_history[-1]["snapshot_date"]) - date.fromisoformat(daily_history[-2]["snapshot_date"])).days if len(daily_history) > 1 else None
        prefix = f"recordings/{customer_id}/{site_id}/{appliance_id}/{camera_id}/" if customer_id and site_id and appliance_id else ""
        retention_days = next((item.get("retention_days") for item in snapshots if item.get("retention_days") is not None), None)
        oldest = min((str(item["oldest_upload_at"]) for item in snapshots if item.get("oldest_upload_at")), default=None)
        newest = max((str(item["newest_upload_at"]) for item in snapshots if item.get("newest_upload_at")), default=None)
        retention = evaluate_retention(retention_days, prefix, rule_rows, oldest) if prefix else {
            "configured_days": retention_days, "lifecycle_expiration_days": None, "policy_status": "lifecycle_not_measured",
            "old_upload_beyond_lifecycle_plus_48h_observed": None, "actual_deletion_verified": False,
            "note": "No complete usage/lifecycle evidence is available.",
        }
        camera_record = {
            "customer_id": customer_id or None, "site_id": site_id or None,
            "site_name": sites.get(site_id, {}).get("name"), "appliance_id": appliance_id or None,
            "appliance_version": appliances.get(appliance_id, {}).get("software_version"),
            "camera_id": camera_id, "camera_name": None, "camera_status": "not_registered",
            "registered": False, "resolution": None, "recording_mode": None,
            "configured_retention_days": retention_days,
            "snapshot_date": latest_usage_date.get(camera_id),
            "bytes_by_storage_class": bytes_by_class, "decimal_gb": storage["decimal_gb"],
            "object_or_version_count": objects,
            "estimated_monthly_storage_usd": storage["monthly_cost_usd"],
            "unpriced_storage_classes": storage["unpriced_storage_classes"],
            "oldest_upload_at": oldest, "newest_upload_at": newest,
            "last_inventory_captured_at": max(str(item["captured_at"]) for item in snapshots),
            "daily_storage_history": daily_history,
            "storage_growth_since_previous_snapshot_bytes": growth_bytes,
            "storage_growth_period_days": growth_days, "retention": retention,
        }
        camera_result.append(camera_record)
        record["cameras"].append(camera_record)
        bucket_cost = customer_usage.setdefault(customer_id, {"known_storage_bytes": 0, "storage_cost_known": Decimal("0"), "unpriced": False, "measured_camera_ids": set()})
        bucket_cost["known_storage_bytes"] += storage["bytes"]
        bucket_cost["measured_camera_ids"].add(camera_id)
        if storage["monthly_cost_usd"] is None:
            bucket_cost["unpriced"] = True
        else:
            bucket_cost["storage_cost_known"] += Decimal(str(storage["monthly_cost_usd"]))

    for customer_id, record in customers.items():
        usage = customer_usage.get(customer_id)
        record["appliance_count"] = len(record.pop("appliance_ids"))
        record["active_camera_count"] = sum(1 for camera in record["cameras"] if str(camera.get("camera_status") or "").casefold() == "active")
        record["storage_measured"] = usage is not None
        record["measured_camera_count"] = len(usage["measured_camera_ids"]) if usage else 0
        record["storage_coverage_complete"] = bool(usage) and record["measured_camera_count"] >= record["camera_count"] and not usage["unpriced"]
        record["known_storage_decimal_gb"] = (usage["known_storage_bytes"] / GB_BYTES) if usage else None
        record["known_measured_storage_estimate_usd"] = float(usage["storage_cost_known"]) if usage and not usage["unpriced"] else None
        record["estimated_storage_monthly_usd"] = record["known_measured_storage_estimate_usd"] if record["storage_coverage_complete"] else None
        record["attributable_request_and_transfer_usd"] = None
        record["shared_infrastructure_allocation_usd"] = None
        record["operating_cost_estimate_usd"] = record["estimated_storage_monthly_usd"]
        record["cost_note"] = "Storage-only estimate; request/transfer charges and shared infrastructure are not attributed."

    def entity_summaries(entities: list[dict], key: str, label_key: str) -> list[dict]:
        summaries = {}
        for entity in entities:
            entity_id = str(entity.get("id") or "")
            if not entity_id:
                continue
            summaries[entity_id] = {
                f"{key}_id": entity_id,
                label_key: entity.get("name"),
                "customer_id": entity.get("customer_id"),
                "camera_count": 0, "active_camera_count": 0, "measured_camera_count": 0,
                "stored_camera_prefix_count": 0,
                "known_storage_decimal_gb": 0.0, "known_measured_storage_estimate_usd": Decimal("0"),
                "has_unpriced_storage": False,
            }
            if key == "site":
                summaries[entity_id]["appliance_count"] = sum(1 for appliance in appliance_rows if str(appliance.get("site_id") or "") == entity_id)
            else:
                summaries[entity_id]["site_id"] = entity.get("site_id")
                summaries[entity_id]["software_version"] = entity.get("software_version")
        for camera in camera_result:
            entity_id = str(camera.get(f"{key}_id") or "")
            if not entity_id:
                continue
            item = summaries.setdefault(entity_id, {
                f"{key}_id": entity_id, label_key: None, "customer_id": camera.get("customer_id"),
                "camera_count": 0, "active_camera_count": 0, "measured_camera_count": 0,
                "stored_camera_prefix_count": 0,
                "known_storage_decimal_gb": 0.0, "known_measured_storage_estimate_usd": Decimal("0"),
                "has_unpriced_storage": False,
            })
            item["stored_camera_prefix_count"] += int(camera.get("decimal_gb") is not None)
            if camera.get("registered"):
                item["camera_count"] += 1
                if str(camera.get("camera_status") or "").casefold() == "active":
                    item["active_camera_count"] += 1
            if camera.get("decimal_gb") is not None:
                item["measured_camera_count"] += 1
                item["known_storage_decimal_gb"] += float(camera["decimal_gb"])
                if camera.get("estimated_monthly_storage_usd") is None:
                    item["has_unpriced_storage"] = True
                else:
                    item["known_measured_storage_estimate_usd"] += Decimal(str(camera["estimated_monthly_storage_usd"]))
        result = []
        for item in summaries.values():
            item["storage_coverage_complete"] = item["measured_camera_count"] >= item["camera_count"] and (item["camera_count"] > 0 or item["stored_camera_prefix_count"] > 0) and not item["has_unpriced_storage"]
            item["known_measured_storage_estimate_usd"] = float(item["known_measured_storage_estimate_usd"]) if not item["has_unpriced_storage"] else None
            item["estimated_storage_monthly_usd"] = item["known_measured_storage_estimate_usd"] if item["storage_coverage_complete"] else None
            item.pop("has_unpriced_storage", None)
            result.append(item)
        return sorted(result, key=lambda item: (item.get("customer_id") or "", item.get(f"{key}_id") or ""))

    site_result = entity_summaries(site_rows, "site", "site_name")
    appliance_result = entity_summaries(appliance_rows, "appliance", "appliance_id")

    monthly: dict[str, dict] = {}
    for row in cost_rows:
        if str(row["dimension_name"]).upper() != "RECORD_TYPE":
            continue
        item = monthly.setdefault(str(row["month"]), {"month": str(row["month"]), "estimated": False, "services": {}, "record_types": {}, "captured_at": None, "source": set()})
        service = str(row["service"])
        record_type = str(row["dimension_value"] or "Unclassified")
        amount = Decimal(str(row["amount_usd"]))
        item["services"][service] = item["services"].get(service, Decimal("0")) + amount
        item["record_types"][record_type] = item["record_types"].get(record_type, Decimal("0")) + amount
        item["has_account_totals"] = True
        item["estimated"] = item["estimated"] or bool(row["estimated"])
        item["captured_at"] = max(filter(None, (item["captured_at"], row.get("captured_at"))), default=None)
        item["source"].add(str(row["source"]))
    for row in cost_rows:
        if str(row["dimension_name"]).upper() != "USAGE_TYPE":
            continue
        item = monthly.setdefault(str(row["month"]), {"month": str(row["month"]), "estimated": bool(row["estimated"]), "services": {}, "record_types": {}, "captured_at": row.get("captured_at"), "source": set()})
        item.setdefault("usage_breakdown", []).append({
            "service": str(row["service"]), "usage_type": str(row["dimension_value"] or "Unclassified"),
            "amount_usd": float(row["amount_usd"]), "classification": classify_usage_type(str(row["dimension_value"] or "")),
        })
        item["estimated"] = item["estimated"] or bool(row["estimated"])
        item["source"].add(str(row["source"]))
    month_result = []
    for month in sorted(monthly, reverse=True):
        item = monthly[month]
        service_amounts = {key: float(value) for key, value in sorted(item["services"].items())}
        has_account_totals = bool(item.get("has_account_totals"))
        month_result.append({
            "month": month,
            "status": ("estimated" if item["estimated"] else "finalized") if has_account_totals else "detail_only",
            "total_usd": float(sum(item["services"].values())) if has_account_totals else None,
            "services": service_amounts,
            "record_types": {key: float(value) for key, value in sorted(item["record_types"].items())},
            "usage_breakdown": sorted(item.get("usage_breakdown", []), key=lambda value: (value["service"], value["usage_type"])),
            "s3_usd": sum(value for key, value in service_amounts.items() if "s3" in key.casefold() or "simple storage" in key.casefold()) if has_account_totals else None,
            "tax_usd": sum(value for key, value in service_amounts.items() if "tax" == key.strip().casefold()) if has_account_totals else None,
            "captured_at": item["captured_at"],
            "sources": sorted(item["source"]),
        })
    current_month = as_of.strftime("%Y-%m")
    previous_month = date(as_of.year - (1 if as_of.month == 1 else 0), 12 if as_of.month == 1 else as_of.month - 1, 1).strftime("%Y-%m")
    current_bill = next((row for row in month_result if row["month"] == current_month), None)
    previous_bill = next((row for row in month_result if row["month"] == previous_month and row["status"] == "finalized"), None)
    delta = None
    if current_bill and previous_bill and current_bill.get("total_usd") is not None and previous_bill.get("total_usd") is not None:
        change = current_bill["total_usd"] - previous_bill["total_usd"]
        delta = {"amount_usd": change, "percent": (change / previous_bill["total_usd"] * 100) if previous_bill["total_usd"] else None, "baseline_month": previous_bill["month"], "current_month": current_bill["month"], "estimated": current_bill["status"] == "estimated"}
    return {
        "as_of": as_of.isoformat(),
        "billing": {
            "current_month": current_bill,
            "previous_month": previous_bill,
            "month_to_month_change": delta,
            "history": month_result,
        },
        "customers": sorted(({
            **{key: value for key, value in record.items() if key != "cameras"},
            "cameras": record["cameras"],
        } for record in customers.values()), key=lambda row: (row.get("customer_name") or "", row["customer_id"])),
        "sites": site_result,
        "appliances": appliance_result,
        "cameras": camera_result,
        "attribution": {
            "customer_level_billed_costs": False,
            "request_and_transfer_costs": "not_attributed_without prefix-aware request/transfer metering",
            "shared_infrastructure": "not allocated",
        },
        "data_quality": {
            "cost_explorer_rows": len(cost_rows),
            "camera_usage_rows": len(usage_rows),
            "daily_history_window_days": 90,
            "lifecycle_rule_rows": len(lifecycle_rows),
            "storage_rate_usd_per_gb_month": float(S3_STANDARD_US_EAST_1_USD_PER_GB_MONTH),
            "storage_rate_basis": "AWS S3 Standard us-east-1 published first-tier list rate; verify current rate before customer pricing",
        },
    }


def render_page() -> str:
    return """<section class=\"panel\"><p class=\"eyebrow\">Internal · platform owner</p><h1>AWS Cost Dashboard</h1>
<p class=\"health-detail\">Billed account totals come from Cost Explorer snapshots. Camera storage is an estimate from S3 Inventory snapshots. Customer allocations are not AWS-billed charges.</p>
<div id=\"aws-cost-status\" role=\"status\">Loading stored cost and usage snapshots…</div>
<section class=\"panel\"><h2>AWS account overview</h2><div id=\"aws-cost-overview\"></div></section>
<section class=\"panel\"><h2>Service and S3 usage detail</h2><div id=\"aws-cost-usage-detail\"></div></section>
<section class=\"panel\"><h2>Customer, site, appliance, and camera usage</h2><div id=\"aws-cost-customers\"></div><div id=\"aws-cost-sites\"></div><div id=\"aws-cost-appliances\"></div></section>
<section class=\"panel\"><h2>Retention evidence</h2><p>Lifecycle configuration is not proof that expiration has completed. Object timestamps are S3 upload times, not necessarily recording capture times.</p><div id=\"aws-cost-retention\"></div></section>
<section class=\"panel\"><h2>Cost projection calculator</h2><p>Illustrative steady-state estimate. It excludes taxes, data transfer, shared infrastructure, and unmeasured requests.</p>
<form id=\"aws-cost-projection\"><label>Cameras <input name=\"cameras\" type=\"number\" min=\"1\" max=\"100000\" value=\"4\"></label>
<label>Retention <select name=\"retention\"><option value=\"7\">7 days</option><option value=\"14\">14 days</option><option value=\"30\" selected>30 days</option></select></label>
<label>Average GB per camera per day <input name=\"gbPerDay\" type=\"number\" min=\"0\" step=\"0.01\" value=\"1\"></label>
<label>Storage USD per GB-month <input name=\"storageRate\" type=\"number\" min=\"0\" step=\"0.0001\" value=\"0.023\"></label>
<label>PUT requests per camera per day (optional) <input name=\"puts\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"Not measured\"></label>
<label>USD per 1,000 PUTs <input name=\"putRate\" type=\"number\" min=\"0\" step=\"0.0001\" placeholder=\"Enter rate\"></label>
<label>GET requests per camera per day (optional) <input name=\"gets\" type=\"number\" min=\"0\" step=\"1\" placeholder=\"Not measured\"></label>
<label>USD per 1,000 GETs <input name=\"getRate\" type=\"number\" min=\"0\" step=\"0.0001\" placeholder=\"Enter rate\"></label>
<button class=\"action-button\" type=\"submit\">Calculate estimate</button></form><div id=\"aws-cost-projection-result\" aria-live=\"polite\"></div></section>
<p class=\"health-detail\">No S3 listing is performed by this page. Data collection must use S3 Inventory snapshots and read-only Cost Explorer reports.</p></section>"""


def render_script() -> str:
    return """<script>
(()=>{
const status=document.getElementById('aws-cost-status');
const money=value=>value===null||value===undefined?'Not measured':new Intl.NumberFormat(undefined,{style:'currency',currency:'USD'}).format(value);
const esc=value=>String(value??'—');
function table(headers,rows){const t=document.createElement('table');const thead=t.createTHead().insertRow();headers.forEach(label=>{const th=document.createElement('th');th.textContent=label;thead.appendChild(th)});const body=t.createTBody();rows.forEach(row=>{const tr=body.insertRow();row.forEach(value=>{const td=tr.insertCell();td.textContent=esc(value)})});return t}
function showCosts(data){const overview=document.getElementById('aws-cost-overview');const bill=data.billing;const change=bill.month_to_month_change;const summary=[['Current month',bill.current_month?`${bill.current_month.status} · ${money(bill.current_month.total_usd)}`:'Not measured'],['Previous finalized month',bill.previous_month?money(bill.previous_month.total_usd):'Not measured'],['Month-to-month change',change?`${money(change.amount_usd)} (${change.percent===null?'n/a':change.percent.toFixed(1)+'%'}) · ${change.estimated?'current month estimated':''}`:'Not measured'],['S3 current month',bill.current_month?money(bill.current_month.s3_usd):'Not measured'],['S3 previous month',bill.previous_month?money(bill.previous_month.s3_usd):'Not measured'],['Taxes current month',bill.current_month?money(bill.current_month.tax_usd):'Not measured']];overview.appendChild(table(['Measure','Value'],summary));const history=document.createElement('div');history.appendChild(table(['Month','Status','Total','S3','Captured'],bill.history.map(m=>[m.month,m.status,money(m.total_usd),money(m.s3_usd),m.captured_at])));overview.appendChild(history);const services=document.getElementById('aws-cost-usage-detail');services.appendChild(table(['Month','Service','S3 usage type','Category','Amount'],bill.history.flatMap(m=>m.usage_breakdown.map(u=>[m.month,u.service,u.usage_type,u.classification,money(u.amount_usd)]))));if(bill.current_month)services.appendChild(table(['Current-month service','Amount'],Object.entries(bill.current_month.services).map(([k,v])=>[k,money(v)])));
const customerWrap=document.getElementById('aws-cost-customers');customerWrap.appendChild(table(['Customer ID','Customer name','Appliances','Active / registered cameras','Stored camera prefixes','Measured cameras','Known measured GB','Known measured storage subtotal (partial)','Complete storage estimate','Attributed requests/transfers','Shared allocation'],data.customers.map(c=>[c.customer_id,c.customer_name,c.appliance_count,`${c.active_camera_count} / ${c.camera_count}`,c.stored_camera_prefix_count,c.measured_camera_count,c.storage_measured?Number(c.known_storage_decimal_gb).toFixed(3):'Not measured',money(c.known_measured_storage_estimate_usd),money(c.estimated_storage_monthly_usd),'Not attributed','Not allocated'])));
document.getElementById('aws-cost-sites').appendChild(table(['Customer ID','Site ID','Site name','Appliances','Active / registered cameras','Stored prefixes','Measured cameras','Known measured GB','Complete storage estimate'],data.sites.map(s=>[s.customer_id,s.site_id,s.site_name,s.appliance_count,`${s.active_camera_count} / ${s.camera_count}`,s.stored_camera_prefix_count,s.measured_camera_count,s.measured_camera_count?Number(s.known_storage_decimal_gb).toFixed(3):'Not measured',money(s.estimated_storage_monthly_usd)])));
document.getElementById('aws-cost-appliances').appendChild(table(['Customer ID','Site ID','Appliance ID','VMS version','Active / registered cameras','Stored prefixes','Measured cameras','Known measured GB','Complete storage estimate'],data.appliances.map(a=>[a.customer_id,a.site_id,a.appliance_id,a.software_version,`${a.active_camera_count} / ${a.camera_count}`,a.stored_camera_prefix_count,a.measured_camera_count,a.measured_camera_count?Number(a.known_storage_decimal_gb).toFixed(3):'Not measured',money(a.estimated_storage_monthly_usd)])));
customerWrap.appendChild(table(['Customer ID','Site ID','Site name','Appliance ID','Camera ID','Camera name','Registration','Plan mode','Cloud upload mode','Local recording mode','Cloud retention','Snapshot','GB','Objects/versions','Growth since prior snapshot','Growth interval (days)','Est. monthly storage','Last upload'],data.cameras.map(c=>[c.customer_id,c.site_id,c.site_name,c.appliance_id,c.camera_id,c.camera_name,c.registered?'Registered':'Historical snapshot only',c.recording_mode,c.cloud_recording_mode,c.local_recording_mode,c.configured_retention_days?`${c.configured_retention_days} days`:'Not measured',c.snapshot_date,c.decimal_gb===null?'Not measured':Number(c.decimal_gb).toFixed(3),c.object_or_version_count,c.storage_growth_since_previous_snapshot_bytes===null?'Not measured':`${c.storage_growth_since_previous_snapshot_bytes} bytes`,c.storage_growth_period_days,c.estimated_monthly_storage_usd===null?'Not measured':money(c.estimated_monthly_storage_usd),c.newest_upload_at])));
customerWrap.appendChild(table(['Camera','Daily inventory snapshot','Stored GB','Objects/versions'],data.cameras.flatMap(c=>(c.daily_storage_history||[]).map(s=>[c.camera_id,s.snapshot_date,s.decimal_gb.toFixed(3),s.object_or_version_count]))));
document.getElementById('aws-cost-retention').appendChild(table(['Camera','Configured','Lifecycle rule','Old upload > rule + 48h','Deletion verified'],data.cameras.map(c=>[c.camera_id,c.retention.configured_days,c.retention.policy_status,c.retention.old_upload_beyond_lifecycle_plus_48h_observed===null?'Not measured':c.retention.old_upload_beyond_lifecycle_plus_48h_observed?'Observed':'Not observed','No'])));
status.textContent=`Snapshot data as of ${data.as_of}. Daily storage history shown for the latest ${data.data_quality.daily_history_window_days} days plus the preceding baseline snapshot. Cost Explorer rows: ${data.data_quality.cost_explorer_rows}; camera usage rows: ${data.data_quality.camera_usage_rows}; lifecycle rules: ${data.data_quality.lifecycle_rule_rows}. Storage standard list rate: $${data.data_quality.storage_rate_usd_per_gb_month}/decimal GB-month. These are not camera-specific billed charges.`}
fetch('/api/admin/aws-costs/summary',{credentials:'same-origin',headers:{'Accept':'application/json'}}).then(r=>{if(!r.ok)throw new Error(r.status===403?'Access denied.':'Stored cost snapshots are unavailable.');return r.json()}).then(showCosts).catch(e=>{status.textContent=e.message;status.classList.add('error')});
document.getElementById('aws-cost-projection').addEventListener('submit',event=>{event.preventDefault();const result=document.getElementById('aws-cost-projection-result');const f=new FormData(event.currentTarget);const cameras=Number(f.get('cameras')),days=Number(f.get('retention')),perDay=Number(f.get('gbPerDay')),rate=Number(f.get('storageRate'));if(!Number.isInteger(cameras)||cameras<1||cameras>100000||![7,14,30].includes(days)||!Number.isFinite(perDay)||perDay<0||!Number.isFinite(rate)||rate<0){result.textContent='Enter a valid camera count, supported retention period, and non-negative storage assumptions.';return}const retained=cameras*days*perDay;const storage=retained*rate;let requests='Request costs not included';const raw=[f.get('puts'),f.get('putRate'),f.get('gets'),f.get('getRate')];if(raw.some(value=>value!=='')){if(raw.some(value=>value==='')){result.textContent='Enter all four request assumptions or leave them blank.';return}const [puts,putRate,gets,getRate]=raw.map(Number);if(!Number.isInteger(puts)||puts<0||!Number.isInteger(gets)||gets<0||![putRate,getRate].every(value=>Number.isFinite(value)&&value>=0)){result.textContent='Request volumes must be non-negative integers and rates must be valid non-negative numbers.';return}const cost=(cameras*30*puts*putRate+cameras*30*gets*getRate)/1000;requests=`Estimated requests: ${money(cost)}`}result.textContent=`${retained.toFixed(2)} GB retained · storage ${money(storage)} / month · ${requests}. Excludes tax, transfer, and shared services.`});
})();</script>"""


def register_aws_cost_dashboard_routes(app: FastAPI, page_shell) -> None:
    @app.get("/admin/aws-costs", response_class=HTMLResponse)
    def aws_cost_dashboard_page(request: Request):
        identity = require_owner(request)
        _record_view(identity)
        return page_shell("AWS Cost Dashboard", "admin-portal", render_page(), render_script())

    @app.get("/api/admin/aws-costs/summary")
    def aws_cost_dashboard_summary(request: Request):
        identity = require_owner(request)
        _record_view(identity)
        try:
            return dashboard_summary()
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=503, detail="Stored AWS cost snapshots are unavailable.") from error
