"""Customer Analytics workspaces (2026-09-25).

One Analytics entry in the portal navigation (a flyout on desktop, a
bottom-bar submenu on phones) listing only the analytics this customer
actually has, and a dedicated workspace per analytic at
/analytics/<slug> -- Smart Motion, People Counting, License Plates (LPR),
PPE, Facial Recognition, Line Crossing, Intrusion -- each presenting its own
data its own way. /analytics is the overview. Selecting a result expands it
in place and plays its clip right there (/static/inline_media.js, the same
player Playback uses); "Open in Playback" is optional.

Existing data only: detection_events (tenant-scoped by customer_id and the
caller's permitted cameras, further limited to the cameras entitled to that
analytic), the detection_event_media join for clips/thumbnails, and each
analytic's own stored fields (PPE hard-hat/vest flags, facial match
state/name, people-counting direction, LPR plate text, rule name/direction
for line crossing and intrusion). Nothing new is stored.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request

from customer_analytics_panel import _parse_detections, _ppe_status, epoch_ms, real_confidence
from partner_db import connection

VEHICLE_TYPES = ("vehicle", "car", "truck", "bus", "motorcycle", "bicycle")

# key -> slug, label, the per-camera entitlement that enables it, stored
# event types, one-line description. Line crossing and intrusion are rules
# that run on Smart Motion's detections (customer_analytics_rule_worker),
# so they need its entitlement -- and a rule (or events) of their own kind.
WORKSPACES: dict[str, dict] = {
    "smart_motion": {"slug": "smart-motion", "label": "Smart Motion", "entitlement": "smart_motion",
                     "types": ("motion", "smart_motion", "person") + VEHICLE_TYPES,
                     "description": "People, vehicles and motion, with nuisance movement filtered out."},
    "people_counting": {"slug": "people-counting", "label": "People Counting", "entitlement": "people_counting",
                        "types": ("people_counting", "people_counting_in", "people_counting_out"),
                        "description": "Entries and exits, totals and daily trends."},
    "lpr": {"slug": "lpr", "label": "License Plates", "entitlement": "lpr", "types": ("plate",),
            "description": "License plate reads with their snapshots and clips."},
    "ppe": {"slug": "ppe", "label": "PPE", "entitlement": "ppe", "types": ("ppe",),
            "description": "Hard-hat and safety-vest checks."},
    "facial_recognition": {"slug": "facial-recognition", "label": "Facial Recognition",
                           "entitlement": "facial_recognition", "types": ("facial_recognition",),
                           "description": "Recognized people and unknown faces."},
    "line_crossing": {"slug": "line-crossing", "label": "Line Crossing", "entitlement": "smart_motion",
                      "types": ("line_crossing",), "rule": True,
                      "description": "Crossings of the lines you drew, with direction."},
    "intrusion": {"slug": "intrusion", "label": "Intrusion", "entitlement": "smart_motion",
                  "types": ("intrusion",), "rule": True,
                  "description": "Activity inside the zones you drew."},
}
BY_SLUG = {spec["slug"]: key for key, spec in WORKSPACES.items()}
TABS = tuple(WORKSPACES)  # kept for callers/tests that list the analytic keys

RESULT_FILTERS: dict[str, dict[str, tuple[str, ...]]] = {
    "smart_motion": {"person": ("person",), "vehicle": VEHICLE_TYPES, "motion": ("motion", "smart_motion")},
    "people_counting": {"in": ("people_counting_in",), "out": ("people_counting_out",)},
    "lpr": {},
    "ppe": {"violation": (), "compliant": (), "missing_hard_hat": (), "missing_vest": ()},
    "facial_recognition": {"known": (), "unknown": ()},
    "line_crossing": {},
    "intrusion": {},
}
SEARCHABLE = {"lpr", "facial_recognition"}  # free-text search over the stored plate / name
PAGE_SIZE = 48
MAX_RANGE_DAYS = 92


def _utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()


def _row_details(key: str, row: dict) -> dict:
    """Each analytic's own stored fields, never another analytic's."""
    detections = _parse_detections(row.get("detections_json"))
    if key == "ppe":
        hard_hat = detections.get("hard_hat_present")
        vest = detections.get("safety_vest_present")
        return {"status": _ppe_status(detections),
                "hard_hat": None if hard_hat is None else bool(hard_hat),
                "vest": None if vest is None else bool(vest)}
    if key == "facial_recognition":
        return {"state": detections.get("match_state"), "person": detections.get("matched_person_name"),
                "watchlist": detections.get("matched_watchlist_name")}
    if key == "lpr":
        return {"plate": detections.get("plate")}
    if key == "people_counting":
        event_type = row.get("event_type")
        return {"direction": "in" if event_type == "people_counting_in" else "out" if event_type == "people_counting_out" else None}
    if key in ("line_crossing", "intrusion"):
        # The appliance names these "Line Crossing (<name>)" / "Intrusion
        # Zone (<name>)"; the page already says which kind it is.
        rule = str(detections.get("rule_name") or "").strip()
        match = re.match(r"^(?:Line Crossing|Intrusion Zone) \((.*)\)$", rule)
        rule = match.group(1) if match else rule
        return {"rule": None if rule in ("", "unnamed rule") else rule, "direction": detections.get("direction")}
    return {"object_count": row.get("object_count")}


def _confidence(key: str, event_type: str, value):
    # PPE rows carry a placeholder 0.0 and people-counting crossings no
    # meaningful score; motion rows a raw motion score, not a probability.
    if key in ("ppe", "people_counting"):
        return None
    return real_confidence(event_type, value)


def _result_clause(key: str, result: str) -> tuple[str, list]:
    if not result:
        return "", []
    if result not in RESULT_FILTERS[key]:
        raise HTTPException(status_code=400, detail="Unknown result filter.")
    types = RESULT_FILTERS[key][result]
    if not types:  # decided from stored fields after the fetch (PPE, faces)
        return "", []
    return f" AND de.event_type IN ({','.join('?' for _ in types)})", list(types)


def _post_filter(key: str, result: str, item: dict) -> bool:
    details = item["details"]
    if key == "ppe" and result:
        if result == "missing_hard_hat":
            return details["hard_hat"] is False
        if result == "missing_vest":
            return details["vest"] is False
        return details["status"] == result
    if key == "facial_recognition" and result:
        return (details["state"] == "known") if result == "known" else (details["state"] != "known")
    return True


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def query_events(*, customer_id: str, camera_ids: list[str], key: str, start_ms: int, end_ms: int,
                 result: str = "", q: str = "", before: str | None = None, limit: int = PAGE_SIZE) -> dict:
    if key not in WORKSPACES:
        raise HTTPException(status_code=404, detail="Unknown analytic.")
    if not camera_ids:
        return {"events": [], "summary": {"total": 0}, "next_before": None}
    event_types = WORKSPACES[key]["types"]
    result_sql, result_args = _result_clause(key, result)
    base_where = (f"de.customer_id=? AND de.camera_id IN ({','.join('?' for _ in camera_ids)}) "
                  f"AND de.event_type IN ({','.join('?' for _ in event_types)}) "
                  "AND de.event_timestamp>=? AND de.event_timestamp<?")
    base_args = [customer_id, *camera_ids, *event_types, _utc_iso(start_ms), _utc_iso(end_ms)]
    search_sql, search_args = "", []
    if q.strip() and key in SEARCHABLE:
        search_sql, search_args = " AND de.detections_json LIKE ? ESCAPE '\\'", [_like(q.strip())]
    page_sql = base_where + result_sql + search_sql + (" AND de.event_timestamp<?" if before else "")
    page_args = base_args + result_args + search_args + ([before] if before else [])
    post_filtered = key in ("ppe", "facial_recognition") and result
    fetch = limit * 4 if post_filtered else limit
    with connection() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT de.id, de.camera_id, de.event_type, de.confidence, de.object_count, de.detections_json, de.event_timestamp, "
            "CASE WHEN dem.id IS NULL THEN 0 ELSE 1 END AS has_clip, "
            "CASE WHEN length(COALESCE(dem.thumbnail_s3_key, ''))>0 THEN 1 ELSE 0 END AS has_thumbnail "
            "FROM detection_events de LEFT JOIN detection_event_media dem ON dem.detection_event_id=de.id "
            f"WHERE {page_sql} ORDER BY de.event_timestamp DESC LIMIT ?", (*page_args, fetch + 1)).fetchall()]
        summary = _summary(db, key, base_where + search_sql, base_args + search_args)
    more = len(rows) > fetch
    rows = rows[:fetch]
    events = []
    for row in rows:
        item = {"event_id": row["id"], "camera_id": row["camera_id"], "event_type": row["event_type"],
                "timestamp_ms": epoch_ms(row["event_timestamp"]), "confidence": _confidence(key, row["event_type"], row["confidence"]),
                "has_clip": bool(row["has_clip"]), "has_thumbnail": bool(row["has_thumbnail"]),
                "details": _row_details(key, row)}
        if _post_filter(key, result, item):
            events.append(item)
    events = events[:limit]
    next_before = rows[-1]["event_timestamp"] if more and rows else None
    return {"events": events, "summary": summary, "next_before": next_before}


def _summary(db, key: str, where: str, args: list) -> dict:
    """Totals over the whole selected range (not just the loaded page)."""
    total = db.execute(f"SELECT COUNT(*) AS n FROM detection_events de WHERE {where}", args).fetchone()["n"]
    out: dict = {"total": total}
    if key in ("smart_motion", "people_counting"):
        out["by_type"] = {r["event_type"]: r["n"] for r in db.execute(
            f"SELECT de.event_type, COUNT(*) AS n FROM detection_events de WHERE {where} GROUP BY de.event_type", args)}
    if key == "people_counting":
        per_camera: dict[str, dict] = {}
        for r in db.execute(f"SELECT de.camera_id, de.event_type, COUNT(*) AS n FROM detection_events de WHERE {where} "
                            "GROUP BY de.camera_id, de.event_type", args):
            entry = per_camera.setdefault(r["camera_id"], {"in": 0, "out": 0})
            if r["event_type"] == "people_counting_in":
                entry["in"] += r["n"]
            elif r["event_type"] == "people_counting_out":
                entry["out"] += r["n"]
        out["per_camera"] = per_camera
        # Hourly UTC buckets; the browser regroups them by the viewer's own
        # local day, so the trend follows the customer's timezone.
        out["hourly"] = [{"hour_ms": epoch_ms(r["hour"] + ":00:00"), "in": r["n_in"], "out": r["n_out"]} for r in db.execute(
            f"SELECT substr(de.event_timestamp,1,13) AS hour, "
            "SUM(CASE WHEN de.event_type='people_counting_in' THEN 1 ELSE 0 END) AS n_in, "
            "SUM(CASE WHEN de.event_type='people_counting_out' THEN 1 ELSE 0 END) AS n_out "
            f"FROM detection_events de WHERE {where} GROUP BY substr(de.event_timestamp,1,13) ORDER BY hour", args)]
    if key in ("ppe", "facial_recognition", "line_crossing", "intrusion"):
        counts: dict[str, int] = {}
        for r in db.execute(f"SELECT de.event_type, de.detections_json FROM detection_events de WHERE {where}", args):
            details = _row_details(key, dict(r))
            if key == "ppe":
                bucket = details["status"] or "unknown"
                for part, missing in (("missing_hard_hat", details["hard_hat"] is False), ("missing_vest", details["vest"] is False)):
                    if missing:
                        counts[part] = counts.get(part, 0) + 1
            elif key == "facial_recognition":
                bucket = "known" if details["state"] == "known" else "unknown"
            else:
                bucket = details["rule"] or "Unnamed rule"
            counts[bucket] = counts.get(bucket, 0) + 1
        out["by_result"] = counts
    return out


def entitled_camera_ids(db, camera_ids: list[str], key: str) -> list[str]:
    """The caller's permitted cameras that are entitled to this analytic."""
    if not camera_ids or key not in WORKSPACES:
        return []
    entitlement = WORKSPACES[key]["entitlement"]
    return [r["camera_id"] for r in db.execute(
        "SELECT camera_id FROM camera_analytics_entitlements WHERE analytic_key=? AND status='active' "
        f"AND camera_id IN ({','.join('?' for _ in camera_ids)})", (entitlement, *camera_ids))]


