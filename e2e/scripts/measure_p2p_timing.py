"""One-off diagnostic (2026-09-17): measures exactly where P2P
negotiation time goes, phase by phase, by wrapping RTCPeerConnection
before live_view_page.js's own script runs (via add_init_script, so it
survives that script overwriting window.attemptLiveP2P) and correlating
with real Python-side wall-clock timestamps on every P2P network
request/response this one page issues. Not part of the regular e2e
suite -- a targeted investigation script, run manually.
"""
import json
import os
import sys
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

E2E_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(E2E_ROOT, ".env"), override=False)

BASE_URL = os.environ.get("ANYAICAM_E2E_BASE_URL", "https://portal-staging.anyaicam.com")
EMAIL = os.environ.get("ANYAICAM_E2E_USERNAME")
PASSWORD = os.environ.get("ANYAICAM_E2E_PASSWORD")

INIT_SCRIPT = """
window.__pcEvents = [];
const _t0 = performance.now();
const OrigRTCPeerConnection = window.RTCPeerConnection;
window.RTCPeerConnection = function(...args) {
  const pc = new OrigRTCPeerConnection(...args);
  window.__pcEvents.push({t: performance.now() - _t0, event: 'pc_constructed'});
  pc.addEventListener('iceconnectionstatechange', () => {
    window.__pcEvents.push({t: performance.now() - _t0, event: 'iceconnectionstatechange', state: pc.iceConnectionState});
  });
  pc.addEventListener('icegatheringstatechange', () => {
    window.__pcEvents.push({t: performance.now() - _t0, event: 'icegatheringstatechange', state: pc.iceGatheringState});
  });
  pc.addEventListener('connectionstatechange', () => {
    window.__pcEvents.push({t: performance.now() - _t0, event: 'connectionstatechange', state: pc.connectionState});
  });
  pc.addEventListener('track', () => {
    window.__pcEvents.push({t: performance.now() - _t0, event: 'ontrack'});
  });
  return pc;
};
window.RTCPeerConnection.prototype = OrigRTCPeerConnection.prototype;
window.__pageT0 = _t0;
"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(base_url=BASE_URL)
    page = context.new_page()
    page.add_init_script(INIT_SCRIPT)

    net_events = []
    def on_request(request):
        if "/p2p/" in request.url or "/transport-outcome" in request.url:
            net_events.append({"t": time.time(), "kind": "request", "url": request.url, "method": request.method})
    def on_response(response):
        if "/p2p/" in response.url or "/transport-outcome" in response.url:
            net_events.append({"t": time.time(), "kind": "response", "url": response.url, "status": response.status})
    page.on("request", on_request)
    page.on("response", on_response)

    page.goto("/customer-login.html")
    page.fill("#email", EMAIL)
    page.fill("#password", PASSWORD)
    page.click("form#login button.submit")
    page.wait_for_load_state("networkidle")

    page.goto("/customer-live")
    page.wait_for_selector("[data-camera-id]")
    camera_id = page.locator("[data-camera-id]").first.get_attribute("data-camera-id")

    page_nav_time = time.time()
    page.goto(f"/customer/cameras/{camera_id}/live")
    page.wait_for_selector("#live-view-video")
    page.wait_for_timeout(6000)

    pc_events = page.evaluate("() => window.__pcEvents")
    page_t0_epoch = page_nav_time  # approx: page navigation start, close enough to page's own performance.now() t0

    print(json.dumps({"pc_events": pc_events, "net_events": net_events, "page_nav_epoch": page_nav_time}, indent=2))
    browser.close()
