"""Phase 6d (docs/AI_HANDOFF.md Sec 8): the cloud-specific live-view
frontend surface -- a standalone page, in the cloud `partner_identity()`
auth system Phase 1-6c already use, that drives the full customer flow:
click -> start-session (Phase 6c) -> poll the playlist route (Phase 6b)
until it has real segments -> attach hls.js -> stop-session (Phase 6c)
on explicit stop or page unload.

Deliberately NOT built on `/customer-portal` (`app/customer_platform.py`):
that page's own auth (`current_user()`/`authenticated_user()`) reads a
completely different cookie/session mechanism (itsdangerous-signed,
backed by main.py's local `load_sessions()`/`load_users()`) than
`partner_identity()` (HMAC-signed, backed by `partner_db`'s
`user_sessions` table) -- a pre-existing mismatch confirmed during Phase
6d's own investigation, not something this phase fixes; it is worked
around by living entirely in the correct (cloud) auth system instead.

Authorization logic (customer_owner role, camera ownership, can_live
permission) is deliberately duplicated here rather than imported from
live_playlist.py/live_view_sessions.py -- the same explicit scope
decision those two modules already made relative to each other.

No AWS/S3 access, no new backend contract: this module only ever reads
`cameras`/`partner_users`/`customer_camera_permissions` to decide
whether to render the page at all, then hands the browser three URLs
(the Phase 6c start route, the Phase 6c stop route, and the Phase 6b
playlist route) -- every actual state transition (session creation,
relay start/stop, segment delivery) is driven entirely by those
already-reviewed, unchanged routes.
"""

import json
from datetime import datetime
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from partner_db import connection
from partner_portal import partner_identity
from customer_analytics_panel import analytics_row_state, camera_entitlement_rows, event_types_for_analytic, summarize, UPGRADE_CARD_CONTENT, assign_entitlement, remove_entitlement, LicenseLimitExceeded
from camera_access import is_camera_authorized, set_camera_access, remove_camera_access, ACCESS_MODES

POLL_INTERVAL_MS = 2000
POLL_TIMEOUT_MS = 45000

