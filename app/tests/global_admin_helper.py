"""Test helper (2026-10-02): identity-grant management requires a LIVE global
administrator grant (main._is_live_global_administrator). Tests that act as
"the Admin Portal administrator" make that account a real platform
administrator here -- a partner_users row plus an active global grant --
the same state platform_owner.provision_platform_owner() creates."""
from partner_db import connection


def make_live_global_admin(email: str, *, user_id: str | None = None) -> str:
    from appliance_identity import create_grant
    now = "2026-01-01T00:00:00"
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('anyaicam-primary','AnyAiCam','approved','real',?)", (now,))
        existing = db.execute("SELECT id FROM partner_users WHERE lower(email)=lower(?)", (email,)).fetchone()
        uid = existing["id"] if existing else (user_id or "global-admin-" + email.split("@")[0])
        if not existing:
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at,account_status) "
                       "VALUES(?,?,?,?,?,?,1,?,'active')", (uid, "anyaicam-primary", email, "Platform admin", "administrator", "x", now))
        live = db.execute("SELECT 1 FROM identity_grants WHERE user_id=? AND role='administrator' AND scope_type='global' AND revoked_at IS NULL",
                          (uid,)).fetchone()
        if not live:
            create_grant(db, user_id=uid, role="administrator", scope_type="global", scope_id=None, granted_by="test-bootstrap", now=now)
    return uid