def available_workspaces(customer_id: str | None, camera_ids: list[str]) -> list[dict]:
    """Only the analytics this customer really has: entitled on at least one
    permitted camera; line crossing / intrusion additionally need a rule (or
    recorded events) of their own kind."""
    if not customer_id or not camera_ids:
        return []
    marks = ",".join("?" for _ in camera_ids)
    with connection() as db:
        entitled = {r["analytic_key"] for r in db.execute(
            f"SELECT DISTINCT analytic_key FROM camera_analytics_entitlements WHERE status='active' AND camera_id IN ({marks})",
            tuple(camera_ids))}
        rule_kinds: set[str] = set()
        if "smart_motion" in entitled:
            rule_kinds = {r["rule_type"] for r in db.execute(
                f"SELECT DISTINCT rule_type FROM customer_analytics_rules WHERE customer_id=? AND camera_id IN ({marks})",
                (customer_id, *camera_ids))}
            rule_kinds |= {r["event_type"] for r in db.execute(
                f"SELECT DISTINCT event_type FROM detection_events WHERE customer_id=? AND camera_id IN ({marks}) "
                "AND event_type IN ('line_crossing','intrusion')", (customer_id, *camera_ids))}
    out = []
    for key, spec in WORKSPACES.items():
        if spec["entitlement"] not in entitled:
            continue
        if spec.get("rule") and key not in rule_kinds:
            continue
        out.append({"key": key, "slug": spec["slug"], "label": spec["label"], "href": f"/analytics/{spec['slug']}",
                    "description": spec["description"]})
    return out