# Shared press-and-hold push-to-talk client, used identically by both
# the /customer-live grid (one call per tile) and the single-camera
# page (one call for its one mic button) -- written once here rather
# than duplicated per page, unlike this module's usual per-page-
# duplication convention, since audio capture/transport logic is
# meaningfully more failure-prone than the small per-page auth checks
# that convention exists for.
#
# Mic permission is requested only on the first real press (inside
# start(), never at page load) -- getUserMedia() itself throws for
# denied/unavailable devices, caught and surfaced as a toast rather
# than left as an unhandled rejection. The WebSocket is opened only
# after a real mic stream exists, and carries the browser's actual
# AudioContext sample rate as a query parameter so the appliance-side
# transcode step never has to guess it. A silent GainNode (gain=0) is
# interposed between the capture ScriptProcessorNode and the audio
# destination -- required by some browsers for onaudioprocess to fire
# reliably, without which the mic's own input would otherwise be
# audibly routed back out to the speakers.
_TALK_MIC_JS = """
function wireTalkMic(button, cameraId) {
  // Press-and-hold state, keyed by a monotonically increasing generation
  // token (pressId) rather than a single boolean -- this is what makes
  // the async pointer lifecycle race-free. start() is async and awaits
  // three separate things in sequence (the REST /talk/start fetch, its
  // response.json(), then getUserMedia()); a release (pointerup/
  // pointercancel/pointerleave/pagehide) can land after any one of
  // those awaits resumes. Every resume point below re-checks
  // `myPress === pressId` -- the token captured synchronously at this
  // press's own pointerdown, before any await ran -- and refuses to
  // proceed (including refusing to ever construct a WebSocket) once a
  // release or a newer press has invalidated it. This is what
  // previously let a stale start() resume after stop() had already
  // cleared sessionId to null and open a WebSocket at
  // /sessions/null/audio.
  let pressId = 0;
  let held = false;
  let ws = null, audioCtx = null, mediaStream = null, processor = null, source = null, silentGain = null;
  let sessionId = null;   // the REST-created session this press currently owns, if any
  let wsOpened = false;   // true only once this session's WebSocket has actually finished connecting

  function cleanupOrphanSession(sid) {
    // Best-effort: releases a REST-created talk session that will never
    // get a WebSocket (because the press that created it ended, or
    // permission was denied, before the socket could open), so no
    // 'requested' row is left behind for talk_sessions.py's own
    // expiry sweep to have to age out later. Fire-and-forget -- the
    // stop route is already idempotent server-side, and there is no
    // UI state left to update for a press that's already over.
    fetch(`/api/customer/talk/sessions/${sid}/stop`, { method: 'POST' }).catch(() => {});
  }

  function teardownLocal() {
    if (processor) { try { processor.disconnect() } catch (e) {} }
    if (source) { try { source.disconnect() } catch (e) {} }
    if (silentGain) { try { silentGain.disconnect() } catch (e) {} }
    if (audioCtx) { try { audioCtx.close() } catch (e) {} }
    if (mediaStream) { mediaStream.getTracks().forEach(track => track.stop()) }
    if (ws) { try { ws.close() } catch (e) {} }
    ws = null; audioCtx = null; mediaStream = null; processor = null; source = null; silentGain = null;
  }

  function stop(event) {
    // event is only present for a real pointerup/pointercancel/
    // pointerleave -- stop() is also called internally (ws.onclose,
    // ws.onerror, a stale press's own cleanup) with no event at all,
    // so every event-only call below is guarded. preventDefault()/
    // stopPropagation() here are defensive, matching pointerdown's own
    // handling below; releasePointerCapture() is technically automatic
    // on pointerup/pointercancel, but calling it explicitly costs
    // nothing and removes any doubt.
    if (event) {
      try { event.preventDefault(); } catch (e) {}
      try { event.stopPropagation(); } catch (e) {}
      if (event.pointerId !== undefined) {
        try { button.releasePointerCapture(event.pointerId); } catch (e) {}
      }
    }
    held = false;
    pressId++;   // invalidates any in-flight start() still awaiting something for the press that just ended
    button.classList.remove('active');
    const sid = sessionId;
    const openedBeforeStop = wsOpened;
    sessionId = null;
    wsOpened = false;
    teardownLocal();
    if (sid && !openedBeforeStop) {
      // Released after /talk/start created a session but before the
      // WebSocket finished opening (still mid-fetch, mid-getUserMedia,
      // or mid-handshake) -- nothing else will ever clean this session
      // up, since the WebSocket route (the only other place that marks
      // it 'stopped') never got a chance to run.
      cleanupOrphanSession(sid);
    }
  }

  async function start(event) {
    // Mobile press-and-hold on a button that sits near/over a playing
    // <video> tile is a well-known source of touch-event conflicts --
    // without these, a sustained touch-hold can be interpreted by the
    // browser's own default gesture handling as also targeting the
    // video underneath (observed as the video pausing on press and
    // resuming on release). preventDefault() stops the browser's
    // default touch handling for this pointerdown; stopPropagation()
    // keeps it from reaching any ancestor handler; setPointerCapture()
    // pins every subsequent pointer event for this exact touch (move,
    // up, cancel) to this button specifically, so a finger drifting
    // slightly during the hold can never be reinterpreted as
    // interacting with whatever is underneath it. touch-action:none in
    // this button's own CSS is the equivalent instruction at the CSS
    // layer, for browsers that decide gesture handling before any JS
    // runs at all. None of this touches press-and-hold semantics --
    // it only ever runs once, synchronously, at the very top of the
    // same pointerdown handler that already existed.
    if (event) {
      try { event.preventDefault(); } catch (e) {}
      try { event.stopPropagation(); } catch (e) {}
      try { button.setPointerCapture(event.pointerId); } catch (e) {}
    }
    if (button.disabled || held) return;
    held = true;
    const myPress = ++pressId;

    let response;
    try {
      response = await fetch(`/api/customer/cameras/${cameraId}/talk/start`, { method: 'POST' });
    } catch (e) {
      if (myPress === pressId) held = false;
      return;
    }
    if (!response.ok) {
      if (myPress === pressId) held = false;
      return;
    }
    const body = await response.json();
    const sid = body && body.session_id;
    if (!sid) {
      if (myPress === pressId) held = false;
      return;
    }

    if (myPress !== pressId) {
      // Released (or superseded by a newer press) while /talk/start was
      // in flight -- stop() already ran and had no sessionId to see yet,
      // so this press is the only one that knows this session exists.
      cleanupOrphanSession(sid);
      return;
    }
    sessionId = sid;   // now visible to stop(), which takes over orphan cleanup from here if released

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      if (myPress === pressId) { held = false; sessionId = null; }
      showToast('Microphone permission denied or unavailable.');
      cleanupOrphanSession(sid);
      return;
    }

    if (myPress !== pressId) {
      // Released while the permission prompt was pending. stop() already
      // saw sessionId set and released it itself -- this stream simply
      // arrived too late to be used; stop its tracks immediately so a
      // granted-but-unwanted mic stays off.
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    mediaStream = stream;

    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    source = audioCtx.createMediaStreamSource(mediaStream);
    processor = audioCtx.createScriptProcessor(2048, 1, 1);
    silentGain = audioCtx.createGain();
    silentGain.gain.value = 0;

    // Always built from the local `sid` captured above, never the
    // shared `sessionId` -- this is the specific guarantee that a
    // WebSocket can never be constructed with a null/empty session id,
    // independent of anything stop() may have done concurrently.
    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${wsProtocol}//${location.host}/api/customer/talk/sessions/${sid}/audio?sample_rate=${audioCtx.sampleRate}`);
    socket.binaryType = 'arraybuffer';
    socket.onopen = () => {
      if (myPress !== pressId) { try { socket.close() } catch (e) {} return; }
      wsOpened = true;
      button.classList.add('active');
    };
    socket.onclose = () => { if (myPress === pressId) stop(); };
    socket.onerror = () => { if (myPress === pressId) stop(); };
    ws = socket;

    processor.onaudioprocess = (event) => {
      if (myPress !== pressId || !ws || ws.readyState !== WebSocket.OPEN) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm16 = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        const clamped = Math.max(-1, Math.min(1, input[i]));
        pcm16[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7FFF;
      }
      ws.send(pcm16.buffer);
    };
    source.connect(processor);
    processor.connect(silentGain);
    silentGain.connect(audioCtx.destination);
  }

  button.addEventListener('pointerdown', start);
  button.addEventListener('pointerup', (event) => stop(event));
  button.addEventListener('pointercancel', (event) => stop(event));
  button.addEventListener('pointerleave', (event) => stop(event));
  window.addEventListener('pagehide', () => stop());
}
"""


def _talk_down_state(supported) -> dict:
    """Maps the raw tri-state talk_down_supported column value (NULL /
    0 / 1 -- see db_migrations.py's own note on why this is tri-state,
    not boolean) into the exact enabled/tooltip pair the mic button
    template needs. Never touches whether THIS identity is additionally
    permitted to talk (can_talk) -- see _customer_live_cameras(), which
    combines both before this ever reaches the page."""
    if supported == 1:
        return {"enabled": True, "tooltip": None}
    if supported == 0:
        return {"enabled": False, "tooltip": "Talk-down not supported by this camera"}
    return {"enabled": False, "tooltip": "Talk-down capability not verified"}


