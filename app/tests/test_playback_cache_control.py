"""Regression coverage for a real customer-reported bug (2026-09-22):
the customer Playback UI kept showing only old, stale ("Wednesday")
recordings in the browser even after every server-side check --
Playback API responses, the local recordings catalog, and fresh
Event-mode files on disk -- was independently confirmed current.

Root cause, found by curl -D against the real live route: GET /playback
carried no Cache-Control (or Expires/ETag) header at all. The customer
branch of that route bakes a fresh snapshot of the initial camera's own
recordings directly into the page's inline <script> at render time
(recordingsByCamera=...) -- correct the instant it is rendered, but
with no explicit Cache-Control, a browser (especially this PWA-shell
app's bfcache/app-resume behavior on a phone) is free to resurrect an
old rendering of the exact same page from disk cache or back/forward
cache without a genuine network round trip, permanently freezing that
customer's view at whatever data existed the first time the page was
ever rendered.

Fixed by:
  1. GET /playback setting Cache-Control: no-store, forcing every visit
     to be a real network request (see main.py's own playback() route
     comment for the full mechanism).
  2. Every fetch() call the Playback page's own JS makes for data that
     can change after initial render (clips metadata, camera events,
     available dates) passing {cache:'no-store'} explicitly, so even a
     browser that ignores the page-level header can't serve a stale
     cached response for those specific requests.

This test covers (1) directly via TestClient. (2) is a plain string
fixture in the rendered page's own JS and is checked directly on the
templates below since there is no browser in this test process to
exercise real HTTP caching.
"""
import sqlite3

import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_playback_cache_control.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        with TestClient(main.app, follow_redirects=False) as test_client:
            yield test_client


def _seed_customer_with_one_camera(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Real Customer','customer@example.test','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main Site','2026-01-01')")
    conn.execute(
        "INSERT INTO appliances(id,customer_id,site_id,cloud_id,activation_status,created_at) VALUES('appl-1','cust-1','site-1','AIC-appl-1','activated','2026-01-01')"
    )
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,status,device_key,created_at) VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','configured','urn:uuid:real-device-1','2026-01-01')"
    )
    conn.commit()
    conn.close()


def _owner_cookie(customer_id="cust-1"):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def test_customer_playback_page_sets_no_store_cache_control(http_client, db_path):
    """The exact fix: a real customer session's /playback response must
    never be cacheable, or a bfcache/app-resume restore can freeze the
    page at stale, first-rendered data forever."""
    _seed_customer_with_one_camera(db_path)

    response = http_client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"


def test_customer_playback_data_fetches_all_opt_out_of_the_http_cache(http_client, db_path):
    """Even a browser that ignores the page-level Cache-Control header
    must not be able to serve a stale cached response for any of this
    page's own data requests -- clips metadata, camera events, and
    available dates all change after initial render and must always be
    re-fetched over the network."""
    _seed_customer_with_one_camera(db_path)

    response = http_client.get("/playback", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    page = response.text

    assert "/api/customer/recordings/" in page  # sanity: this is really the data-fetching script
    for fragment in (
        "/api/customer/recordings/${encodeURIComponent(cameraId)}?${query}`,{cache:'no-store'}",
        "/api/customer/events/${encodeURIComponent(cameraId)}`,{cache:'no-store'}",
        "/api/customer/recordings/${encodeURIComponent(cameraId)}/dates`,{cache:'no-store'}",
    ):
        assert fragment in page, f"expected {fragment!r} in the rendered Playback page's own JS"


def test_unauthenticated_playback_redirect_is_unaffected(http_client, db_path):
    """The Cache-Control fix lives at the top of playback(), before the
    customer/legacy branch split -- an unauthenticated request must
    still get its normal, pre-existing redirect (to login), proving
    this fix didn't disturb that unrelated auth gate."""
    response = http_client.get("/playback")

    assert response.status_code == 303
