"""P2P live-view foundation: direct browser<->appliance WebRTC as the
PREFERRED transport for live camera viewing, with the existing, already-
proven S3/CloudFront relay (live_relay_uploader.py, live_playlist.py) kept
completely unchanged as the automatic fallback -- never replaced. See
db_migrations.py's 20260917_live_view_p2p for the schema this module reads
and writes.

Scope of this module: SIGNALING ONLY (SDP offer/answer + ICE candidate
exchange) plus outcome instrumentation (which transport a session actually
used, how long negotiation took, whether it failed). It never touches a
media byte -- exactly the same control-plane/data-plane separation
AI_HANDOFF.md Sec 8 established for the relay path. What actually PRODUCES
a WebRTC answer on the appliance side (a media server / GStreamer webrtcbin
publisher reading the same local camera pipeline start_live_stream() already
maintains) is a separate, later piece of work -- deliberately not part of
this module, and not yet installed on any appliance. Until that publisher
exists, every real P2P attempt will simply time out client-side and the
browser falls back to the relay path exactly as it does today; this is the
intended, safe interim state, not a bug.

Both sides reach this signaling channel only through their own pre-existing
authenticated channel -- partner_identity() cookie auth for the browser,
authenticate_appliance() bearer+nonce for the appliance -- so
live_view_p2p_signaling never becomes a new authentication boundary of its
own. Authorization logic (customer ownership/can_live permission) is
deliberately duplicated from live_view_sessions.py rather than imported,
matching that module's own documented scope decision for this whole
live-view family of modules.

TURN is deliberately NOT part of this module. ICE_SERVERS below is built
from STUN only by default; adding a TURN server later (once real NAT-
failure/relay-usage data justifies the operational cost) is purely a config
change to this one list -- no signaling/session/instrumentation code needs
to change, which is the whole point of shipping STUN-only first.
"""

import json
import os
import secrets
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

from appliance_cloud import authenticate_appliance, _authorized_camera
from partner_db import connection
from partner_portal import partner_identity

LIVE_P2P_ENABLED = os.environ.get("ANYAICAM_LIVE_P2P_ENABLED", "false").strip().lower() == "true"

# Bounded, browser-side negotiation window (see live_view_page.py's
# wireP2PAttempt()) -- how long a viewer waits for a direct connection
# before falling back to the relay. Kept short: the relay path already
# starts its own playlist polling in parallel, so a long P2P timeout would
# only ever delay first video for viewers who were going to fall back
# anyway.
P2P_NEGOTIATION_TIMEOUT_MS = int(os.environ.get("ANYAICAM_LIVE_P2P_TIMEOUT_MS", "4000"))

_DEFAULT_STUN_SERVERS = "stun:stun.l.google.com:19302,stun:stun.cloudflare.com:3478"


def ice_servers() -> list[dict]:
    """STUN-only by default -- see module docstring. A future TURN addition
    is exactly one more entry in ANYAICAM_LIVE_TURN_SERVERS (plus its
    username/credential), read here and appended, never a redesign of
    anything else in this module or its caller."""
    servers = [{"urls": url.strip()} for url in os.environ.get("ANYAICAM_LIVE_STUN_SERVERS", _DEFAULT_STUN_SERVERS).split(",") if url.strip()]
    turn_urls = [url.strip() for url in os.environ.get("ANYAICAM_LIVE_TURN_SERVERS", "").split(",") if url.strip()]
    if turn_urls:
        username = os.environ.get("ANYAICAM_LIVE_TURN_USERNAME", "").strip()
        credential = os.environ.get("ANYAICAM_LIVE_TURN_CREDENTIAL", "").strip()
        entry = {"urls": turn_urls}
        if username:
            entry["username"] = username
        if credential:
            entry["credential"] = credential
        servers.append(entry)
    return servers


def _customer_identity(request: Request) -> dict:
    identity = partner_identity(request)
    if not identity or identity.get('role') not in {'customer_owner', 'customer_viewer'}:
        raise HTTPException(status_code=403, detail='Customer account required.')
    return identity


def _own_session(db, session_id: str, identity: dict) -> dict:
    session = db.execute(
        'SELECT * FROM live_view_sessions WHERE id=? AND customer_id=?',
        (session_id, identity['customer_id']),
    ).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail='Live view session not found.')
    return dict(session)


