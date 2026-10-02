"""License Plates table in a real browser (2026-09-30), desktop and phone.
The page is rendered by the real app; the data API, plate image and clip
are mocked in the browser (same harness as test_analytics_workspace_browser).
Opt-in: ANYAICAM_RUN_BROWSER_TESTS=1."""
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from test_analytics_workspace_browser import ASSETS, BROWSERS, ORIGIN, SNAPSHOT, _wait_playing, playwright_instance, webm  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)


@pytest.fixture(scope="module")
def lpr_page():
    from fastapi.testclient import TestClient
    from database_backend import override_target
    db_path = Path(tempfile.mkdtemp()) / "lpr_browser.db"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        import main
        import partner_portal
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust','p1','C','c@e.test','active','x')")
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('s1','cust','Home','x')")
        conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES('cam-1','cust','s1','Driveway','configured',1,'x')")
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                     "VALUES('u','p1','o@e.test','O','customer_owner','x',1,'cust','x','active','all')")
        conn.execute("INSERT INTO camera_analytics_entitlements(camera_id,analytic_key,status,created_at,updated_at) VALUES('cam-1','lpr','active','x','x')")
        conn.commit()
        conn.close()
        with TestClient(main.app, base_url=ORIGIN, follow_redirects=False) as client:
            cookie = {partner_portal.SESSION_COOKIE: partner_portal._token("o@e.test", "customer_owner", None, "cust", None)}
            return client.get("/analytics/lpr", cookies=cookie).text


def _data():
    now = int(time.time() * 1000)
    full = {"plate": "KXT4821", "has_plate_image": True, "vehicle_type": "Car", "vehicle_color": "White", "vehicle_make": None, "vehicle_model": None}
    old = {"plate": None, "has_plate_image": False, "vehicle_type": None, "vehicle_color": None, "vehicle_make": None, "vehicle_model": None}
    rows = [{"event_id": "p-1", "camera_id": "cam-1", "event_type": "plate", "timestamp_ms": now - 60000, "confidence": 0.94,
             "has_clip": True, "has_thumbnail": True, "details": full},
            {"event_id": "p-2", "camera_id": "cam-1", "event_type": "plate", "timestamp_ms": now - 3600000, "confidence": 0.9,
             "has_clip": False, "has_thumbnail": False, "details": old}]
    return {"events": rows, "summary": {"total": 2}, "next_before": None, "enabled_camera_ids": ["cam-1"]}


def _open(playwright_instance, channel, html, webm, *, mobile):  # noqa: F811
    try:
        browser = playwright_instance.chromium.launch(channel=channel) if channel else playwright_instance.chromium.launch()
    except Exception as error:
        pytest.skip(f"browser unavailable: {error}")
    options = ({"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True} if mobile
               else {"viewport": {"width": 1440, "height": 900}})
    page = browser.new_context(**options).new_page()

    def handle(route):
        tail = route.request.url[len(ORIGIN):].split("?")[0]
        if tail == "/analytics/lpr":
            return route.fulfill(status=200, content_type="text/html", body=html)
        if tail.startswith("/static/") and tail[8:] in ASSETS:
            return route.fulfill(status=200, content_type="text/css" if tail.endswith(".css") else "application/javascript", body=ASSETS[tail[8:]])
        if tail == "/api/customer/analytics/lpr/events":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(_data()))
        if tail.endswith("/plate-image") or tail.endswith("/thumbnail"):
            return route.fulfill(status=200, content_type="image/jpeg", body=SNAPSHOT)
        if tail.endswith("/media/url"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"url": "/clips/p-1.webm"}))
        if tail.startswith("/clips/"):
            return route.fulfill(status=200, content_type="video/webm", body=webm)
        return route.fulfill(status=404, body="")

    page.route("**/*", handle)
    page.goto(f"{ORIGIN}/analytics/lpr")
    page.wait_for_selector(".aw-lpr-row")
    return browser, page


def _cells(page, index):
    return page.locator(".aw-lpr-row").nth(index).locator("[role=cell]").all_inner_texts()


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "phone"])
@pytest.mark.parametrize("engine,channel", BROWSERS, ids=[b[0] for b in BROWSERS])
def test_lpr_table_rows_unknowns_image_and_inline_clip(playwright_instance, lpr_page, webm, engine, channel, mobile):  # noqa: F811
    browser, page = _open(playwright_instance, channel, lpr_page, webm, mobile=mobile)
    try:
        full, old = _cells(page, 0), _cells(page, 1)
        assert full[1:3] == ["Driveway", "KXT4821"]
        assert full[4:7] == ["Unknown", "Unknown", "White car"]
        assert old[2:7] == ["Not recorded", "—", "Unknown", "Unknown", "Unknown"] and old[7] == "No clip"
        page.wait_for_function("() => { const i = document.querySelector('.aw-plate-img'); return i && i.complete && i.naturalWidth > 0; }")
        if mobile:
            assert page.locator(".aw-lpr-head").is_hidden()
            label = page.evaluate("getComputedStyle(document.querySelector('.aw-lpr-row [data-label=\"Plate\"]'),'::before').content")
            assert label == '"Plate"'
        else:
            assert page.locator(".aw-lpr-head [role=columnheader]").all_inner_texts() == [
                "Time", "Camera", "Plate", "Plate image", "Make", "Model", "Color / type", "Clip"]
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.locator(".aw-lpr-row").nth(0).locator(".aw-lpr-open").click()
        assert page.evaluate("() => document.querySelector('.aw-lpr-row').nextElementSibling.classList.contains('inline-media-card')")
        _wait_playing(page)
    finally:
        browser.close()
