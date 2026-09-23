"""Regression coverage for the AAC Voice Call iframe-embed defect
(2026-09-23, found via independent review of Voice Call Phase 1, no
live incident): cloud_security.py's CloudSecurityMiddleware set
`frame-ancestors 'none'` / `X-Frame-Options: DENY` unconditionally on
every response. The AAC Voice Call call screen (aac_voice_call.py,
GET /aac/voice-call/{event_id}) embeds the customer single-camera Live
view (GET /customer/cameras/{camera_id}/live) via <iframe>, exactly per
the product spec's "reuse the existing Live camera... instead of
creating a completely separate streaming system" -- but the blanket
'none' silently blocked the browser from ever rendering that iframe
("<domain> refused to connect"), confirmed live on
portal-staging.anyaicam.com with a real customer session and a real
camera. Fixed by relaxing frame-ancestors to 'self' (same-origin only
-- an external attacker's site still cannot frame this page from
anywhere else) for exactly that one route, and X-Frame-Options to
SAMEORIGIN there, leaving every other route's 'none'/DENY unchanged.
"""

from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def test_the_live_view_route_allows_same_origin_framing(tmp_path):
    with override_target(sqlite_path=tmp_path / "test_frame_ancestors.db"):
        initialize_database()
        with TestClient(main.app, base_url="https://portal-staging.anyaicam.com", follow_redirects=False) as client:
            response = client.get(
                "/customer/cameras/some-camera-id/live",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
            )
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_every_other_route_still_forbids_all_framing(tmp_path):
    """The fix must be scoped to exactly one route -- every other page
    (including the AAC Voice Call call screen page itself, which is the
    FRAME, not the framed content) keeps the original, strict policy."""
    with override_target(sqlite_path=tmp_path / "test_frame_ancestors2.db"):
        initialize_database()
        with TestClient(main.app, base_url="https://portal-staging.anyaicam.com", follow_redirects=False) as client:
            for path in ("/health", "/dashboard", "/customer-login.html", "/aac/voice-call/some-event-id"):
                response = client.get(path, cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
                assert "frame-ancestors 'none'" in response.headers["content-security-policy"], path
                assert response.headers["x-frame-options"] == "DENY", path


def test_a_path_merely_containing_the_live_route_shape_elsewhere_is_not_matched():
    """_frame_ancestors_csp() matches by exact prefix+suffix, not a
    loose substring -- a route that happens to end in "/live" for an
    unrelated reason, or contain "/customer/cameras/" without actually
    being the live-view route, must not be relaxed."""
    from cloud_security import _frame_ancestors_csp

    class _FakeURL:
        def __init__(self, path):
            self.path = path

    class _FakeRequest:
        def __init__(self, path):
            self.url = _FakeURL(path)

    assert _frame_ancestors_csp(_FakeRequest("/customer/cameras/abc123/live")) == "self"
    assert _frame_ancestors_csp(_FakeRequest("/customer/cameras/abc123/live/")) == "none"
    assert _frame_ancestors_csp(_FakeRequest("/api/customer/cameras/abc123/live")) == "none"
    assert _frame_ancestors_csp(_FakeRequest("/some/other/live")) == "none"
    assert _frame_ancestors_csp(_FakeRequest("/health")) == "none"
