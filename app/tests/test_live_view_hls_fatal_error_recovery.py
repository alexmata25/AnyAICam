"""Regression coverage for the HLS.js fatal-error auto-recovery fix
(2026-09-13): confirmed live -- a brief, already-self-healing relay/
upload gap for Camera 1 (the same transient-blip category documented
elsewhere in this project) surfaced to the browser as an hls.js fatal
error, and the ONLY thing the old error handler on both the grid
(/customer-live) and single-camera (/customer/cameras/{id}/live) pages
did was `setStatus('Reconnecting…')` -- a label change with zero actual
recovery action, so the tile stayed dead forever even once the
underlying stream had fully recovered server-side.

Both pages render their player script server-side as a plain string
(no headless browser/JS engine exists in this test suite), so these
tests -- like this project's own established precedent in
test_talk_down_foundation.py's test_pointer_lifecycle_all_wired_to_
stop_on_both_pages -- assert on the exact emitted JS source rather than
executing it. That is deliberate: the thing under regression is that
the CORRECT recovery calls are present, wired to the right error types,
bounded, and reachable from both surfaces -- not the visual result of
running them, which no tool in this suite can observe.

Uses main.app via TestClient (matches test_live_view_page_customer_
auth.py's own established pattern) so the real page_shell() -- not a
no-op stub -- actually renders the <script> content these tests read.
"""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_hls_fatal_error_recovery.db"


def _seed(conn):
    now = "2026-09-13T00:00:00"
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


# ------------------------------------------------------- the old cosmetic-only handler is gone


def test_grid_no_longer_has_the_cosmetic_only_fatal_handler(client):
    html = _grid_html(client)
    assert "if(data.fatal)setStatus(id,'Reconnecting…')" not in html


def test_single_camera_no_longer_has_the_cosmetic_only_fatal_handler(client):
    html = _single_html(client)
    assert "if(data.fatal)setStatus('Reconnecting…')" not in html


# ------------------------------------------------------- real recovery calls wired to the right error types


def test_grid_network_error_calls_start_load(client):
    html = _grid_html(client)
    assert "handleFatalError" in html
    assert "Hls.ErrorTypes.NETWORK_ERROR" in html
    assert "tile.hls.startLoad()" in html


def test_grid_media_error_calls_recover_media_error(client):
    html = _grid_html(client)
    assert "Hls.ErrorTypes.MEDIA_ERROR" in html
    assert "tile.hls.recoverMediaError()" in html


def test_single_camera_network_error_calls_start_load(client):
    html = _single_html(client)
    assert "handleFatalError" in html
    assert "Hls.ErrorTypes.NETWORK_ERROR" in html
    assert "hls.startLoad()" in html


def test_single_camera_media_error_calls_recover_media_error(client):
    html = _single_html(client)
    assert "Hls.ErrorTypes.MEDIA_ERROR" in html
    assert "hls.recoverMediaError()" in html


# ------------------------------------------------------- other/unrecoverable fatal errors: teardown + reattach


def test_grid_falls_back_to_destroy_and_reattach_via_the_existing_poll_flow(client):
    html = _grid_html(client)
    # The fallback path must destroy the failed instance and hand off to
    # the SAME bounded playlist-poll-then-attach flow a fresh page load
    # already uses -- never a bespoke second retry mechanism, and never
    # a raw startSession() call, since the still-valid live_view_session
    # must not be restarted just because the browser-side player broke.
    assert "destroyHls(id)" in html
    assert "pollPlaylist(id,Date.now()+pollTimeoutMs)" in html


def test_single_camera_falls_back_to_destroy_and_reattach_via_the_existing_poll_flow(client):
    html = _single_html(client)
    assert "destroyHls()" in html
    assert "pollPlaylist(Date.now()+pollTimeoutMs)" in html


# ------------------------------------------------------- bounded retries, not a runaway loop


def test_grid_bounds_inplace_recovery_attempts(client):
    html = _grid_html(client)
    assert "MAX_INPLACE_RECOVERY_ATTEMPTS" in html
    assert "recoveryAttempts" in html


def test_single_camera_bounds_inplace_recovery_attempts(client):
    html = _single_html(client)
    assert "MAX_INPLACE_RECOVERY_ATTEMPTS" in html
    assert "recoveryAttempts" in html


def test_grid_resets_recovery_counter_on_successful_reattach(client):
    """A stream that keeps working must not carry forward a stale
    attempt count from a completely unrelated, long-past error."""
    html = _grid_html(client)
    assert "tile.recoveryAttempts=0" in html


def test_single_camera_resets_recovery_counter_on_successful_reattach(client):
    html = _single_html(client)
    assert "recoveryAttempts=0" in html


# ------------------------------------------------------- no duplicate HLS instances


def test_grid_attach_player_destroys_any_existing_instance_first(client):
    """attachPlayer() must never run two Hls() instances against the
    same tile at once -- destroyHls(id) must appear before the `new
    Hls()` call inside attachPlayer's own body."""
    html = _grid_html(client)
    attach_start = html.index("function attachPlayer(id,playlistUrl)")
    new_hls_index = html.index("tile.hls=new Hls()", attach_start)
    destroy_index = html.index("destroyHls(id)", attach_start)
    assert destroy_index < new_hls_index


def test_single_camera_attach_player_destroys_any_existing_instance_first(client):
    html = _single_html(client)
    attach_start = html.index("function attachPlayer()")
    new_hls_index = html.index("hls=new Hls()", attach_start)
    destroy_index = html.index("destroyHls()", attach_start)
    assert destroy_index < new_hls_index


def test_single_camera_stop_button_uses_the_shared_destroy_helper(client):
    """Regression for a latent bug this fix also closes in passing: the
    Stop button used to call hls.destroy() directly without ever
    nulling the module-level `hls` reference, leaving a stale reference
    to an already-destroyed instance."""
    html = _single_html(client)
    assert "stopButton.addEventListener('click',()=>{stopPolling();destroyHls();stopSession(false)})" in html
    assert "if(hls)hls.destroy()" not in html


# ------------------------------------------------------- per-camera isolation on the grid


def test_grid_fatal_error_handling_is_scoped_per_tile(client):
    """One camera's fatal-error recovery cycle must never touch another
    tile's own hls instance/recovery state -- everything routes through
    tiles[id], never a bare module-level `hls`/`recoveryAttempts`."""
    html = _grid_html(client)
    handler_start = html.index("function handleFatalError(id,data)")
    handler_end = html.index("function attachPlayer(id,playlistUrl)", handler_start)
    handler_body = html[handler_start:handler_end]
    assert "tiles[id]" in handler_body
    assert "tile.recoveryAttempts" in handler_body
    assert "tile.hls" in handler_body
