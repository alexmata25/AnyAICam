"""Video Door Call + Access Control camera-card buttons (2026-09-05):
UI/framework placeholders on the Live View grid (customer_live_landing(),
GET /customer-live in live_view_page.py). Both are pure presentation --
no doorbell signaling, Talk Down session, relay trigger, ONVIF/ISAPI
call, or camera/recording/event-media behavior starts from either
button, and neither makes any network request; clicking either just
opens that button's own single shared "Coming Soon" panel.

Deliberately calls the real registered route's endpoint function
directly (main.app.routes lookup, never a hand-copied duplicate of its
logic) instead of running a live TestClient/uvicorn server: starting
the real ASGI app on this edge-role codebase drives its lifespan
startup, which spawns real ffmpeg recording/AI-detection worker tasks
against this appliance's actual configured cameras (confirmed live
during this milestone's own staging verification) -- something a
UI-only placeholder test must never risk triggering. Calling the
already-decorated function directly exercises the exact same rendering
code with none of that risk.
"""
import re
from unittest.mock import MagicMock

import pytest

import live_view_page
import main


@pytest.fixture()
def customer_live_landing():
    route = next(r for r in main.app.routes if getattr(r, "path", None) == "/customer-live")
    return route.endpoint


@pytest.fixture()
def rendered_html(customer_live_landing, monkeypatch):
    fake_identity = {
        "role": "customer_owner",
        "customer_id": "test-customer-render-only",
        "email": "render-test@example.test",
    }
    fake_cameras = [
        {"id": "cam-1", "name": "Front Door", "tooltip": "Press and hold to talk", "enabled": True},
        {"id": "cam-2", "name": "Back Yard", "tooltip": "Press and hold to talk", "enabled": False},
    ]

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeDB:
        def execute(self, *_args, **_kwargs):
            return FakeCursor()

    class FakeConnCtx:
        def __enter__(self):
            return FakeDB()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(live_view_page, "partner_identity", lambda request: fake_identity)
    monkeypatch.setattr(live_view_page, "connection", lambda: FakeConnCtx())
    monkeypatch.setattr(live_view_page, "_customer_live_cameras", lambda db, identity: fake_cameras)

    return customer_live_landing(MagicMock())


def _tiles(html):
    return re.finditer(r'<article class="live-grid-tile"[^>]*>.*?</article>', html, re.S)


def test_renders_html_not_a_redirect(rendered_html):
    assert isinstance(rendered_html, str)


def test_control_order_is_mic_door_call_access_control_settings(rendered_html):
    for tile_match in _tiles(rendered_html):
        controls = re.findall(r'class="camera-tool[^"]*"', tile_match.group(0))
        assert controls == [
            'class="camera-tool talk-mic"',
            'class="camera-tool door-call-tool"',
            'class="camera-tool access-control-tool"',
            'class="camera-tool"',
        ]


def test_mic_button_unchanged(rendered_html):
    assert "🎤" in rendered_html
    enabled_tag = rendered_html.split('id="talk-mic-cam-1"')[1].split(">")[0]
    disabled_tag = rendered_html.split('id="talk-mic-cam-2"')[1].split(">")[0]
    assert "disabled" not in enabled_tag
    assert "disabled" in disabled_tag


def test_settings_link_unchanged(rendered_html):
    assert 'href="/customer/cameras/cam-1/live"' in rendered_html
    assert 'title="Camera tools (mute, snapshot, download, share, analytics, bookmark, stop)"' in rendered_html


def test_door_call_button_icon_and_camera_aware_attributes(rendered_html):
    assert "🔔" in rendered_html
    assert 'id="door-call-cam-1"' in rendered_html
    assert 'data-door-call-enabled="0"' in rendered_html
    assert 'data-door-call-device-id=""' in rendered_html
    assert 'data-door-call-provider=""' in rendered_html
    assert 'data-entry-point-id=""' in rendered_html
    assert 'data-two-way-audio-capable="0"' in rendered_html
    assert 'data-relay-capable="0"' in rendered_html


def test_access_control_button_icon_and_camera_aware_attributes(rendered_html):
    assert "⚿" in rendered_html
    assert 'id="access-control-cam-1"' in rendered_html
    assert 'data-access-control-enabled="0"' in rendered_html
    assert 'data-relay-device-id=""' in rendered_html
    assert 'data-relay-channel=""' in rendered_html
    assert 'data-allowed-action=""' in rendered_html


def test_shared_modals_present_exactly_once_each_with_correct_copy(rendered_html):
    assert rendered_html.count('id="door-call-modal"') == 1
    assert rendered_html.count('id="access-control-modal"') == 1
    assert "Video Door Call" in rendered_html
    assert "Receive doorbell calls with live video and two-way audio." in rendered_html
    assert "Control doors, gates, and relay-enabled entry points from AnyAiCam." in rendered_html
    assert rendered_html.count(">Coming soon<") == 2


def test_no_network_call_in_door_call_or_access_control_click_wiring(rendered_html):
    wiring_start = rendered_html.index("wireTileModal(")
    wiring_block = rendered_html[wiring_start:wiring_start + 1200]
    assert "fetch(" not in wiring_block
