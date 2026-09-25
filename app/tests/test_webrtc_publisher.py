"""Appliance-side WebRTC publisher (2026-09-17): regression coverage for
webrtc_publisher.py -- the MediaMTX config/process lifecycle and the
cloud-signaling <-> MediaMTX-WHEP bridge.

Runs against a real local HTTP server (http.server, a background thread,
stdlib only) that reproduces MediaMTX's own confirmed real route contract
(WHEP POST/Location/answer shape, config REST API add/delete, and -- as
of the 2026-09-17 trickle-ICE redesign -- real PATCH media-line
validation), not a live `mediamtx` binary directly. That live-binary
verification has since happened separately, twice (Phase 1's full WHEP
contract, and this redesign's own trickle-specific PATCH verification --
see webrtc_publisher.py's own module docstring for both transcripts),
and this fake's own contract was tightened to match exactly what each
verification found, including the one real bug (a malformed PATCH media
line) a too-permissive earlier version of this same fake let ship
undetected. Process spawning is tested with a fake subprocess.Popen,
never a real MediaMTX process.

The single most safety-critical test in this file is
test_worker_does_nothing_at_all_when_the_feature_flag_is_off -- P2P must
stay fully inert (no MediaMTX spawn, no config sync, no signaling poll)
until ANYAICAM_LIVE_P2P_ENABLED is explicitly set, exactly matching the
"do not enable P2P" instruction this phase was built under.
"""

import asyncio
import http.server
import json
import threading
import time

import pytest

import webrtc_publisher as wp


# --------------------------------------------------------------- config rendering


def test_render_config_is_loopback_only_and_stun_only_by_default(monkeypatch):
    monkeypatch.setattr(wp, "STUN_SERVERS", ["stun:stun.l.google.com:19302"])
    monkeypatch.setattr(wp, "TURN_SERVERS", [])
    text = wp.render_mediamtx_config()
    assert "apiAddress: 127.0.0.1:9997" in text
    assert "webrtcAddress: 127.0.0.1:8889" in text
    assert 'url: "stun:stun.l.google.com:19302"' in text
    assert "0.0.0.0" not in text  # never bound to a non-loopback interface


def test_render_config_adds_turn_entry_with_credentials_when_configured(monkeypatch):
    monkeypatch.setattr(wp, "STUN_SERVERS", ["stun:stun.l.google.com:19302"])
    monkeypatch.setattr(wp, "TURN_SERVERS", ["turn:turn.example.test:3478"])
    monkeypatch.setattr(wp, "TURN_USERNAME", "webrtc-user")
    monkeypatch.setattr(wp, "TURN_CREDENTIAL", "webrtc-pass")
    text = wp.render_mediamtx_config()
    assert 'url: "turn:turn.example.test:3478"' in text
    assert 'username: "webrtc-user"' in text
    assert 'password: "webrtc-pass"' in text


def test_render_config_omits_turn_section_when_unconfigured(monkeypatch):
    monkeypatch.setattr(wp, "STUN_SERVERS", ["stun:stun.l.google.com:19302"])
    monkeypatch.setattr(wp, "TURN_SERVERS", [])
    text = wp.render_mediamtx_config()
    assert "turn:" not in text


# --------------------------------------------------------------- fake MediaMTX HTTP server


