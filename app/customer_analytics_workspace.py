"""Customer Analytics workspace (2026-09-25).

The portal customer's full analytics reporting view across cameras --
Smart Motion, People Counting, LPR, PPE and Facial Recognition -- at
/analytics (main.analytics() hands portal customers here, the same way
/events and /playback branch). The camera detail page keeps only a
concise per-camera summary and links here pre-filtered to that camera.

Existing data only: detection_events (tenant-scoped by customer_id and
the caller's permitted cameras), the detection_event_media join for the
event's clip/thumbnail, and each analytic's own stored fields (PPE
hard-hat/vest flags, facial match state/name, people-counting direction,
LPR plate text when the appliance sent it). Nothing new is stored and no
detection logic changes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request

from customer_analytics_panel import ANALYTIC_LABELS, _parse_detections, _ppe_status, epoch_ms, real_confidence
from partner_db import connection

TABS = ("smart_motion", "people_counting", "lpr", "ppe", "facial_recognition")
TAB_LABELS = {
    "smart_motion": "Smart Motion",
    "people_counting": "People Counting",
    "lpr": "License Plates",
    "ppe": "PPE",
    "facial_recognition": "Facial Recognition",
}
# Customer-selectable result filters per analytic -> stored event types or
# a stored-field predicate. Only values the data actually carries.
VEHICLE_TYPES = ("vehicle", "car", "truck", "bus", "motorcycle", "bicycle")
RESULT_FILTERS: dict[str, dict[str, tuple[str, ...]]] = {
    "smart_motion": {"person": ("person",), "vehicle": VEHICLE_TYPES, "motion": ("motion", "smart_motion"),
                     "zone": ("intrusion", "line_crossing")},
    "people_counting": {"in": ("people_counting_in",), "out": ("people_counting_out",)},
    "lpr": {},
    "ppe": {"violation": (), "compliant": ()},
    "facial_recognition": {"known": (), "unknown": ()},
}
PAGE_SIZE = 50
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
    if key == "ppe":
        # Status is decided from the stored hard-hat/vest flags (see
        # customer_analytics_panel._ppe_status); filtered after fetch.
        return "", []
    if key == "facial_recognition":
        return "", []
    types = RESULT_FILTERS[key][result]
    return f" AND de.event_type IN ({','.join('?' for _ in types)})", list(types)


def _post_filter(key: str, result: str, item: dict) -> bool:
    if key == "ppe" and result:
        return item["details"]["status"] == result
    if key == "facial_recognition" and result:
        state = item["details"]["state"]
        return (state == "known") if result == "known" else (state != "known")
    return True


def query_events(*, customer_id: str, camera_ids: list[str], key: str, start_ms: int, end_ms: int,
                 result: str = "", before: str | None = None, limit: int = PAGE_SIZE) -> dict:
    if key not in TABS:
        raise HTTPException(status_code=404, detail="Unknown analytic.")
    if not camera_ids:
        return {"events": [], "summary": _empty_summary(key), "next_before": None}
    event_types = ANALYTIC_LABELS[key][1]
    result_sql, result_args = _result_clause(key, result)
    base_where = (f"de.customer_id=? AND de.camera_id IN ({','.join('?' for _ in camera_ids)}) "
                  f"AND de.event_type IN ({','.join('?' for _ in event_types)}) "
                  "AND de.event_timestamp>=? AND de.event_timestamp<?")
    base_args = [customer_id, *camera_ids, *event_types, _utc_iso(start_ms), _utc_iso(end_ms)]
    page_sql = base_where + result_sql + (" AND de.event_timestamp<?" if before else "")
    page_args = base_args + result_args + ([before] if before else [])
    fetch = limit * 4 if key in ("ppe", "facial_recognition") and result else limit
    with connection() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT de.id, de.camera_id, de.event_type, de.confidence, de.object_count, de.detections_json, de.event_timestamp, "
            "CASE WHEN dem.id IS NULL THEN 0 ELSE 1 END AS has_clip, "
            "CASE WHEN length(COALESCE(dem.thumbnail_s3_key, ''))>0 THEN 1 ELSE 0 END AS has_thumbnail "
            "FROM detection_events de LEFT JOIN detection_event_media dem ON dem.detection_event_id=de.id "
            f"WHERE {page_sql} ORDER BY de.event_timestamp DESC LIMIT ?", (*page_args, fetch + 1)).fetchall()]
        summary = _summary(db, key, base_where, base_args)
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


def _empty_summary(key: str) -> dict:
    return {"total": 0}


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
    if key in ("ppe", "facial_recognition"):
        counts: dict[str, int] = {}
        for r in db.execute(f"SELECT de.detections_json FROM detection_events de WHERE {where}", args):
            details = _row_details(key, dict(r))
            bucket = (details["status"] or "unknown") if key == "ppe" else ("known" if details["state"] == "known" else "unknown")
            counts[bucket] = counts.get(bucket, 0) + 1
        out["by_result"] = counts
    return out


def _enabled_camera_ids(db, camera_ids: list[str], key: str) -> list[str]:
    if not camera_ids:
        return []
    return [r["camera_id"] for r in db.execute(
        "SELECT camera_id FROM camera_analytics_entitlements WHERE analytic_key=? AND status='active' "
        f"AND camera_id IN ({','.join('?' for _ in camera_ids)})", (key, *camera_ids))]


def register_customer_analytics_routes(app: FastAPI, customer_cameras: Callable[[Request], list[dict] | None],
                                       customer_identity: Callable[[Request], dict | None]) -> None:
    @app.get("/api/customer/analytics/{key}/events")
    def customer_analytics_events(request: Request, key: str, start_ms: int, end_ms: int, camera_id: str = "",
                                  result: str = "", before: str = "") -> dict:
        cameras = customer_cameras(request)
        identity = customer_identity(request)
        if cameras is None or not identity:
            raise HTTPException(status_code=403, detail="Customer access is required.")
        if end_ms <= start_ms or end_ms - start_ms > MAX_RANGE_DAYS * 86400000:
            raise HTTPException(status_code=400, detail=f"Choose a date range of up to {MAX_RANGE_DAYS} days.")
        permitted = [c["id"] for c in cameras]
        if camera_id:
            # One camera, or a site's cameras (comma-separated) -- every id
            # must be one this caller may see.
            requested = [item for item in camera_id.split(",") if item]
            if not requested or any(item not in permitted for item in requested):
                raise HTTPException(status_code=404, detail="Camera not found.")
            permitted = requested
        data = query_events(customer_id=identity["customer_id"], camera_ids=permitted, key=key, start_ms=start_ms,
                            end_ms=end_ms, result=result, before=before or None)
        with connection() as db:
            data["enabled_camera_ids"] = _enabled_camera_ids(db, [c["id"] for c in cameras], key)
        return data


def render_page(request: Request, cameras: list[dict], page_shell: Callable) -> str:
    params = request.query_params
    tab = params.get("type") if params.get("type") in TABS else "smart_motion"
    camera_ids = {c["id"] for c in cameras}
    selected_camera = params.get("camera") if params.get("camera") in camera_ids else ""
    with connection() as db:
        site_rows = db.execute(
            f"SELECT c.id AS camera_id, s.id AS site_id, s.name AS site_name FROM cameras c LEFT JOIN sites s ON s.id=c.site_id "
            f"WHERE c.id IN ({','.join('?' for _ in camera_ids)})", tuple(camera_ids)).fetchall() if camera_ids else []
    sites = {r["camera_id"]: {"id": r["site_id"], "name": r["site_name"]} for r in site_rows}
    camera_data = [{"id": c["id"], "name": c.get("name") or f"Camera {c.get('camera_number') or ''}".strip(),
                    "site_id": (sites.get(c["id"]) or {}).get("id"), "site_name": (sites.get(c["id"]) or {}).get("name")}
                   for c in cameras]
    tabs = "".join(
        f'<button class="filter{" active" if key == tab else ""}" type="button" role="tab" data-tab="{key}" '
        f'aria-selected="{"true" if key == tab else "false"}">{escape(TAB_LABELS[key])}</button>' for key in TABS)
    camera_options = '<option value="">All cameras</option>' + "".join(
        f'<option value="{escape(c["id"], quote=True)}"{" selected" if c["id"] == selected_camera else ""}>{escape(c["name"])}</option>'
        for c in camera_data)
    site_names = {c["site_id"]: c["site_name"] for c in camera_data if c["site_id"]}
    site_filter = ""
    if len(site_names) > 1:
        site_filter = ('<label class="analytics-filter"><span>Site</span><select id="analytics-site"><option value="">All sites</option>'
                       + "".join(f'<option value="{escape(sid, quote=True)}">{escape(name or "Site")}</option>' for sid, name in site_names.items())
                       + "</select></label>")
    content = f"""<style>