def register_customer_analytics_routes(app: FastAPI, customer_cameras: Callable[[Request], list[dict] | None],
                                       customer_identity: Callable[[Request], dict | None]) -> None:
    @app.get("/api/customer/analytics/{key}/events")
    def customer_analytics_events(request: Request, key: str, start_ms: int, end_ms: int, camera_id: str = "",
                                  result: str = "", q: str = "", before: str = "") -> dict:
        cameras = customer_cameras(request)
        identity = customer_identity(request)
        if cameras is None or not identity:
            raise HTTPException(status_code=403, detail="Customer access is required.")
        if key not in WORKSPACES:
            raise HTTPException(status_code=404, detail="Unknown analytic.")
        if end_ms <= start_ms or end_ms - start_ms > MAX_RANGE_DAYS * 86400000:
            raise HTTPException(status_code=400, detail=f"Choose a date range of up to {MAX_RANGE_DAYS} days.")
        permitted = [c["id"] for c in cameras]
        with connection() as db:
            entitled = entitled_camera_ids(db, permitted, key)
        if camera_id:
            # One camera, or a site's cameras (comma-separated) -- each must
            # be one this caller may see; unentitled ones simply have no data.
            requested = [item for item in camera_id.split(",") if item]
            if not requested or any(item not in permitted for item in requested):
                raise HTTPException(status_code=404, detail="Camera not found.")
            scope = [item for item in requested if item in entitled]
        else:
            scope = entitled
        data = query_events(customer_id=identity["customer_id"], camera_ids=scope, key=key, start_ms=start_ms,
                            end_ms=end_ms, result=result, q=q[:60], before=before or None)
        data["enabled_camera_ids"] = entitled
        return data


