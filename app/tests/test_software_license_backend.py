"""Software License backend (owner-approved 2026-10-15; LICENSE_REVIEW.md s.10).

Covers: the customer contract (shape, own account only, owner/viewer),
the authoritative license summary for every kind (including the owner's
case: a legacy camera_slots_local 8 plan plus a paid appliance order is the
appliance-included license, never "stand-alone"), the release catalog
(draft -> approved -> withdrawn, admin-only, audited, only approved rows
served, unsigned Windows never listed, latest.json fallback while the
catalog is empty), installed-version reporting from the authenticated
heartbeat only, installer_older_than_installed, and the safe update check.

No AWS, S3 or Stripe: storage is a LocalStorage in tmp_path, Stripe is the
conftest fake used by the existing billing tests.
"""
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

import pytest

from test_pricing_ff_commission import (  # noqa: F401 -- fixtures
    OWNER,
    _cookie,
    _make_global_admin,
    _paid_appliance_order,
    _seed,
    db_path,
    license_portal,
    portal,
)

COMMIT_124 = "3b2317e8e5323fbdf07b5933d874c1fde449d748"
COMMIT_126 = "01d1470ceacc80352d2e299190d77f7f2388f7dd"
COMMIT_127 = "7" * 40
COMMIT_WIN = "7c66928" + "a" * 33
CONTRACT_KEYS = {"account", "license", "appliances", "downloads", "notes"}
APPLIANCE_KEYS = {"appliance_id", "cloud_id", "name", "connection", "last_check_in", "installed_version",
                  "version_reported_at", "registration"}
DOWNLOAD_KEYS = {"platform", "version", "commit", "filename", "size_bytes", "sha256", "signed", "min_requirements",
                 "install_flow", "url"}
ADMIN_EMAIL = "admin@example.test"
CREDENTIAL = "appliance-credential-1"


# ------------------------------------------------------------------ fixtures / helpers

@pytest.fixture()
def storage(tmp_path, monkeypatch):
    import customer_downloads
    from object_storage import LocalStorage
    store = LocalStorage(tmp_path / "objects")
    monkeypatch.setattr(customer_downloads, "get_storage", lambda: store)
    return store


def _linux_name(version, commit):
    return f"anyaicam-appliance-installer-{version}-vms-{commit[:12]}.tar.gz"


def _package(tmp_path, name, payload=None):
    path = tmp_path / name
    path.write_bytes(payload or (name.encode() * 500))
    return path


def _publish_latest_json(tmp_path):
    """The pre-catalog pointer (customer_downloads publish), the live 1.2.4 shape."""
    import customer_downloads
    package = _package(tmp_path, _linux_name("1.2.4", COMMIT_124))
    return customer_downloads.publish_vms_installer(package, commit=COMMIT_124, version="1.2.4"), package


