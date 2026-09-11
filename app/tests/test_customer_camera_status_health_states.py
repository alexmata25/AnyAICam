"""Regression coverage for GET /api/customer/cameras/{camera_id}/status's
online_status handling.

Confirmed live on staging: an appliance at 91.9% disk usage (health_state()
in appliance_protocol.py sets online_status='degraded' for exactly this
kind of warning -- low_disk/high_cpu -- on an appliance that is still
heartbeating and authenticated) kept successfully relaying live video for
all 5 cameras through the multi-camera grid page (/customer-live, which
has no such pre-check at all) the whole time. The dedicated single-camera
page (/customer/cameras/{camera_id}/live) called this exact endpoint
first and treated 'degraded' identically to a genuinely unreachable
appliance, refusing to ever start a session -- the actual bug this fix
closes. 'degraded' must return its own distinct state so the client
still proceeds to start live view (with a surfaced health warning);
every other non-'online' value (offline, revoked, NULL/never-checked-in,
or any future/unrecognized value) must still fail closed as
'appliance_offline' -- this is a fail-closed allowlist of exactly two
known 'still reachable' states, not a broadened fail-open check.

Same TestClient(main.app) + throwaway-sqlite style as
test_live_view_page_customer_auth.py, reusing its exact seed/fixture
pattern. TestClient is created and used INSIDE the same override_target
context the seed data was written under -- creating it after that
context exits means every request queries whatever database the process
otherwise defaults to, not the seeded one (a real mistake made and caught
while writing this file the first time).
"""

import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from cloud_config import settings
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_camera_status_health_states.db"


def _seed(conn, online_status):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,online_status,created_at) "
        "VALUES('appl-a','cust-a','site-a','AIC-A',?,'2026-01-01')",
        (online_status,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'Customer A Camera','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all','2026-01-01')"
    )
    conn.commit()


def _owner_cookie():
    return partner_portal._token("owner-a@example.test", "customer_owner", None, "cust-a", None)


def _status_response(db_path, online_status):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed(conn, online_status)
        # base_url matches whatever this process's own ANYAICAM_TRUSTED_HOSTS
        # allows -- TestClient's default Host header ("testserver") is
        # rejected by TrustedHostMiddleware on any deployment (like
        # staging) that has narrowed trusted_hosts down from the wide-open
        # default, a detail the original test-suite authors in a clean/
        # untouched-default CI environment never needed to account for.
        trusted = settings.effective_trusted_hosts or []
        allowed_host = "testserver" if ("*" in trusted or "testserver" in trusted or not trusted) else trusted[0]
        with TestClient(main.app, base_url=f"http://{allowed_host}") as client:
            client.cookies.set("anyaicam_partner_session", _owner_cookie())
            return client.get("/api/customer/cameras/cam-a/status")


# --------------------------------------------------- online = allowed, no warning


def test_online_appliance_reports_unknown_state_no_warning(db_path):
    response = _status_response(db_path, "online")
    assert response.status_code == 200, response.text
    assert response.json() == {"state": "unknown"}


# --------------------------------------------------- degraded = allowed, with warning


def test_degraded_appliance_reports_degraded_not_offline(db_path):
    response = _status_response(db_path, "degraded")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "degraded"
    assert "message" in body and body["message"]


# --------------------------------------------------- genuinely offline/unreachable = blocked


@pytest.mark.parametrize("online_status", ["offline", "revoked", None])
def test_offline_or_revoked_or_never_checked_in_appliance_blocks_live_view(db_path, online_status):
    response = _status_response(db_path, online_status)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "appliance_offline"
    assert "message" in body and body["message"]


def test_unrecognized_future_status_value_fails_closed_as_offline(db_path):
    # A status value this code has never seen before (a future addition,
    # or corrupted data) must never be silently treated as "reachable" --
    # only 'online' and 'degraded' are explicitly allowlisted.
    response = _status_response(db_path, "some-new-status-nobody-has-seen-yet")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "appliance_offline"