# ---------------------------------------------------------------- pages

def _camera_data(cameras: list[dict]) -> list[dict]:
    ids = [c["id"] for c in cameras]
    sites: dict = {}
    if ids:
        with connection() as db:
            sites = {r["camera_id"]: {"id": r["site_id"], "name": r["site_name"]} for r in db.execute(
                f"SELECT c.id AS camera_id, s.id AS site_id, s.name AS site_name FROM cameras c LEFT JOIN sites s ON s.id=c.site_id "
                f"WHERE c.id IN ({','.join('?' for _ in ids)})", tuple(ids)).fetchall()}
    return [{"id": c["id"], "name": c.get("name") or f"Camera {c.get('camera_number') or ''}".strip(),
             "site_id": (sites.get(c["id"]) or {}).get("id"), "site_name": (sites.get(c["id"]) or {}).get("name")}
            for c in cameras]


def _identity(request: Request) -> dict:
    from partner_portal import partner_identity
    return partner_identity(request) or {}


PAGE_CSS = """<link rel="stylesheet" href="/static/inline_media.css"><style>
.aw-filters{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:4px 0 14px}
.aw-filter{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
.aw-filter[hidden]{display:none}
.aw-filter select,.aw-filter input{min-height:40px;padding:7px 10px;border:1px solid rgba(170,196,207,.3);border-radius:9px;background:#111827;color:#fff;font:inherit;font-size:14px;color-scheme:dark}
.aw-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.aw-stat{padding:14px 16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:rgba(24,33,50,.94)}
.aw-stat span{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}
.aw-stat strong{font-size:22px}
.aw-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.aw-card{display:flex;flex-direction:column;border-radius:14px;overflow:hidden;background:rgba(24,33,50,.94);border:1px solid rgba(170,196,207,.14);cursor:pointer;color:inherit;text-align:left;padding:0;font:inherit}
.aw-card:hover,.aw-card:focus-visible{border-color:rgba(67,209,204,.6);outline:none}
.aw-card[aria-expanded="true"]{border-color:#43d1cc;box-shadow:0 0 0 1px #43d1cc}
.aw-thumb{position:relative;aspect-ratio:16/9;background:#0b1018;display:grid;place-items:center;color:var(--muted);font-size:12px}
.aw-thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.aw-badge{position:absolute;top:8px;left:8px;padding:3px 8px;border-radius:999px;background:rgba(8,10,14,.78);color:#fff;font-size:11px;font-weight:800;letter-spacing:.02em}
.aw-badge.bad{background:#7b2331}.aw-badge.good{background:#1f5b4d}
.aw-play{position:absolute;right:8px;bottom:8px;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:rgba(8,10,14,.72);color:#fff;font-size:14px}
.aw-plate{position:absolute;left:8px;bottom:8px;padding:4px 10px;border-radius:6px;background:#f5f1d6;color:#111;font:800 15px/1.1 ui-monospace,Consolas,monospace;letter-spacing:.06em}
.aw-body{display:flex;flex-direction:column;gap:3px;padding:10px 12px 12px}
.aw-body strong{font-size:15px}
.aw-meta{color:var(--muted);font-size:12px;line-height:1.45}
.aw-empty{grid-column:1/-1;padding:26px;border:1px dashed rgba(170,196,207,.3);border-radius:12px;color:var(--muted);text-align:center}
.aw-trend{display:flex;align-items:end;gap:6px;min-height:130px;padding:12px;border-radius:12px;background:#111827;margin-bottom:14px;overflow-x:auto}
.aw-bar{display:flex;flex-direction:column;align-items:center;gap:4px;min-width:46px;font-size:11px;color:var(--muted)}
.aw-bar div{display:flex;gap:3px;align-items:end;height:90px}
.aw-bar i{display:block;width:16px;border-radius:4px 4px 0 0;background:#43d1cc}
.aw-bar i.out{background:#bd2a8b}
.aw-table{width:100%;border-collapse:collapse;margin-bottom:14px;background:rgba(24,33,50,.94);border-radius:12px;overflow:hidden}
.aw-table th,.aw-table td{padding:10px 12px;text-align:left;border-top:1px solid rgba(170,196,207,.12)}
.aw-table th{color:var(--muted);font-size:12px;font-weight:700;border-top:0}
.aw-section{margin:18px 0 10px;font-size:15px}
.aw-landing{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.aw-tile{display:flex;flex-direction:column;gap:6px;padding:18px;border-radius:14px;background:rgba(24,33,50,.94);border:1px solid rgba(170,196,207,.16);color:inherit;text-decoration:none}
.aw-tile:hover,.aw-tile:focus-visible{border-color:rgba(67,209,204,.6);outline:none}
.aw-tile strong{font-size:17px}.aw-tile .aw-count{font-size:26px;font-weight:800}
@media(max-width:640px){.aw-filter{flex:1 1 140px}.aw-grid{grid-template-columns:1fr}.aw-stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>"""


