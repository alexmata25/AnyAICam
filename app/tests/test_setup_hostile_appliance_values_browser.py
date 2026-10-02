"""Stored HTML injection in customer setup (2026-10-02), in a real browser.

An appliance controls its heartbeat software_version and its camera
discovery results (manufacturer, model, device key...). The setup page used
to interpolate them into innerHTML / data-* attributes, so a hostile value
became executable DOM in the customer's session. The page now builds those
rows with DOM nodes and textContent; the cloud also bounds what it stores
(appliance_protocol). The value below is stored directly, as a value from an
older build would be, so the page itself is what is tested.

Opt-in (launches a real browser): ANYAICAM_RUN_BROWSER_TESTS=1.
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ANYAICAM_RUN_BROWSER_TESTS") != "1",
    reason="browser validation is opt-in (ANYAICAM_RUN_BROWSER_TESTS=1)",
)

ORIGIN = "https://anyaicam.test"
HOSTILE = '<img src=x onerror="window.__pwned=(window.__pwned||0)+1">'
ATTR_BREAK = '" onmouseover="window.__pwned=99" x="'
SCAN = {"job_id": "job-1", "status": "completed", "progress": 100, "message": "Done", "results": [
    {"device_key": ATTR_BREAK, "manufacturer": HOSTILE, "model": "<script>window.__pwned=7</script>",
     "name": HOSTILE, "onvif_endpoint": ATTR_BREAK},
]}


def _render_setup_page():
    from fastapi.testclient import TestClient

    from database_backend import override_target

    db_path = Path(tempfile.mkdtemp()) / "setup_browser.db"
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        conn = sqlite3.connect(db_path)
        now = "2026-10-02T00:00:00"
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('partner-1','P',?)", (now,))
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active',?)", (now,))
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Home',?)", (now,))
        conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,software_version,online_status,created_at) "
                     "VALUES('appl-1','cust-1','site-1','AIC-1',?,'online',?)", (HOSTILE, now))
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,created_at) "
                     "VALUES('owner-1','partner-1','owner@example.test','Owner','customer_owner','x',1,'cust-1','active',?)", (now,))
        conn.commit()
        conn.close()
        import main
        import partner_portal
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as client:
            token = partner_portal._token("owner@example.test", "customer_owner", "partner-1", "cust-1", None)
            response = client.get("/customer/setup", cookies={partner_portal.SESSION_COOKIE: token})
    assert response.status_code == 200, response.text[:300]
    return response.text


def test_hostile_appliance_values_never_become_executable_dom():
    from playwright.sync_api import sync_playwright

    html = _render_setup_page()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def route(r):
            url = r.request.url
            if url.startswith(ORIGIN + "/customer/setup"):
                return r.fulfill(status=200, content_type="text/html", body=html)
            if "/camera-scans/" in url or "/scans/latest" in url:
                return r.fulfill(status=200, content_type="application/json", body=json.dumps(SCAN))
            if "/api/customer/cameras" in url:
                return r.fulfill(status=200, content_type="application/json", body=json.dumps({"configured_camera_count": 0, "expected_camera_count": 8}))
            return r.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/*", route)
        page.goto(ORIGIN + "/customer/setup")
        page.evaluate("() => { setupStep = 3; showSetup(); }")
        status_html = page.inner_html("#appliance-status")
        page.evaluate("async () => { scanJob = 'job-1'; await pollScan(); }")
        page.dispatch_event("#scan-results .health-row", "mouseover")  # would fire an injected handler
        page.wait_for_timeout(300)

        assert page.evaluate("() => window.__pwned") is None
        # the values are shown as text, not markup
        assert page.locator("#appliance-status img").count() == 0 and "<img" not in status_html.replace("&lt;img", "")
        assert HOSTILE in page.inner_text("#appliance-status")
        assert page.locator("#scan-results img, #scan-results script").count() == 0
        row = page.locator("#scan-results .health-row").first
        assert row.get_attribute("onmouseover") is None
        assert row.get_attribute("data-device-key") == ATTR_BREAK  # kept exactly, as data
        assert HOSTILE in page.inner_text("#scan-results")
        browser.close()

