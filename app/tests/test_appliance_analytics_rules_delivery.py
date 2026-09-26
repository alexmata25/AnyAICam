"""Cloud -> appliance rule delivery (2026-09-21): GET /api/appliance/
configuration now also exposes THIS appliance's own enabled
customer_analytics_rules rows -- reusing the existing polling route
(appliance_configuration()) rather than inventing a new, separate
channel, per explicit instruction. See customer_analytics_rule_worker.py
and edge_camera_sync.py's own docstrings for the rest of the delivery
path this feeds.

Mirrors test_camera_people_counting_entitlement.py's own established
harness exactly (same seed helper shape, same appliance-auth-header
helper, same override_target()-before-import constraint).
"""

import json
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_appliance_analytics_rules_delivery.db"):
    import appliance_cloud
    from partner_db import connection, password_hash


def _seed_appliance(db, appliance_id, cloud_id, credential, camera_id, *, customer_id="cust-1", site_id="site-1", camera_number=1):
    now = "2026-09-21T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", (f"partner-{customer_id}", "Test Partner", "approved", "real", now))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (customer_id, f"partner-{customer_id}", "Test Customer", f"{customer_id}@example.test", "active", "real", now))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (site_id, customer_id, "Test Site", now))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (appliance_id, customer_id, site_id, cloud_id, now))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"cred-{appliance_id}", appliance_id, password_hash(credential), now))
    db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (camera_id, customer_id, site_id, appliance_id, "Camera", camera_number, "configured", now))


def _seed_rule(db, rule_id, *, customer_id, site_id, appliance_id, camera_id, rule_type="line_crossing", direction="both", enabled=1, geometry=None, name="Test rule"):
    now = "2026-09-21T00:00:00"
    geometry = geometry if geometry is not None else [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}]
    db.execute(
        "INSERT INTO customer_analytics_rules(id,customer_id,site_id,appliance_id,camera_id,rule_type,name,direction,geometry_json,enabled,created_at,updated_at,created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rule_id, customer_id, site_id, appliance_id, camera_id, rule_type, name, direction, json.dumps(geometry), enabled, now, now, None),
    )


def _appliance_auth_headers(appliance_id: str, credential: str) -> dict:
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_appliance_analytics_rules_delivery.db"


@pytest.fixture()
def client(db_path):
    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def test_enabled_rule_for_this_appliances_own_camera_is_included(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1")
            _seed_rule(db, "rule-1", customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-1")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    rules = response.json()["analytics_rules"]
    assert len(rules) == 1
    assert rules[0]["id"] == "rule-1"
    assert rules[0]["camera_id"] == "cam-1"
    assert rules[0]["rule_type"] == "line_crossing"
    assert rules[0]["direction"] == "both"
    assert rules[0]["geometry"] == [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.9}]


def test_disabled_rule_is_never_delivered(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1")
            _seed_rule(db, "rule-1", customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-1", enabled=0)

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    assert response.json()["analytics_rules"] == []


def test_a_rule_belonging_to_a_different_appliance_and_customer_is_never_delivered_here(client, db_path):
    """The core tenant/ownership boundary: two appliances, two
    customers, one shared cloud database -- appliance-1's poll must
    never see appliance-2's rule."""
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1", customer_id="cust-1", site_id="site-1")
            _seed_appliance(db, "appl-2", "AIC-2", "cred-2", "cam-2", customer_id="cust-2", site_id="site-2")
            _seed_rule(db, "rule-2", customer_id="cust-2", site_id="site-2", appliance_id="appl-2", camera_id="cam-2")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    assert response.json()["analytics_rules"] == []

    # And the reverse direction: appliance-2 sees only its own rule.
    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-2", "cred-2"))
    rules = response.json()["analytics_rules"]
    assert len(rules) == 1
    assert rules[0]["id"] == "rule-2"


def test_rules_for_two_cameras_on_the_same_appliance_are_both_scoped_correctly(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1", camera_number=1)
            db.execute(
                "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,camera_number,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("cam-2", "cust-1", "site-1", "appl-1", "Camera 2", 2, "configured", "2026-09-21T00:00:00"),
            )
            _seed_rule(db, "rule-1", customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-1")
            _seed_rule(
                db, "rule-2", customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-2",
                rule_type="intrusion", direction=None,
                geometry=[{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.9, "y": 0.9}],
            )

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1"))
    rules = {rule["id"]: rule for rule in response.json()["analytics_rules"]}
    assert set(rules) == {"rule-1", "rule-2"}
    assert rules["rule-2"]["rule_type"] == "intrusion"
    assert rules["rule-2"]["camera_id"] == "cam-2"
    assert rules["rule-2"]["direction"] is None
    assert len(rules["rule-2"]["geometry"]) == 3


def test_wrong_appliance_credential_is_rejected_and_reveals_no_rule_data(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1")
            _seed_rule(db, "rule-1", customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-1")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "wrong-credential"))
    assert response.status_code == 403
    assert "analytics_rules" not in response.text


def test_no_rules_at_all_returns_an_empty_list_not_an_error(client, db_path):
    with override_target(sqlite_path=str(db_path)):
        with connection() as db:
            _seed_appliance(db, "appl-1", "AIC-1", "cred-1", "cam-1")

    response = client.get("/api/appliance/configuration", headers=_appliance_auth_headers("appl-1", "cred-1"))
    assert response.status_code == 200
    assert response.json()["analytics_rules"] == []
