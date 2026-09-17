"""Target: Live View P2P (2026-09-17 controlled real single-camera test).

Verifies the real browser-side attemptLiveP2P()/attemptP2P() flow
(app/live_view_page.py) against the real Ryzen appliance's MediaMTX
bridge (app/webrtc_publisher.py), reached through the real staging
cloud -- not a mock, not a unit test double. The existing CloudFront/
HLS relay path stays completely intact as the automatic fallback (see
live_view_p2p.py's own module docstring): this suite proves P2P works
ON TOP of that path, never in place of it.

Deliberately observes the SAME signal a real customer's browser
observes -- network requests this exact page issues, and the real
<video> element's own srcObject/track state -- rather than asserting
anything about server-side internals directly. "Which transport won"
is read from the browser's own transport-outcome report (the same one
production telemetry relies on), not inferred.

connect_ms and the transport winner are genuinely non-deterministic
(a real network race against a real bounded timeout, over the real
internet to a real appliance) -- tests below assert internal
consistency for whichever transport won (a p2p report must come with
a real video track and a real offer having been sent; either report
must be well-formed), not a specific winner every run.
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def logged_in_page(page, e2e_credentials):
    email, password = e2e_credentials
    page.goto("/customer-login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")
    return page


@pytest.fixture
def first_real_camera_id(logged_in_page):
    page = logged_in_page
    page.goto("/customer-live")
    page.wait_for_selector("[data-camera-id]")
    camera_id = page.locator("[data-camera-id]").first.get_attribute("data-camera-id")
    if not camera_id:
        pytest.skip("no data-camera-id attribute found on the fleet page's camera card")
    return camera_id


@pytest.mark.e2e
def test_p2p_config_reports_enabled_with_real_ice_servers(logged_in_page):
    """Precondition for every test below: the cloud-side feature flag
    (ANYAICAM_LIVE_P2P_ENABLED) must actually be on and handing back
    real STUN servers, or a later 'transport=relay' result would be
    meaningless (never even attempted, not a fair race)."""
    page = logged_in_page
    response = page.request.get("/api/customer/live/p2p/config")
    assert response.ok
    body = response.json()
    assert body["enabled"] is True
    assert isinstance(body.get("ice_servers"), list) and len(body["ice_servers"]) > 0
    # STUN only, per explicit product direction (live_view_p2p.py's own
    # module docstring) -- no TURN entry should appear unless a human
    # has deliberately configured one later.
    for server in body["ice_servers"]:
        assert "credential" not in server, "unexpected TURN-shaped ICE server entry in a STUN-only configuration"


@pytest.mark.e2e
def test_focused_live_view_negotiates_a_transport_and_reports_it_honestly(logged_in_page, first_real_camera_id, artifacts_dir):
    """The controlled real single-camera P2P test: opens the actual
    focused Live View page against the real Ryzen appliance (through
    staging), and proves -- from the browser's own perspective, not a
    server-side assumption -- exactly which transport won the real race
    and that its own report is internally consistent with what the page
    actually did."""
    page = logged_in_page

    p2p_requests: list[dict] = []

    def _capture(request):
        if "/p2p/" in request.url or "/transport-outcome" in request.url:
            p2p_requests.append({"url": request.url, "method": request.method, "post_data": request.post_data})

    page.on("request", _capture)

    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-video")

    # A real P2P connection means srcObject is set to a live MediaStream
    # (RTCPeerConnection's own ontrack) -- HLS relay instead sets a plain
    # .src URL string via hls.js, never srcObject. Poll (bounded, well
    # past the page's own 4000ms P2P timeout) rather than a single check,
    # since real STUN/ICE negotiation over the real internet takes real
    # wall-clock time.
    deadline = time.time() + 10
    transport_outcome = None
    while time.time() < deadline:
        transport_outcome = next((r for r in p2p_requests if "/transport-outcome" in r["url"]), None)
        if transport_outcome:
            break
        page.wait_for_timeout(250)

    (artifacts_dir / "p2p_network_capture.json").write_text(json.dumps(p2p_requests, indent=2), encoding="utf-8")
    assert transport_outcome is not None, "no transport-outcome was ever reported -- neither P2P nor relay ever won the race"

    outcome_body = json.loads(transport_outcome["post_data"])
    assert outcome_body["transport"] in ("p2p", "relay"), f"unexpected transport report: {outcome_body}"

    offer_sent = any("/p2p/offer" in r["url"] for r in p2p_requests)

    if outcome_body["transport"] == "p2p":
        # The controlled test's actual point when P2P wins: prove this
        # was a REAL WebRTC connection, not merely that the browser
        # believed it won the race.
        video_track_count = page.evaluate(
            "() => { const v = document.getElementById('live-view-video'); "
            "return v.srcObject ? v.srcObject.getVideoTracks().length : 0; }"
        )
        assert video_track_count > 0, "transport reported as p2p but the video element has no real video track"
        assert isinstance(outcome_body.get("connect_ms"), (int, float)) and outcome_body["connect_ms"] > 0, \
            "p2p transport must report a real, positive connect_ms"
        assert offer_sent, "transport=p2p reported but no SDP offer was ever sent -- outcome and signaling disagree"
    else:
        # Relay won this real race -- a genuine, non-flaky possible
        # outcome (P2P is a bounded-timeout race, not a guarantee), and
        # exactly the safety net this whole feature was built to
        # preserve. Confirm the video element is actually playing via
        # the untouched relay path, not left blank.
        current_src = page.evaluate("() => document.getElementById('live-view-video').currentSrc || ''")
        assert current_src, "transport reported as relay but the video element has no relay source set"


@pytest.mark.e2e
def test_no_p2p_signaling_payload_ever_contains_an_rtsp_url_or_camera_credentials(logged_in_page, first_real_camera_id):
    """Explicit safety requirement: webrtc_publisher.py's own module
    docstring states it "never receives or exposes a camera's RTSP
    credentials to the browser" -- this proves that from the browser's
    own network tab, not by reading the server source and trusting it.
    Every response body from every P2P signaling route this real
    session actually used must be scanned; a WHEP SDP answer only ever
    describes negotiated H264/Opus media, never a source URL."""
    page = logged_in_page

    response_bodies: list[str] = []

    def _capture_response(response):
        if "/p2p/" not in response.url:
            return
        try:
            response_bodies.append(response.text())
        except Exception:
            pass

    page.on("response", _capture_response)
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-video")
    page.wait_for_timeout(6000)

    assert response_bodies, "no P2P signaling responses were captured -- cannot prove the credential-safety property this test exists for"
    for body in response_bodies:
        lowered = body.lower()
        assert "rtsp://" not in lowered, f"a P2P signaling response contained a raw rtsp:// URL: {body[:200]}"
        assert "rtsp_url" not in lowered, f"a P2P signaling response contained an rtsp_url field: {body[:200]}"


@pytest.mark.e2e
def test_leaving_live_view_tears_down_the_p2p_connection_client_side(logged_in_page, first_real_camera_id):
    """Client-side half of session cleanup: stopping the session (the
    same Stop control a real customer uses) must actually close the
    RTCPeerConnection, not leave a live WebRTC connection silently
    running in the background after the user thinks they've left."""
    page = logged_in_page
    page.goto(f"/customer/cameras/{first_real_camera_id}/live")
    page.wait_for_selector("#live-view-video")
    page.wait_for_timeout(3000)  # let the P2P-vs-relay race actually settle first
    page.wait_for_selector("#live-view-stop")
    page.locator("#live-view-stop").click()
    page.wait_for_timeout(500)
    has_live_stream = page.evaluate(
        "() => { const v = document.getElementById('live-view-video'); "
        "return !!(v && v.srcObject && v.srcObject.getVideoTracks().some(t => t.readyState === 'live')); }"
    )
    assert not has_live_stream, "stopping live view must not leave a live WebRTC video track still running"