class _FakeMediaMTX(http.server.BaseHTTPRequestHandler):
    """Reproduces the exact route contract confirmed from MediaMTX's own
    source (see webrtc_publisher.py's module docstring): POST/DELETE on
    /v3/config/paths/(add|delete)/{name}, POST on /{path}/whep (201,
    Location header, application/sdp body), PATCH on a returned session
    location (application/trickle-ice-sdpfrag)."""

    calls: list = []
    fail_whep = False
    fail_config = False
    api_down = False
    fail_list = False
    existing: set = set()

    def log_message(self, *a):
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _headers_lower(self):
        return {k.lower(): v for k, v in self.headers.items()}

    def do_POST(self):
        body = self._body()
        self.__class__.calls.append((self.command, self.path, body, self._headers_lower()))
        if self.path.startswith("/v3/config/paths/add/") or self.path.startswith("/v3/config/paths/replace/"):
            name = self.path.rsplit("/", 1)[-1]
            if self.__class__.fail_config or (self.path.startswith("/v3/config/paths/add/") and name in self.__class__.existing):
                self.send_response(400)
                self.end_headers()
                return
            self.__class__.existing.add(name)
            self.send_response(200)
            self.end_headers()
            return
        if self.path.endswith("/whep"):
            if self.__class__.fail_whep:
                self.send_response(400)
                self.end_headers()
                return
            path_name = self.path.rsplit("/whep", 1)[0].lstrip("/")
            self.send_response(201)
            self.send_header("Content-Type", "application/sdp")
            self.send_header("Location", f"/{path_name}/whep/fake-secret-123")
            self.end_headers()
            self.wfile.write(b"v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\na=fake-answer\r\n")
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        self.__class__.calls.append((self.command, self.path, b"", self._headers_lower()))
        if self.__class__.api_down:
            self.send_response(503)
            self.end_headers()
            return
        if self.path == "/v3/config/global/get":
            body = b"{}"
        elif self.path.startswith("/v3/config/paths/list"):
            if self.__class__.fail_list:
                self.send_response(500)
                self.end_headers()
                return
            # Same shape as the real v1.21 API (confirmed on the Ryzen):
            # paginated, and the static all_others path is listed too.
            names = sorted(self.__class__.existing | {"all_others"})
            body = json.dumps({"itemCount": len(names), "pageCount": 1, "items": [{"name": n} for n in names]}).encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self.__class__.calls.append((self.command, self.path, b"", self._headers_lower()))
        self.__class__.existing.discard(self.path.rsplit("/", 1)[-1])
        self.send_response(200)
        self.end_headers()

    def do_PATCH(self):
        body = self._body()
        self.__class__.calls.append((self.command, self.path, body, self._headers_lower()))
        if self.path == "/v3/config/global/patch":
            self.send_response(200)
            self.end_headers()
            return
        # Validates the media line the real way MediaMTX itself does
        # (confirmed live against the real v1.21.0 binary, 2026-09-17):
        # "m=<media> <port> <proto> <fmt...>" with a real numeric port --
        # "m=0" or "m=application" (this codebase's own real, never-
        # actually-exercised-until-then bug) is rejected with a real 400
        # "sdp: invalid port value" there. A fake that accepted anything
        # here, as this one previously did, is exactly what let that bug
        # ship undetected -- this now closes that gap.
        media_line = next((line for line in body.decode(errors="replace").splitlines() if line.startswith("m=")), "")
        parts = media_line.split()
        port_valid = len(parts) >= 2 and parts[1].isdigit()
        if not port_valid:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"status":"error","error":"sdp: invalid port value"}')
            return
        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def fake_mediamtx(monkeypatch):
    _FakeMediaMTX.calls = []
    _FakeMediaMTX.fail_whep = False
    _FakeMediaMTX.fail_config = False
    _FakeMediaMTX.api_down = False
    _FakeMediaMTX.fail_list = False
    _FakeMediaMTX.existing = set()
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeMediaMTX)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setattr(wp, "MEDIAMTX_API_BASE", base)
    monkeypatch.setattr(wp, "MEDIAMTX_WEBRTC_BASE", base)
    try:
        yield _FakeMediaMTX
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_real_capability_discovery(monkeypatch):
    """Path-sync tests must never touch a real DB or a real camera's ONVIF
    service; each test starts with no stored capabilities or cached choice."""
    monkeypatch.setattr(wp, "_p2p_source_choice", {})
    monkeypatch.setattr(wp, "_load_capabilities", lambda camera_id: None)
    monkeypatch.setattr(wp, "_save_capabilities", lambda camera_id, record: None)
    monkeypatch.setattr(wp, "_onvif_soap_call", lambda: None)
    monkeypatch.setattr(wp, "_probe_video_stream", lambda url: False)
    monkeypatch.setattr(wp, "_paths_resync_pending", False)
    monkeypatch.setattr(wp, "_whep_sessions", {})
    monkeypatch.setattr(wp, "_applied_hosts", [])  # a MediaMTX (re)start sets it; never leak it to other tests


# --------------------------------------------------------------- path sync