def _entitle(customer_id, product, quantity, status="active"):
    import customer_entitlements as ce
    ce.upsert_entitlement(customer_id=customer_id, product=product, camera_slot_quantity=quantity, status=status)


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(db_path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _query(db_path, statement, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result = [dict(item) for item in conn.execute(statement, params).fetchall()]
    conn.close()
    return result


def _v2_plan(db_path, customer_id="cust-1", quantity=6, status="active"):
    _sql(db_path, "INSERT INTO camera_plan_entitlements_v2(id,customer_id,plan_key,camera_quantity,stripe_customer_id,"
                  "stripe_subscription_id,stripe_subscription_item_id,stripe_price_id,status,local_retention_days,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"v2-{customer_id}", customer_id, "basic_local", quantity, f"cus_{customer_id}", f"sub_{customer_id}",
          f"si_{customer_id}", "price_basic", status, 2, "2026-10-01", "2026-10-01"))


def _appliance(db_path, appliance_id="appl-1", customer_id="cust-1", cloud_id="AIC-C814766E", *, credential=CREDENTIAL,
               activation_status="activated", last_check_in=None, software_version=None, site_id="site-1"):
    from partner_db import password_hash
    _sql(db_path, "INSERT INTO appliances(id,customer_id,site_id,cloud_id,software_version,last_check_in,activation_status,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?)",
         (appliance_id, customer_id, site_id, cloud_id, software_version, last_check_in, activation_status, "2026-10-01"))
    if credential:
        _sql(db_path, "INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)",
             (f"cred-{appliance_id}", appliance_id, password_hash(credential), "2026-10-01"))


def _auth(appliance_id="appl-1", credential=CREDENTIAL):
    return {"X-Appliance-Id": appliance_id, "X-Request-Timestamp": str(int(time.time())),
            "X-Request-Nonce": secrets.token_hex(16), "Authorization": f"Bearer {credential}"}


def _appliance_row(db_path, appliance_id="appl-1"):
    return _query(db_path, "SELECT * FROM appliances WHERE id=?", (appliance_id,))[0]


def _second_customer(db_path):
    _seed(db_path, customer_id="cust-2", email="other@example.test", cameras=0)
    _sql(db_path, "INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Office','2026-01-01')")


def _contract(client, cookie=None):
    return client.get("/api/customer/software-license", cookies=cookie or _cookie(*OWNER))


def _draft(tmp_path, version="1.2.6", commit=COMMIT_126, *, payload=None, **kwargs):
    import software_releases
    package = _package(tmp_path, _linux_name(version, commit), payload)
    return software_releases.create_draft(platform="linux", version=version, commit=commit, package=package,
                                          actor={"email": "ops@example.test", "role": "operator_cli"}, **kwargs), package


# ------------------------------------------------------------------ contract shape + owner's case

def test_owner_case_contract_shape_appliance_included_never_stand_alone(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    _paid_appliance_order(db_path)
    _entitle("cust-1", "camera_slots_local", 8)
    _appliance(db_path, last_check_in=None)
    _publish_latest_json(tmp_path)

    response = _contract(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == CONTRACT_KEYS
    assert body["account"] == {"customer_id": "cust-1", "name": "Real Customer"}
    assert set(body["license"]) == {"kind", "cameras", "status", "label", "usable_cameras"}  # usable_cameras: 2026-10-16, compatible addition
    assert body["license"]["kind"] == "appliance_included" and body["license"]["cameras"] == 8
    assert body["license"]["status"] == "active"
    assert "stand-alone" not in body["license"]["label"].lower() and "appliance" in body["license"]["label"]
    # The count is its own field (the panel shows it on its own row); the label never repeats it.
    assert body["license"]["label"] == "VMS software license included with your AnyAiCam appliance"
    assert [set(item) for item in body["appliances"]] == [APPLIANCE_KEYS]
    assert body["appliances"][0]["cloud_id"] == "AIC-C814766E" and body["appliances"][0]["connection"] == "never"
    assert body["appliances"][0]["installed_version"] is None
    assert [set(item) for item in body["downloads"]] == [DOWNLOAD_KEYS]
    download = body["downloads"][0]
    assert download["platform"] == "linux" and download["version"] == "1.2.4" and download["commit"] == COMMIT_124
    assert download["url"] == "/api/customer/downloads/vms-installer" and download["signed"] is False
    assert download["install_flow"] == "terminal_claim"
    assert body["notes"] == {"installer_older_than_installed": False}

    import customer_entitlements as ce
    assert body["license"]["cameras"] == ce.capacity_breakdown("cust-1")["vms_license_capacity"]


@pytest.mark.parametrize("setup,kind,cameras,status,stand_alone", [
    ("nothing", "none", 0, "none", False),
    ("one_time", "one_time_vms_license", 16, "active", True),
    ("per_camera", "per_camera_plan", 6, "active", False),
    ("per_camera_suspended", "per_camera_plan", 0, "suspended", False),
    ("legacy_no_order", "legacy_slots", 0, "none", False),
    ("appliance_included", "appliance_included", 8, "active", False),
    ("appliance_paid_awaiting_delivery", "appliance_included", 0, "pending", False),
    ("one_time_suspended", "one_time_vms_license", 0, "suspended", True),
])
def test_every_license_kind_matches_the_signed_capacity(license_portal, db_path, setup, kind, cameras, status, stand_alone):
    _seed(db_path)
    if setup == "one_time":
        _entitle("cust-1", "vms_license", 16)
    elif setup == "per_camera":
        _v2_plan(db_path, quantity=6)
    elif setup == "per_camera_suspended":
        _v2_plan(db_path, quantity=6, status="suspended")
    elif setup == "legacy_no_order":
        _entitle("cust-1", "camera_slots_local", 8)
    elif setup == "appliance_included":
        _paid_appliance_order(db_path)
        _entitle("cust-1", "camera_slots_hybrid", 8)
    elif setup == "one_time_suspended":
        _entitle("cust-1", "vms_license", 16, status="suspended")
    elif setup == "appliance_paid_awaiting_delivery":
        _paid_appliance_order(db_path)  # hardware-first: paid, no camera plan yet
    import customer_entitlements as ce
    summary = ce.vms_license_summary("cust-1")
    assert (summary["kind"], summary["cameras"], summary["status"]) == (kind, cameras, status)
    # The page's number is the number the appliance snapshot signs.
    assert summary["cameras"] == ce.capacity_breakdown("cust-1")["vms_license_capacity"]
    assert ("stand-alone" in summary["label"].lower()) is stand_alone
    if setup == "legacy_no_order":
        assert "Local" in summary["label"] and "no VMS software license" in summary["label"]
    if setup == "appliance_paid_awaiting_delivery":
        # Green's 1.2.7 journey review: a buyer awaiting delivery must not read "no license on record".
        assert summary["label"] == "VMS software license included with your AnyAiCam appliance (activates when you set up your appliance)"
        assert "no VMS software license" not in summary["label"]


def test_per_camera_plan_also_mentions_an_owned_one_time_license(license_portal, db_path):
    _seed(db_path)
    _v2_plan(db_path, quantity=6)
    _entitle("cust-1", "vms_license", 8)
    import customer_entitlements as ce
    summary = ce.vms_license_summary("cust-1")
    assert summary["kind"] == "per_camera_plan" and summary["cameras"] == 6 == ce.capacity_breakdown("cust-1")["vms_license_capacity"]
    # a vms_license row exists: the label names the license size and what the plan covers (2026-10-16 wording)
    assert summary["label"] == "One-time AnyAiCam VMS software license (up to 8 cameras) · your plan covers 6 cameras"


def test_appliance_snapshot_signs_the_same_number(license_portal, db_path):
    _seed(db_path)
    _paid_appliance_order(db_path)
    _entitle("cust-1", "camera_slots_local", 8)
    _appliance(db_path)
    import customer_entitlements as ce
    import feature_entitlements
    snapshot = feature_entitlements.appliance_snapshot(_appliance_row(db_path))
    assert snapshot["camera_capacity"]["vms_license_capacity"] == ce.vms_license_summary("cust-1")["cameras"] == 8


# ------------------------------------------------------------------ account separation + roles

def test_each_customer_sees_only_their_own_appliances(license_portal, db_path, storage):
    client, _, _ = license_portal
    _seed(db_path)
    _second_customer(db_path)
    _appliance(db_path, "appl-1", "cust-1", "AIC-AAAA0001")
    _appliance(db_path, "appl-2", "cust-2", "AIC-BBBB0002", credential="other-credential", site_id="site-2")
    mine = _contract(client).json()
    theirs = _contract(client, _cookie("other@example.test", "customer_owner", "cust-2")).json()
    assert [a["cloud_id"] for a in mine["appliances"]] == ["AIC-AAAA0001"]
    assert [a["cloud_id"] for a in theirs["appliances"]] == ["AIC-BBBB0002"]
    assert mine["account"]["customer_id"] == "cust-1" and theirs["account"]["customer_id"] == "cust-2"
    # There is no customer_id parameter to ask for someone else's account.
    assert _contract(client).json() == client.get("/api/customer/software-license?customer_id=cust-2",
                                                  cookies=_cookie(*OWNER)).json()


def test_viewer_reads_without_downloads_others_are_refused(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    _entitle("cust-1", "vms_license", 8)
    _publish_latest_json(tmp_path)
    owner = _contract(client).json()
    viewer = _contract(client, _cookie("viewer@example.test", "customer_viewer", "cust-1"))
    assert viewer.status_code == 200 and viewer.json()["license"] == owner["license"]
    assert owner["downloads"] and viewer.json()["downloads"] == []
    assert client.get("/api/customer/software-license").status_code == 401
    assert _contract(client, _cookie("sales@example.test", "salesperson")).status_code == 403
    assert _contract(client, _cookie(ADMIN_EMAIL, "administrator")).status_code == 403


def test_unlicensed_owner_gets_no_downloads(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    _publish_latest_json(tmp_path)
    body = _contract(client).json()
    assert body["license"]["kind"] == "none" and body["downloads"] == []


# ------------------------------------------------------------------ appliances: connection + registration

def test_connection_and_registration_states(license_portal, db_path, storage):
    from datetime import datetime, timedelta
    client, _, _ = license_portal
    _seed(db_path)
    fresh = (datetime.now() - timedelta(seconds=30)).isoformat()
    stale = (datetime.now() - timedelta(minutes=30)).isoformat()
    _appliance(db_path, "appl-1", cloud_id="AIC-ONLINE01", last_check_in=fresh)
    _appliance(db_path, "appl-2", cloud_id="AIC-STALE002", last_check_in=stale, credential="c2")
    _appliance(db_path, "appl-3", cloud_id="AIC-PENDING3", activation_status="pending", credential=None)
    _appliance(db_path, "appl-4", cloud_id="AIC-RELEASE4", activation_status="released", credential="c4", last_check_in=stale)
    _appliance(db_path, "appl-5", cloud_id="AIC-REVOKED5", credential="c5", last_check_in=stale)
    _sql(db_path, "UPDATE appliance_credentials SET revoked_at='2026-10-02' WHERE appliance_id='appl-5'")
    _sql(db_path, "UPDATE appliances SET state='revoked' WHERE id='appl-5'")
    found = {a["cloud_id"]: a for a in _contract(client).json()["appliances"]}
    assert (found["AIC-ONLINE01"]["connection"], found["AIC-ONLINE01"]["registration"]) == ("online", "activated")
    assert (found["AIC-STALE002"]["connection"], found["AIC-STALE002"]["registration"]) == ("offline", "activated")
    assert (found["AIC-PENDING3"]["connection"], found["AIC-PENDING3"]["registration"]) == ("never", "claimed_pending")
    assert found["AIC-RELEASE4"]["registration"] == "deactivated"
    assert found["AIC-REVOKED5"]["registration"] == "deactivated"
    assert found["AIC-ONLINE01"]["name"] == "AnyAiCam appliance · Home"


# ------------------------------------------------------------------ release catalog

def test_catalog_lifecycle_admin_only_audited_and_only_approved_is_served(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    _entitle("cust-1", "vms_license", 8)
    admin = _make_global_admin(db_path, ADMIN_EMAIL)
    _, old_package = _publish_latest_json(tmp_path)
    release, package = _draft(tmp_path)
    assert release["status"] == "draft" and release["install_flow"] == "desktop_link"
    assert release["sha256"] == hashlib.sha256(package.read_bytes()).hexdigest()

    # A draft is never offered: the latest.json 1.2.4 still is.
    assert [d["version"] for d in _contract(client).json()["downloads"]] == ["1.2.4"]
    assert client.get("/api/customer/downloads/linux", cookies=_cookie(*OWNER)).content == old_package.read_bytes()

    # Only a global administrator approves.
    approve_path = f"/api/admin/software-releases/{release['id']}/approve"
    assert client.post(approve_path, cookies=_cookie(*OWNER)).status_code == 403
    assert client.post(approve_path, cookies=_cookie("partneradmin@example.test", "administrator")).status_code == 403
    assert client.get("/api/admin/software-releases", cookies=_cookie(*OWNER)).status_code == 403
    approved = client.post(approve_path, cookies=admin)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == ADMIN_EMAIL
    assert client.post(approve_path, cookies=admin).status_code == 409  # not a draft any more

    downloads = _contract(client).json()["downloads"]
    assert [(d["version"], d["commit"]) for d in downloads] == [("1.2.6", COMMIT_126)]
    for path in ("/api/customer/downloads/linux", "/api/customer/downloads/vms-installer"):
        served = client.get(path, cookies=_cookie(*OWNER))
        assert served.status_code == 200 and served.content == package.read_bytes()
        assert served.headers["x-content-sha256"] == release["sha256"]

    # A newer DRAFT (1.2.7) never appears before it is approved.
    _draft(tmp_path, "1.2.7", COMMIT_127)
    assert [d["version"] for d in _contract(client).json()["downloads"]] == ["1.2.6"]

    # Withdrawing the only approved Linux row falls back to latest.json.
    withdrawn = client.post(f"/api/admin/software-releases/{release['id']}/withdraw", json={"reason": "test"}, cookies=admin)
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert [d["version"] for d in _contract(client).json()["downloads"]] == ["1.2.4"]
    assert client.post(approve_path, cookies=admin).status_code == 409  # a withdrawn row is final

    actions = [row["action"] for row in _query(db_path, "SELECT action FROM audit_logs WHERE entity_type='software_release' ORDER BY id")]
    assert actions == ["software_release.drafted", "software_release.approved", "software_release.drafted", "software_release.withdrawn"]
    admin_list = client.get("/api/admin/software-releases", cookies=admin).json()
    assert {r["version"]: r["status"] for r in admin_list["releases"]} == {"1.2.6": "withdrawn", "1.2.7": "draft"}


def test_admin_api_draft_requires_the_object_already_stored_and_matching(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    admin = _make_global_admin(db_path, ADMIN_EMAIL)
    import software_releases
    name = _linux_name("1.2.6", COMMIT_126)
    data = b"stored-installer" * 100
    body = {"platform": "linux", "version": "1.2.6", "commit": COMMIT_126, "filename": name,
            "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    assert client.post("/api/admin/software-releases", json=body, cookies=_cookie(*OWNER)).status_code == 403
    missing = client.post("/api/admin/software-releases", json=body, cookies=admin)
    assert missing.status_code == 400 and "not in storage" in missing.json()["detail"]
    storage.put("downloads", software_releases.storage_key("linux", COMMIT_126, name), data)
    wrong = client.post("/api/admin/software-releases", json=dict(body, sha256="0" * 64), cookies=admin)
    assert wrong.status_code == 400
    created = client.post("/api/admin/software-releases", json=body, cookies=admin)
    assert created.status_code == 200 and created.json()["status"] == "draft"
    assert client.post("/api/admin/software-releases", json=body, cookies=admin).status_code == 409  # duplicate


def test_draft_reverifies_local_file_and_approval_rehashes_storage(license_portal, db_path, storage, tmp_path):
    _seed(db_path)
    import software_releases
    with pytest.raises(software_releases.ReleaseError):
        _draft(tmp_path, sha256="0" * 64)  # expected hash from the build manifest does not match
    assert software_releases.list_releases() == []
    release, _ = _draft(tmp_path)
    storage.put("downloads", release["storage_key"], b"tampered")
    with pytest.raises(software_releases.ReleaseError):
        software_releases.approve(release["id"], actor={"email": ADMIN_EMAIL, "role": "administrator"})
    assert software_releases.get_release(release["id"])["status"] == "draft"
    with pytest.raises(software_releases.ReleaseError):  # name/commit/version mismatch
        software_releases.create_draft(platform="linux", version="1.2.6", commit=COMMIT_127,
                                       package=_package(tmp_path, _linux_name("1.2.6", COMMIT_126)),
                                       actor={"email": "ops@example.test"})


def test_windows_only_signed_and_never_the_public_unsigned_130(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    _entitle("cust-1", "vms_license", 8)
    import software_releases
    actor = {"email": ADMIN_EMAIL, "role": "administrator"}
    exe = _package(tmp_path, f"AnyAiCam-VMS-Setup-1.3.1-{COMMIT_WIN[:7]}.exe")
    with pytest.raises(software_releases.ReleaseError):
        software_releases.create_draft(platform="windows", version="1.3.1", commit=COMMIT_WIN, package=exe, actor=actor)
    public_130 = _package(tmp_path, "AnyAiCam-VMS-Setup-1.3.0-aedecb6.exe")
    with pytest.raises(software_releases.ReleaseError):
        software_releases.create_draft(platform="windows", version="1.3.0", commit="aedecb6" + "0" * 33,
                                       package=public_130, actor=actor, signed=True)
    # Even a row written straight into the table is not listed when unsigned or 1.3.0.
    for rid, version, commit, signed in (("w-unsigned", "1.3.2", "b" * 40, 0), ("w-130", "1.3.0", "aedecb6" + "0" * 33, 1)):
        _sql(db_path, "INSERT INTO software_releases(id,platform,version,build_commit,filename,size_bytes,sha256,storage_key,signed,"
                      "min_requirements,install_flow,status,created_by,created_at,approved_by,approved_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (rid, "windows", version, commit, "x.exe", 1, "0" * 64, "k", signed, "Windows", "windows_setup", "approved",
              "x", "2026-10-15", "x", "2026-10-15"))
    assert [d["platform"] for d in _contract(client).json()["downloads"]] == []
    assert client.get("/api/customer/downloads/windows", cookies=_cookie(*OWNER)).status_code == 404

    signed = software_releases.create_draft(platform="windows", version="1.3.1", commit=COMMIT_WIN, package=exe, actor=actor,
                                            signed=True, signer_subject="CN=Any AI Cam")
    software_releases.approve(signed["id"], actor=actor)
    windows = [d for d in _contract(client).json()["downloads"] if d["platform"] == "windows"]
    assert len(windows) == 1 and windows[0]["signed"] is True and windows[0]["version"] == "1.3.1"
    assert windows[0]["url"] == "/api/customer/downloads/windows" and windows[0]["install_flow"] == "windows_setup"
    served = client.get("/api/customer/downloads/windows", cookies=_cookie(*OWNER))
    assert served.status_code == 200 and served.content == exe.read_bytes()
    assert client.get("/api/customer/downloads/macos", cookies=_cookie(*OWNER)).status_code == 404


def test_empty_catalog_and_no_latest_json_lists_nothing(license_portal, db_path, storage):
    client, _, _ = license_portal
    _seed(db_path)
    _entitle("cust-1", "vms_license", 8)
    assert _contract(client).json()["downloads"] == []
    assert client.get("/api/customer/downloads/linux", cookies=_cookie(*OWNER)).status_code == 404


# ------------------------------------------------------------------ installed versions

@pytest.mark.parametrize("label,expected", [
    ("1.2.6+01d1470ceacc", ("1.2.6", "01d1470ceacc")),
    ("1.2.6", ("1.2.6", None)),
    ("0.1.0", (None, None)),
    ("Unknown", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
    ("Not installed", (None, None)),
    ("garbage+zz", (None, None)),
])
def test_version_label_parsing(label, expected):
    import software_license
    assert software_license.parse_version_label(label) == expected


def test_heartbeat_reports_the_installed_version_and_setup_status_no_longer_overwrites_it(license_portal, db_path, storage, monkeypatch):
    client, _, _ = license_portal
    _seed(db_path)
    _appliance(db_path)
    assert client.post("/api/appliance/heartbeat", json={"software_version": "1.2.6+01d1470ceacc"}, headers=_auth()).status_code == 200
    stored = _appliance_row(db_path)
    assert (stored["reported_version"], stored["reported_commit"]) == ("1.2.6", "01d1470ceacc")
    assert stored["software_version"] == "1.2.6+01d1470ceacc" and stored["version_reported_at"]
    assert _contract(client).json()["appliances"][0]["installed_version"] == "1.2.6"

    # The provisioning backend's view no longer replaces what the appliance reported.
    import partner_workspace

    class _Backend:
        def get_status(self, cloud_id):
            return {"online_status": "online", "software_version": "9.9.9", "last_check_in": "2026-10-15T00:00:00", "entitlement": {}}

    monkeypatch.setattr(partner_workspace, "get_provisioning_backend", lambda: _Backend())
    assert client.get("/api/customer/setup/status", cookies=_cookie(*OWNER)).status_code == 200
    stored = _appliance_row(db_path)
    assert stored["software_version"] == "1.2.6+01d1470ceacc" and stored["reported_version"] == "1.2.6"

    # An older payload without a version leaves the report untouched ...
    client.post("/api/appliance/heartbeat", json={"cpu": 1}, headers=_auth())
    assert _appliance_row(db_path)["reported_version"] == "1.2.6"
    # ... the agent's 0.1.0 fallback is not a version ...
    client.post("/api/appliance/heartbeat", json={"software_version": "0.1.0"}, headers=_auth())
    assert _appliance_row(db_path)["reported_version"] is None
    assert _contract(client).json()["appliances"][0]["installed_version"] is None
    # ... structured fields from newer agents win, and the version route records too.
    client.post("/api/appliance/heartbeat", json={"software_version": "x", "release_version": "1.2.7", "build_id": COMMIT_127,
                                                  "platform": "linux"}, headers=_auth())
    stored = _appliance_row(db_path)
    assert (stored["reported_version"], stored["reported_commit"], stored["reported_platform"]) == ("1.2.7", COMMIT_127, "linux")
    assert client.post("/api/appliance/version", json={"software_version": "1.2.8+abcdef123456"}, headers=_auth()).status_code == 200
    assert _appliance_row(db_path)["reported_version"] == "1.2.8"
    # Unauthenticated reports change nothing.
    assert client.post("/api/appliance/version", json={"software_version": "9.9.9"}).status_code == 401
    assert _appliance_row(db_path)["reported_version"] == "1.2.8"


def test_installer_older_than_installed(license_portal, db_path, storage, tmp_path):
    client, _, _ = license_portal
    _seed(db_path)
    _entitle("cust-1", "vms_license", 8)
    _publish_latest_json(tmp_path)  # offers 1.2.4
    _appliance(db_path)
    assert _contract(client).json()["notes"]["installer_older_than_installed"] is False  # unknown is not newer
    client.post("/api/appliance/heartbeat", json={"software_version": "1.2.3+aaaaaaaaaaaa"}, headers=_auth())
    assert _contract(client).json()["notes"]["installer_older_than_installed"] is False
    client.post("/api/appliance/heartbeat", json={"software_version": "1.2.6+01d1470ceacc"}, headers=_auth())
    assert _contract(client).json()["notes"]["installer_older_than_installed"] is True
    # A Windows appliance's version is not compared with the Linux installer.
    client.post("/api/appliance/heartbeat", json={"software_version": "1.3.1+7c66928aaaaa", "platform": "windows"}, headers=_auth())
    assert _contract(client).json()["notes"]["installer_older_than_installed"] is False

    import software_license
    assert software_license.installer_older_than_installed([{"platform": "linux", "version": "1.2.x"}],
                                                           [{"installed_version": "1.2.6", "platform": None}]) is False


# ------------------------------------------------------------------ safe update check

@pytest.mark.parametrize("installed,offered", [
    ("1.2.0+aaaaaaaaaaaa", True),
    ("1.2.6+01d1470ceacc", False),  # the Ryzen's "Update available 1.2.1" case
    ("1.2.1+bbbbbbbbbbbb", False),  # same version
    ("0.1.0", False),
    (None, False),
])
def test_update_check_offers_only_a_strictly_newer_release(license_portal, db_path, monkeypatch, installed, offered):
    client, _, _ = license_portal
    _seed(db_path)
    _appliance(db_path)
    if installed:
        client.post("/api/appliance/heartbeat", json={"software_version": installed}, headers=_auth())
    import updates_storage
    record = {"manifest": {"update_id": "upd-121", "version": "1.2.1", "build_id": "c" * 40}, "package_key": "k", "signature": "c2ln"}
    monkeypatch.setattr(updates_storage, "get_latest_release", lambda target, channel: record)
    monkeypatch.setattr(updates_storage, "package_download_url", lambda release, expires_seconds=300: "https://example.test/pkg")
    response = client.get("/api/appliance/updates/latest?target=anyaicam-appliance&channel=stable", headers=_auth())
    assert response.status_code == 200
    if offered:
        assert response.json()["manifest"]["version"] == "1.2.1"
    else:
        assert response.json() == {"status": "no_update_available"}


def test_migration_is_additive_and_recorded(db_path):
    columns = {row["name"] for row in _query(db_path, "PRAGMA table_info(appliances)")}
    assert {"reported_version", "reported_commit", "reported_platform", "version_reported_at", "software_version"} <= columns
    assert _query(db_path, "SELECT version FROM schema_migrations WHERE version='20261015_software_releases_and_versions'")
