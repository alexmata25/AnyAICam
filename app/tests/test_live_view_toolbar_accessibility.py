"""Regression coverage for the single-camera Live view toolbar (2026-09-23):

1. Every icon-only camera-tool button/link (Mute, Snapshot, Download,
   Share, Playback, Analytics, Bookmark, Stop, Retry) must carry an
   explicit aria-label, not rely on `title` alone. Each of these
   elements' visible content is just an emoji/symbol character, and per
   the standard accessible-name computation algorithm, an element's own
   visible text content wins over its `title` attribute -- so a screen
   reader announced only the raw symbol (e.g. "left pointing envelope")
   for every one of these controls until this fix, even though a
   `title` tooltip was already present. talk-mic and unlock-door already
   did this correctly; this brings the rest of the row up to the same
   standard.

2. The Download button's click handler must not route through
   comingSoon(), which always appends "is ready for a future update."
   to whatever label it's given -- for Download the label is itself a
   full sentence ("Download applies to recorded clips in Playback"),
   and running it through comingSoon() produced a broken run-on: "...in
   Playback is ready for a future update." This isn't an unbuilt
   feature notice (unlike Share/Bookmark, which correctly still use
   comingSoon()) -- it's a redirect to where download already works
   today, so it now calls showToast() directly with a clean, complete
   sentence.

Same fixture idiom as test_live_view_page_face_access_ui.py: real HTTP
through the real app (TestClient(main.app)), a throwaway sqlite DB via
override_target().

Camera-count-agnostic coverage: parametrized across camera_id/
camera_number pairs outside the 1-5 Ryzen validation-pilot range, per
this project's standing architecture rule that nothing here may be
keyed to a specific camera count.
"""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


CAMERA_CASES = [("cam-tools-a", 9), ("cam-tools-b", 40)]

TOOLBAR_LABELS = {
    "live-view-mute": "Mute",
    "live-view-snapshot": "Snapshot",
    "live-view-fullscreen": "Fullscreen",
    "live-view-analytics": "Analytics",
    "live-view-stop": "Stop",
    "live-view-retry": "Retry",
}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_toolbar_accessibility.db"


def _seed(conn, camera_id, camera_number):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) "
        "VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (camera_id, "cust-a", "site-a", "appl-a", camera_number, "Test Camera", "2026-01-01"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-owner','owner-a@example.test','customer_owner','cust-a','x','all','2026-01-01')"
    )
    conn.commit()


def _owner_cookie():
    return partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-a", None)


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                for camera_id, camera_number in CAMERA_CASES:
                    _seed(conn, camera_id, camera_number)
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_every_toolbar_button_has_an_explicit_aria_label(client, camera_id, camera_number):
    response = client.get(f"/customer/cameras/{camera_id}/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    for element_id, label in TOOLBAR_LABELS.items():
        assert f'id="{element_id}" title="{label}" aria-label="{label}"' in response.text, (
            f'{element_id} is missing aria-label="{label}"'
        )


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_playback_toolbar_link_has_an_explicit_aria_label(client, camera_id, camera_number):
    response = client.get(f"/customer/cameras/{camera_id}/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    # Opens this camera's own recordings (2026-09-25), not bare /playback.
    assert f'<a class="camera-tool" href="/playback?camera={camera_id}" title="Playback" aria-label="Playback">' in response.text


@pytest.mark.parametrize("camera_id,camera_number", CAMERA_CASES)
def test_only_working_controls_are_shown(client, camera_id, camera_number):
    """2026-09-25 standing rule: a button either works or does not
    appear. Download (a toast pointing at Playback) and Share/Bookmark
    ("coming soon") were placeholders on a live stream and are removed;
    Fullscreen -- already supported by double-click -- gets a real button."""
    response = client.get(f"/customer/cameras/{camera_id}/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()})
    assert response.status_code == 200
    html = response.text
    for gone in ('id="live-view-download"', 'id="live-view-share"', 'id="live-view-bookmark"',
                 "comingSoon('Share')", "comingSoon('Bookmark')", "Download applies to recorded clips"):
        assert gone not in html, gone
    assert "fullscreenButton.addEventListener('click'" in html
    assert "cameraView.requestFullscreen().catch" in html and "video.webkitEnterFullscreen()" in html