def render_landing(request: Request, cameras: list[dict], page_shell: Callable) -> str:
    identity = _identity(request)
    workspaces = available_workspaces(identity.get("customer_id"), [c["id"] for c in cameras])
    if not workspaces:
        body = ('<section class="panel"><h2>No analytics enabled yet</h2><p class="health-detail">Smart Motion, People Counting, '
                'License Plates, PPE and Facial Recognition are added per camera from your plan.</p>'
                '<a class="action-button" href="/subscription-portal">View plans</a></section>')
    else:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
        tiles = []
        with connection() as db:
            for spec in workspaces:
                scope = entitled_camera_ids(db, [c["id"] for c in cameras], spec["key"])
                types = WORKSPACES[spec["key"]]["types"]
                count = db.execute(
                    f"SELECT COUNT(*) AS n FROM detection_events WHERE customer_id=? AND camera_id IN ({','.join('?' for _ in scope) or 'NULL'}) "
                    f"AND event_type IN ({','.join('?' for _ in types)}) AND event_timestamp>=?",
                    (identity.get("customer_id"), *scope, *types, since.isoformat())).fetchone()["n"] if scope else 0
                tiles.append(f'<a class="aw-tile" href="{spec["href"]}"><strong>{escape(spec["label"])}</strong>'
                             f'<span class="aw-count">{count:,}</span><span class="aw-meta">in the last 24 hours</span>'
                             f'<span class="aw-meta">{escape(spec["description"])}</span></a>')
        body = f'<div class="aw-landing">{"".join(tiles)}</div>'
    content = (PAGE_CSS + '<header class="topbar"><div><p class="eyebrow">Reports</p><h1>Analytics</h1></div>'
               '<a class="ghost-button" href="/investigate">Search evidence</a></header>' + body)
    return page_shell("Analytics", "analytics", content)


