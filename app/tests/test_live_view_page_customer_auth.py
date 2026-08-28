"""Regression coverage for the /customer/cameras/{camera_id}/live
customer-auth routing fix: confirmed live on Samsung that clicking Live
view redirected to /login?next=... (the legacy local-emergency-recovery
login) instead of ever reaching this page.

Investigation traced this to TWO separate things, both covered here:

1. live_view_page.py's own genuinely-unauthenticated branch redirected
   to /partner-login, never /customer-login.html -- fixed by this
   commit to distinguish "no identity at all" (-> /customer-login.html,
   preserving the intended destination via ?next=) from "a recognized
   identity with the wrong role, e.g. partner/administrator" (still
   /partner-login, deliberately unchanged).

2. The actual live-observed /login?next=... redirect turned out to come
   from a DIFFERENT layer entirely -- authentication_middleware
   (main.py) falls through to that legacy redirect when
   partner_identity(request) returns None, which happens when the
   user's session was force-revoked by appliance_identity.
   sessions_to_revoke() for having no identity_grants row (a separate,
   deeper account-provisioning gap, fixed live on Samsung directly, out
   of scope for this isolated routing fix -- see the session's own
   report). This file tests live_view_page.py's OWN guard in isolation,
   using partner_portal._token() with no session_id (same pattern
   test_customer_analytics_integration.py already established), which
   bypasses the separate user_sessions revocation check entirely --
   exactly right for testing this route's routing logic on its own,
   without needing to also stand up the identity_grants system.

Real HTTP through the real app (TestClient(main.app)), a throwaway
sqlite DB via override_target() -- same style as test_customer_
analytics_integration.py, which already exercises this exact route.
"""

import pytest
from fastapi.testclient import TestClient

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
    conn.commit()


def _owner_cookie(customer_id, email):
    # session_id=None deliberately: bypasses the separate user_sessions
    # revocation lookup, matching test_customer_analytics_integration.
    # py's own established pattern for testing route-level auth logic
    # in isolation from that unrelated mechanism.
    return partner_portal._token(email, "customer_owner", None, customer_id, None)


def _admin_cookie():
    return partner_portal._token("admin@example.test", "administrator", None, None, None)


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with override_target(sqlite_path=str(db_path)):
            from partner_db import connection
            with connection() as conn:
                _seed_two_customers(conn)
        with TestClient(main.app) as test_client:
            yield test_client


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