def _customer_live_cameras(db, identity: dict) -> list[dict]:
    """Every camera this identity may view live, scoped to identity's own
    customer_id: the full fleet for customer_owner, or only the subset
    explicitly granted can_live for customer_viewer -- same ownership/
    permission rule _authorized_camera() applies to one camera_id from a
    URL, evaluated here for the whole fleet at once so /customer-live can
    render a grid instead of picking a single camera to redirect to.

    Each returned dict also carries talk_enabled/talk_tooltip -- the
    server-persisted camera CAPABILITY state (talk_down_supported),
    rendered here purely so the page can show the mic button's initial
    state without a separate capability round-trip per camera per page
    load, per the architecture requirement that Live View reads
    persisted state rather than probing on every load. This is a
    capability hint only, not the authorization decision -- whether
    THIS identity may actually start a talk session (customer_owner's
    implicit access vs. customer_viewer's own can_talk=1 grant) is
    re-checked from scratch, server-side, by
    talk_sessions.py's _authorized_talk_camera() at session-start time,
    exactly like every other authorization in this codebase never
    trusts what a page merely rendered. A viewer lacking can_talk still
    sees an enabled-looking mic for a capable camera and gets a real
    403 the moment they try to use it -- a UX gap acceptable for this
    foundation, not a security one."""
    if identity.get('role') == 'customer_owner':
        cameras = [
            dict(camera) for camera in db.execute(
                'SELECT id, name, talk_down_supported FROM cameras WHERE customer_id=? '
                'ORDER BY camera_number, id',
                (identity['customer_id'],),
            ).fetchall()
        ]
    else:
        user = db.execute(
            'SELECT id FROM partner_users WHERE email=?',
            (identity['email'],),
        ).fetchone()
        if not user:
            return []

        cameras = [
            dict(camera) for camera in db.execute(
                'SELECT c.id, c.name, c.talk_down_supported FROM cameras c '
                'JOIN customer_camera_permissions p ON p.camera_id=c.id AND p.user_id=? '
                'WHERE c.customer_id=? AND p.can_live=1 '
                'ORDER BY c.camera_number, c.id',
                (user['id'], identity['customer_id']),
            ).fetchall()
        ]

    for camera in cameras:
        camera.update(_talk_down_state(camera.pop('talk_down_supported')))
    return cameras


def _authorized_camera(db, camera_id: str, identity: dict) -> dict:
    """Camera lookup + can_live permission check, scoped to the
    authenticated customer -- mirrors live_playlist.py's/
    live_view_sessions.py's own inline logic exactly (see module
    docstring for why this is duplicated, not shared). Only used to
    decide whether to render the page at all; the actual start/stop/
    playlist calls this page's own JavaScript makes are independently
    re-checked by Phase 6b/6c's own routes regardless."""
    camera = db.execute(
        'SELECT * FROM cameras WHERE id=? AND customer_id=?',
        (camera_id, identity['customer_id']),
    ).fetchone()
    if not camera:
        raise HTTPException(status_code=404, detail='Camera not found.')

    user = db.execute(
        'SELECT id, camera_access_mode FROM partner_users WHERE email=?',
        (identity['email'],),
    ).fetchone()
    if not user:
        raise HTTPException(status_code=403, detail='Customer owner permission required.')

    permitted_camera_ids = {
        row['camera_id'] for row in db.execute(
            'SELECT camera_id FROM customer_camera_permissions WHERE user_id=? AND can_live=1', (user['id'],)
        ).fetchall()
    }
    if not is_camera_authorized(
        camera_id,
        role=identity.get('role', ''),
        access_mode=user['camera_access_mode'] or 'selected',
        permitted_camera_ids=permitted_camera_ids,
    ):
        raise HTTPException(status_code=403, detail='Not authorized to view this camera live.')

    return dict(camera)


