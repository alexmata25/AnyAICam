"""Real-server Talkdown lifecycle check (run as a subprocess by
test_talk_real_server.py).

Starlette's in-process TestClient (0.41.x, the version the release image
pins) cancels the server's WebSocket task when the client context exits,
so two TestClient-based relay tests cannot observe the server's own
teardown there. This runs the real talk routes under a real uvicorn
server with a real WebSocket client -- the production path -- and
prints a PASS/FAIL line. Offline: a fake local relay, no camera.
"""
import os, sys, time, threading, json, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
db = tempfile.mktemp(suffix=".db")
os.environ["ANYAICAM_PARTNER_DB"] = db
os.environ["ANYAICAM_DATABASE_BACKEND"] = "sqlite"
import appliance_cloud, talk_sessions, talk_audio_relay, partner_portal
from partner_db import connection, initialize_database
from test_talk_audio_relay import _seed_tenant, _seed_camera, _owner_cookie
initialize_database()
with connection() as c:
    _seed_tenant(c); _seed_camera(c, "cam-1", talk_down_supported=1)
calls = []
class FakeLocalRelay:
    def __init__(self, camera, sample_rate, session_id): calls.append(("init", camera["id"], sample_rate)); self.error=None; self.error_reason=None
    def start(self): return True
    def send_pcm16(self, frame): calls.append(("frame", len(frame)))
    def stop(self): calls.append("stopped")
talk_audio_relay._LocalIsapiTalkRelay = FakeLocalRelay
from fastapi import FastAPI
app = FastAPI()
appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
talk_sessions.register_talk_session_routes(app)
talk_audio_relay.register_talk_audio_relay_routes(app)
import socket, uvicorn
_s = socket.socket(); _s.bind(("127.0.0.1", 0)); PORT = _s.getsockname()[1]; _s.close()
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started: time.sleep(0.05)
import requests
cookie = {partner_portal.SESSION_COOKIE: _owner_cookie()}
def state(sid):
    with connection() as c: return c.execute("SELECT state FROM customer_talk_sessions WHERE id=?", (sid,)).fetchone()["state"]
from websockets.sync.client import connect
hdr = {"Cookie": f"{partner_portal.SESSION_COOKIE}={cookie[partner_portal.SESSION_COOKIE]}"}
ok = True
# 1) local ISAPI fallback: ready message, frame forwarded, relay stopped, session stopped
resp = requests.post(f"http://127.0.0.1:{PORT}/api/customer/cameras/cam-1/talk/start", cookies=cookie); print("start:", resp.status_code, resp.text[:200]); sid = resp.json()["session_id"]
with connect(f"ws://127.0.0.1:{PORT}/api/customer/talk/sessions/{sid}/audio?sample_rate=44100", additional_headers=hdr) as ws:
    first = json.loads(ws.recv(timeout=5)); ws.send(b"\x00\x00"); time.sleep(0.3)
time.sleep(1.0)
r1 = (first == {"type": "ready"}, ("frame", 2) in calls, calls[-1] == "stopped", state(sid) == "stopped")
print("local fallback  ready,frame_forwarded,relay_stopped,session_stopped =", r1); ok &= all(r1)
# 2) repeated press/release x5 leaves no orphan sessions
sids = []
for i in range(5):
    s = requests.post(f"http://127.0.0.1:{PORT}/api/customer/cameras/cam-1/talk/start", cookies=cookie).json()["session_id"]; sids.append(s)
    with connect(f"ws://127.0.0.1:{PORT}/api/customer/talk/sessions/{s}/audio?sample_rate=48000", additional_headers=hdr) as ws:
        ws.recv(timeout=5); ws.send(b"\x01\x02\x03\x04")
time.sleep(1.5)
states = [state(s) for s in sids]
print("repeated press/release states =", states, "active relays =", len(talk_audio_relay._active_relays)); ok &= all(s == "stopped" for s in states) and not talk_audio_relay._active_relays
print("REAL-SERVER TALK CHECK:", "PASS" if ok else "FAIL")
server.should_exit = True
sys.exit(0 if ok else 1)
