"""Cross-browser validation of the live-view P2P upgrade (2026-09-24).

Runs the REAL, server-rendered live pages (grid /customer-live and single
/customer/cameras/{id}/live, via main.app) in Chromium, Microsoft Edge and
WebKit (Safari engine) with Playwright. Every browser loads the pages from
the same local harness origin with the same mocked network, so behavior
differences are browser differences, not environment differences.

Only the network, RTCPeerConnection and hls.js are replaced (no real
camera, appliance, relay or media server is involved). Scenarios:

  upgrade   relay shows video first; P2P connects later and takes over the
            tile (HLS player destroyed, stream switched, 'p2p' reported)
  fallback  that P2P connection then fails; the page returns to the relay
            (playlist polled again, a new HLS player, 'relay' reported)
  p2p_first P2P connects before the relay has segments; relay never attaches

Opt-in (launches real browsers): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import json
import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

ORIGIN = "http://anyaicam.test"

FAKES_JS = r"""
window.__log = {hlsLoads: [], hlsDestroyed: 0, pcs: []};
// Playwright's WebKit build for Windows ships without WebRTC, MediaStream
// and MediaSource (real Safari has all three), and its video.srcObject
// rejects anything but a real MediaProvider. A Blob IS a MediaProvider
// there, so the fake connection hands the page a Blob as its "stream" --
// enough for the page's own upgrade/fallback logic to run in the Safari
// engine. Real WebRTC playback on Safari still needs Apple hardware.
window.__makeFakeStream = () => {
  if (typeof window.MediaStream === 'undefined') return new Blob([]);
  try {
    const canvas = document.createElement('canvas'); canvas.width = 64; canvas.height = 48;
    canvas.getContext('2d').fillRect(0, 0, 64, 48);
    return canvas.captureStream ? canvas.captureStream(5) : new MediaStream();
  } catch (e) { return new MediaStream(); }
};
window.__p2pDelayMs = 1500;
class FakeHls {
  static isSupported() { return true; }
  static get Events() { return {MANIFEST_PARSED: 'manifestParsed', ERROR: 'hlsError'}; }
  static get ErrorTypes() { return {NETWORK_ERROR: 'networkError', MEDIA_ERROR: 'mediaError'}; }
  constructor() { this.handlers = {}; }
  loadSource(url) { window.__log.hlsLoads.push(url); }
  attachMedia(video) { this.video = video; setTimeout(() => (this.handlers.manifestParsed || []).forEach(f => f()), 10); }
  on(event, fn) { (this.handlers[event] = this.handlers[event] || []).push(fn); }
  destroy() { window.__log.hlsDestroyed++; }
  startLoad() {}
  recoverMediaError() {}
}
window.__FakeHls = FakeHls;
class FakePC extends EventTarget {
  constructor() {
    super();
    this.connectionState = 'new'; this.iceConnectionState = 'new';
    this.ontrack = null; this.oniceconnectionstatechange = null; this.onicecandidate = null;
    this.localDescription = null; this.closed = false; this.framesDecoded = 0;
    window.__log.pcs.push(this);
  }
  // Decoded frames advance every 100ms once connected -- unless the test
  // sets window.__p2pNoFrames (never any) or window.__p2pFreeze (stop now).
  async getStats() {
    return new Map([['inbound-video', {type: 'inbound-rtp', kind: 'video', framesDecoded: this.framesDecoded}]]);
  }
  addTransceiver() {}
  async createOffer() { return {type: 'offer', sdp: 'v=0 fake-offer'}; }
  async setLocalDescription(d) { this.localDescription = d; }
  async setRemoteDescription() {
    setTimeout(() => {
      if (this.closed) return;
      this._set('connected');
      this._frames = setInterval(() => {
        if (!this.closed && !window.__p2pNoFrames && !window.__p2pFreeze) this.framesDecoded += 1;
      }, 100);
      if (this.ontrack) this.ontrack({streams: [window.__makeFakeStream()]});
    }, window.__p2pDelayMs);
  }
  async addIceCandidate() {}
  close() { this.closed = true; clearInterval(this._frames); this.connectionState = 'closed'; this.iceConnectionState = 'closed'; }
  _set(state) {
    this.connectionState = state; this.iceConnectionState = state;
    this.dispatchEvent(new Event('connectionstatechange'));
    this.dispatchEvent(new Event('iceconnectionstatechange'));
    if (this.oniceconnectionstatechange) this.oniceconnectionstatechange();
  }
}
window.RTCPeerConnection = FakePC;
"""


def _render_pages():
    """The real pages, rendered by the real app for an authorized test
    customer in a throwaway database."""
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    from database_backend import override_target

    db_path = Path(tempfile.mkdtemp()) / "browser_live_pages.db"
    with override_target(sqlite_path=str(db_path)):
        import main
        import partner_portal
        from partner_db import connection, initialize_database

        initialize_database()
        with connection() as conn:
            now = "2026-09-24T00:00:00"
            conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (now,))
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
            conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
            conn.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
                "VALUES('cam-a','cust-a','site-a','appl-a',1,'urn:uuid:fake-a','configured','Camera 1',?)", (now,)
            )
            conn.execute(
                "INSERT INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all',?)", (now,)
            )
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-a", None)}
        with TestClient(main.app, base_url=f"http://{host}") as client:
            grid = client.get("/customer-live", cookies=cookie)
            single = client.get("/customer/cameras/cam-a/live", cookies=cookie)
    assert grid.status_code == 200 and single.status_code == 200
    return {"grid": grid.text, "single": single.text}


@pytest.fixture(scope="module")
def pages():
    return _render_pages()


BROWSERS = [("chromium", None), ("msedge", "msedge"), ("webkit", None)]


@pytest.fixture(scope="module")
def playwright_instance():
    playwright_sync = pytest.importorskip("playwright.sync_api")
    with playwright_sync.sync_playwright() as instance:
        yield instance


def _launch(playwright_instance, engine, channel):
    try:
        if engine == "webkit":
            return playwright_instance.webkit.launch()
        return playwright_instance.chromium.launch(channel=channel) if channel else playwright_instance.chromium.launch()
    except Exception as error:  # browser not installed on this machine
        pytest.skip(f"{engine} unavailable: {error}")


class Harness:
    def __init__(self, page, html, *, playlist_has_segments=True):
        self.page = page
        self.playlist_has_segments = playlist_has_segments
        self.playlist_requests = 0
        self.outcomes = []
        self.p2p_timeout_ms = 15000
        page.add_init_script(FAKES_JS)
        page.route("**/*", lambda route: self._handle(route, html))

    def _json(self, route, body):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _handle(self, route, html):
        request = route.request
        url = request.url
        if url.startswith(f"{ORIGIN}/page"):
            return route.fulfill(status=200, content_type="text/html", body=html)
        if "cdn.jsdelivr.net/npm/hls.js" in url:
            return route.fulfill(status=200, content_type="application/javascript", body="window.Hls = window.__FakeHls;")
        if not url.startswith(ORIGIN):
            return route.fulfill(status=200, body="")
        if "/live/start" in url:
            return self._json(route, {"session_id": "sess-1", "status": "requested"})
        if url.endswith("/live/playlist.m3u8") or "/live/playlist.m3u8?" in url:
            self.playlist_requests += 1
            body = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg-1.ts\n" if self.playlist_has_segments else "#EXTM3U\n#EXT-X-TARGETDURATION:2\n"
            return route.fulfill(status=200, content_type="application/vnd.apple.mpegurl", body=body)
        if "/live/p2p/config" in url:
            return self._json(route, {"enabled": True, "ice_servers": [], "timeout_ms": self.p2p_timeout_ms})
        if "/p2p/answer" in url:
            return self._json(route, {"answer": {"sdp": "v=0 fake-answer"}, "candidates": []})
        if "/transport-outcome" in url:
            self.outcomes.append(json.loads(request.post_data or "{}").get("transport"))
            return self._json(route, {"status": "recorded"})
        if "/wireguard/config" in url:
            return self._json(route, {"enabled": False})
        return self._json(route, {})

    def log(self):
        return self.page.evaluate("() => ({hlsLoads: window.__log.hlsLoads.length, hlsDestroyed: window.__log.hlsDestroyed, pcs: window.__log.pcs.length})")

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self.page.wait_for_timeout(100)
        return False


VIDEO_ID = {"grid": "live-grid-video-cam-a", "single": "live-view-video"}


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
@pytest.mark.parametrize("kind", ["grid", "single"])
def test_relay_first_then_p2p_upgrade_then_relay_fallback(playwright_instance, pages, engine, channel, kind):
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_page()
        harness = Harness(page, pages[kind])
        page.goto(f"{ORIGIN}/page")

        # 1. relay attaches first (P2P answer is delayed 1.5s)
        assert harness.wait_for(lambda: "relay" in harness.outcomes), f"relay never attached: {harness.outcomes}"
        assert harness.log()["hlsLoads"] >= 1

        # 2. P2P connects and upgrades the tile
        assert harness.wait_for(lambda: "p2p" in harness.outcomes), f"no p2p upgrade: {harness.outcomes}"
        log = harness.log()
        assert log["hlsDestroyed"] >= 1, "relay HLS player must be torn down on upgrade"
        assert page.evaluate(f"() => !!document.getElementById('{VIDEO_ID[kind]}').srcObject")
        requests_after_upgrade = harness.playlist_requests
        page.wait_for_timeout(2500)
        assert harness.playlist_requests == requests_after_upgrade, "relay playlist must not be fetched after upgrade"

        # 3. P2P fails -> back to relay
        loads_before = harness.log()["hlsLoads"]
        page.evaluate("() => window.__log.pcs[window.__log.pcs.length - 1]._set('failed')")
        assert harness.wait_for(lambda: harness.log()["hlsLoads"] > loads_before), "relay not re-attached after P2P failure"
        assert harness.outcomes.count("relay") >= 2
        assert harness.playlist_requests > requests_after_upgrade
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
@pytest.mark.parametrize("kind", ["grid", "single"])
def test_p2p_first_keeps_the_relay_player_off(playwright_instance, pages, engine, channel, kind):
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_page()
        harness = Harness(page, pages[kind], playlist_has_segments=False)
        page.add_init_script("window.__p2pDelayMs = 300;")
        page.goto(f"{ORIGIN}/page")
        assert harness.wait_for(lambda: "p2p" in harness.outcomes), f"p2p never won: {harness.outcomes}"
        page.wait_for_timeout(1000)
        assert harness.log()["hlsLoads"] == 0
        assert "relay" not in harness.outcomes
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
@pytest.mark.parametrize("kind", ["grid", "single"])
def test_p2p_that_connects_but_never_decodes_a_frame_never_replaces_the_relay(playwright_instance, pages, engine, channel, kind):
    """The live Ryzen failure (2026-09-24): P2P ICE connected and a track
    arrived, but packet loss meant no frame ever decoded -- and the tile
    switched to that empty stream anyway, tearing the relay down."""
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_page()
        harness = Harness(page, pages[kind])
        harness.p2p_timeout_ms = 3000
        page.add_init_script("window.__p2pDelayMs = 300; window.__p2pNoFrames = true;")
        page.goto(f"{ORIGIN}/page")
        assert harness.wait_for(lambda: "relay" in harness.outcomes), f"relay never attached: {harness.outcomes}"
        page.wait_for_timeout(4000)  # past the P2P attempt's own timeout
        assert "p2p" not in harness.outcomes, f"a frameless P2P stream must never win: {harness.outcomes}"
        assert harness.log()["hlsDestroyed"] == 0, "the relay player must keep playing"
        assert page.evaluate(f"() => !document.getElementById('{VIDEO_ID[kind]}').srcObject")
    finally:
        browser.close()


@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
@pytest.mark.parametrize("kind", ["grid", "single"])
def test_p2p_that_freezes_mid_session_falls_back_to_the_relay(playwright_instance, pages, engine, channel, kind):
    browser = _launch(playwright_instance, engine, channel)
    try:
        page = browser.new_page()
        harness = Harness(page, pages[kind])
        page.add_init_script("window.__p2pDelayMs = 300; window.liveP2PFrozenMs = 1500;")
        page.goto(f"{ORIGIN}/page")
        assert harness.wait_for(lambda: "p2p" in harness.outcomes), f"p2p never won: {harness.outcomes}"
        loads_before = harness.log()["hlsLoads"]
        page.evaluate("() => { window.__p2pFreeze = true; }")  # still 'connected', frames stop
        assert harness.wait_for(lambda: harness.log()["hlsLoads"] > loads_before), "frozen P2P must fall back to the relay"
        assert harness.outcomes[-1] == "relay"
    finally:
        browser.close()
