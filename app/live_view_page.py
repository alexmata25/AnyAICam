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
from html import escape
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from partner_db import connection
from partner_portal import partner_identity

POLL_INTERVAL_MS = 2000
POLL_TIMEOUT_MS = 45000


def _customer_live_cameras(db, identity: dict) -> list[dict]:
    """Every camera this identity may view live, scoped to identity's own
    customer_id: the full fleet for customer_owner, or only the subset
    explicitly granted can_live for customer_viewer -- same ownership/
    permission rule _authorized_camera() applies to one camera_id from a
    URL, evaluated here for the whole fleet at once so /customer-live can
    render a grid instead of picking a single camera to redirect to."""
    if identity.get('role') == 'customer_owner':
        return [
            dict(camera) for camera in db.execute(
                'SELECT id, name FROM cameras WHERE customer_id=? '
                'ORDER BY camera_number, id',
                (identity['customer_id'],),
            ).fetchall()
        ]

    user = db.execute(
        'SELECT id FROM partner_users WHERE email=?',
        (identity['email'],),
    ).fetchone()
    if not user:
        return []

    return [
        dict(camera) for camera in db.execute(
            'SELECT c.id, c.name FROM cameras c '
            'JOIN customer_camera_permissions p ON p.camera_id=c.id AND p.user_id=? '
            'WHERE c.customer_id=? AND p.can_live=1 '
            'ORDER BY c.camera_number, c.id',
            (user['id'], identity['customer_id']),
        ).fetchall()
    ]


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
        'SELECT id FROM partner_users WHERE email=?',
        (identity['email'],),
    ).fetchone()
    if not user:
        raise HTTPException(status_code=403, detail='Customer owner permission required.')

    if identity.get('role') != 'customer_owner':
        permission = db.execute(
            'SELECT can_live FROM customer_camera_permissions WHERE user_id=? AND camera_id=?',
            (user['id'], camera_id),
        ).fetchone()
        if not permission or not permission['can_live']:
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
            f'''<article class="live-grid-tile">
              <div class="camera-view" style="border-radius:10px">
                <video id="live-grid-video-{escape(camera['id'], quote=True)}" muted playsinline></video>
                <div class="camera-placeholder" id="live-grid-placeholder-{escape(camera['id'], quote=True)}">
                  <span class="signal">◉</span>
                  <strong id="live-grid-status-{escape(camera['id'], quote=True)}">Starting live view…</strong>
                </div>
              </div>
              <div class="live-grid-tile-head">
                <span>{escape(camera.get('name') or camera['id'])}</span>
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
            f'@media(max-width:760px){{.live-grid{{grid-template-columns:1fr}}}}'
            f'</style>'
            f'<section class="live-grid">{tiles}</section>'
        )

        scripts = f'''<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>
(function(){{
  const cameraIds={json.dumps(camera_ids)};
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

  window.addEventListener('pagehide',()=>{{cameraIds.forEach(id=>stopSession(id,true))}});

  cameraIds.forEach(startSession);
}})();
</script>'''

        return page_shell('Live view', 'live', content, scripts)

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

        content = (
            f'<header class="topbar"><div><p class="eyebrow">Live view</p>'
            f'<h1>{escape(camera_name)}</h1></div>'
            f'<a class="ghost-button" href="/customer-live">Back to Live</a></header>'
            f'<section class="panel"><div class="camera-view" style="border-radius:10px">'
            f'<video id="live-view-video" controls muted playsinline></video>'
            f'<div class="camera-placeholder" id="live-view-placeholder">'
            f'<span class="signal">◉</span>'
            f'<strong id="live-view-status">Starting live view…</strong>'
            f'<small>This can take a few seconds.</small></div></div>'
            f'<div class="camera-tools" style="justify-content:center">'
            f'<button class="camera-tool" id="live-view-mute" title="Mute">♪</button>'
            f'<button class="camera-tool" id="live-view-snapshot" title="Snapshot">◉</button>'
            f'<button class="camera-tool" id="live-view-download" title="Download">⬇</button>'
            f'<button class="camera-tool" id="live-view-share" title="Share">↗</button>'
            f'<a class="camera-tool" href="/playback" title="Playback">◴</a>'
            f'<button class="camera-tool" id="live-view-analytics" title="Analytics">⌕</button>'
            f'<button class="camera-tool" id="live-view-bookmark" title="Bookmark">◈</button>'
            f'<button class="camera-tool" id="live-view-stop" title="Stop">◼</button>'
            f'<button class="camera-tool" id="live-view-retry" title="Retry" hidden>↻</button>'
            f'</div></section>'
            f'<section class="panel" style="margin-top:16px">'
            f'<div class="panel-head"><div><h2>Live analytics</h2>'
            f'<div class="health-detail">No analytics events recorded yet for this camera.</div></div></div>'
            f'<div class="health-list">'
            f'<div class="health-row"><span class="health-name"><i class="legend-dot event-motion" style="display:inline-block;margin-right:8px"></i>Motion</span><span class="health-detail">No detections yet</span></div>'
            f'<div class="health-row"><span class="health-name"><i class="legend-dot event-person" style="display:inline-block;margin-right:8px"></i>Person</span><span class="health-detail">No detections yet</span></div>'
            f'<div class="health-row"><span class="health-name"><i class="legend-dot event-vehicle" style="display:inline-block;margin-right:8px"></i>Vehicle</span><span class="health-detail">No detections yet</span></div>'
            f'<div class="health-row"><span class="health-name"><i class="legend-dot event-lpr" style="display:inline-block;margin-right:8px"></i>License plate</span><span class="health-detail">No detections yet</span></div>'
            f'<div class="health-row"><span class="health-name"><i class="legend-dot event-people_counting" style="display:inline-block;margin-right:8px"></i>People count</span><span class="health-detail">No detections yet</span></div>'
            f'<div class="health-row"><span class="health-name"><i class="legend-dot event-intrusion" style="display:inline-block;margin-right:8px"></i>Intrusion</span><span class="health-detail">No detections yet</span></div>'
            f'</div></section>'
        )

        scripts = f'''<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script><script>
(function(){{
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
  analyticsButton.addEventListener('click',()=>comingSoon('Live analytics'));
  bookmarkButton.addEventListener('click',()=>comingSoon('Bookmark'));
  stopButton.addEventListener('click',()=>{{stopPolling();if(hls)hls.destroy();stopSession(false)}});
  retryButton.addEventListener('click',startSession);
  window.addEventListener('pagehide',()=>stopSession(true));

  startSession();
}})();
</script>'''

        return page_shell(f'Live view · {camera_name}', 'live', content, scripts)