def test_sync_camera_paths_adds_one_path_per_known_camera_using_injected_camera_url(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_camera_map", {"cam-a": 1, "cam-b": 2})
    monkeypatch.setattr(wp, "_known_paths", set())

    def fake_camera_url(camera_number):
        return f"rtsp://user:pass@10.0.0.{camera_number}:554/stream"

    wp.sync_camera_paths(fake_camera_url)

    add_calls = [c for c in fake_mediamtx.calls if c[1].startswith("/v3/config/paths/add/")]
    assert {c[1].rsplit("/", 1)[-1] for c in add_calls} == {"cam-a", "cam-b"}
    for _, path, body, _headers in add_calls:
        payload = json.loads(body)
        assert payload["sourceOnDemand"] is True
        assert payload["source"].startswith("rtsp://user:pass@")
    assert wp._known_paths == {"cam-a", "cam-b"}


def test_sync_camera_paths_never_hardcodes_a_camera_id_works_for_any_set(fake_mediamtx, monkeypatch):
    """Explicit requirement: support all cameras generically, no hardcoded
    IDs. Proven here with camera_ids that look nothing like the real
    fleet's IDs."""
    monkeypatch.setattr(wp, "_camera_map", {"totally-different-id-1": 7, "another-shape-2": 8})
    monkeypatch.setattr(wp, "_known_paths", set())
    wp.sync_camera_paths(lambda n: f"rtsp://u:p@host{n}:554/x")
    assert wp._known_paths == {"totally-different-id-1", "another-shape-2"}


def test_sync_camera_paths_removes_paths_for_cameras_no_longer_known(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_camera_map", {"cam-a": 1})
    monkeypatch.setattr(wp, "_known_paths", {"cam-a", "cam-removed"})
    wp.sync_camera_paths(lambda n: "rtsp://u:p@host:554/x")
    delete_calls = [c for c in fake_mediamtx.calls if c[0] == "DELETE"]
    assert any(c[1] == "/v3/config/paths/delete/cam-removed" for c in delete_calls)
    assert wp._known_paths == {"cam-a"}


def test_sync_camera_paths_skips_a_camera_whose_url_builder_raises(fake_mediamtx, monkeypatch):
    """A camera with no real provisioned RTSP source yet (camera_url_fn
    raises, matching camera_url()'s own CameraNotConfiguredError
    contract) must never crash the sync -- and must never be added as a
    broken path."""
    monkeypatch.setattr(wp, "_camera_map", {"cam-broken": 1, "cam-ok": 2})
    monkeypatch.setattr(wp, "_known_paths", set())

    def flaky(camera_number):
        if camera_number == 1:
            raise RuntimeError("not configured")
        return "rtsp://u:p@host:554/x"

    wp.sync_camera_paths(flaky)
    assert wp._known_paths == {"cam-ok"}


def test_sync_camera_paths_never_logs_or_returns_the_credential(fake_mediamtx, monkeypatch, caplog):
    monkeypatch.setattr(wp, "_camera_map", {"cam-a": 1})
    monkeypatch.setattr(wp, "_known_paths", set())
    import logging
    caplog.set_level(logging.DEBUG, logger="anyaicam.webrtc_publisher")
    wp.sync_camera_paths(lambda n: "rtsp://secretuser:secretpass@10.0.0.1:554/x")
    assert "secretpass" not in caplog.text


# --------------------------------------------------------------- WHEP offer/answer


def test_whep_offer_returns_resolved_location_and_answer_sdp(fake_mediamtx):
    result = wp.whep_offer("cam-a", "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\na=fake-offer\r\n")
    assert result is not None
    location, answer = result
    assert location.startswith("http://127.0.0.1:")
    assert location.endswith("/cam-a/whep/fake-secret-123")
    assert "a=fake-answer" in answer
    post_calls = [c for c in fake_mediamtx.calls if c[0] == "POST" and c[1] == "/cam-a/whep"]
    assert len(post_calls) == 1
    assert post_calls[0][3]["content-type"] == "application/sdp"


def test_whep_offer_returns_none_on_rejection(fake_mediamtx):
    fake_mediamtx.fail_whep = True
    assert wp.whep_offer("cam-a", "v=0\r\n...") is None


def test_whep_offer_returns_none_when_mediamtx_unreachable(monkeypatch):
    monkeypatch.setattr(wp, "MEDIAMTX_WEBRTC_BASE", "http://127.0.0.1:1")  # nothing listens here
    assert wp.whep_offer("cam-a", "v=0\r\n...") is None


def test_forward_client_ice_candidate_patches_the_session_location(fake_mediamtx):
    session_location, _ = wp.whep_offer("cam-a", "v=0\r\n...")
    wp._forward_client_ice_candidate(session_location, {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host", "sdpMid": "0"})
    patch_calls = [c for c in fake_mediamtx.calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0][3]["content-type"] == "application/trickle-ice-sdpfrag"
    assert b"a=candidate:1 1 UDP 1 10.0.0.1 5000 typ host" in patch_calls[0][2]


def test_forward_client_ice_candidate_sends_a_real_valid_sdp_media_line(fake_mediamtx):
    """Regression test for a real bug (2026-09-17): the media line this
    function builds MUST be genuine, valid SDP ("m=<media> <port>
    <proto> <fmt...>") -- confirmed live against the real MediaMTX
    v1.21.0 binary that "m=<mid>" (e.g. "m=0") is rejected outright
    with a real 400 "sdp: invalid port value" error. _FakeMediaMTX's
    own do_PATCH now enforces this same real validity check, so this
    test fails loudly (not silently, as it previously did) if this
    function ever regresses back to the broken shape."""
    session_location, _ = wp.whep_offer("cam-a", "v=0\r\n...")
    wp._forward_client_ice_candidate(session_location, {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host", "sdpMid": "0"})
    patch_calls = [c for c in fake_mediamtx.calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    body = patch_calls[0][2].decode()
    media_line = next(line for line in body.splitlines() if line.startswith("m="))
    parts = media_line.split()
    assert len(parts) >= 4, f"media line is not valid SDP: {media_line!r}"
    assert parts[1].isdigit(), f"media line has no real port field: {media_line!r}"
    assert "a=mid:0" in body


def test_forward_client_ice_candidate_is_silently_best_effort_on_failure(monkeypatch):
    # No server listening -- must not raise.
    wp._forward_client_ice_candidate("http://127.0.0.1:1/x/whep/y", {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host"})


# --------------------------------------------------------------- signaling bridge


def test_handle_offer_signal_posts_answer_back_to_cloud(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_known_paths", {"cam-a"})
    posted = {}

    def fake_control_plane_post(path, payload):
        posted["path"] = path
        posted["payload"] = payload
        return {"status": "accepted"}

    monkeypatch.setattr(wp, "_control_plane_post", fake_control_plane_post)
    wp._handle_pending_signal(lambda n: "rtsp://u:p@h:554/x", {
        "session_id": "sess-1", "camera_id": "cam-a", "kind": "offer",
        "payload": {"sdp": "v=0\r\n...offer..."},
    })
    assert posted["path"] == "/api/appliance/live/cam-a/p2p/answer"
    assert posted["payload"]["session_id"] == "sess-1"
    assert "a=fake-answer" in posted["payload"]["sdp"]
    assert wp._whep_sessions["sess-1"].endswith("/cam-a/whep/fake-secret-123")


def test_handle_offer_signal_for_unconfigured_camera_is_a_noop(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_known_paths", set())
    calls = []
    monkeypatch.setattr(wp, "_control_plane_post", lambda *a, **k: calls.append(a) or {})
    wp._handle_pending_signal(lambda n: "rtsp://u:p@h:554/x", {
        "session_id": "sess-1", "camera_id": "cam-unknown", "kind": "offer", "payload": {"sdp": "v=0\r\n..."},
    })
    assert calls == []
    assert [c for c in fake_mediamtx.calls if c[0] == "POST" and "/whep" in c[1]] == []


def test_handle_ice_client_signal_forwards_to_the_tracked_whep_session(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_whep_sessions", {"sess-1": f"{wp.MEDIAMTX_WEBRTC_BASE}/cam-a/whep/fake-secret-123"})
    wp._handle_pending_signal(lambda n: "rtsp://u:p@h:554/x", {
        "session_id": "sess-1", "camera_id": "cam-a", "kind": "ice_client",
        "payload": {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host", "sdpMid": "0"},
    })
    assert any(c[0] == "PATCH" for c in fake_mediamtx.calls)


def test_handle_ice_client_signal_for_unknown_session_is_dropped_not_raised(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_whep_sessions", {})
    wp._handle_pending_signal(lambda n: "rtsp://u:p@h:554/x", {
        "session_id": "sess-never-offered", "camera_id": "cam-a", "kind": "ice_client",
        "payload": {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host"},
    })
    assert [c for c in fake_mediamtx.calls if c[0] == "PATCH"] == []


@pytest.mark.anyio
async def test_bridge_tick_never_blocks_the_event_loop_on_a_slow_signal(monkeypatch):
    """Real-binary finding (2026-09-17): MediaMTX can legitimately take
    ~10s to fail a WHEP offer against an unreachable camera. _bridge_tick()
    must run that work off the event loop (asyncio.to_thread), or every
    other background task sharing this process's loop stalls for the
    same ~10s. Proven here by making the pending-signal handler block
    for real (time.sleep) and confirming a concurrently-scheduled
    asyncio task still gets to run well before it finishes."""
    import time

    monkeypatch.setattr(wp, "_control_plane_get", lambda path, timeout=10: {"pending": [{"session_id": "s", "camera_id": "c", "kind": "offer", "payload": {}}]})

    def slow_handle(camera_url_fn, item):
        time.sleep(0.3)

    monkeypatch.setattr(wp, "_handle_pending_signal", slow_handle)

    other_task_ran = asyncio.Event()

    async def other_task():
        await asyncio.sleep(0.02)
        other_task_ran.set()

    tick_task = asyncio.ensure_future(wp._bridge_tick(lambda n: "rtsp://u:p@h:554/x"))
    asyncio.ensure_future(other_task())
    await asyncio.wait_for(other_task_ran.wait(), timeout=1.0)
    await tick_task


# --------------------------------------------------------------- MediaMTX process lifecycle (fake subprocess)


class _FakeProcess:
    def __init__(self, returncode=None):
        self._returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._returncode

    @property
    def returncode(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.killed = True


def test_start_mediamtx_writes_config_and_spawns_the_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "MEDIAMTX_CONFIG_PATH", tmp_path / "mediamtx.yml")
    spawned = {}

    def fake_popen(args, **kwargs):
        spawned["args"] = args
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(wp.subprocess, "Popen", fake_popen)
    wp._start_mediamtx()
    assert (tmp_path / "mediamtx.yml").exists()
    assert spawned["args"][0] == wp.MEDIAMTX_BINARY
    assert wp.webrtc_publisher_state["mediamtx_status"] == "starting"
    wp._mediamtx_process = None  # test isolation


def test_start_mediamtx_records_spawn_failure_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "MEDIAMTX_CONFIG_PATH", tmp_path / "mediamtx.yml")

    def fake_popen(args, **kwargs):
        raise OSError("binary not found")

    monkeypatch.setattr(wp.subprocess, "Popen", fake_popen)
    wp._start_mediamtx()
    assert wp.webrtc_publisher_state["mediamtx_status"] == "spawn_failed"
    assert wp._mediamtx_process is None


def test_ensure_mediamtx_running_restarts_after_an_unexpected_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "MEDIAMTX_CONFIG_PATH", tmp_path / "mediamtx.yml")
    dead = _FakeProcess(returncode=1)
    wp._mediamtx_process = dead
    restarted = {}

    def fake_popen(args, **kwargs):
        restarted["called"] = True
        return _FakeProcess(returncode=None)

    monkeypatch.setattr(wp.subprocess, "Popen", fake_popen)
    wp._ensure_mediamtx_running()
    assert restarted.get("called") is True
    wp._mediamtx_process = None  # test isolation


def test_ensure_mediamtx_running_leaves_a_healthy_process_alone(monkeypatch):
    alive = _FakeProcess(returncode=None)
    wp._mediamtx_process = alive
    called = {"popen": False}
    monkeypatch.setattr(wp.subprocess, "Popen", lambda *a, **k: called.__setitem__("popen", True))
    wp._ensure_mediamtx_running()
    assert called["popen"] is False
    assert wp.webrtc_publisher_state["mediamtx_status"] == "running"
    wp._mediamtx_process = None  # test isolation


def test_stop_mediamtx_terminates_the_process(monkeypatch):
    proc = _FakeProcess(returncode=None)
    wp._mediamtx_process = proc
    wp.stop_mediamtx()
    assert proc.terminated is True
    assert wp._mediamtx_process is None
    assert wp.webrtc_publisher_state["mediamtx_status"] == "stopped"


# --------------------------------------------------------------- the safety-critical flag gate


@pytest.mark.anyio
async def test_worker_does_nothing_at_all_when_the_feature_flag_is_off(monkeypatch):
    """The single most important test in this file: P2P must remain fully
    inert -- no MediaMTX spawn, no config sync, no signaling poll -- until
    ANYAICAM_LIVE_P2P_ENABLED is explicitly set. Matches the explicit
    'do not enable P2P on the Ryzen yet' instruction this module was
    built under."""
    import asyncio
    monkeypatch.setattr(wp, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(wp, "LIVE_P2P_ENABLED", False)
    spawn_called = {"value": False}
    monkeypatch.setattr(wp, "_start_mediamtx", lambda: spawn_called.__setitem__("value", True))
    poll_called = {"value": False}
    monkeypatch.setattr(wp, "_control_plane_get", lambda *a, **k: poll_called.__setitem__("value", True) or None)

    task = asyncio.ensure_future(wp.webrtc_publisher_worker(lambda n: "rtsp://u:p@h:554/x"))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert spawn_called["value"] is False
    assert poll_called["value"] is False
    assert wp.webrtc_publisher_state["worker_status"] == "disabled"


@pytest.mark.anyio
async def test_worker_never_runs_on_cloud_role_regardless_of_the_flag(monkeypatch):
    import asyncio
    monkeypatch.setattr(wp, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(wp, "LIVE_P2P_ENABLED", True)
    spawn_called = {"value": False}
    monkeypatch.setattr(wp, "_start_mediamtx", lambda: spawn_called.__setitem__("value", True))

    task = asyncio.ensure_future(wp.webrtc_publisher_worker(lambda n: "rtsp://u:p@h:554/x"))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert spawn_called["value"] is False


@pytest.mark.anyio
async def test_worker_ticks_when_enabled_on_edge_role(monkeypatch):
    import asyncio
    monkeypatch.setattr(wp, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(wp, "LIVE_P2P_ENABLED", True)
    monkeypatch.setattr(wp, "SCAN_SECONDS", 0.01)
    monkeypatch.setattr(wp, "CONFIG_REFRESH_SECONDS", 9999)
    ticks = {"value": 0}
    monkeypatch.setattr(wp, "_ensure_mediamtx_running", lambda: None)
    # CONFIG_REFRESH_SECONDS alone does not skip the first refresh: the
    # worker compares against time.monotonic() (time since boot), so
    # stub the refresh itself -- no network/MediaMTX call in this test.
    monkeypatch.setattr(wp, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(wp, "reconcile_camera_paths", lambda camera_url_fn: None)

    async def fake_bridge_tick(camera_url_fn):
        ticks["value"] += 1

    monkeypatch.setattr(wp, "_bridge_tick", fake_bridge_tick)

    task = asyncio.ensure_future(wp.webrtc_publisher_worker(lambda n: "rtsp://u:p@h:554/x"))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ticks["value"] > 0
    assert wp.webrtc_publisher_state["worker_status"] == "running"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_a_path_mediamtx_already_has_is_adopted_not_left_unconfigured(fake_mediamtx, monkeypatch):
    """Confirmed live on Ryzen (2026-09-24): an add that timed out on our
    side had in fact created the path, so every later add returned 400 and
    the camera's P2P offers were refused as unconfigured until a restart."""
    monkeypatch.setattr(wp, "_camera_map", {"cam-a": 1, "cam-b": 2})
    monkeypatch.setattr(wp, "_known_paths", set())
    fake_mediamtx.existing = {"cam-b"}
    wp.sync_camera_paths(lambda n: f"rtsp://u:p@10.0.0.{n}:554/x")
    assert wp._known_paths == {"cam-a", "cam-b"}
    replaced = [c for c in fake_mediamtx.calls if c[1] == "/v3/config/paths/replace/cam-b"]
    assert len(replaced) == 1 and json.loads(replaced[0][2])["sourceOnDemand"] is True
    assert not [c for c in fake_mediamtx.calls if c[1] == "/v3/config/paths/replace/cam-a"]


def test_an_add_that_timed_out_is_recovered_on_the_next_sync(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_camera_map", {"cam-a": 1})
    monkeypatch.setattr(wp, "_known_paths", set())
    real_request = wp._config_request
    outcome = {"first": True}

    def timing_out_once(method, path, payload=None):
        if outcome["first"] and path.startswith("/v3/config/paths/"):
            outcome["first"] = False
            real_request(method, path.replace("/replace/", "/add/"), payload)  # MediaMTX did create it...
            return 0, ""  # ...but our request timed out
        return real_request(method, path, payload)

    monkeypatch.setattr(wp, "_config_request", timing_out_once)
    wp.sync_camera_paths(lambda n: "rtsp://u:p@h:554/x")
    wp.sync_camera_paths(lambda n: "rtsp://u:p@h:554/x")
    assert wp._known_paths == {"cam-a"}


def test_a_path_that_really_cannot_be_configured_stays_unconfigured(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_camera_map", {"cam-a": 1})
    monkeypatch.setattr(wp, "_known_paths", set())
    fake_mediamtx.fail_config = True
    wp.sync_camera_paths(lambda n: "rtsp://u:p@h:554/x")
    assert wp._known_paths == set()


# ------------------------------------------------ P2P substream selection
# 2026-09-24, confirmed live on Ryzen: P2P sent the full-bitrate main
# stream (one camera: 1.7 fps at 72% packet loss). P2P now uses a verified
# substream when the URL follows a known convention; recording and the
# live HLS encode keep the untouched main stream.

MAIN = "rtsp://u:p@10.0.0.9:554/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
SUB = "rtsp://u:p@10.0.0.9:554/Streaming/Channels/102?transportmode=unicast&profile=Profile_2"


def test_substream_candidates_follow_generic_conventions_only():
    assert wp.substream_candidates(MAIN) == [SUB]
    assert wp.substream_candidates("rtsp://u:p@h/Streaming/Channels/201") == ["rtsp://u:p@h/Streaming/Channels/202"]
    assert wp.substream_candidates("rtsp://u:p@h/cam/realmonitor?channel=1&subtype=0") == ["rtsp://u:p@h/cam/realmonitor?channel=1&subtype=1"]
    assert wp.substream_candidates("rtsp://u:p@h/live/main") == []
    assert wp.substream_candidates("rtsp://u:p@h/cam?subtype=01") == []


@pytest.fixture()
def fresh_choice(monkeypatch):
    monkeypatch.setattr(wp, "_p2p_source_choice", {})
    monkeypatch.setattr(wp, "P2P_STREAM_PREFERENCE", "auto")
    # No real DB or camera: no stored capabilities and no ONVIF service, so
    # the capability layer falls back to its URL-convention adapter.
    monkeypatch.setattr(wp, "_load_capabilities", lambda camera_id: None)
    monkeypatch.setattr(wp, "_save_capabilities", lambda camera_id, record: None)
    monkeypatch.setattr(wp, "_onvif_soap_call", lambda: None)
    probes = []
    monkeypatch.setattr(wp, "_probe_video_stream", lambda url: probes.append(url) or url == SUB)
    return probes


def test_a_verified_substream_is_what_p2p_sends(fake_mediamtx, monkeypatch, fresh_choice):
    monkeypatch.setattr(wp, "_camera_map", {"cam-x": 7})
    monkeypatch.setattr(wp, "_known_paths", set())
    handed_to_recording = []
    wp.sync_camera_paths(lambda n: handed_to_recording.append(MAIN) or MAIN)
    added = [c for c in fake_mediamtx.calls if c[1] == "/v3/config/paths/add/cam-x"]
    assert json.loads(added[0][2])["source"] == SUB
    assert handed_to_recording == [MAIN]  # camera_url() itself (recording/HLS) is untouched


def test_no_working_substream_keeps_the_main_stream(fake_mediamtx, monkeypatch, fresh_choice):
    monkeypatch.setattr(wp, "_camera_map", {"cam-x": 7})
    monkeypatch.setattr(wp, "_known_paths", set())
    monkeypatch.setattr(wp, "_probe_video_stream", lambda url: False)
    wp.sync_camera_paths(lambda n: MAIN)
    added = [c for c in fake_mediamtx.calls if c[1] == "/v3/config/paths/add/cam-x"]
    assert json.loads(added[0][2])["source"] == MAIN


def test_main_preference_never_probes_a_substream(monkeypatch, fresh_choice):
    monkeypatch.setattr(wp, "P2P_STREAM_PREFERENCE", "main")
    assert wp.p2p_source_for("cam-x", MAIN) == (MAIN, "main")
    assert fresh_choice == []


def test_the_choice_is_probed_once_and_redecided_when_the_camera_url_changes(monkeypatch, fresh_choice):
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "substream")
    assert wp.p2p_source_for("cam-x", MAIN) == (SUB, "substream")
    assert fresh_choice == [SUB]
    other = MAIN.replace("10.0.0.9", "10.0.0.10")  # camera re-provisioned at a new address
    assert wp.p2p_source_for("cam-x", other) == (other, "main")  # the fake probe rejects this host's substream
    assert fresh_choice == [SUB, SUB.replace("10.0.0.9", "10.0.0.10")]  # probed again, not served from cache


def test_recording_and_live_hls_still_use_the_main_stream_only():
    import main
    source = open(main.__file__, encoding="utf-8").read()
    assert "p2p_source_for" not in source and "substream_candidates" not in source


# ------------------------------------------------ LAN ICE candidates
# 2026-09-25: a viewer on the appliance's own LAN never reached MediaMTX
# (container-only candidates); the agent now publishes the host's LAN
# addresses and they are advertised as webrtcAdditionalHosts.


def _write_lan(tmp_path, monkeypatch, addresses):
    path = tmp_path / "lan_addresses.json"
    path.write_text(json.dumps({"addresses": addresses}))
    monkeypatch.setattr(wp, "LAN_ADDRESSES_FILE", path)


def test_advertised_hosts_accept_only_lan_and_tailscale_ipv4(tmp_path, monkeypatch):
    _write_lan(tmp_path, monkeypatch, ["192.168.0.228", "10.0.5.3", "100.77.253.28", "203.0.113.9", "8.8.8.8",
                                       "127.0.0.1", "169.254.1.1", "fd00::1", "garbage", "192.168.0.228"])
    assert wp.advertised_hosts() == ["192.168.0.228", "10.0.5.3", "100.77.253.28"]


def test_a_missing_address_file_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "LAN_ADDRESSES_FILE", tmp_path / "absent.json")
    assert wp.advertised_hosts() == []
    assert "webrtcAdditionalHosts" not in wp.render_mediamtx_config()


def test_lan_hosts_are_in_the_mediamtx_config(tmp_path, monkeypatch):
    _write_lan(tmp_path, monkeypatch, ["192.168.0.228"])
    assert 'webrtcAdditionalHosts: ["192.168.0.228"]' in wp.render_mediamtx_config().splitlines()


def test_host_changes_are_applied_live_only_when_they_change(fake_mediamtx, tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "_applied_hosts", None)
    _write_lan(tmp_path, monkeypatch, ["192.168.0.228"])
    wp.sync_additional_hosts()
    wp.sync_additional_hosts()
    _write_lan(tmp_path, monkeypatch, ["192.168.0.99"])  # DHCP moved the host
    wp.sync_additional_hosts()
    patches = [json.loads(c[2]) for c in fake_mediamtx.calls if c[1] == "/v3/config/global/patch"]
    assert patches == [{"webrtcAdditionalHosts": ["192.168.0.228"]}, {"webrtcAdditionalHosts": ["192.168.0.99"]}]


# --------------------------------------------------------------- MediaMTX crash/restart recovery

CAMERAS = {f"cam-{n}": n for n in range(1, 8)}  # any count; nothing below depends on it


def _url(n):
    return f"rtsp://u:p@10.0.0.{n}/main"


def _crash_and_restart(fake, tmp_path, monkeypatch):
    """The MediaMTX child dies (taking every runtime path with it) and the
    worker's own _ensure_mediamtx_running() spawns a new one."""
    monkeypatch.setattr(wp, "MEDIAMTX_CONFIG_PATH", tmp_path / "mediamtx.yml")
    monkeypatch.setattr(wp.subprocess, "Popen", lambda *a, **k: _FakeProcess(returncode=None))
    monkeypatch.setattr(wp, "_mediamtx_process", _FakeProcess(returncode=1))
    fake.existing.clear()
    wp._ensure_mediamtx_running()


@pytest.fixture()
def configured(fake_mediamtx, monkeypatch):
    monkeypatch.setattr(wp, "_camera_map", dict(CAMERAS))
    monkeypatch.setattr(wp, "_known_paths", set())
    monkeypatch.setattr(wp, "MEDIAMTX_STARTUP_GRACE_SECONDS", 0.2)
    wp.reconcile_camera_paths(_url)
    assert fake_mediamtx.existing == set(CAMERAS) and wp._known_paths == set(CAMERAS)
    fake_mediamtx.calls.clear()
    return fake_mediamtx


def _adds(fake):
    return sorted(c[1].rsplit("/", 1)[-1] for c in fake.calls
                  if c[0] == "POST" and c[1].startswith(("/v3/config/paths/add/", "/v3/config/paths/replace/")))


def test_a_mediamtx_restart_re_adds_every_camera_path_without_a_vms_restart(configured, tmp_path, monkeypatch):
    _crash_and_restart(configured, tmp_path, monkeypatch)
    assert wp._known_paths == set() and wp._paths_resync_pending is True
    wp.reconcile_camera_paths(_url)
    assert configured.existing == set(CAMERAS) == wp._known_paths
    assert _adds(configured) == sorted(CAMERAS)
    assert wp._paths_resync_pending is False


def test_offers_after_a_restart_wait_for_the_path_then_reach_mediamtx_again(configured, tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(wp, "_control_plane_post", lambda path, payload: posted.append(path))
    wp._whep_sessions["old-session"] = "http://127.0.0.1:1/old/whep/x"
    _crash_and_restart(configured, tmp_path, monkeypatch)
    assert wp._whep_sessions == {}  # belonged to the dead process
    offer = {"session_id": "s1", "camera_id": "cam-3", "kind": "offer", "payload": {"sdp": "v=0\r\n"}}
    wp._handle_pending_signal(None, offer)  # not re-added yet: refused, the viewer falls back to relay
    assert not [c for c in configured.calls if c[1].endswith("/whep")]
    wp.reconcile_camera_paths(_url)
    wp._handle_pending_signal(None, dict(offer, session_id="s2"))
    assert [c[1] for c in configured.calls if c[1].endswith("/whep")] == ["/cam-3/whep"]
    assert posted == ["/api/appliance/live/cam-3/p2p/answer"]


def test_a_still_starting_mediamtx_keeps_the_resync_pending_until_its_api_answers(configured, tmp_path, monkeypatch):
    _crash_and_restart(configured, tmp_path, monkeypatch)
    configured.api_down = True
    wp.reconcile_camera_paths(_url)
    assert _adds(configured) == [] and wp._paths_resync_pending is True
    configured.api_down = False
    wp.reconcile_camera_paths(_url)
    assert configured.existing == set(CAMERAS) == wp._known_paths


def test_steady_state_never_recreates_paths(configured):
    for _ in range(3):
        wp.reconcile_camera_paths(_url)
    assert _adds(configured) == []
    assert {c[0] for c in configured.calls} == {"GET"}  # one read-only list per refresh


def test_a_path_mediamtx_lost_without_an_observed_restart_is_re_added_alone(configured):
    configured.existing.discard("cam-5")
    wp.reconcile_camera_paths(_url)
    assert _adds(configured) == ["cam-5"] and wp._known_paths == set(CAMERAS)


def test_an_unreadable_path_list_changes_nothing(configured):
    configured.fail_list = True
    wp.reconcile_camera_paths(_url)
    assert _adds(configured) == [] and wp._known_paths == set(CAMERAS)


def test_a_restart_keeps_the_substream_choice_and_lan_hosts_without_reprobing(configured, tmp_path, monkeypatch):
    probes = []
    monkeypatch.setattr(wp, "_probe_video_stream", lambda url: probes.append(url) or True)
    main = "rtsp://u:p@10.0.0.9/Streaming/Channels/101"
    monkeypatch.setattr(wp, "_camera_map", {"cam-x": 9})
    monkeypatch.setattr(wp, "_known_paths", set())
    wp.reconcile_camera_paths(lambda n: main)
    first_source = json.loads([c for c in configured.calls if c[1] == "/v3/config/paths/add/cam-x"][0][2])["source"]
    assert first_source.endswith("/Streaming/Channels/102") and len(probes) == 1
    _write_lan(tmp_path, monkeypatch, ["192.168.0.228"])
    configured.calls.clear()
    _crash_and_restart(configured, tmp_path, monkeypatch)
    assert "192.168.0.228" in (tmp_path / "mediamtx.yml").read_text()
    wp.reconcile_camera_paths(lambda n: main)
    readded = json.loads([c for c in configured.calls if c[1] == "/v3/config/paths/add/cam-x"][0][2])["source"]
    assert readded == first_source and len(probes) == 1  # cached choice, no re-probe
    wp.sync_additional_hosts()
    assert not [c for c in configured.calls if c[1] == "/v3/config/global/patch"]  # new process already has them


@pytest.mark.anyio
async def test_the_worker_resyncs_right_after_a_restart_not_at_the_next_refresh(monkeypatch):
    import asyncio
    monkeypatch.setattr(wp, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(wp, "LIVE_P2P_ENABLED", True)
    monkeypatch.setattr(wp, "SCAN_SECONDS", 0.5)
    monkeypatch.setattr(wp, "CONFIG_REFRESH_SECONDS", 9999)
    monkeypatch.setattr(wp, "_ensure_mediamtx_running", lambda: None)
    monkeypatch.setattr(wp, "stop_mediamtx", lambda: None)
    monkeypatch.setattr(wp, "_refresh_camera_map", lambda: None)
    monkeypatch.setattr(wp, "sync_additional_hosts", lambda: None)
    resyncs = []

    def fake_reconcile(camera_url_fn):
        resyncs.append(wp._paths_resync_pending)
        wp._paths_resync_pending = False

    monkeypatch.setattr(wp, "reconcile_camera_paths", fake_reconcile)

    async def fake_bridge_tick(camera_url_fn):
        await asyncio.sleep(0.01)
        return False

    monkeypatch.setattr(wp, "_bridge_tick", fake_bridge_tick)
    task = asyncio.ensure_future(wp.webrtc_publisher_worker(lambda n: "rtsp://u:p@h:554/x"))
    await asyncio.sleep(0.2)
    assert resyncs == [False]  # only the first (due) refresh
    wp._paths_resync_pending = True  # what _start_mediamtx() sets on a restart
    await asyncio.sleep(0.8)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert resyncs == [False, True]  # re-synced within one loop; the refresh is still not due
