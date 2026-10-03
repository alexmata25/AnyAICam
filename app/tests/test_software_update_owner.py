"""Settings -> System -> Software Update (2026-10-03): owner-only installation,
enforced server-side, one update at a time, state that survives refreshes,
and offline-signed releases only (software_update.py)."""
import base64
import hashlib
import json
import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException

from database_backend import override_target
from test_pricing_ff_commission import (  # noqa: F401 -- fixtures
    OWNER,
    _cookie,
    _make_global_admin,
    _seed,
    db_path,

    portal,
)

PACKAGE = b"installer-tarball-bytes"
BUILD_A = "a" * 40
BUILD_B = "b" * 40
RELEASE = {"update_id": "1.2.0-bbbbbbbbbbbb", "version": "1.2.0", "build_id": BUILD_B,
           "sha256": hashlib.sha256(PACKAGE).hexdigest(), "package_size_bytes": len(PACKAGE),
           "target": "anyaicam-appliance", "platform": "ubuntu", "architecture": "x86_64", "channel": "stable",
           "issued_at": "2026-10-03T00:00:00Z", "migration_safety": "additive"}
VIEWER = ("viewer@example.test", "customer_viewer", "cust-1")


@pytest.fixture()
def published(tmp_path, monkeypatch):
    """One release, signed OFFLINE by the test (the server only has the
    public key), published through the real updates_storage."""
    import updates_storage
    from object_storage import LocalStorage
    storage = LocalStorage(root=tmp_path / "storage")
    monkeypatch.setattr(updates_storage, "get_storage", lambda: storage)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = tmp_path / "update_public.pem"
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE", str(public))
    signature = key.sign(updates_storage.canonical_manifest_bytes(RELEASE), padding.PKCS1v15(), hashes.SHA256())
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=RELEASE, package_bytes=PACKAGE, signature=signature)
    return RELEASE


def _appliances(db_path, *, label=f"1.1.0+{BUILD_A[:12]}"):
    _seed(db_path)
    _seed(db_path, customer_id="cust-2", email="other@example.test")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,software_version,online_status,created_at) "
                 "VALUES('appl-1','cust-1','site-1','AIC-HOME',?,'online','2026-01-01')", (label,))
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Other','2026-01-01')")
    conn.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,software_version,created_at) "
                 "VALUES('appl-2','cust-2','site-2','AIC-OTHER',?,'2026-01-01')", (label,))
    conn.commit()
    conn.close()


def _install(client, cookie=None, **overrides):
    body = {"appliance_id": "appl-1", "update_id": RELEASE["update_id"], "version": "1.2.0", "confirm": True}
    body.update(overrides)
    return client.post("/api/customer/software-update/install", json=body, cookies=cookie or _cookie(*OWNER))


def _q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------------ page and route

def test_settings_system_is_the_software_update_page_not_the_generic_placeholder(portal, db_path, published):
    import main
    client, _, _ = portal
    _appliances(db_path)
    page = client.get("/settings/system", cookies=_cookie(*OWNER))
    assert page.status_code == 200
    assert "Software update" in page.text and "Read-only preview" not in page.text
    assert "may pause while the new version is prepared and started" in page.text
    import re
    dialog = page.text[page.text.index('<dialog id="su-confirm"'):page.text.index("</dialog>")]
    assert not re.search(r"\d+\s*(minute|min|second)", dialog)  # no downtime duration that was never measured
    paths = [getattr(route, "path", "") for route in main.app.routes]
    assert paths.index("/settings/system") < paths.index("/settings/{settings_slug}")
    signed_out = client.get("/settings/system", follow_redirects=False)
    assert signed_out.status_code in (302, 303) and "login" in signed_out.headers["location"].lower()