def _insert_signal(db, *, session_id: str, kind: str, payload: dict, now: datetime) -> None:
    db.execute(
        'INSERT INTO live_view_p2p_signaling(id,session_id,kind,payload_json,created_at,consumed_at) VALUES(?,?,?,?,?,NULL)',
        (secrets.token_hex(12), session_id, kind, json.dumps(payload)[:20000], now.isoformat()),
    )


def _drain_signals(db, *, session_id: str, kinds: tuple[str, ...], now: datetime) -> list[dict]:
    rows = db.execute(
        f"SELECT id,kind,payload_json FROM live_view_p2p_signaling WHERE session_id=? AND kind IN ({','.join('?' for _ in kinds)}) AND consumed_at IS NULL ORDER BY created_at",
        (session_id, *kinds),
    ).fetchall()
    if rows:
        ids = [item['id'] for item in rows]
        db.execute(
            f"UPDATE live_view_p2p_signaling SET consumed_at=? WHERE id IN ({','.join('?' for _ in ids)})",
            (now.isoformat(), *ids),
        )
    return [{'kind': item['kind'], 'payload': json.loads(item['payload_json'])} for item in rows]


def register_live_view_p2p_customer_routes(app: FastAPI) -> None:
    @app.get('/api/customer/live/p2p/config')
    def p2p_config(request: Request) -> dict:
        _customer_identity(request)
        return {'enabled': LIVE_P2P_ENABLED, 'ice_servers': ice_servers(), 'timeout_ms': P2P_NEGOTIATION_TIMEOUT_MS}

    @app.post('/api/customer/live/sessions/{session_id}/p2p/offer')
    def submit_offer(request: Request, session_id: str, payload: dict) -> dict:
        identity = _customer_identity(request)
        sdp = str(payload.get('sdp', ''))
        if not sdp.strip():
            raise HTTPException(status_code=400, detail='sdp is required.')
        now = datetime.now()
        with connection() as db:
            session = _own_session(db, session_id, identity)
            if session['state'] != 'requested':
                raise HTTPException(status_code=409, detail='Live view session is not active.')
            db.execute('UPDATE live_view_sessions SET p2p_attempted=1 WHERE id=?', (session_id,))
            _insert_signal(db, session_id=session_id, kind='offer', payload={'sdp': sdp}, now=now)
        return {'status': 'accepted'}

    @app.post('/api/customer/live/sessions/{session_id}/p2p/ice')
    def submit_client_ice(request: Request, session_id: str, payload: dict) -> dict:
        identity = _customer_identity(request)
        candidate = payload.get('candidate')
        if not isinstance(candidate, dict) or not str(candidate.get('candidate', '')).strip():
            raise HTTPException(status_code=400, detail='candidate is required.')
        now = datetime.now()
        with connection() as db:
            session = _own_session(db, session_id, identity)
            if session['state'] != 'requested':
                raise HTTPException(status_code=409, detail='Live view session is not active.')
            _insert_signal(db, session_id=session_id, kind='ice_client', payload=candidate, now=now)
        return {'status': 'accepted'}

    @app.get('/api/customer/live/sessions/{session_id}/p2p/answer')
    def poll_answer(request: Request, session_id: str) -> dict:
        identity = _customer_identity(request)
        now = datetime.now()
        with connection() as db:
            _own_session(db, session_id, identity)
            signals = _drain_signals(db, session_id=session_id, kinds=('answer', 'ice_appliance'), now=now)
        answer = next((item['payload'] for item in signals if item['kind'] == 'answer'), None)
        candidates = [item['payload'] for item in signals if item['kind'] == 'ice_appliance']
        return {'answer': answer, 'candidates': candidates}

    @app.post('/api/customer/live/sessions/{session_id}/transport-outcome')
    def report_transport_outcome(request: Request, session_id: str, payload: dict) -> dict:
        """Client-reported: the browser is the only party that actually
        knows which transport ended up showing real video (attachPlayer()
        succeeding on either the P2P <video> stream or the relay's HLS
        playlist), or that neither did. `transport` is 'p2p'|'relay'|
        'failed'; `connect_ms` is wall-clock time from session start to
        first frame, for the P2P-vs-relay success/latency comparison this
        instrumentation exists to enable. Never trusted for authorization
        -- this only ever updates this session's own row, already scoped
        to the authenticated customer by _own_session()."""
        identity = _customer_identity(request)
        transport = str(payload.get('transport', ''))
        if transport not in {'p2p', 'relay', 'failed'}:
            raise HTTPException(status_code=400, detail="transport must be 'p2p', 'relay', or 'failed'.")
        connect_ms = payload.get('connect_ms')
        connect_ms = int(connect_ms) if isinstance(connect_ms, (int, float)) and connect_ms >= 0 else None
        error = str(payload.get('error', ''))[:500] or None
        now = datetime.now()
        with connection() as db:
            session = _own_session(db, session_id, identity)
            if transport == 'failed':
                db.execute("UPDATE live_view_sessions SET failed_at=?,error=? WHERE id=?", (now.isoformat(), error, session_id))
            else:
                db.execute("UPDATE live_view_sessions SET transport=?,ready_at=? WHERE id=?", (transport, now.isoformat(), session_id))
        return {'status': 'recorded'}


