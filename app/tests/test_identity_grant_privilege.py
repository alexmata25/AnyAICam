"""Identity-grant management requires a LIVE global administrator grant
(2026-10-02, Codex launch blocker).

The routes accepted the broad manage_settings permission, so a legacy
support_admin (or any legacy administrator without a platform grant) could
list, create and revoke identity grants -- including creating a global
administrator grant that elevates an existing portal account to
platform-wide administrator. Only has_global_administrator_grant() now
passes, re-checked live on every request.
"""
import pytest

import main
import partner_portal
from database_backend import override_target
from partner_db import connection
from test_platform_owner_rbac import _seed_operator, http_client  # noqa: F401 -- fixtures and helpers

ROUTES = (
    ("get", "/api/operations/identity-grants", None),
    ("post", "/api/operations/identity-grants", {"email": "target@anyaicam.test", "role": "administrator", "scope_type": "global"}),
    ("post", "/api/operations/identity-grants/{grant}/revoke", None),
    ("get", "/operations/identity-grants", None),
)


def _legacy(role, email):
    main.save_users([{"id": "legacy-1", "email": email, "role": role, "enabled": True, "camera_ids": []}])
    return {main.SESSION_COOKIE_NAME: main.create_session("legacy-1")}


def _portal(email, role, partner_id=None, customer_id=None):
    return {partner_portal.SESSION_COOKIE: partner_portal._token(email, role, partner_id, customer_id, None)}


def _seed(db_path):
    target = _seed_operator(db_path, email="target@anyaicam.test")
    from global_admin_helper import make_live_global_admin
    from appliance_identity import create_grant
    with override_target(sqlite_path=str(db_path)):
        make_live_global_admin("platform@anyaicam.test")
        make_live_global_admin("second-platform@anyaicam.test")
        with connection() as db:
            grant = create_grant(db, user_id=target, role="partner_owner", scope_type="partner", scope_id="partner-1", granted_by="t", now="2026-01-01")
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at,account_status) "
                       "VALUES('scoped-admin','partner-1','scoped@partner.test','S','administrator','x',1,'2026-01-01','active')")
            create_grant(db, user_id="scoped-admin", role="administrator", scope_type="partner", scope_id="partner-1", granted_by="t", now="2026-01-01")
    return grant


def _call(client, method, path, body, cookies, grant):
    path = path.replace("{grant}", grant)
    return client.get(path, cookies=cookies) if method == "get" else client.post(path, json=body or {}, cookies=cookies)


def _denied(response):
    return response.status_code in (401, 403) or (response.status_code == 200 and "text/html" in response.headers.get("content-type", "")
                                                    and "identity_grants" not in response.text and "revoke-grant" not in response.text)


def _grants(db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return [tuple(r) for r in db.execute("SELECT user_id,role,scope_type,revoked_at FROM identity_grants ORDER BY id")]


@pytest.mark.parametrize("who", ["legacy_support_admin", "legacy_admin_without_grant", "partner_scoped_administrator", "partner_owner", "viewer"])
def test_everyone_but_a_live_global_administrator_is_refused(http_client, who):
    client, db_path = http_client
    grant = _seed(db_path)
    before = _grants(db_path)
    cookies = {
        "legacy_support_admin": lambda: _legacy("support_admin", "support@anyaicam.test"),
        "legacy_admin_without_grant": lambda: _legacy("administrator", "local-admin@anyaicam.test"),
        "partner_scoped_administrator": lambda: _portal("scoped@partner.test", "administrator", partner_id="partner-1"),
        "partner_owner": lambda: _portal("owner@partner.test", "partner_owner", partner_id="partner-1"),
        "viewer": lambda: _legacy("viewer", "viewer@anyaicam.test"),
    }[who]()
    for method, path, body in ROUTES:
        response = _call(client, method, path, body, cookies, grant)
        assert _denied(response), (who, path, response.status_code)
    assert _grants(db_path) == before  # nothing created or revoked


def test_a_live_global_administrator_can_manage_grants(http_client):
    client, db_path = http_client
    grant = _seed(db_path)
    cookies = _legacy("administrator", "platform@anyaicam.test")
    assert client.get("/api/operations/identity-grants", cookies=cookies).status_code == 200
    created = client.post("/api/operations/identity-grants", cookies=cookies,
                          json={"email": "target@anyaicam.test", "role": "administrator", "scope_type": "global"})
    assert created.status_code == 200 and created.json()["status"] == "granted"
    assert client.post(f"/api/operations/identity-grants/{grant}/revoke", cookies=cookies).json()["status"] == "revoked"
    assert "Identity" in client.get("/operations/identity-grants", cookies=cookies).text


def test_losing_the_global_grant_removes_access_on_the_next_request(http_client):
    client, db_path = http_client
    _seed(db_path)
    cookies = _legacy("administrator", "platform@anyaicam.test")
    assert client.get("/api/operations/identity-grants", cookies=cookies).status_code == 200
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            db.execute("UPDATE identity_grants SET revoked_at='now' WHERE user_id='global-admin-platform' AND scope_type='global'")
    assert client.get("/api/operations/identity-grants", cookies=cookies).status_code == 403


def test_the_last_active_global_administrator_is_still_protected(http_client):
    client, db_path = http_client
    _seed(db_path)  # two global administrators
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            second = db.execute("SELECT g.id FROM identity_grants g JOIN partner_users u ON u.id=g.user_id "
                                "WHERE u.email='second-platform@anyaicam.test' AND g.scope_type='global'").fetchone()[0]
            own = db.execute("SELECT g.id FROM identity_grants g JOIN partner_users u ON u.id=g.user_id "
                             "WHERE u.email='platform@anyaicam.test' AND g.scope_type='global'").fetchone()[0]
    cookies = _legacy("administrator", "platform@anyaicam.test")
    assert client.post(f"/api/operations/identity-grants/{second}/revoke", cookies=cookies).json()["status"] == "revoked"
    refused = client.post(f"/api/operations/identity-grants/{own}/revoke", cookies=cookies)
    assert refused.status_code == 409 and "last active platform administrator" in refused.json()["detail"]