def register_live_view_page_routes(app: FastAPI, page_shell: Callable) -> None:
    @app.get('/customer-live', response_class=HTMLResponse)
    def customer_live_landing(request: Request):
        identity = partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            return RedirectResponse('/partner-login', status_code=303)

        with connection() as db:
            cameras = _customer_live_cameras(db, identity)

        if not cameras:
            # No cameras at all, or (customer_viewer) none explicitly
            # granted can_live -- /customer-account already renders the
            # right fallback for both cases (including bouncing a
            # not-yet-activated customer_owner on to /customer/setup).
            return RedirectResponse('/customer-account', status_code=303)

        columns = 1 if len(cameras) == 1 else 2 if len(cameras) <= 4 else 3 if len(cameras) <= 9 else 4
        camera_ids = [camera['id'] for camera in cameras]

        tiles = ''.join(
            f'''<article class="live-grid-tile" data-camera-id="{escape(camera['id'], quote=True)}">
              <div class="camera-view" style="border-radius:10px">
                <video id="live-grid-video-{escape(camera['id'], quote=True)}" muted playsinline></video>
                <div class="camera-placeholder" id="live-grid-placeholder-{escape(camera['id'], quote=True)}">
                  <span class="signal">◉</span>
                  <strong id="live-grid-status-{escape(camera['id'], quote=True)}">Starting live view…</strong>
                </div>
              </div>
              <div class="live-grid-tile-head">
                <span>{escape(camera.get('name') or camera['id'])}</span>
                <button class="camera-tool talk-mic" id="talk-mic-{escape(camera['id'], quote=True)}"
                  data-camera-id="{escape(camera['id'], quote=True)}"
                  title="{escape(camera['tooltip'] or 'Press and hold to talk')}"
                  aria-label="{escape(camera['tooltip'] or 'Press and hold to talk')}"
                  {'' if camera['enabled'] else 'disabled'}>🎤</button>
                <a class="ghost-button" href="/customer/cameras/{escape(camera['id'], quote=True)}/live">Full screen</a>
              </div>
            </article>'''
            for camera in cameras
        )

        content = (
            f'<header class="topbar"><div><p class="eyebrow">Live view</p><h1>Your cameras</h1></div>'
            f'<a class="ghost-button" href="/customer-account">Account</a></header>'
            f'<style>'
            f'.live-grid{{display:grid;grid-template-columns:repeat({columns},minmax(0,1fr));gap:16px}}'
            f'.live-grid-tile{{display:grid;gap:8px}}'
            f'.live-grid-tile .camera-view{{aspect-ratio:16/9}}'
            f'.live-grid-tile-head{{display:flex;align-items:center;justify-content:space-between;gap:8px}}'
            # touch-action:none -- stops the browser's own default
            # touch-gesture handling for a sustained press-and-hold on
            # this button, so it can never be interpreted as also
            # targeting the video tile underneath/nearby (the exact
            # cause of a real "video pauses on mic press, resumes on
            # release" mobile bug this pairs with pointerdown's own
            # preventDefault()/stopPropagation()/setPointerCapture()).
            f'.talk-mic{{touch-action:none}}'
            f'.talk-mic.active{{background:var(--accent,#42e4dc);color:#04211f}}'
            f'.talk-mic:disabled{{opacity:.4;cursor:not-allowed}}'
            f'@media(max-width:760px){{.live-grid{{grid-template-columns:1fr}}}}'
            f'</style>'
            f'<section class="live-grid">{tiles}</section>'
        )

        scripts = f'''<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>{_TALK_MIC_JS}</script><script>
(function(){{
  const cameraIds={json.dumps(camera_ids)};

  // Punch-list item 1: double-click (desktop) / double-tap (mobile) a
  // grid tile opens the Focused Live View for that camera. Full screen
  // navigation already existed via the explicit button above; this adds
  // the same destination as a faster gesture without replacing it.
  document.querySelectorAll('.live-grid-tile').forEach(tile=>{{
    const openFocused=()=>{{location.href=`/customer/cameras/${{tile.dataset.cameraId}}/live`}};
    tile.addEventListener('dblclick',openFocused);
    let lastTap=0;
    tile.addEventListener('touchend',()=>{{
      const now=Date.now();
      if(now-lastTap<350)openFocused();
      lastTap=now;
    }});
  }});

  const pollIntervalMs={POLL_INTERVAL_MS};
  const pollTimeoutMs={POLL_TIMEOUT_MS};
  const tiles={{}};

  cameraIds.forEach(id=>{{
    tiles[id]={{
      sessionId:null, hls:null, pollTimer:null, stopped:false,
      video:document.getElementById(`live-grid-video-${{id}}`),
      placeholder:document.getElementById(`live-grid-placeholder-${{id}}`),
      status:document.getElementById(`live-grid-status-${{id}}`),
    }};
  }});

  function setStatus(id,text){{tiles[id].status.textContent=text}}
  function stopPolling(id){{if(tiles[id].pollTimer){{clearTimeout(tiles[id].pollTimer);tiles[id].pollTimer=null}}}}

  async function stopSession(id,isUnload){{
    const tile=tiles[id];
    if(!tile.sessionId||tile.stopped)return;
    tile.stopped=true;
    const url=`/api/customer/live/sessions/${{tile.sessionId}}/stop`;
    if(isUnload){{try{{fetch(url,{{method:'POST',keepalive:true}})}}catch(e){{}}}}
    else{{try{{await fetch(url,{{method:'POST'}})}}catch(e){{}}}}
  }}

  function showUnavailable(id){{
    stopPolling(id);
    setStatus(id,'Live view unavailable right now.');
  }}

  async function pollPlaylist(id,deadline){{
    const tile=tiles[id];
    if(tile.stopped)return;
    if(Date.now()>deadline){{showUnavailable(id);return}}
    const playlistUrl=`/api/customer/cameras/${{id}}/live/playlist.m3u8`;
    let response=null;
    try{{response=await fetch(playlistUrl,{{cache:'no-store'}})}}catch(e){{}}
    if(response&&response.ok){{
      const text=await response.text();
      if(text.includes('#EXTINF')){{attachPlayer(id,playlistUrl);return}}
    }}else if(response&&[403,404,409,503].includes(response.status)){{
      showUnavailable(id);return;
    }}
    tile.pollTimer=setTimeout(()=>pollPlaylist(id,deadline),pollIntervalMs);
  }}

  function attachPlayer(id,playlistUrl){{
    const tile=tiles[id];
    stopPolling(id);
    setStatus(id,'Connecting…');
    if(window.Hls&&Hls.isSupported()){{
      tile.hls=new Hls();
      tile.hls.loadSource(playlistUrl);
      tile.hls.attachMedia(tile.video);
      tile.hls.on(Hls.Events.MANIFEST_PARSED,()=>{{tile.placeholder.hidden=true;tile.video.play().catch(()=>{{}})}});
      tile.hls.on(Hls.Events.ERROR,(_,data)=>{{if(data.fatal)setStatus(id,'Reconnecting…')}});
    }}else if(tile.video.canPlayType('application/vnd.apple.mpegurl')){{
      tile.video.src=playlistUrl;
      tile.video.addEventListener('loadedmetadata',()=>{{tile.placeholder.hidden=true;tile.video.play().catch(()=>{{}})}});
    }}else{{
      setStatus(id,'This browser cannot play live video.');
    }}
  }}

  async function startSession(id){{
    const tile=tiles[id];
    tile.stopped=false;setStatus(id,'Starting live view…');
    let response;
    try{{response=await fetch(`/api/customer/cameras/${{id}}/live/start`,{{method:'POST'}})}}catch(e){{showUnavailable(id);return}}
    if(!response.ok){{showUnavailable(id);return}}
    const body=await response.json();
    tile.sessionId=body.session_id;
    pollPlaylist(id,Date.now()+pollTimeoutMs);
  }}

  // Push-to-talk: press-and-hold only, real mic capture, no always-open
  // duplex mode. Each tile's mic is wired entirely independently via
  // wireTalkMic() (see the shared client defined once at module level)
  // -- one camera's press/release/error/permission-denial can never
  // affect another's. Every enabled button here already reflects the
  // server-persisted capability (talk_down_supported) at page-render
  // time; wireTalkMic()'s own start-session call still gets its own
  // fresh rejection if authorization or capability has changed since
  // the page loaded -- this button never assumes the render-time state
  // is still valid.
  document.querySelectorAll('.talk-mic').forEach(button=>{{
    wireTalkMic(button, button.dataset.cameraId);
  }});
  window.addEventListener('pagehide',()=>{{
    cameraIds.forEach(id=>stopSession(id,true));
  }});

  cameraIds.forEach(startSession);
}})();
</script>'''

        return page_shell('Live view', 'live', content, scripts)

    def _require_customer_owner(request: Request) -> dict:
        identity = partner_identity(request)
        if not identity or identity.get('role') != 'customer_owner':
            raise HTTPException(status_code=403, detail='Customer owner permission required.')
        return identity

    @app.post('/api/customer/users/{user_id}/camera-access')
    def set_user_camera_access(request: Request, user_id: str, payload: dict) -> dict:
        """Customer-owner-only: grants a restricted user (customer_viewer)
        'all' cameras or an explicit 'selected' list. Takes effect
        immediately (set_camera_access() deletes and replaces the row set
        in one call) and never crosses into another customer's users or
        cameras -- both are re-scoped to identity['customer_id'] here."""
        identity = _require_customer_owner(request)
        mode = str(payload.get('mode', '')).strip()
        if mode not in ACCESS_MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {ACCESS_MODES}.")
        camera_ids = [str(item) for item in payload.get('camera_ids', [])]
        with connection() as db:
            user = db.execute(
                'SELECT id FROM partner_users WHERE id=? AND customer_id=?',
                (user_id, identity['customer_id']),
            ).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail='User not found.')
            if camera_ids:
                owned = {row['id'] for row in db.execute(
                    'SELECT id FROM cameras WHERE customer_id=?', (identity['customer_id'],)
                ).fetchall()}
                if not set(camera_ids) <= owned:
                    raise HTTPException(status_code=400, detail='One or more camera_ids do not belong to this customer.')
            set_camera_access(db, user_id=user_id, access_mode=mode, camera_ids=camera_ids, now=datetime.now().isoformat())
        return {'message': 'Camera access updated.', 'mode': mode, 'camera_ids': camera_ids if mode == 'selected' else []}

    @app.delete('/api/customer/users/{user_id}/camera-access/{camera_id}')
    def remove_user_camera_access(request: Request, user_id: str, camera_id: str) -> dict:
        identity = _require_customer_owner(request)
        with connection() as db:
            user = db.execute(
                'SELECT id FROM partner_users WHERE id=? AND customer_id=?',
                (user_id, identity['customer_id']),
            ).fetchone()
            if not user:
                raise HTTPException(status_code=404, detail='User not found.')
            remove_camera_access(db, user_id=user_id, camera_id=camera_id)
        return {'message': 'Camera access removed.'}

    @app.post('/api/customer/cameras/{camera_id}/analytics/{analytic_key}')
    def assign_camera_analytic(request: Request, camera_id: str, analytic_key: str) -> dict:
        """Customer-owner-only camera-level entitlement assignment.
        assign_entitlement() itself enforces the licensed_quantity cap
        against analytics_subscriptions (see LicenseLimitExceeded) --
        billing is always the authority on how many camera-seats exist."""
        identity = _require_customer_owner(request)
        with connection() as db:
            camera = db.execute(
                'SELECT id FROM cameras WHERE id=? AND customer_id=?',
                (camera_id, identity['customer_id']),
            ).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail='Camera not found.')
            try:
                assign_entitlement(db, camera_id, analytic_key, now=datetime.now().isoformat())
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            except LicenseLimitExceeded as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        return {'message': 'Analytic enabled for this camera.'}

    @app.delete('/api/customer/cameras/{camera_id}/analytics/{analytic_key}')
    def remove_camera_analytic(request: Request, camera_id: str, analytic_key: str) -> dict:
        identity = _require_customer_owner(request)
        with connection() as db:
            camera = db.execute(
                'SELECT id FROM cameras WHERE id=? AND customer_id=?',
                (camera_id, identity['customer_id']),
            ).fetchone()
            if not camera:
                raise HTTPException(status_code=404, detail='Camera not found.')
            remove_entitlement(db, camera_id, analytic_key, now=datetime.now().isoformat())
        return {'message': 'Analytic removed from this camera.'}

    @app.get('/api/customer/cameras/{camera_id}/analytics')
    def customer_camera_analytics_enabled(request: Request, camera_id: str) -> dict:
        """Every analytic the Focused Live View's row can ever show for
        this camera, each flagged enabled (real results) or not (an
        upgrade-opportunity card) -- the row is never empty, even for a
        camera with nothing purchased yet. See
        customer_analytics_panel.analytics_row_state()'s docstring for the
        site-level subscription scoping."""
        identity = partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            return RedirectResponse('/partner-login', status_code=303)
        with connection() as db:
            camera = _authorized_camera(db, camera_id, identity)
            # Per-camera entitlements (camera_analytics_entitlements), not
            # the site-level analytics_subscriptions billing record -- a
            # customer with 10 cameras at one site can have LPR on 2 of
            # them and nothing on the rest. See
            # customer_analytics_panel's module docstring.
            entitlements = camera_entitlement_rows(db, camera_id)
        analytics = analytics_row_state(entitlements)
        for item in analytics:
            item['upgrade'] = UPGRADE_CARD_CONTENT[item['key']]
        return {'analytics': analytics}

    @app.get('/api/customer/cameras/{camera_id}/analytics/{analytic_key}/summary')
    def customer_camera_analytics_summary(request: Request, camera_id: str, analytic_key: str) -> dict:
        """Most-recent-useful-results for one selected analytic pill --
        never the underlying long recording, never every historical
        detection, just what customer_analytics_panel.summarize() decides
        is relevant for that analytic type."""
        identity = partner_identity(request)
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            return RedirectResponse('/partner-login', status_code=303)
        try:
            event_types = event_types_for_analytic(analytic_key)
        except ValueError as error:
            raise HTTPException(status_code=404, detail='Unknown analytic.') from error
        with connection() as db:
            camera = _authorized_camera(db, camera_id, identity)
            placeholders = ','.join('?' for _ in event_types)
            rows = db.execute(
                f'SELECT event_type, confidence, object_count, detections_json, event_timestamp '
                f'FROM detection_events WHERE camera_id=? AND customer_id=? AND event_type IN ({placeholders}) '
                f'ORDER BY event_timestamp DESC LIMIT 20',
                (camera_id, identity['customer_id'], *event_types),
            ).fetchall()
        return summarize(analytic_key, [dict(row) for row in rows])

    @app.get('/customer/cameras/{camera_id}/live', response_class=HTMLResponse)
    def live_view_page(request: Request, camera_id: str):
        identity = partner_identity(request)
        # Both customer_owner and customer_viewer may reach this page --
        # per-camera authorization (ownership + the can_live permission
        # row) is enforced immediately below by _authorized_camera(),
        # which fails closed for customer_viewer with no explicit grant.
        # This page's JavaScript calls the SAME Phase 6b/6c routes, which
        # apply the identical permission check server-side, so a
        # customer_viewer who reaches this page for a camera they're not
        # granted can_live on gets a real 403 from those routes, not a
        # silently-broken page.
        if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
            return RedirectResponse('/partner-login', status_code=303)

        with connection() as db:
            camera = _authorized_camera(db, camera_id, identity)

        camera_name = camera.get('name') or camera_id
        start_url = f'/api/customer/cameras/{camera_id}/live/start'
        playlist_url = f'/api/customer/cameras/{camera_id}/live/playlist.m3u8'
        talk_state = _talk_down_state(camera.get('talk_down_supported'))
        talk_tooltip = talk_state['tooltip'] or 'Press and hold to talk'

        content = (
            f'<header class="topbar"><div><p class="eyebrow">Live view</p>'
            f'<h1>{escape(camera_name)}</h1></div>'
            f'<a class="ghost-button" href="/customer-live">Back to Live</a></header>'
            f'<style>.talk-mic{{touch-action:none}}.talk-mic.active{{background:var(--accent,#42e4dc);color:#04211f}}.talk-mic:disabled{{opacity:.4;cursor:not-allowed}}</style>'
            f'<section class="panel"><div class="camera-view" style="border-radius:10px">'
            f'<video id="live-view-video" controls muted playsinline></video>'
            f'<div class="camera-placeholder" id="live-view-placeholder">'
            f'<span class="signal">◉</span>'
            f'<strong id="live-view-status">Starting live view…</strong>'
            f'<small>This can take a few seconds.</small></div></div>'
            f'<div class="camera-tools" style="justify-content:center">'
            f'<button class="camera-tool" id="live-view-mute" title="Mute">♪</button>'
            f'<button class="camera-tool talk-mic" id="talk-mic-{escape(camera_id, quote=True)}" '
            f'title="{escape(talk_tooltip)}" aria-label="{escape(talk_tooltip)}" '
            f'{"" if talk_state["enabled"] else "disabled"}>🎤</button>'
            f'<button class="camera-tool" id="live-view-snapshot" title="Snapshot">◉</button>'
            f'<button class="camera-tool" id="live-view-download" title="Download">⬇</button>'
            f'<button class="camera-tool" id="live-view-share" title="Share">↗</button>'
            f'<a class="camera-tool" href="/playback" title="Playback">◴</a>'
            f'<button class="camera-tool" id="live-view-analytics" title="Analytics">⌕</button>'
            f'<button class="camera-tool" id="live-view-bookmark" title="Bookmark">◈</button>'
            f'<button class="camera-tool" id="live-view-stop" title="Stop">◼</button>'
            f'<button class="camera-tool" id="live-view-retry" title="Retry" hidden>↻</button>'
            f'</div></section>'
            f'<section class="panel" style="margin-top:16px" id="live-analytics-section" hidden>'
            f'<div class="panel-head"><div><h2>Analytics</h2></div></div>'
            f'<div id="live-analytics-pills" class="filter-row" role="tablist" aria-label="Camera analytics"></div>'
            f'<div id="live-analytics-panel" class="health-list"></div>'
            f'</section>'
        )

        scripts = f'''<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>{_TALK_MIC_JS}</script><script>
(function(){{
  const cameraId={json.dumps(camera_id)};
  const startUrl={json.dumps(start_url)};
  const playlistUrl={json.dumps(playlist_url)};
  const pollIntervalMs={POLL_INTERVAL_MS};
  const pollTimeoutMs={POLL_TIMEOUT_MS};
  const video=document.getElementById('live-view-video');
  const placeholder=document.getElementById('live-view-placeholder');
  const statusLabel=document.getElementById('live-view-status');
  const muteButton=document.getElementById('live-view-mute');
  const snapshotButton=document.getElementById('live-view-snapshot');
  const downloadButton=document.getElementById('live-view-download');
  const shareButton=document.getElementById('live-view-share');
  const analyticsButton=document.getElementById('live-view-analytics');
  const bookmarkButton=document.getElementById('live-view-bookmark');
  const stopButton=document.getElementById('live-view-stop');
  const retryButton=document.getElementById('live-view-retry');
  let sessionId=null, hls=null, pollTimer=null, stopped=false;

  function setStatus(text){{statusLabel.textContent=text}}
  function stopPolling(){{if(pollTimer){{clearTimeout(pollTimer);pollTimer=null}}}}

  async function stopSession(isUnload){{
    if(!sessionId||stopped)return;
    stopped=true;
    const url=`/api/customer/live/sessions/${{sessionId}}/stop`;
    if(isUnload){{
      try{{fetch(url,{{method:'POST',keepalive:true}})}}catch(e){{}}
    }}else{{
      try{{await fetch(url,{{method:'POST'}})}}catch(e){{}}
    }}
  }}

  function showUnavailable(){{
    stopPolling();
    setStatus('Live view unavailable right now.');
    retryButton.hidden=false;
  }}

  async function pollPlaylist(deadline){{
    if(stopped)return;
    if(Date.now()>deadline){{showUnavailable();return}}
    let response=null;
    try{{response=await fetch(playlistUrl,{{cache:'no-store'}})}}catch(e){{}}
    if(response&&response.ok){{
      const text=await response.text();
      if(text.includes('#EXTINF')){{attachPlayer();return}}
    }}else if(response&&[403,404,409,503].includes(response.status)){{
      // Structural failures never resolve by polling longer -- stop
      // immediately rather than waiting out the full timeout.
      showUnavailable();return;
    }}
    pollTimer=setTimeout(()=>pollPlaylist(deadline),pollIntervalMs);
  }}

  function attachPlayer(){{
    stopPolling();
    setStatus('Connecting…');
    if(window.Hls&&Hls.isSupported()){{
      hls=new Hls();
      hls.loadSource(playlistUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED,()=>{{placeholder.hidden=true;video.play().catch(()=>{{}})}});
      hls.on(Hls.Events.ERROR,(_,data)=>{{if(data.fatal)setStatus('Reconnecting…')}});
    }}else if(video.canPlayType('application/vnd.apple.mpegurl')){{
      video.src=playlistUrl;
      video.addEventListener('loadedmetadata',()=>{{placeholder.hidden=true;video.play().catch(()=>{{}})}});
    }}else{{
      setStatus('This browser cannot play live video.');
    }}
  }}

  async function startSession(){{
    stopped=false;retryButton.hidden=true;setStatus('Starting live view…');
    let response;
    try{{response=await fetch(startUrl,{{method:'POST'}})}}catch(e){{showUnavailable();return}}
    if(!response.ok){{showUnavailable();return}}
    const body=await response.json();
    sessionId=body.session_id;
    pollPlaylist(Date.now()+pollTimeoutMs);
  }}

  muteButton.addEventListener('click',()=>{{video.muted=!video.muted;muteButton.textContent=video.muted?'♪':'♫'}});
  snapshotButton.addEventListener('click',()=>{{
    try{{
      if(!video.videoWidth)throw new Error('not ready');
      const canvas=document.createElement('canvas');
      canvas.width=video.videoWidth;canvas.height=video.videoHeight;
      canvas.getContext('2d').drawImage(video,0,0);
      const link=document.createElement('a');
      link.href=canvas.toDataURL('image/png');
      link.download=`snapshot-${{Date.now()}}.png`;
      document.body.appendChild(link);link.click();link.remove();
    }}catch(e){{
      comingSoon('Snapshot is not available for this stream right now');
    }}
  }});
  downloadButton.addEventListener('click',()=>comingSoon('Download applies to recorded clips in Playback'));
  shareButton.addEventListener('click',()=>comingSoon('Share'));
  analyticsButton.addEventListener('click',()=>{{document.getElementById('live-analytics-section').scrollIntoView({{behavior:'smooth',block:'nearest'}})}});
  bookmarkButton.addEventListener('click',()=>comingSoon('Bookmark'));
  stopButton.addEventListener('click',()=>{{stopPolling();if(hls)hls.destroy();stopSession(false)}});
  retryButton.addEventListener('click',startSession);

  // Push-to-talk: press-and-hold only, real mic capture, no always-open
  // duplex mode. This button already reflects the server-persisted
  // capability (talk_down_supported) at page-render time; wireTalkMic()'s
  // own start-session call still gets its own fresh rejection if
  // authorization or capability has changed since the page loaded --
  // never assumes render-time state still holds.
  const talkButton=document.getElementById({json.dumps('talk-mic-' + camera_id)});
  wireTalkMic(talkButton, {json.dumps(camera_id)});

  window.addEventListener('pagehide',()=>{{stopSession(true)}});

  // Focused Live View's switchable analytics row (punch-list item 1).
  // The video itself is never paused/reloaded by any of this -- switching
  // pills only swaps the summary panel below it.
  const analyticsSection=document.getElementById('live-analytics-section');
  const analyticsPills=document.getElementById('live-analytics-pills');
  const analyticsPanel=document.getElementById('live-analytics-panel');
  let activeAnalytic=null;
  let analyticsByKey={{}};

  function renderUpgradeCard(key){{
    const info=analyticsByKey[key].upgrade;
    const benefits=info.benefits.map(item=>`<li>${{item}}</li>`).join('');
    analyticsPanel.innerHTML=`<div class="upgrade-card"><span class="pill wait">Not enabled on this camera</span><p class="health-detail">${{info.description}}</p><ul style="margin:8px 0 12px 18px;padding:0">${{benefits}}</ul><div class="dialog-actions"><button class="action-button" id="upgrade-request-${{key}}">Request Upgrade</button><button class="ghost-button" id="upgrade-add-${{key}}">Add to This Camera</button><button class="ghost-button" id="upgrade-learn-${{key}}">Learn More</button></div></div>`;
    document.getElementById(`upgrade-request-${{key}}`).addEventListener('click',()=>comingSoon('Upgrade requests are coming soon -- contact your partner for now.'));
    document.getElementById(`upgrade-add-${{key}}`).addEventListener('click',()=>comingSoon('Adding analytics directly from Live View is coming soon.'));
    document.getElementById(`upgrade-learn-${{key}}`).addEventListener('click',()=>comingSoon(analyticsByKey[key].label+': '+info.description));
  }}

  function renderAnalyticsSummary(key,data){{
    if(key==='lpr'){{
      analyticsPanel.innerHTML=`<div class="health-row"><span class="health-name">Latest plate</span><span class="health-detail">${{data.latest_plate||'No plates read yet'}}</span></div><div class="health-row"><span class="health-name">Confidence</span><span class="health-detail">${{data.latest_confidence!=null?data.latest_confidence+'%':'—'}}</span></div><div class="health-row"><span class="health-name">Recent</span><span class="health-detail">${{data.recent.length}} recent detection(s)</span></div>`;
    }}else if(key==='people_counting'){{
      analyticsPanel.innerHTML=`<div class="health-row"><span class="health-name">Latest count</span><span class="health-detail">${{data.latest_count!=null?data.latest_count:'No counts yet'}}</span></div><div class="health-row"><span class="health-name">Entries / exits</span><span class="health-detail">${{data.entries!=null?data.entries:'—'}} / ${{data.exits!=null?data.exits:'—'}}</span></div>`;
    }}else if(key==='ppe'){{
      analyticsPanel.innerHTML=`<div class="health-row"><span class="health-name">Latest status</span><span class="health-detail">${{data.latest_status||'No PPE events yet'}}</span></div><div class="health-row"><span class="health-name">As of</span><span class="health-detail">${{data.latest_timestamp||'—'}}</span></div>`;
    }}else{{
      const rows=data.recent.map(item=>`<div class="health-row"><span class="health-name">${{item.event_type||'motion'}}</span><span class="health-detail">${{item.timestamp||''}}</span></div>`).join('')||'<div class="health-row"><span class="health-detail">No motion/person/vehicle events yet</span></div>';
      analyticsPanel.innerHTML=rows;
    }}
  }}

  async function selectAnalytic(key){{
    activeAnalytic=key;
    [...analyticsPills.children].forEach(pill=>{{
      const isActive=pill.dataset.key===key;
      pill.classList.toggle('active',isActive);
      pill.setAttribute('aria-selected',isActive?'true':'false');
    }});
    if(!analyticsByKey[key].enabled){{renderUpgradeCard(key);return}}
    analyticsPanel.innerHTML='<div class="health-row"><span class="health-detail">Loading…</span></div>';
    try{{
      const response=await fetch(`/api/customer/cameras/${{cameraId}}/analytics/${{key}}/summary`);
      if(!response.ok)throw new Error('summary request failed');
      renderAnalyticsSummary(key,await response.json());
    }}catch(e){{
      analyticsPanel.innerHTML='<div class="health-row"><span class="health-detail">Analytics data is temporarily unavailable.</span></div>';
    }}
  }}

  async function loadEnabledAnalytics(){{
    try{{
      const response=await fetch(`/api/customer/cameras/${{cameraId}}/analytics`);
      if(!response.ok)return;
      const {{analytics}}=await response.json();
      if(!analytics.length)return;
      analyticsByKey={{}};
      analytics.forEach(item=>{{analyticsByKey[item.key]=item}});
      // Every analytic always gets a pill -- enabled ones show real
      // results, disabled ones show an upgrade card (punch-list:
      // "no dead space, never leave the analytics section blank").
      analyticsPills.innerHTML=analytics.map(item=>`<button type="button" class="filter" role="tab" data-key="${{item.key}}">${{item.label}}${{item.enabled?'':' <span class='pill wait' style='margin-left:4px'>Upgrade</span>'}}</button>`).join('');
      [...analyticsPills.children].forEach(pill=>pill.addEventListener('click',()=>selectAnalytic(pill.dataset.key)));
      analyticsSection.hidden=false;
      const firstEnabled=analytics.find(item=>item.enabled);
      selectAnalytic((firstEnabled||analytics[0]).key);  // one panel active at a time, default to the first real analytic if any is enabled
    }}catch(e){{}}
  }}

  startSession();
  loadEnabledAnalytics();
}})();
</script>'''

        return page_shell(f'Live view · {camera_name}', 'live', content, scripts)
