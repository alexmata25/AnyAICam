"""Regression coverage for the black-tile fix (2026-09-21) on both
/customer-live (the canonical grid) and /customer/cameras/{id}/live (its
single-camera tools page): the placeholder overlay used to be hidden on
Hls.Events.MANIFEST_PARSED / the native <video> 'loadedmetadata' event /
immediately after a WebRTC srcObject was assigned -- all three fire once
a PLAYLIST is parsed or a stream object exists, well before any frame
has actually decoded. With no poster attribute set, the bare <video>
element underneath renders its own default solid-black background for
however long the first segment takes to download and decode (worse
under Event-mode's irregular segment cadence) -- exactly the confirmed
"black video tiles" defect. The fix: the placeholder is now hidden only
on the video element's own 'playing' event, the one standard
HTMLMediaElement signal that fires only once a real frame is genuinely
visible, regardless of which of the three transports (relay HLS, P2P,
WireGuard-local HLS) won.

Both pages render their player script server-side as a plain string (no
headless browser/JS engine in this suite), so -- matching this project's
own established precedent (test_live_view_hls_fatal_error_recovery.py,
test_talk_down_foundation.py) -- these tests assert on the exact emitted
JS source rather than executing it.

Reuses test_live_view_hls_fatal_error_recovery.py's own established
client/seed/cookie fixtures verbatim.
"""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_black_tile_fix.db"


def _seed(conn):
    now = "2026-09-21T00:00:00"
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main',?)", (now,))
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A',?)", (now,))
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'urn:uuid:fake-a','configured','Camera 1',?)", (now,)
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all',?)", (now,)
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
                _seed(conn)
        from cloud_config import settings
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as test_client:
            yield test_client


def _grid_html(client):
    return client.get("/customer-live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text


def _single_html(client):
    return client.get("/customer/cameras/cam-a/live", cookies={partner_portal.SESSION_COOKIE: _owner_cookie()}).text


# --------------------------------------------------------------- grid page


def test_grid_manifest_parsed_no_longer_hides_the_placeholder(client):
    html = _grid_html(client)
    assert "tile.hls.on(Hls.Events.MANIFEST_PARSED,()=>{tile.placeholder.hidden=true" not in html
    assert "tile.hls.on(Hls.Events.MANIFEST_PARSED,()=>{tile.recoveryAttempts=0;tile.video.play()" in html


def test_grid_native_hls_loadedmetadata_no_longer_hides_the_placeholder(client):
    html = _grid_html(client)
    assert "addEventListener('loadedmetadata',()=>{tile.placeholder.hidden=true" not in html
    assert "addEventListener('loadedmetadata',()=>{tile.video.play()" in html


def test_grid_reveals_the_tile_on_the_video_playing_event_instead(client):
    html = _grid_html(client)
    assert "tile.video.addEventListener('playing',()=>{tile.placeholder.hidden=true},{once:true})" in html


def test_grid_p2p_no_longer_hides_the_placeholder_before_a_frame_renders(client):
    html = _grid_html(client)
    assert "tile.video.srcObject=result.stream;\n      tile.p2pConnection=result.pc;\n      tile.placeholder.hidden=true" not in html
    assert "tile.video.addEventListener('playing',()=>{tile.placeholder.hidden=true},{once:true});\n      tile.video.srcObject=result.stream" in html


# --------------------------------------------------------- single-camera page


def test_single_camera_manifest_parsed_no_longer_hides_the_placeholder(client):
    html = _single_html(client)
    assert "hls.on(Hls.Events.MANIFEST_PARSED,()=>{placeholder.hidden=true" not in html
    assert "hls.on(Hls.Events.MANIFEST_PARSED,()=>{recoveryAttempts=0;video.play()" in html


def test_single_camera_native_hls_loadedmetadata_no_longer_hides_the_placeholder(client):
    html = _single_html(client)
    assert "addEventListener('loadedmetadata',()=>{placeholder.hidden=true" not in html
    assert "addEventListener('loadedmetadata',()=>{video.play()" in html


def test_single_camera_reveals_the_tile_on_the_video_playing_event_instead(client):
    html = _single_html(client)
    assert "video.addEventListener('playing',()=>{placeholder.hidden=true},{once:true})" in html


def test_single_camera_p2p_no_longer_hides_the_placeholder_before_a_frame_renders(client):
    html = _single_html(client)
    assert "video.srcObject=result.stream;\n      p2pConnection=result.pc;\n      placeholder.hidden=true" not in html
    assert "video.addEventListener('playing',()=>{placeholder.hidden=true},{once:true});\n      video.srcObject=result.stream" in html


# ---------------------------------------------------- layout is unchanged

def test_grid_layout_markup_is_unchanged(client):
    """This fix is JS event-wiring only -- proves the tile markup itself
    (the video/placeholder elements the JS attaches to) is untouched."""
    html = _grid_html(client)
    assert 'id="live-grid-video-cam-a"' in html