def render_workspace(request: Request, cameras: list[dict], page_shell: Callable, slug: str) -> str:
    key = BY_SLUG.get(slug)
    identity = _identity(request)
    if key is None:
        raise HTTPException(status_code=404, detail="Unknown analytic.")
    spec = WORKSPACES[key]
    available = {w["key"] for w in available_workspaces(identity.get("customer_id"), [c["id"] for c in cameras])}
    header = (f'<header class="topbar"><div><p class="eyebrow"><a class="download" href="/analytics">Analytics</a></p>'
              f'<h1>{escape(spec["label"])}</h1><p class="health-detail" style="margin:4px 0 0">{escape(spec["description"])}</p></div></header>')
    if key not in available:
        body = ('<section class="panel"><h2>Not enabled for your cameras</h2><p class="health-detail">'
                f'{escape(spec["label"])} is added per camera from your plan.</p>'
                '<a class="action-button" href="/subscription-portal">View plans</a></section>')
        return page_shell(spec["label"], "analytics", PAGE_CSS + header + body)
    with connection() as db:
        entitled = set(entitled_camera_ids(db, [c["id"] for c in cameras], key))
    camera_data = [c for c in _camera_data(cameras) if c["id"] in entitled]
    selected = request.query_params.get("camera") or ""
    if selected not in entitled:
        selected = ""
    camera_options = '<option value="">All cameras</option>' + "".join(
        f'<option value="{escape(c["id"], quote=True)}"{" selected" if c["id"] == selected else ""}>{escape(c["name"])}</option>'
        for c in camera_data)
    site_names = {c["site_id"]: c["site_name"] for c in camera_data if c["site_id"]}
    site_filter = ""
    if len(site_names) > 1:
        site_filter = ('<label class="aw-filter"><span>Site</span><select id="aw-site"><option value="">All sites</option>'
                       + "".join(f'<option value="{escape(sid, quote=True)}">{escape(name or "Site")}</option>' for sid, name in site_names.items())
                       + "</select></label>")
    result_options = {
        "smart_motion": [["", "Everything"], ["person", "People"], ["vehicle", "Vehicles"], ["motion", "Motion"]],
        "people_counting": [["", "Entries and exits"], ["in", "Entries"], ["out", "Exits"]],
        "ppe": [["", "All checks"], ["violation", "Violations"], ["missing_hard_hat", "Missing hard hat"],
                ["missing_vest", "Missing vest"], ["compliant", "Compliant"]],
        "facial_recognition": [["", "All faces"], ["known", "Recognized people"], ["unknown", "Unknown faces"]],
    }.get(key, [])
    result_filter = ""
    if result_options:
        result_filter = ('<label class="aw-filter"><span>Show</span><select id="aw-result">'
                         + "".join(f'<option value="{escape(v, quote=True)}">{escape(t)}</option>' for v, t in result_options)
                         + "</select></label>")
    search_filter = ""
    if key in SEARCHABLE:
        placeholder = "Plate contains…" if key == "lpr" else "Name contains…"
        search_filter = (f'<label class="aw-filter"><span>Search</span><input id="aw-search" type="search" '
                         f'placeholder="{placeholder}" autocomplete="off" maxlength="60"></label>')
    content = PAGE_CSS + header + f"""
<div class="aw-filters">
<label class="aw-filter"><span>Camera</span><select id="aw-camera">{camera_options}</select></label>
{site_filter}
<label class="aw-filter"><span>Date range</span><select id="aw-range"><option value="today">Today</option><option value="7" selected>Last 7 days</option><option value="30">Last 30 days</option><option value="custom">Custom…</option></select></label>
<label class="aw-filter" id="aw-from-wrap" hidden><span>From</span><input id="aw-from" type="date"></label>
<label class="aw-filter" id="aw-to-wrap" hidden><span>To</span><input id="aw-to" type="date"></label>
{result_filter}{search_filter}
</div>
<div class="aw-stats" id="aw-stats" aria-live="polite"></div>
<div id="aw-extra"></div>
<div class="aw-grid" id="aw-results" aria-label="{escape(spec['label'])} results"><div class="aw-empty">Loading…</div></div>
<div style="text-align:center;margin-top:12px"><button class="ghost-button" id="aw-more" type="button" hidden>Load more</button></div>"""
    config = {"key": key, "slug": slug, "label": spec["label"], "selected": selected}
    scripts = ('<script src="/static/event_media.js"></script><script src="/static/inline_media.js"></script>'
               f'<script>window.__AW={json.dumps({"config": config, "cameras": camera_data})};</script>'
               '<script src="/static/analytics_workspace.js"></script>')
    return page_shell(spec["label"], "analytics", content, scripts)


