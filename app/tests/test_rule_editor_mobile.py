"""Smart Rules drawing on a phone (2026-10-01).

Reported: drawing a line crossing worked on a laptop but not on a phone.
Reproduced on a 390 px touch screen:
- the line and points were drawn in camera-frame pixels -- a 1280-wide
  frame on a 322 px canvas made the 2 px line half a pixel and the 4 px dots
  one pixel, so a placed line was practically invisible;
- input was a mouse-style click with default touch scrolling, so a finger
  that moved slightly scrolled the page instead of placing a point;
- a tap before the camera frame arrived (about 9 s on a phone) was ignored
  silently, and a placed point could not be adjusted.
"""
import sqlite3

import partner_portal
from test_customer_analytics_rules import (  # noqa: F401 -- fixtures and helpers
    _owner_cookie,
    _seed_camera,
    _seed_tenant,
    db_path,
    http_client,
)


def _editor(http_client, db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-1")
    _seed_camera(conn, "cam-1", customer_id="cust-1", camera_number=1)
    conn.commit()
    conn.close()
    response = http_client.get("/customer/cameras/cam-1/analytics-rules",
                               cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-1")})
    assert response.status_code == 200
    return response.text


def test_the_canvas_never_scrolls_the_page_under_a_finger(http_client, db_path):
    html = _editor(http_client, db_path)
    canvas = html[html.index('<canvas id="rule-canvas"'):]
    assert "touch-action:none" in canvas[:canvas.index("</canvas>")]


def test_input_is_pointer_based_for_finger_pen_and_mouse(http_client, db_path):
    html = _editor(http_client, db_path)
    for event in ("pointerdown", "pointermove", "pointerup", "pointercancel"):
        assert f"canvas.addEventListener('{event}'" in html
    assert "canvas.addEventListener('click'" not in html  # one input path, no double points


def test_points_can_be_dragged_and_undone(http_client, db_path):
    html = _editor(http_client, db_path)
    assert "setPointerCapture" in html and "dragIndex" in html
    assert 'id="undo-point"' in html and "getElementById('undo-point')" in html
    assert "Drag a point to move it" in html


def test_line_and_points_are_sized_for_the_screen_not_the_camera_frame(http_client, db_path):
    html = _editor(http_client, db_path)
    assert "function screenScale()" in html
    assert "ctx.lineWidth=3*px" in html and "7*px" in html and "ctx.lineWidth=2;" not in html
    assert "Math.round(14*screenScale())" in html  # the security line's PROTECTED label too


def test_a_tap_before_the_frame_arrives_says_what_is_happening(http_client, db_path):
    html = _editor(http_client, db_path)
    assert "Waiting for the camera image. Points can be placed as soon as it appears." in html
