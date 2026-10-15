"""A password change (/api/partner/activate-account) bumps the user's
authorization_version, so appliance sessions and offline appliance passwords
issued under the old password are revoked by the next signed manifest
(VMS 1.2.6 local_customer_access.py). The cloud's own session is not
compared against authorization_version and stays signed in."""
from database_backend import override_target
from partner_db import connection

from test_admin_access_lockout import ADMIN_PASSWORD, _login, _seed_admin, client, db_path  # noqa: F401 (fixtures)


def _version(db_path, user_id="u-owner"):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            return db.execute("SELECT authorization_version FROM partner_users WHERE id=?", (user_id,)).fetchone()[0]


def test_a_voluntary_password_change_bumps_the_authorization_version(client, db_path):
    _seed_admin(db_path)
    _login(client)
    before = _version(db_path)
    ok = client.post("/api/partner/activate-account",
                     json={"password": "rotated-admin-pass-77", "current_password": ADMIN_PASSWORD, "accept_terms": True})
    assert ok.status_code == 200, ok.text
    assert _version(db_path) == before + 1
    # The cloud session that made the change keeps working.
    assert client.get("/api/website/partner-session").status_code == 200


def test_a_refused_change_does_not_bump_it(client, db_path):
    _seed_admin(db_path)
    _login(client)
    before = _version(db_path)
    refused = client.post("/api/partner/activate-account",
                          json={"password": "hijacker-password-99", "current_password": "wrong", "accept_terms": True})
    assert refused.status_code == 403
    assert _version(db_path) == before