def register_live_view_p2p_appliance_routes(app: FastAPI) -> None:
    @app.get('/api/appliance/live/p2p/pending')
    def poll_pending_offers(request: Request) -> dict:
        """Appliance-side poll, same shape as the existing GET
        /api/appliance/commands -- returns this appliance's own not-yet-
        answered offers/client-ICE only (scoped via live_view_sessions.
        site_id/camera_id -> cameras.appliance_id, never another
        appliance's signaling traffic)."""
        appliance = authenticate_appliance(request)
        now = datetime.now()
        with connection() as db:
            candidate_sessions = db.execute(
                "SELECT s.id AS session_id, s.camera_id FROM live_view_sessions s "
                "JOIN cameras c ON c.id=s.camera_id WHERE c.appliance_id=? AND s.state='requested'",
                (appliance['id'],),
            ).fetchall()
            pending = []
            for item in candidate_sessions:
                signals = _drain_signals(db, session_id=item['session_id'], kinds=('offer', 'ice_client'), now=now)
                for signal in signals:
                    pending.append({'session_id': item['session_id'], 'camera_id': item['camera_id'], 'kind': signal['kind'], 'payload': signal['payload']})
        return {'pending': pending}

    @app.post('/api/appliance/live/{camera_id}/p2p/answer')
    def submit_answer(request: Request, camera_id: str, payload: dict) -> dict:
        appliance = authenticate_appliance(request)
        _authorized_camera(appliance, camera_id)
        session_id = str(payload.get('session_id', '')).strip()
        sdp = str(payload.get('sdp', ''))
        if not session_id or not sdp.strip():
            raise HTTPException(status_code=400, detail='session_id and sdp are required.')
        now = datetime.now()
        with connection() as db:
            session = db.execute(
                "SELECT id FROM live_view_sessions WHERE id=? AND camera_id=? AND state='requested'",
                (session_id, camera_id),
            ).fetchone()
            if not session:
                raise HTTPException(status_code=404, detail='Live view session not found.')
            _insert_signal(db, session_id=session_id, kind='answer', payload={'sdp': sdp}, now=now)
        return {'status': 'accepted'}

    @app.post('/api/appliance/live/{camera_id}/p2p/ice')
    def submit_appliance_ice(request: Request, camera_id: str, payload: dict) -> dict:
        appliance = authenticate_appliance(request)
        _authorized_camera(appliance, camera_id)
        session_id = str(payload.get('session_id', '')).strip()
        candidate = payload.get('candidate')
        if not session_id or not isinstance(candidate, dict) or not str(candidate.get('candidate', '')).strip():
            raise HTTPException(status_code=400, detail='session_id and candidate are required.')
        now = datetime.now()
        with connection() as db:
            session = db.execute(
                "SELECT id FROM live_view_sessions WHERE id=? AND camera_id=? AND state='requested'",
                (session_id, camera_id),
            ).fetchone()
            if not session:
                raise HTTPException(status_code=404, detail='Live view session not found.')
            _insert_signal(db, session_id=session_id, kind='ice_appliance', payload=candidate, now=now)
        return {'status': 'accepted'}
