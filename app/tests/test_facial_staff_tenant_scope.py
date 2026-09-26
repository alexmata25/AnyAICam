"""Admin portal completion pass (2026-09-26): Facial Recognition staff access
is tenant-scoped. Staff roles used to be format-checked only, so a partner's
owner/technician/salesperson could read or change ANY customer's facial
people (biometric enrollments) by supplying another partner's customer_id.
Also pins the staff customer picker's data source, /api/aac/customers."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import facial_recognition_ui
import partner_portal
from database_backend import override_target
from partner_db import connection, initialize_database

NOW = "2026-09-26T00:00:00"


@pytest.fixture()
def client(tmp_path):
    with override_target(sqlite_path=str(tmp_path / "facial_scope.db")):
        initialize_database()
        with connection() as db:
            for pid in ("p1", "p2"):
                db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (pid, pid, "approved", "real", NOW))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-a','p1','Alpha','a@example.test','active','real',?)", (NOW,))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-b','p2','Bravo','b@example.test','active','real',?)", (NOW,))
            db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,created_at) VALUES('u-admin','p1','admin@example.test','A','administrator','x',1,?)", (NOW,))
            db.execute("INSERT INTO identity_grants(id,user_id,role,scope_type,scope_id,granted_at,granted_by) VALUES('g1','u-admin','administrator','global',NULL,?,'test')", (NOW,))
        app = FastAPI()
        facial_recognition_ui.register_facial_recognition_routes(app, lambda title, active, content, scripts="": f"<html>{content}{scripts}</html>")
        with TestClient(app) as test_client:
            yield test_client


def _cookies(token):
    return {partner_portal.SESSION_COOKIE: token}


ADMIN = partner_portal._token("admin@example.test", "administrator")
P1_OWNER = partner_portal._token("owner@p1.test", "partner_owner", "p1")
P1_TECH = partner_portal._token("tech@p1.test", "technician", "p1")


def test_partner_staff_cannot_reach_another_partners_customer(client):
    for token in (P1_OWNER, P1_TECH):
        assert client.get("/api/aac/people", params={"customer_id": "cust-b"}, cookies=_cookies(token)).status_code == 404
    created = client.post("/api/aac/people", json={"customer_id": "cust-b", "display_name": "X"}, cookies=_cookies(P1_OWNER))
    assert created.status_code in (403, 404)
    with connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM facial_people WHERE customer_id='cust-b'").fetchone()["n"] == 0


def test_partner_staff_still_reach_their_own_customer(client):
    assert client.get("/api/aac/people", params={"customer_id": "cust-a"}, cookies=_cookies(P1_OWNER)).status_code == 200


def test_unknown_customer_answers_like_a_foreign_one(client):
    assert client.get("/api/aac/people", params={"customer_id": "cust-zzz"}, cookies=_cookies(P1_OWNER)).status_code == 404


def test_global_administrator_reaches_every_customer(client):
    for cid in ("cust-a", "cust-b"):
        assert client.get("/api/aac/people", params={"customer_id": cid}, cookies=_cookies(ADMIN)).status_code == 200


def test_customer_picker_lists_only_reachable_customers(client):
    names = lambda token: sorted(c["id"] for c in client.get("/api/aac/customers", cookies=_cookies(token)).json()["customers"])
    assert names(ADMIN) == ["cust-a", "cust-b"]
    assert names(P1_OWNER) == ["cust-a"]
    customer = partner_portal._token("c@a.test", "customer_owner", None, "cust-a")
    assert names(customer) == ["cust-a"]


def test_staff_pages_offer_the_picker(client):
    page = client.get("/aac/people", cookies=_cookies(ADMIN)).text
    assert 'list="aac-customer-options"' in page and "/api/aac/customers" in page