.analytics-tabs{{margin:0 0 14px}}
.analytics-filters{{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-bottom:14px}}
.analytics-filter{{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}}
.analytics-filter[hidden]{{display:none}}
.analytics-filter select,.analytics-filter input{{min-height:38px;padding:7px 10px;border:1px solid rgba(170,196,207,.3);border-radius:9px;background:#111827;color:#fff;font:inherit;font-size:14px}}
.analytics-stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}}
.analytics-stat{{padding:14px 16px;border:1px solid rgba(170,196,207,.18);border-radius:12px;background:rgba(24,33,50,.94)}}
.analytics-stat span{{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}}
.analytics-stat strong{{font-size:22px}}
.analytics-list{{display:grid;gap:8px}}
.analytics-item{{display:grid;grid-template-columns:112px minmax(0,1fr) auto;gap:14px;align-items:center;padding:10px;border:1px solid rgba(170,196,207,.14);border-radius:12px;background:rgba(24,33,50,.94);color:inherit;text-decoration:none}}
a.analytics-item:hover,a.analytics-item:focus-visible{{border-color:rgba(67,209,204,.55);outline:none}}
.analytics-item img,.analytics-item .analytics-noimg{{width:112px;height:63px;border-radius:8px;object-fit:cover;background:#0b1018}}
.analytics-noimg{{display:grid;place-items:center;color:var(--muted);font-size:20px}}
.analytics-item strong{{display:block;margin-bottom:3px}}
.analytics-meta{{color:var(--muted);font-size:12px;line-height:1.5}}
.analytics-go{{color:#8df0ea;font-size:12px;font-weight:700;white-space:nowrap}}
.analytics-trend{{display:flex;align-items:end;gap:6px;min-height:120px;padding:12px;border-radius:12px;background:#111827;margin-bottom:14px;overflow-x:auto}}
.analytics-bar{{display:flex;flex-direction:column;align-items:center;gap:4px;min-width:44px;font-size:11px;color:var(--muted)}}
.analytics-bar i{{display:block;width:16px;border-radius:4px 4px 0 0;background:#43d1cc}}
.analytics-bar i.out{{background:#bd2a8b}}
.analytics-bar div{{display:flex;gap:3px;align-items:end;height:90px}}
.analytics-empty{{padding:26px;border:1px dashed rgba(170,196,207,.3);border-radius:12px;color:var(--muted);text-align:center}}
@media(max-width:640px){{.analytics-item{{grid-template-columns:84px minmax(0,1fr)}}.analytics-item img,.analytics-item .analytics-noimg{{width:84px;height:47px}}.analytics-go{{grid-column:2}}.analytics-filter{{flex:1 1 140px}}}}
</style>
<header class="topbar"><div><p class="eyebrow">Reports</p><h1>Analytics</h1></div><a class="ghost-button" href="/investigate">Search evidence</a></header>
<div class="filter-row analytics-tabs" id="analytics-tabs" role="tablist" aria-label="Analytics type">{tabs}</div>
<div class="analytics-filters">
<label class="analytics-filter"><span>Camera</span><select id="analytics-camera">{camera_options}</select></label>
{site_filter}
<label class="analytics-filter"><span>Date range</span><select id="analytics-range"><option value="today">Today</option><option value="7" selected>Last 7 days</option><option value="30">Last 30 days</option><option value="custom">Custom…</option></select></label>
<label class="analytics-filter" id="analytics-from-wrap" hidden><span>From</span><input id="analytics-from" type="date"></label>
<label class="analytics-filter" id="analytics-to-wrap" hidden><span>To</span><input id="analytics-to" type="date"></label>
<label class="analytics-filter" id="analytics-result-wrap"><span>Result</span><select id="analytics-result"></select></label>
</div>
<div class="analytics-stats" id="analytics-stats" aria-live="polite"></div>
<div id="analytics-trend-wrap"></div>
<div class="analytics-list" id="analytics-list"><div class="analytics-empty">Loading…</div></div>
<div style="text-align:center;margin-top:12px"><button class="ghost-button" id="analytics-more" type="button" hidden>Load more</button></div>"""
    scripts = f"""<script>(function(){{
const CAMERAS={json.dumps(camera_data)};
const RESULT_OPTIONS={json.dumps({
        "smart_motion": [["", "All results"], ["person", "People"], ["vehicle", "Vehicles"], ["motion", "Motion"], ["zone", "Zones & lines"]],
        "people_counting": [["", "Entries and exits"], ["in", "Entries"], ["out", "Exits"]],
        "lpr": [],
        "ppe": [["", "All checks"], ["violation", "Violations"], ["compliant", "Compliant"]],
        "facial_recognition": [["", "All faces"], ["known", "Recognized people"], ["unknown", "Unknown faces"]],
    })};
const LABELS={{motion:'Motion detected',smart_motion:'Motion detected',person:'Person detected',vehicle:'Vehicle detected',car:'Car detected',truck:'Truck detected',bus:'Bus detected',motorcycle:'Motorcycle detected',bicycle:'Bicycle detected',intrusion:'Zone intrusion',line_crossing:'Line crossed',people_counting_in:'Person entered',people_counting_out:'Person left',plate:'License plate read'}};
const cameraName=Object.fromEntries(CAMERAS.map(c=>[c.id,c.name]));
const state={{tab:{json.dumps(tab)},camera:{json.dumps(selected_camera)},site:'',range:'7',from:'',to:'',result:'',before:null,loading:false}};
const $=id=>document.getElementById(id);
const list=$('analytics-list'),stats=$('analytics-stats'),more=$('analytics-more'),trend=$('analytics-trend-wrap');
function esc(v){{return String(v==null?'':v).replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[ch])}}
function label(type){{const k=String(type||'').toLowerCase();return LABELS[k]||(k?k.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase())+' detected':'Activity detected')}}
function when(ms){{
  if(typeof ms!=='number')return '';
  const d=new Date(ms),now=new Date(),y=new Date(now.getFullYear(),now.getMonth(),now.getDate()-1);
  const t=d.toLocaleTimeString([],{{hour:'numeric',minute:'2-digit'}});
  if(d.toDateString()===now.toDateString())return `Today, ${{t}}`;
  if(d.toDateString()===y.toDateString())return `Yesterday, ${{t}}`;
  return d.toLocaleDateString([],{{weekday:'short',month:'short',day:'numeric'}})+`, ${{t}}`;
}}
function localDay(offsetDays){{const n=new Date();return new Date(n.getFullYear(),n.getMonth(),n.getDate()+offsetDays)}}
function range(){{
  if(state.range==='today')return [localDay(0).getTime(),localDay(1).getTime()];
  if(state.range==='custom'&&state.from&&state.to){{
    const [fy,fm,fd]=state.from.split('-').map(Number),[ty,tm,td]=state.to.split('-').map(Number);
    return [new Date(fy,fm-1,fd).getTime(),new Date(ty,tm-1,td+1).getTime()];
  }}
  const days=Number(state.range)||7;return [localDay(1-days).getTime(),localDay(1).getTime()];
}}
function cameraIds(){{
  if(state.camera)return [state.camera];
  return state.site?CAMERAS.filter(c=>c.site_id===state.site).map(c=>c.id):null;
}}
function href(e){{
  const cam=encodeURIComponent(e.camera_id);
  if(e.has_clip)return `/playback?camera=${{cam}}&event=${{encodeURIComponent(e.event_id)}}&autoplay=event`;
  return typeof e.timestamp_ms==='number'?`/playback?camera=${{cam}}&t=${{e.timestamp_ms}}&autoplay=event`:null;
}}
function pct(v){{return v==null?null:Math.round(Number(v)*100)+'% confidence'}}
function title(e){{
  const d=e.details||{{}};
  if(state.tab==='ppe')return d.status==='violation'?'PPE violation':d.status==='compliant'?'PPE compliant':'PPE check';
  if(state.tab==='facial_recognition')return d.person||(d.state==='known'?'Recognized person':'Unknown face');
  if(state.tab==='lpr')return d.plate?`Plate ${{d.plate}}`:'License plate read';
  return label(e.event_type);
}}
function extra(e){{
  const d=e.details||{{}};
  if(state.tab==='ppe'){{
    const part=(name,v)=>v==null?null:`${{name}} ${{v?'✓':'✗ missing'}}`;
    return [part('Hard hat',d.hard_hat),part('Vest',d.vest)];
  }}
  if(state.tab==='facial_recognition')return [d.watchlist?`Watchlist: ${{d.watchlist}}`:null,pct(e.confidence)];
  if(state.tab==='lpr')return [d.plate?null:'Plate text kept on the appliance',pct(e.confidence)];
  if(state.tab==='smart_motion')return [d.object_count>1?`${{d.object_count}} objects`:null,pct(e.confidence)];
  return [];
}}
function item(e){{
  const link=href(e);
  const img=e.has_thumbnail?`<img src="/api/customer/events/${{encodeURIComponent(e.camera_id)}}/${{encodeURIComponent(e.event_id)}}/thumbnail" alt="" loading="lazy" onerror="this.style.visibility='hidden'">`:'<span class="analytics-noimg" aria-hidden="true">◴</span>';
  const meta=[cameraName[e.camera_id]||'Camera',when(e.timestamp_ms),...extra(e)].filter(Boolean).map(esc).join(' · ');
  const go=link?`<span class="analytics-go">${{e.has_clip?'Play clip':'Open in Playback'}} <span aria-hidden="true">→</span></span>`:'';
  const inner=`${{img}}<span><strong>${{esc(title(e))}}</strong><span class="analytics-meta">${{meta}}</span></span>${{go}}`;
  return link?`<a class="analytics-item" href="${{esc(link)}}">${{inner}}</a>`:`<div class="analytics-item">${{inner}}</div>`;
}}
function stat(name,value){{return `<div class="analytics-stat"><span>${{esc(name)}}</span><strong>${{esc(value)}}</strong></div>`}}
function renderSummary(s,enabledIds){{
  const t=s.total||0,by=s.by_type||{{}},res=s.by_result||{{}};
  let html='';
  if(state.tab==='smart_motion'){{
    const sum=keys=>keys.reduce((n,k)=>n+(by[k]||0),0);
    html=stat('Events',t)+stat('People',sum(['person']))+stat('Vehicles',sum(['vehicle','car','truck','bus','motorcycle','bicycle']))+stat('Motion',sum(['motion','smart_motion']));
  }}else if(state.tab==='people_counting'){{
    const i=by.people_counting_in||0,o=by.people_counting_out||0;
    html=stat('Entries',i)+stat('Exits',o)+stat('Net change',(i-o>0?'+':'')+(i-o));
  }}else if(state.tab==='ppe'){{
    html=stat('Checks',t)+stat('Violations',res.violation||0)+stat('Compliant',res.compliant||0);
  }}else if(state.tab==='facial_recognition'){{
    html=stat('Faces',t)+stat('Recognized',res.known||0)+stat('Unknown',res.unknown||0);
  }}else{{
    html=stat('Plates read',t);
  }}
  const scope=cameraIds()||CAMERAS.map(c=>c.id);
  if(!scope.some(id=>enabledIds.includes(id)))html+=`<div class="analytics-stat"><span>Not enabled on ${{scope.length===1?'this camera':'these cameras'}}</span><a class="download" href="/subscription-portal">View plans</a></div>`;
  stats.innerHTML=html;
  trend.innerHTML='';
  if(state.tab==='people_counting'&&Array.isArray(s.hourly)&&s.hourly.length){{
    const days={{}};
    s.hourly.forEach(h=>{{const d=new Date(h.hour_ms);const k=d.toLocaleDateString([],{{month:'short',day:'numeric'}});const e=days[k]||(days[k]={{in:0,out:0}});e.in+=h.in;e.out+=h.out}});
    const max=Math.max(1,...Object.values(days).flatMap(d=>[d.in,d.out]));
    trend.innerHTML=`<div class="analytics-trend" role="img" aria-label="Entries and exits per day">${{Object.entries(days).map(([k,d])=>`<div class="analytics-bar" title="${{esc(k)}}: ${{d.in}} in, ${{d.out}} out"><div><i style="height:${{Math.round(d.in/max*90)}}px"></i><i class="out" style="height:${{Math.round(d.out/max*90)}}px"></i></div>${{esc(k)}}</div>`).join('')}}</div>`;
  }}
}}
async function load(append){{
  if(state.loading)return;state.loading=true;
  if(!append){{state.before=null;list.innerHTML='<div class="analytics-empty">Loading…</div>'}}
  const [start,end]=range();
  const ids=cameraIds();
  const q=new URLSearchParams({{start_ms:start,end_ms:end,result:state.result}});
  if(state.before)q.set('before',state.before);
  try{{
    if(ids!==null&&!ids.length){{list.innerHTML='<div class="analytics-empty">No cameras at this site.</div>';stats.innerHTML='';more.hidden=true;return}}
    if(ids)q.set('camera_id',ids.join(','));
    const r=await fetch(`/api/customer/analytics/${{state.tab}}/events?${{q}}`,{{credentials:'same-origin'}});
    const body=await r.json().catch(()=>({{}}));
    if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:'Analytics could not be loaded.');
    const events=body.events,summary=body.summary,enabled=body.enabled_camera_ids||[],next=body.next_before;
    if(!append)renderSummary(summary,enabled);
    const html=events.map(item).join('');
    if(append)list.insertAdjacentHTML('beforeend',html);
    else list.innerHTML=html||`<div class="analytics-empty">No ${{esc(({{smart_motion:'Smart Motion events',people_counting:'entries or exits',lpr:'license plate reads',ppe:'PPE checks',facial_recognition:'face detections'}})[state.tab])}} in this period.</div>`;
    state.before=next;more.hidden=!next;
  }}catch(error){{
    if(!append)list.innerHTML=`<div class="analytics-empty">${{esc(error.message)}}</div>`;
  }}finally{{state.loading=false}}
}}
function syncUrl(){{
  const q=new URLSearchParams();q.set('type',state.tab);if(state.camera)q.set('camera',state.camera);
  history.replaceState(null,'',`/analytics?${{q}}`);
}}
function renderResultOptions(){{
  const opts=RESULT_OPTIONS[state.tab]||[];
  $('analytics-result-wrap').hidden=!opts.length;
  $('analytics-result').innerHTML=opts.map(([v,t])=>`<option value="${{v}}">${{esc(t)}}</option>`).join('');
  state.result='';
}}
$('analytics-tabs').addEventListener('click',e=>{{
  const b=e.target.closest('[data-tab]');if(!b)return;
  state.tab=b.dataset.tab;
  [...$('analytics-tabs').children].forEach(x=>{{x.classList.toggle('active',x===b);x.setAttribute('aria-selected',x===b?'true':'false')}});
  renderResultOptions();syncUrl();load(false);
}});
$('analytics-camera').addEventListener('change',e=>{{state.camera=e.target.value;syncUrl();load(false)}});
if($('analytics-site'))$('analytics-site').addEventListener('change',e=>{{
  state.site=e.target.value;state.camera='';
  const sel=$('analytics-camera');[...sel.options].forEach(o=>{{if(!o.value)return;const c=CAMERAS.find(x=>x.id===o.value);o.hidden=!!state.site&&c.site_id!==state.site}});sel.value='';
  syncUrl();load(false);
}});
$('analytics-range').addEventListener('change',e=>{{
  state.range=e.target.value;const custom=state.range==='custom';
  $('analytics-from-wrap').hidden=!custom;$('analytics-to-wrap').hidden=!custom;
  if(custom){{const today=localDay(0),week=localDay(-6);const f=d=>`${{d.getFullYear()}}-${{String(d.getMonth()+1).padStart(2,'0')}}-${{String(d.getDate()).padStart(2,'0')}}`;
    if(!state.from){{state.from=f(week);$('analytics-from').value=state.from}}if(!state.to){{state.to=f(today);$('analytics-to').value=state.to}}}}
  load(false);
}});
['analytics-from','analytics-to'].forEach(id=>$(id).addEventListener('change',e=>{{state[id==='analytics-from'?'from':'to']=e.target.value;if(state.from&&state.to&&state.from<=state.to)load(false)}}));
$('analytics-result').addEventListener('change',e=>{{state.result=e.target.value;load(false)}});
more.addEventListener('click',()=>load(true));
renderResultOptions();load(false);
}})();</script>"""
    return page_shell("Analytics", "analytics", content, scripts)