def test_a_viewer_sees_status_but_gets_no_install_controls(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    page = client.get("/settings/system", cookies=_cookie(*VIEWER))
    assert "Only the account owner can install updates." in page.text
    assert "'0'==='1'" in page.text


# ------------------------------------------------------------------ status

def test_the_owner_sees_the_installed_and_the_published_release(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    status = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()
    assert status["release"]["version"] == "1.2.0"
    [appliance] = status["appliances"]  # never another customer's appliance
    assert (appliance["id"], appliance["current_version"], appliance["update_available"]) == ("appl-1", "1.1.0", True)


def test_an_unsigned_catalog_record_is_never_offered(portal, db_path, tmp_path, monkeypatch):
    import updates_storage
    from object_storage import LocalStorage
    storage = LocalStorage(root=tmp_path / "unsigned")
    monkeypatch.setattr(updates_storage, "get_storage", lambda: storage)
    storage.put("updates", "anyaicam-appliance/stable/latest.json",
                json.dumps({"manifest": RELEASE, "package_key": "x"}).encode())
    client, _, _ = portal
    _appliances(db_path)
    status = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()
    assert status["release"] is None and status["appliances"][0]["update_available"] is False
    assert _install(client).status_code == 409


# ------------------------------------------------------------------ owner-only installation

def test_the_owner_installs_exactly_the_published_release(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    response = _install(client)
    assert response.status_code == 200, response.text
    [command] = _q(db_path, "SELECT * FROM appliance_commands WHERE command='install_update'")
    assert command["appliance_id"] == "appl-1" and command["status"] == "pending"
    assert json.loads(command["payload_json"]) == {"update_id": RELEASE["update_id"], "version": "1.2.0",
                                                    "sha256": RELEASE["sha256"], "confirmed": True,
                                                    "requested_by": "owner@example.test"}
    [ledger] = _q(db_path, "SELECT * FROM appliance_update_results")
    assert (ledger["state"], ledger["from_version"], ledger["to_version"]) == ("requested", "1.1.0", "1.2.0")


@pytest.mark.parametrize("who", ["viewer", "signed_out", "global_admin", "partner_salesperson"])
def test_nobody_but_the_owner_can_install(portal, db_path, published, who):
    client, _, _ = portal
    _appliances(db_path)
    cookie = {"viewer": _cookie(*VIEWER), "signed_out": {}, "global_admin": None,
              "partner_salesperson": _cookie("sales@example.test", "salesperson")}[who]
    if who == "global_admin":
        cookie = _make_global_admin(db_path)
    response = client.post("/api/customer/software-update/install", cookies=cookie,
                           json={"appliance_id": "appl-1", "update_id": RELEASE["update_id"], "version": "1.2.0", "confirm": True})
    assert response.status_code in (401, 403)  # signed out: the portal's auth layer answers 401 first
    assert _q(db_path, "SELECT * FROM appliance_commands") == []


def test_partners_and_admins_cannot_queue_install_update_directly(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    response = client.post("/api/partner/appliances/appl-1/commands", cookies=_make_global_admin(db_path),
                           json={"command": "install_update", "confirmed": True,
                                 "payload": {"update_id": RELEASE["update_id"], "version": "1.2.0"}})
    assert response.status_code == 403 and "account owner" in response.json()["detail"]
    assert _q(db_path, "SELECT * FROM appliance_commands") == []


def test_another_customers_appliance_is_not_found(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    assert _install(client, appliance_id="appl-2").status_code == 404


@pytest.mark.parametrize("change, status", [({"confirm": False}, 400), ({"version": "1.3.0"}, 409),
                                            ({"update_id": "1.2.0-somethingelse"}, 409)])
def test_only_a_confirmed_request_for_the_published_release_is_accepted(portal, db_path, published, change, status):
    client, _, _ = portal
    _appliances(db_path)
    assert _install(client, **change).status_code == status
    assert _q(db_path, "SELECT * FROM appliance_commands") == []


def test_an_appliance_already_on_the_release_is_not_offered_it_again(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path, label=f"1.2.0+{BUILD_B[:12]}")
    assert _install(client).status_code == 409


# ------------------------------------------------------------------ one at a time

def test_a_second_request_gets_update_already_in_progress(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    assert _install(client).status_code == 200
    again = _install(client)
    assert again.status_code == 409 and "already in progress" in again.json()["detail"]
    assert len(_q(db_path, "SELECT * FROM appliance_commands")) == 1


def test_simultaneous_requests_produce_exactly_one_install(portal, db_path, published):
    import software_update
    from partner_db import connection
    _appliances(db_path)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt():
        with override_target(sqlite_path=str(db_path)):
            barrier.wait()
            try:
                with connection() as db:
                    software_update.request_install(db, identity={"customer_id": "cust-1", "email": "owner@example.test"},
                                                    appliance_id="appl-1", update_id=RELEASE["update_id"], version="1.2.0",
                                                    confirmed=True)
                outcomes.append("installed")
            except HTTPException as error:
                outcomes.append(error.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes, key=str) == sorted(["installed", 409], key=str)
    assert len(_q(db_path, "SELECT * FROM appliance_commands")) == 1


# ------------------------------------------------------------------ state that survives

def test_progress_moves_forward_and_a_final_outcome_is_never_overwritten(portal, db_path, published):
    import software_update
    from partner_db import connection
    client, _, _ = portal
    _appliances(db_path)
    assert _install(client).status_code == 200
    with override_target(sqlite_path=str(db_path)):
        for state in ("downloading", "installing", "health_checking", "rolled_back"):
            with connection() as db:
                assert software_update.record_update_progress(db, update_id=RELEASE["update_id"], appliance_id="appl-1",
                                                              payload={"error": "health_check_failed: /health answered 500"},
                                                              state=state, now="2026-10-03T01:00:00")
        with connection() as db:
            assert not software_update.record_update_progress(db, update_id=RELEASE["update_id"], appliance_id="appl-1",
                                                              payload={}, state="healthy", now="2026-10-03T02:00:00")
    # A fresh request (a browser refresh, or after the VMS restarted) reads it back.
    status = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()
    [appliance] = status["appliances"]
    assert appliance["in_progress"] is None
    assert appliance["history"][0]["state"] == "rolled_back"
    assert appliance["history"][0]["reason"] == "health_check_failed"
    assert "previous version restored" in appliance["history"][0]["label"]


# ------------------------------------------------------------------ appliances without a valid installed release

def _check_in(db_path, appliance_id="appl-1", when="2026-10-03T12:00:00"):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE appliances SET last_check_in=? WHERE id=?", (when, appliance_id))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("label", ["0.1.0", "Not installed", "Unknown", "1.1.0", "1.2.0+not-a-build"])
def test_a_checked_in_appliance_without_a_valid_release_needs_a_one_time_update(portal, db_path, published, label):
    """Regression (2026-10-03): an agent from before 1.2.0 reports its own
    package version ("0.1.0"). The page offered "Install" (or "Up to date"),
    and an install queued a command that appliance cannot run -- it sat at
    "Waiting for appliance" forever."""
    client, _, _ = portal
    _appliances(db_path, label=label)
    _check_in(db_path)
    [appliance] = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()["appliances"]
    assert appliance["current_version"] == ""
    assert appliance["update_available"] is False and appliance["one_time_update_required"] is True
    refused = _install(client)
    assert refused.status_code == 409 and "one-time update" in refused.json()["detail"]
    assert _q(db_path, "SELECT * FROM appliance_commands") == []
    assert _q(db_path, "SELECT * FROM appliance_update_results") == []


def test_an_appliance_that_never_checked_in_is_not_offered_an_update(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path, label="")
    [appliance] = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()["appliances"]
    assert appliance["update_available"] is False and appliance["one_time_update_required"] is False
    assert _install(client).status_code == 409
    assert _q(db_path, "SELECT * FROM appliance_commands") == []


def test_a_valid_release_label_is_still_offered_the_update(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    _check_in(db_path)
    [appliance] = client.get("/api/customer/software-update", cookies=_cookie(*OWNER)).json()["appliances"]
    assert (appliance["update_available"], appliance["one_time_update_required"]) == (True, False)
    assert _install(client).status_code == 200


def test_the_page_explains_the_one_time_update_and_never_says_up_to_date_without_a_version(portal, db_path, published):
    client, _, _ = portal
    _appliances(db_path)
    page = client.get("/settings/system", cookies=_cookie(*OWNER)).text
    import software_update
    assert "One-time update required" in page
    assert json.dumps(software_update.ONE_TIME_UPDATE_MESSAGE) in page and "__ONE_TIME_MESSAGE__" not in page
    script = page[page.index("function render()"):page.index("async function load()")]
    # The explanation and the "no version" branch come before Install and "Up to date".
    assert script.index("a.one_time_update_required") < script.index("a.update_available&&OWNER")
    assert script.index("!a.current_version") < script.index("Up to date")


def test_is_newer_never_treats_an_unknown_version_as_installable():
    import software_update
    assert software_update.is_newer("1.2.1", "1.2.0") is True
    assert software_update.is_newer("1.2.1", "") is False
    assert software_update.is_newer("1.2.1", "0.1.0+x") is False