def nav_menu_html(workspaces: list[dict], active_path: str) -> tuple[str, str, str]:
    """(desktop sidebar item, mobile bar item, mobile sheet) for the one
    Analytics entry. The flyout/sheet list only the analytics given."""
    links = '<a role="menuitem" href="/analytics">All analytics</a>' + "".join(
        f'<a role="menuitem" href="{w["href"]}"{" aria-current=\"page\"" if active_path == w["href"] else ""}>{escape(w["label"])}</a>'
        for w in workspaces)
    active = active_path == "/analytics" or active_path.startswith("/analytics/")
    desktop = (f'<div class="nav-flyout-wrap" data-nav-flyout>'
               f'<a class="{"active" if active else ""}" href="/analytics" aria-haspopup="menu" aria-expanded="false" data-nav-flyout-toggle>'
               f'<span class="nav-icon">▥</span><span>Analytics</span></a>'
               f'<div class="nav-flyout" role="menu" aria-label="Analytics" hidden>{links}</div></div>')
    mobile = (f'<button type="button" class="mobile-analytics-toggle{" active" if active else ""}" aria-haspopup="menu" '
              f'aria-expanded="false" aria-controls="mobile-analytics-sheet">Analytics</button>')
    sheet = f'<div class="mobile-analytics-sheet" id="mobile-analytics-sheet" role="menu" aria-label="Analytics" hidden>{links}</div>'
    return desktop, mobile, sheet


