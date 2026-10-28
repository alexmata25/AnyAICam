"""Partner installation write authorization for Windows 1.3.4.1 MFA scope work."""
import json

import pytest

import main
from authz_helpers import cookies_for, isolated_app, make_partner_user, partner_session_cookie
from partner_db import connection


def _seed_customers():
    with connection() as db:
        for partner_id, customer_id in (("partner-a", "cust-a"), ("partner-b", "cust-b")):
            db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?, 'approved','real','2026-10-28')",
                       (partner_id, partner_id))
            db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,'active','real','2026-10-28')",
                       (customer_id, partner_id, customer_id, customer_id + "@customers.test"))


@pytest.mark.parametrize("profile", ["edge", "cloud"])
def test_technician_installation_writes_require_customer_edit_and_tenant_ownership(tmp_path, monkeypatch, profile):
    with isolated_app(tmp_path, monkeypatch, cloud=profile == "cloud") as client:
        _seed_customers()
        install_file = tmp_path / "partner_installations.json"
        monkeypatch.setattr(main, "PARTNER_INSTALLATIONS_FILE", install_file)
        email = "field-tech@partner-a.test"
        make_partner_user(email, "technician", partner_id="partner-a")
        token = partner_session_cookie(email, "technician", mfa_state=None)

        import partner_db
        original = partner_db.ROLE_PERMISSIONS["technician"]
        monkeypatch.setitem(partner_db.ROLE_PERMISSIONS, "technician", original - {"customer.edit"})
        denied = client.post("/api/partner/installations", cookies=cookies_for(token), json={"customer_id": "cust-a"})
        assert denied.status_code == 403
        assert not install_file.exists()
        monkeypatch.setitem(partner_db.ROLE_PERMISSIONS, "technician", original)

        cross_tenant = client.post("/api/partner/installations", cookies=cookies_for(token), json={"customer_id": "cust-b"})
        assert cross_tenant.status_code == 200 and cross_tenant.json()["status"] == "error"
        assert not install_file.exists()

        own = client.post("/api/partner/installations", cookies=cookies_for(token), json={"customer_id": "cust-a"})
        assert own.status_code == 200 and own.json()["status"] == "complete", own.text
        created = own.json()["installation"]
        assert created["partner_id"] == "partner-a" and created["customer_id"] == "cust-a"
        rows = json.loads(install_file.read_text(encoding="utf-8"))
        assert len(rows) == 1 and rows[0]["id"] == created["id"]

        # A mismatched legacy record cannot be edited by changing its claimed
        # partner_id while retaining another tenant's customer_id.
        rows[0].update({"id": "poisoned", "customer_id": "cust-b", "partner_id": "partner-a", "status": "not_started"})
        install_file.write_text(json.dumps(rows), encoding="utf-8")
        update = client.put("/api/partner/installations/poisoned", cookies=cookies_for(token), json={"status": "completed"})
        assert update.status_code == 200 and update.json()["status"] == "error"
        preserved = json.loads(install_file.read_text(encoding="utf-8"))
        assert next(row for row in preserved if row["id"] == "poisoned")["status"] == "not_started"
