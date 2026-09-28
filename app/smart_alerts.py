"""Smart Alerts as the customer's attention inbox (2026-09-28).

Smart Alerts answers "what needs my attention, and what can I do about
it?"; the Events page stays the complete history. So this page:

- shows only what still needs attention by default (not acknowledged or
  dismissed), with Saved and Handled views one tap away;
- groups repetitive alerts -- the same camera and event type arriving
  close together -- into one card with a count, instead of a wall of
  near-identical cards;
- filters by category: All | People | Vehicles | Security | System;
- puts the actions on the card: View clip, Live, Talk (talk-capable
  cameras), Acknowledge, Dismiss, Save;
- keeps AAC Voice Call alerts as individual calls with Answer / View
  camera;
- pins every unacknowledged AAC Secure Edge INTRUSION ALARM at the top,
  never grouped, with Live, Talk, View clip, Call 911 and Acknowledge.

Rendering only: main.py supplies the rows (tenant-scoped, the caller's own
notification rows) and the link helpers, so this module has no database or
routing of its own.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Callable

GROUP_GAP_SECONDS = 30 * 60
NEVER_GROUPED = frozenset({"intrusion_alarm", "aac_voice_call"})
CATEGORIES = ("all", "people", "vehicles", "security", "system")
CATEGORY_LABELS = {"all": "All", "people": "People", "vehicles": "Vehicles", "security": "Security", "system": "System"}
_CATEGORY_BY_TYPE = {
    "person": "people", "ppe": "people", "people_counting": "people", "people_counting_in": "people",
    "people_counting_out": "people", "face": "people",
    "vehicle": "vehicles", "car": "vehicles", "truck": "vehicles", "bus": "vehicles", "motorcycle": "vehicles",
    "bicycle": "vehicles", "lpr": "vehicles", "plate": "vehicles",
    "intrusion_alarm": "security", "aac_voice_call": "security", "facial_recognition": "security",
    "line_crossing": "security", "intrusion": "security", "door_access": "security",
    "storage_problem": "system", "camera_offline": "system", "appliance_offline": "system", "low_disk": "system",
    "high_cpu": "system", "software_update": "system", "recording_problem": "system",
}
VIEWS = ("active", "saved", "handled")


def category_for(event_type: str | None, camera_id: str | None = None) -> str:
    """People / vehicles / security / system, or "other" (motion and
    anything unrecognised -- listed under All only). An alert without a
    camera is a system alert."""
    event_type = str(event_type or "")
    if event_type in _CATEGORY_BY_TYPE:
        return _CATEGORY_BY_TYPE[event_type]
    return "system" if not camera_id else "other"


def _parse(timestamp: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(timestamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def group_alerts(items: list[dict], *, gap_seconds: int = GROUP_GAP_SECONDS) -> list[dict]:
    """Collapse runs of the same camera + event type into one group while
    each alert arrives within gap_seconds of the previous one in that run.
    Intrusion alarms and Voice Calls always stay individual. Input and
    output are newest first; active intrusion alarms come first."""
    ordered = sorted(items, key=lambda n: str(n.get("timestamp") or ""), reverse=True)
    open_groups: dict[tuple, dict] = {}
    groups: list[dict] = []
    for item in ordered:
        event_type = str(item.get("event_type") or "")
        when = _parse(item.get("timestamp"))
        key = (item.get("camera_id"), event_type)
        group = None if event_type in NEVER_GROUPED else open_groups.get(key)
        if group is not None and when and group["oldest_at"] and (group["oldest_at"] - when).total_seconds() <= gap_seconds:
            group["items"].append(item)
            group["oldest_at"] = when
            continue
        group = {"key": key, "event_type": event_type, "camera_id": item.get("camera_id"), "items": [item],
                 "latest": item, "latest_at": when, "oldest_at": when,
                 "category": category_for(event_type, item.get("camera_id"))}
        groups.append(group)
        if event_type not in NEVER_GROUPED:
            open_groups[key] = group
    for group in groups:
        group["count"] = len(group["items"])
        group["pinned"] = group["event_type"] == "intrusion_alarm" and not group["latest"].get("acknowledged_at")
    return sorted(groups, key=lambda g: not g["pinned"])  # stable: pinned alarms first, otherwise newest first


def _time_label(timestamp: str | None, tz) -> str:
    parsed = _parse(timestamp)
    if not parsed:
        return str(timestamp or "Unknown time")
    return parsed.astimezone(tz).strftime("%b %d · %I:%M %p").replace(" 0", " ")


def _thumb(group: dict) -> str:
    latest = group["latest"]
    camera_id, event_id = latest.get("camera_id"), latest.get("event_id")
    if latest.get("thumbnail"):
        src = str(latest["thumbnail"])
    elif camera_id and event_id and group["event_type"] != "aac_voice_call":
        src = f"/api/customer/events/{camera_id}/{event_id}/thumbnail?size=card"
    else:
        return '<div class="sa-thumb sa-thumb--empty" aria-hidden="true">♢</div>'
    return (f'<img class="sa-thumb" loading="lazy" src="{escape(src, quote=True)}" alt="" '
            "onerror=\"this.replaceWith(Object.assign(document.createElement('div'),{className:'sa-thumb sa-thumb--empty',textContent:'♢'}))\">")


def _ids(group: dict) -> str:
    return escape(",".join(str(item["id"]) for item in group["items"]), quote=True)


def render_inbox(groups: list[dict], *, view: str, tz, alert_text: Callable[[dict], tuple[str, str]],
                 clip_href: Callable[[dict], str | None], talk_camera_ids: set, counts: dict) -> tuple[str, str]:
    """(content, scripts) for the Smart Alerts page."""
    cards = []
    for group in groups:
        latest = group["latest"]
        camera_id = latest.get("camera_id")
        camera_name = latest.get("camera_name") or "System"
        title, message = alert_text(latest)
        when = _time_label(latest.get("timestamp"), tz)
        if group["count"] > 1:
            when = f'{_time_label(group["items"][-1].get("timestamp"), tz)} – {when.split(" · ")[-1]}'
        is_saved = any(item.get("bookmarked_at") for item in group["items"])
        actions = []
        if group["event_type"] == "aac_voice_call" and latest.get("event_id"):
            call = f'/aac/voice-call/{escape(str(latest["event_id"]), quote=True)}'
            actions += [f'<a class="action-button" href="{call}">Answer</a>', f'<a class="ghost-button" href="{call}">View camera</a>']
        else:
            clip = clip_href(latest)
            if clip:
                # clip_href returns an HTML-ready href (its parts already escaped).
                actions.append(f'<a class="ghost-button" href="{clip}">View clip</a>')
            if camera_id:
                live = f'/customer/cameras/{escape(str(camera_id), quote=True)}/live'
                if group["event_type"] == "intrusion_alarm" and latest.get("event_id"):
                    live += f'?alarm={escape(str(latest["event_id"]), quote=True)}'
                actions.append(f'<a class="ghost-button" href="{live}">Live</a>')
                if camera_id in talk_camera_ids:
                    actions.append(f'<a class="ghost-button" href="{live}" title="Opens the live view; press and hold the microphone to talk">Talk</a>')
        if group["event_type"] == "intrusion_alarm":
            actions.append('<a class="sa-911" href="tel:911">Call 911</a>')
        if view != "handled":
            actions.append(f'<button type="button" class="action-button" data-sa-action="acknowledge" data-ids="{_ids(group)}">Acknowledge</button>')
            if group["event_type"] != "intrusion_alarm":
                actions.append(f'<button type="button" class="ghost-button" data-sa-action="dismiss" data-ids="{_ids(group)}">Dismiss</button>')
        actions.append(f'<button type="button" class="ghost-button" data-sa-action="save" data-saved="{"1" if is_saved else "0"}" '
                       f'data-ids="{_ids(group)}">{"Saved ★" if is_saved else "Save"}</button>')
        count_badge = f'<span class="sa-count">×{group["count"]}</span>' if group["count"] > 1 else ""
        attrs = (f'data-sa-card data-category="{group["category"]}" data-event-type="{escape(group["event_type"], quote=True)}" '
                 f'data-ids="{_ids(group)}"')
        head = (f'{_thumb(group)}<div class="sa-text"><strong>{escape(title)} {count_badge}</strong>'
                f'<span>{escape(camera_name)} · {escape(when)}</span>'
                f'{f"<span class=sa-msg>{escape(message)}</span>" if message and message != title else ""}</div>')
        body = f'<div class="sa-actions">{"".join(actions)}</div>'
        if group["pinned"]:
            cards.append(f'<article class="sa-card sa-alarm" role="alert" {attrs}><div class="sa-head">'
                         f'<span class="sa-alarm-label">INTRUSION ALARM</span>{head}</div>{body}</article>')
        else:
            cards.append(f'<details class="sa-card" {attrs}><summary class="sa-head">{head}</summary>{body}</details>')

    filters = "".join(
        f'<button type="button" class="sa-filter{" active" if key == "all" else ""}" data-sa-filter="{key}" aria-pressed="{str(key == "all").lower()}">'
        f'{CATEGORY_LABELS[key]} <span class="sa-filter-count">{counts.get(key, 0)}</span></button>'
        for key in CATEGORIES
    )
    views = "".join(
        f'<a class="sa-view{" active" if key == view else ""}" href="/alerts?view={key}">{label}</a>'
        for key, label in (("active", "Needs attention"), ("saved", "Saved"), ("handled", "Handled"))
    )
    empty = {"active": "Nothing needs your attention right now.", "saved": "No saved alerts yet.",
             "handled": "Nothing acknowledged in the last 7 days."}[view]
    content = SMART_ALERTS_CSS + f'''<header class="topbar"><div><p class="eyebrow">Smart alerts</p><h1>What needs your attention</h1></div>
<div class="sa-top-links"><a class="ghost-button" href="/events">Full history in Events</a> <a class="ghost-button" href="/settings/notifications">Alert settings</a></div></header>
<nav class="sa-views" aria-label="Alert views">{views}</nav>
<div class="sa-filters" role="toolbar" aria-label="Filter alerts">{filters}</div>
<section class="sa-list" id="sa-list">{"".join(cards) or f'<div class="empty-stage" id="sa-empty">{empty}</div>'}</section>
<div class="empty-stage" id="sa-filter-empty" hidden>No alerts in this category.</div>'''
    return content, SMART_ALERTS_SCRIPT


SMART_ALERTS_CSS = '''<style>
.sa-views{display:flex;gap:8px;margin:0 0 12px;flex-wrap:wrap}
.sa-view{padding:6px 12px;border-radius:999px;border:1px solid #334155;color:inherit;text-decoration:none;font-size:14px}
.sa-view.active{background:#2dd4bf;color:#0b1220;border-color:#2dd4bf;font-weight:700}
.sa-filters{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px;margin-bottom:12px;-webkit-overflow-scrolling:touch}
.sa-filter{flex:0 0 auto;min-height:40px;padding:6px 14px;border-radius:999px;border:1px solid #475467;background:transparent;color:inherit;font-weight:600;cursor:pointer}
.sa-filter.active{background:#e5e7eb;color:#0b1220;border-color:#e5e7eb}
.sa-filter-count{opacity:.7;font-weight:400;margin-left:2px}
.sa-list{display:grid;gap:10px}
.sa-card{border:1px solid #1f2a3a;border-radius:12px;background:rgba(15,23,42,.6);overflow:hidden}
.sa-head{display:flex;gap:12px;align-items:center;padding:10px 12px;cursor:pointer;list-style:none}
.sa-head::-webkit-details-marker{display:none}
.sa-thumb{flex:0 0 auto;width:112px;height:63px;border-radius:8px;object-fit:cover;background:#0b1018}
.sa-thumb--empty{display:grid;place-items:center;color:#667085;font-size:20px}
.sa-text{display:flex;flex-direction:column;gap:2px;min-width:0}
.sa-text strong{font-size:15px}.sa-text span{color:#98a2b3;font-size:13px;overflow:hidden;text-overflow:ellipsis}
.sa-msg{white-space:nowrap}
.sa-count{display:inline-block;margin-left:6px;padding:0 8px;border-radius:999px;background:#334155;font-size:12px;vertical-align:middle}
.sa-actions{display:flex;flex-wrap:wrap;gap:8px;padding:0 12px 12px}
.sa-actions>*{min-height:40px;display:inline-flex;align-items:center}
.sa-alarm{border:2px solid #b42318;background:rgba(180,35,24,.16)}
.sa-alarm .sa-head{cursor:default;flex-wrap:wrap}
.sa-alarm-label{width:100%;color:#f97066;font-weight:800;letter-spacing:.04em}
.sa-911{background:#b42318;color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none;font-weight:700}
@media(max-width:560px){.sa-thumb{width:84px;height:48px}.sa-top-links{display:none}.sa-msg{display:none}}
</style>'''

SMART_ALERTS_SCRIPT = '''<script>
(function(){
  const list=document.getElementById('sa-list');
  const filterEmpty=document.getElementById('sa-filter-empty');
  let current='all';
  function cards(){return [...document.querySelectorAll('[data-sa-card]')];}
  function applyFilter(){
    let shown=0;
    cards().forEach(card=>{const ok=current==='all'||card.dataset.category===current;card.hidden=!ok;if(ok)shown++;});
    filterEmpty.hidden=shown>0||!cards().length;
  }
  function recount(){
    const counts={all:0,people:0,vehicles:0,security:0,system:0};
    cards().forEach(card=>{const n=card.dataset.ids.split(',').length;counts.all+=n;if(card.dataset.category in counts)counts[card.dataset.category]+=n;});
    document.querySelectorAll('[data-sa-filter]').forEach(b=>{const c=b.querySelector('.sa-filter-count');if(c)c.textContent=counts[b.dataset.saFilter]||0;});
  }
  document.querySelectorAll('[data-sa-filter]').forEach(button=>button.addEventListener('click',()=>{
    current=button.dataset.saFilter;
    document.querySelectorAll('[data-sa-filter]').forEach(b=>{const on=b===button;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});
    applyFilter();
  }));
  list&&list.addEventListener('click',async event=>{
    const button=event.target.closest('[data-sa-action]');
    if(!button)return;
    event.preventDefault();
    const ids=button.dataset.ids.split(',').filter(Boolean);
    const action=button.dataset.saAction;
    const saved=button.dataset.saved==='1';
    const url=action==='save'?'/api/customer/notifications/bookmark':`/api/customer/notifications/${action}`;
    const body=action==='save'?{ids:ids,saved:!saved}:{ids:ids};
    button.disabled=true;
    try{
      const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!response.ok)throw new Error('HTTP '+response.status);
      const card=button.closest('[data-sa-card]');
      if(action==='save'){button.dataset.saved=saved?'0':'1';button.textContent=saved?'Save':'Saved ★';}
      else if(card){card.remove();recount();applyFilter();if(!cards().length){list.innerHTML='<div class="empty-stage">Nothing needs your attention right now.</div>';}}
    }catch(e){if(typeof showToast==='function')showToast('Could not update this alert. Please try again.');}
    finally{button.disabled=false;}
  });
  applyFilter();
})();
</script>'''
