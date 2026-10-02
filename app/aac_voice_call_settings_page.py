"""Visitor Voice Call settings page (2026-09-29).

Until now a customer had no way to turn AAC Voice Call on for a camera, or
to change its greeting: the owner-only APIs existed
(aac_voice_call.register_aac_voice_call_routes) but no page used them, so
enabling the feature needed support. This page lists the customer's own
cameras and, for each, lets the account owner:

- use it as an entrance camera (visitors are greeted and you get a Voice Call);
- set the camera's own greeting (blank = the site/default greeting);
- choose the greeting volume (Low / Medium / High, greeting audio only).

Capability-driven: a camera whose speaker (talkback) was detected can greet;
one without can still be an entrance camera (you still get the Voice Call),
and the page says visitors won't hear a greeting there. Viewers see the
settings read-only. Nothing here is camera- or customer-specific.
"""
from __future__ import annotations

import json
from html import escape

import aac_voice_call_events as store
from partner_db import rows

GREETING_MAX_LENGTH = 300


def _speaker_note(talk_down_supported) -> tuple[str, str]:
    if talk_down_supported == 1:
        return "ok", "Speaker detected: visitors hear the greeting."
    if talk_down_supported == 0:
        return "warn", "No speaker detected: you still get the Voice Call, but visitors won't hear a greeting."
    return "wait", "Checking this camera for a speaker…"


def render_settings(identity: dict) -> tuple[str, str]:
    customer_id = identity["customer_id"]
    is_owner = identity.get("role") == "customer_owner"
    cameras = rows(
        "SELECT id,name,camera_number,site_id,talk_down_supported FROM cameras "
        "WHERE customer_id=? AND status NOT IN ('removed','pending_installation') ORDER BY camera_number",
        (customer_id,),
    )
    if not is_owner:  # a household member sees only cameras their grant covers
        from aac_voice_call import camera_permitted
        cameras = [camera for camera in cameras if camera_permitted(identity, camera["id"])]
    entrance = {item["camera_id"]: item for item in store.list_entrance_cameras(customer_id)}
    disabled = "" if is_owner else " disabled"
    cards = []
    for camera in cameras:
        config = entrance.get(camera["id"]) or {}
        enabled = bool(config.get("enabled"))
        volume = store.normalize_greeting_volume(config.get("greeting_volume")) or store.DEFAULT_GREETING_VOLUME
        tone, note = _speaker_note(camera["talk_down_supported"])
        label = camera["name"] or f"Camera {camera['camera_number']}"
        options = "".join(
            f'<option value="{level}"{" selected" if level == volume else ""}>{level.title()}</option>'
            for level in store.GREETING_VOLUMES
        )
        cid = escape(camera["id"], quote=True)
        cards.append(f'''
        <article class="panel vc-camera" data-camera-id="{cid}">
          <div class="panel-head"><div><h2>{escape(label)}</h2>
            <div class="health-detail vc-speaker vc-{tone}">{escape(note)}</div></div></div>
          <label class="vc-toggle"><input type="checkbox" class="vc-enabled"{" checked" if enabled else ""}{disabled}>
            Use as an entrance camera</label>
          <div class="vc-details"{"" if enabled else " hidden"}>
            <label>Greeting<textarea class="vc-greeting" maxlength="{GREETING_MAX_LENGTH}" rows="3"
              placeholder="{escape(store.DEFAULT_GREETING_TEXT, quote=True)}"{disabled}>{escape(config.get("greeting_text") or "")}</textarea></label>
            <div class="health-detail">Leave blank to use the standard greeting shown above.</div>
            <label>Greeting volume<select class="vc-volume"{disabled}>{options}</select></label>
            <div class="health-detail">Changes only how loud the greeting is. Talking through the camera and recordings are not affected.</div>
            {'<button class="action-button vc-save" type="button">Save greeting</button>' if is_owner else ''}
          </div>
        </article>''')

    owner_note = "" if is_owner else (
        '<div class="health-detail" style="margin-top:8px">Only the account owner can change these settings.</div>')
    empty = ('<section class="panel"><div class="empty">No cameras are set up on your account yet. '
             'Once a camera is added, you can choose it as an entrance camera here.</div></section>')
    content = f'''
    <style>
      .vc-camera label{{display:grid;gap:6px;margin-top:12px}}
      .vc-camera .vc-toggle{{display:flex;align-items:center;gap:10px;font-weight:600}}
      .vc-camera textarea,.vc-camera select{{width:100%;max-width:520px;box-sizing:border-box;padding:9px 11px;border:1px solid rgba(170,196,207,.3);
        border-radius:9px;background:#111827;color:#fff;font:inherit;font-size:15px}}
      .vc-camera select{{max-width:220px;min-height:40px}}
      .vc-camera .action-button{{margin-top:14px}}
      .vc-ok{{color:#8df0ea}} .vc-warn{{color:#f5c26b}} .vc-wait{{color:var(--muted,#9aa7b5)}}
      .vc-list{{display:grid;gap:14px;margin-top:14px}}
    </style>
    <header class="topbar">
      <div><p class="eyebrow">Settings</p><h1>Visitor Voice Call</h1></div>
      <a class="ghost-button" href="/customer-app-settings">All settings</a>
    </header>
    <section class="panel">
      <div class="health-detail">When someone comes to an entrance camera, AnyAiCam greets them through the camera's
      speaker, listens to what they say, and sends you a Voice Call you can answer live from your phone.</div>{owner_note}
    </section>
    <div class="vc-list">{"".join(cards) if cards else empty}</div>'''

    scripts = '''<script>
    (function(){
      const isOwner=''' + json.dumps(is_owner) + ''';
      async function post(url,body){
        const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
        let data={};try{data=await response.json()}catch(e){}
        if(!response.ok)throw new Error(data.detail||'Could not save. Please try again.');
        return data;
      }
      document.querySelectorAll('.vc-camera').forEach(card=>{
        const id=encodeURIComponent(card.dataset.cameraId);
        const toggle=card.querySelector('.vc-enabled'),details=card.querySelector('.vc-details');
        if(!isOwner)return;
        toggle.addEventListener('change',async()=>{
          toggle.disabled=true;
          try{
            const r=await post(`/api/customer/aac/voice-call/entrance-cameras/${id}?enabled=${toggle.checked}`);
            details.hidden=!toggle.checked;showToast(r.message||'Saved.');
          }catch(e){toggle.checked=!toggle.checked;showToast(e.message)}
          finally{toggle.disabled=false}
        });
        const save=card.querySelector('.vc-save');
        if(save)save.addEventListener('click',async()=>{
          save.disabled=true;
          try{
            const text=card.querySelector('.vc-greeting').value.trim();
            await post(`/api/customer/aac/voice-call/entrance-cameras/${id}/greeting`,{greeting_text:text||null});
            await post(`/api/customer/aac/voice-call/entrance-cameras/${id}/greeting-volume`,{volume:card.querySelector('.vc-volume').value});
            showToast('Greeting saved. Your AnyAiCam appliance picks it up within a minute.');
          }catch(e){showToast(e.message)}
          finally{save.disabled=false}
        });
      });
    })();
    </script>'''
    return content, scripts
