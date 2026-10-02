"""Regression coverage for GET /help (2026-09-23):

All three help cards described a different product entirely -- the
original local single-tenant desktop VMS this codebase grew out of, not
the cloud customer portal every other page in this suite covers:
  - "Getting started" claimed Playback only shows "completed five-minute
    recordings" -- the exact stale-index bug this session's own Playback
    fix (fix/playback-event-clip-union-20260922) already corrected:
    Playback shows both continuous recordings AND real-time event clips.
  - "Remote access" described a private Tailscale address and "the home
    computer and VMS running" -- there is no "home computer" in this
    product; cameras connect through the customer's own appliance to
    this always-on cloud portal, reachable from any signed-in browser
    (see phone-connect's own "Connect your phone" flow).
  - "Camera offline" described the interface reconnecting after "the VMS
    restarts" -- again a local-desktop-app framing; this cloud portal
    itself never "restarts" from the customer's perspective, only an
    individual camera/appliance's own connectivity does.

help_page() is a fully static function (no request/identity parameter
at all), so this is a pure content check.
"""

from fastapi.testclient import TestClient

import main
import partner_portal


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def test_help_page_no_longer_describes_the_wrong_product():
    with TestClient(main.app, base_url="https://portal-staging.anyaicam.com") as client:
        response = client.get("/help", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    html = response.text
    assert "five-minute recordings" not in html
    assert "Tailscale" not in html
    assert "the home computer and VMS" not in html
    assert "the VMS restarts" not in html


def test_help_page_describes_the_real_cloud_portal_product():
    with TestClient(main.app, base_url="https://portal-staging.anyaicam.com") as client:
        response = client.get("/help", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    html = response.text
    assert "event clips" in html
    assert "Phone access" in html
    assert "Dashboard" in html
