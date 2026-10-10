"""HTTP regression coverage for live-view customer authentication.

The real application middleware and route are exercised with signed,
revocable partner sessions and a throwaway database. TestClient does not
enter the appliance lifespan, keeping unrelated edge workers out of this
focused HTTP test without skipping authentication or authorization.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_RUNTIME=Path(__file__).parent/'.live-view-test-runtime'
os.environ.setdefault('ANYAICAM_PARTNER_DB',str(_TEST_RUNTIME/'partner.db'))
os.environ.setdefault('ANYAICAM_RECORDINGS_DIR',str(_TEST_RUNTIME/'recordings'))

import main
import partner_portal
from database_backend import override_target


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_view_customer_auth.db"


def _seed_two_customers(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-a','partner-1','Customer A','a@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-b','partner-1','Customer B','b@example.test','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-a','cust-a','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-b','cust-b','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-a','cust-a','site-a','AIC-A','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-b','cust-b','site-b','AIC-B','2026-01-01')")
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-a','cust-a','site-a','appl-a',1,'Customer A Camera','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) "
        "VALUES('cam-b','cust-b','site-b','appl-b',1,'Customer B Camera','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-a','owner-a@example.test','customer_owner','cust-a','x','all','2026-01-01')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-b','owner-b@example.test','customer_owner','cust-b','x','all','2026-01-01')"
    )
    conn.execute("INSERT OR IGNORE INTO partner_users(id,email,role,password_hash,created_at) VALUES('user-admin','admin@example.test','administrator','x','2026-01-01')")
    for session_id,user_id,email,role in (
        ('session-a','user-a','owner-a@example.test','customer_owner'),
        ('session-b','user-b','owner-b@example.test','customer_owner'),
        ('session-admin','user-admin','admin@example.test','administrator'),
    ):
        conn.execute("INSERT OR IGNORE INTO user_sessions(id,user_id,email,role,session_type,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",(session_id,user_id,email,role,'cookie','2026-10-10T00:00:00','2026-10-10T00:00:00','2099-01-01T00:00:00'))
    conn.commit()


def _owner_cookie(customer_id, email):
    session_id='session-a' if customer_id=='cust-a' else 'session-b'
    return partner_portal._token(email, "customer_owner", None, customer_id, session_id)


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator", None, None, 'session-admin')


@pytest.fixture()
def client(db_path):
    if os.name == "nt":
        pytest.skip("Starlette TestClient needs a local asyncio socketpair; this restricted Windows runner blocks it. Run this real-auth HTTP test on Linux.")
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed_two_customers(conn)
        # Exercise the real app's middleware, registered route, signed,
        # revocable partner sessions, and per-camera authorization. The
        # TestClient is intentionally not entered as a context manager so
        # appliance lifespan workers do not start in this HTTP-only test.
        test_client=TestClient(main.app)
        try:
            yield test_client
        finally:
            test_client.close()


# --------------------------------------------------- 1. authenticated customer_owner, own camera


def test_authenticated_customer_owner_can_open_their_own_camera_live_page(client):
    response = client.get(
        "/customer/cameras/cam-a/live",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Customer A Camera" in response.text
    assert "live-view-video" in response.text


# --------------------------------------------------- 2. genuinely unauthenticated


def test_unauthenticated_visitor_is_sent_to_customer_login_not_legacy_login(client):
    response = client.get("/customer/cameras/cam-a/live", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/customer-login.html")
    assert "/login" not in location.split("?")[0]  # never the legacy local-recovery login
    assert "next=" in location
    assert "next=/customer/cameras/cam-a/live" in location  # intended destination preserved


# --------------------------------------------------- 3. cross-customer isolation


def test_customer_a_cannot_open_customer_bs_camera_by_changing_the_url(client):
    response = client.get(
        "/customer/cameras/cam-b/live",  # B's camera
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a", "owner-a@example.test")},  # A's session
        follow_redirects=False,
    )
    # Authenticated, just not authorized for someone else's camera_id --
    # a 404 (never found via the customer_id-scoped lookup), not a login
    # redirect and never a 200 with someone else's camera data.
    assert response.status_code == 404
    assert "Customer B Camera" not in response.text


def test_customer_b_cannot_open_customer_as_camera_either_direction_confirmed(client):
    response = client.get(
        "/customer/cameras/cam-a/live",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-b", "owner-b@example.test")},
        follow_redirects=False,
    )
    assert response.status_code == 404


# --------------------------------------------------- 4. partner/admin/local-session behavior unchanged


def test_administrator_portal_session_still_bounces_to_partner_login_unchanged(client):
    """A recognized identity with the wrong role for this customer-only
    route keeps its exact prior behavior -- unchanged by this fix."""
    response = client.get(
        "/customer/cameras/cam-a/live",
        cookies={partner_portal.SESSION_COOKIE: _admin_cookie()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/partner-login"


def test_customer_viewer_role_is_still_accepted_same_as_before(client, db_path):
    """Confirms the fix didn't narrow the accepted-roles set -- customer_
    viewer must still reach this page (per-camera can_live permission is
    a separate, later check inside _authorized_camera(), not this guard)."""
    with override_target(sqlite_path=str(db_path)):
        from partner_db import connection
        with connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
                "VALUES('user-a-viewer','viewer-a@example.test','customer_viewer','cust-a','x','selected','2026-01-01')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO customer_camera_permissions(user_id,camera_id,can_live) VALUES('user-a-viewer','cam-a',1)"
            )
            conn.commit()

    response = client.get(
        "/customer/cameras/cam-a/live",
        cookies={partner_portal.SESSION_COOKIE: partner_portal._token("viewer-a@example.test", "customer_viewer", None, "cust-a", None)},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Customer A Camera" in response.text
