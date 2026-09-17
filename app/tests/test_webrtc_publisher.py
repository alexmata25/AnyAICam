"""Appliance-side WebRTC publisher (2026-09-17): regression coverage for
webrtc_publisher.py -- the MediaMTX config/process lifecycle and the
cloud-signaling <-> MediaMTX-WHEP bridge.

Runs against a real local HTTP server (http.server, a background thread,
stdlib only) that reproduces MediaMTX's own confirmed real route contract
(WHEP POST/Location/answer shape, config REST API add/delete), NOT a live
`mediamtx` binary -- this sandbox could not reliably download the release
asset (see webrtc_publisher.py's own module docstring for the full
explanation and what still needs live-binary verification before this
feature is trusted end-to-end). Process spawning is tested with a fake
subprocess.Popen, never a real MediaMTX process.

The single most safety-critical test in this file is
test_worker_does_nothing_at_all_when_the_feature_flag_is_off -- P2P must
stay fully inert (no MediaMTX spawn, no config sync, no signaling poll)
until ANYAICAM_LIVE_P2P_ENABLED is explicitly set, exactly matching the
"do not enable P2P" instruction this phase was built under.
"""

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
        if self.path.startswith("/v3/config/paths/add/"):
            if self.__class__.fail_config:
                self.send_response(400)
                self.end_headers()
                return
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

    def do_DELETE(self):
        self.__class__.calls.append((self.command, self.path, b"", self._headers_lower()))
        self.send_response(200)
        self.end_headers()

    def do_PATCH(self):
        body = self._body()
        self.__class__.calls.append((self.command, self.path, body, self._headers_lower()))
        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def fake_mediamtx(monkeypatch):
    _FakeMediaMTX.calls = []
    _FakeMediaMTX.fail_whep = False
    _FakeMediaMTX.fail_config = False
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
    location = list(fake_mediamtx.calls) and None  # noop, just for readability
    session_location, _ = wp.whep_offer("cam-a", "v=0\r\n...")
    wp._forward_client_ice_candidate(session_location, {"candidate": "candidate:1 1 UDP 1 10.0.0.1 5000 typ host", "sdpMid": "0"})
    patch_calls = [c for c in fake_mediamtx.calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0][3]["content-type"] == "application/trickle-ice-sdpfrag"
    assert b"a=candidate:1 1 UDP 1 10.0.0.1 5000 typ host" in patch_calls[0][2]


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