NAV_ASSETS = """<style>
.nav-flyout-wrap{position:relative}
.nav-flyout{position:fixed;z-index:60;min-width:220px;padding:8px;border-radius:12px;background:#111a28;border:1px solid rgba(170,196,207,.2);box-shadow:0 16px 40px rgba(0,0,0,.45);display:grid;gap:2px}
.nav-flyout[hidden]{display:none}
.nav .nav-flyout a,.nav-flyout a{display:block;min-height:0;padding:10px 12px;border-radius:8px;background:transparent;color:#e8eef6;text-decoration:none;text-align:left;font-size:14px;font-weight:650;line-height:1.3}
.nav .nav-flyout a:hover,.nav .nav-flyout a:focus-visible,.nav .nav-flyout a[aria-current="page"]{background:rgba(67,209,204,.14);color:#a7faf4;outline:none}
.mobile-analytics-toggle{border:0;background:transparent;color:inherit;font:inherit;padding:10px 4px;border-radius:10px;cursor:pointer}
.mobile-analytics-toggle.active,.mobile-analytics-toggle[aria-expanded="true"]{background:#193329;color:#7ee8c7}
.mobile-analytics-sheet{position:fixed;z-index:10000;left:12px;right:12px;bottom:78px;padding:8px;border-radius:15px;background:rgba(17,26,40,.98);border:1px solid rgba(170,196,207,.22);box-shadow:0 -10px 30px rgba(0,0,0,.4);display:grid;gap:2px}
.mobile-analytics-sheet[hidden]{display:none}
.mobile-analytics-sheet a{display:block;padding:13px 14px;border-radius:10px;color:#e8eef6;text-decoration:none;font-weight:700}
.mobile-analytics-sheet a:active,.mobile-analytics-sheet a[aria-current="page"]{background:rgba(67,209,204,.14);color:#a7faf4}
@media(min-width:761px){.mobile-analytics-sheet,.mobile-analytics-sheet:not([hidden]){display:none}}
@media(max-width:760px){.mobile-nav{grid-template-columns:repeat(5,1fr)!important}.mobile-nav a,.mobile-analytics-toggle{font-size:12px;padding:10px 2px}}
</style><script>(function(){
  const wrap=document.querySelector('[data-nav-flyout]');
  if(wrap){
    const toggle=wrap.querySelector('[data-nav-flyout-toggle]'),menu=wrap.querySelector('.nav-flyout');
    const place=()=>{const r=toggle.getBoundingClientRect();menu.style.left=(r.right+8)+'px';menu.style.top=Math.max(8,Math.min(r.top,innerHeight-menu.offsetHeight-8))+'px'};
    const open=()=>{menu.hidden=false;place();toggle.setAttribute('aria-expanded','true')};
    const close=()=>{menu.hidden=true;toggle.setAttribute('aria-expanded','false')};
    // Mouse: hovering shows the list and a click opens the overview.
    // Keyboard/touch: the entry toggles the list (first item focused).
    const hover=matchMedia('(hover:hover) and (pointer:fine)').matches;
    let leaveTimer=null;
    toggle.addEventListener('click',e=>{
      if(hover&&e.detail>0)return;
      e.preventDefault();menu.hidden?open():close();
      if(!menu.hidden){const first=menu.querySelector('a');if(first&&e.detail===0)first.focus()}
    });
    if(hover){
      wrap.addEventListener('mouseenter',()=>{clearTimeout(leaveTimer);open()});
      wrap.addEventListener('mouseleave',()=>{leaveTimer=setTimeout(close,250)});
    }
    document.addEventListener('click',e=>{if(!wrap.contains(e.target))close()});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!menu.hidden){close();toggle.focus()}});
    addEventListener('resize',()=>{if(!menu.hidden)place()});
  }
  const mToggle=document.querySelector('.mobile-analytics-toggle'),sheet=document.getElementById('mobile-analytics-sheet');
  if(mToggle&&sheet){
    const setOpen=v=>{sheet.hidden=!v;mToggle.setAttribute('aria-expanded',v?'true':'false')};
    mToggle.addEventListener('click',e=>{e.stopPropagation();setOpen(sheet.hidden)});
    document.addEventListener('click',e=>{if(!sheet.hidden&&!sheet.contains(e.target))setOpen(false)});
    document.addEventListener('keydown',e=>{if(e.key==='Escape')setOpen(false)});
  }
})();</script>"""
