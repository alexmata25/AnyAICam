"""Reconnect an existing camera after its DHCP address changed (2026-10-15).

Customer journey on the Cameras tab: Reconnect -> the appliance searches the
network -> THIS camera is found (matched by its ONVIF identity on the server,
never by IP) -> the customer confirms the saved login or enters one -> the
appliance verifies it at the camera's current address -> only the host of
the stored stream address changes. The camera keeps its id, number, name,
rules and recordings, and no plan place is used. Remove + Add stays the
fallback (a new camera). Real customer and appliance routes (TestClient,
the slot-enforcement fixtures); the appliance side is the real edge sync.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

import edge_camera_sync
from appliance_protocol import decrypt_camera_credentials, encrypt_camera_credentials
from database_backend import override_target
from partner_db import initialize_database
from test_camera_add_remove_capacity import _activated, _add, _camera_id, _per_camera_plan, _remove
from test_camera_slot_enforcement import (  # noqa: F401  (pytest fixtures)
    _appliance_headers, _owner_cookie, _seed_tenant, _session_cookie_name, client, db_path,
)

KEY = "xdPNoveA5Njb5qzIJHY2ZDFQdwnodQbL_u7ZDEqtaoY="
RIGHT = "urn:uuid:fd430000-f7d4-11b4-83a1-142ffd60a285"
OTHER = "urn:uuid:a1348000-043b-11b4-826b-142ffda08360"
PATH = "/Streaming/Channels/101?transportmode=unicast&profile=Profile_1"
OLD = f"rtsp://192.168.0.44:554{PATH}"
NEW = f"rtsp://192.168.0.43:554{PATH}"


@pytest.fixture()
def world(client, db_path, monkeypatch):
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", KEY)
    monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)
    monkeypatch.setattr("appliance_readiness.enrollment_ready", lambda appliance_id: True)
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 1)  # the plan is full once Driveway Right exists
    credential = _activated(client, db_path)
    assert _add(client, credential, RIGHT).status_code == 200
    camera_id = _camera_id(db_path, RIGHT)
    resolved = client.post(f"/api/appliance/AIC-SLOT1/cameras/{camera_id}/media-uri",
                           json={"device_key": RIGHT, "rtsp_uri": OLD}, headers=_appliance_headers("appl-1", credential))
    assert resolved.json()["updated"] is True
    _sql(db_path, "UPDATE cameras SET name='Driveway Right' WHERE id=?", (camera_id,))
    return {"client": client, "db": db_path, "credential": credential, "camera_id": camera_id}


def _sql(db_path, query, params=()):
    with override_target(sqlite_path=str(db_path)):
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(query, params)]
            con.commit()
            return rows
        finally:
            con.close()


def _owner(client):
    client.cookies.set(_session_cookie_name(), _owner_cookie())
    return client


def _camera(w):
    return _sql(w["db"], "SELECT * FROM cameras WHERE id=?", (w["camera_id"],))[0]


def _appliance(w, method, path, **kwargs):
    return getattr(w["client"], method)(path, headers=_appliance_headers("appl-1", w["credential"]), **kwargs)


def _scan_completes(w, scan_job_id, device_keys):
    results = [{"id": f"candidate-{n}", "name": f"Discovered camera {n}", "device_key": key, "rtsp_support": True}
               for n, key in enumerate(device_keys, 1)]
    response = _appliance(w, "post", f"/api/appliance/AIC-SLOT1/scan-jobs/{scan_job_id}",
                          json={"status": "complete", "progress": 100, "results": results, "message": "Done."})
    assert response.status_code == 200, response.text


def _rediscover(w, found_keys):
    client = _owner(w["client"])
    started = client.post(f"/api/customer/cameras/{w['camera_id']}/rediscover")
    assert started.status_code == 200, started.text
    job = started.json()["scan_job_id"]
    _scan_completes(w, job, found_keys)
    status = _owner(w["client"]).get(f"/api/customer/cameras/{w['camera_id']}/rediscover/{job}")
    assert status.status_code == 200, status.text
    return status.json()


def _reconnect(w, **body):
    return _owner(w["client"]).post(f"/api/customer/cameras/{w['camera_id']}/reconnect", json=body)


def _appliance_confirms(w, job_id, success=True, message="Verified."):
    response = _appliance(w, "post", f"/api/appliance/AIC-SLOT1/provisioning-jobs/{job_id}",
                          json={"success": success, "message": message})
    assert response.status_code == 200, response.text


def _appliance_moves_address(w, job_id, rtsp_uri=NEW, camera_id=None):
    return _appliance(w, "post", f"/api/appliance/AIC-SLOT1/cameras/{camera_id or w['camera_id']}/media-uri",
                      json={"device_key": RIGHT, "rtsp_uri": rtsp_uri, "provisioning_job_id": job_id})


# ------------------------------------------------------------ rediscovery

def test_rediscovery_finds_this_camera_by_its_identity(world):
    result = _rediscover(world, [OTHER, RIGHT])
    assert result["status"] == "complete"
    assert result["found"] is True and result["other_devices"] == 1
    assert result["has_saved_credentials"] is False


def test_rediscovery_reports_not_found_and_never_offers_another_device(world):
    result = _rediscover(world, [OTHER])
    assert result["found"] is False and result["other_devices"] == 1
    assert OTHER not in str(result)  # other devices are only counted


def test_rediscovery_scans_the_cameras_own_appliance(world):
    client = _owner(world["client"])
    started = client.post(f"/api/customer/cameras/{world['camera_id']}/rediscover").json()
    job = _sql(world["db"], "SELECT appliance_id, customer_id FROM camera_scan_jobs WHERE id=?", (started["scan_job_id"],))[0]
    assert job == {"appliance_id": "appl-1", "customer_id": "cust-1"}


# ------------------------------------------------------- the full reconnect

def test_reconnect_keeps_the_camera_and_moves_only_its_address(world):
    before = _camera(world)
    cameras_before = _sql(world["db"], "SELECT COUNT(*) AS n FROM cameras")[0]["n"]
    assert _rediscover(world, [RIGHT])["found"]
    started = _reconnect(world, username="admin", password="new-pass")
    assert started.status_code == 200, started.text  # plan is full: a reconnect uses no place
    job_id = started.json()["job_id"]
    job = _sql(world["db"], "SELECT device_key, camera_name, appliance_id FROM camera_provisioning_requests WHERE id=?", (job_id,))[0]
    assert job == {"device_key": RIGHT, "camera_name": "Driveway Right", "appliance_id": "appl-1"}

    _appliance_confirms(world, job_id)
    moved = _appliance_moves_address(world, job_id)
    assert moved.status_code == 200 and moved.json()["updated"] is True, moved.text

    after = _camera(world)
    assert after["onvif_endpoint"] == NEW
    assert {k: v for k, v in after.items() if k != "onvif_endpoint"} == {k: v for k, v in before.items() if k != "onvif_endpoint"}
    assert _sql(world["db"], "SELECT COUNT(*) AS n FROM cameras")[0]["n"] == cameras_before
    saved = _sql(world["db"], "SELECT encrypted_blob FROM camera_credentials WHERE camera_id=?", (world["camera_id"],))[0]
    assert decrypt_camera_credentials(saved["encrypted_blob"]) == {"username": "admin", "password": "new-pass"}
    audit = _sql(world["db"], "SELECT action FROM audit_logs WHERE entity_id=? ORDER BY id", (world["camera_id"],))
    assert [a["action"] for a in audit][-1] == "camera.address_reconnected"


def test_reconnect_status_waits_for_video(world):
    job_id = _reconnect(world, username="admin", password="p").json()["job_id"]
    status = lambda: _owner(world["client"]).get(f"/api/customer/cameras/{world['camera_id']}/reconnect/{job_id}").json()
    assert status()["status"] == "queued"
    _appliance_confirms(world, job_id)
    assert status() == {"status": "provisioned", "message": "Verified.", "failure_code": None, "video_confirmed": False}
    later = (datetime.now() + timedelta(seconds=5)).isoformat()
    _sql(world["db"], "INSERT INTO appliance_camera_status(appliance_id,camera_id,name,online,recording,updated_at) VALUES(?,?,?,?,?,?)",
         ("appl-1", world["camera_id"], "Driveway Right", 1, 1, later))
    assert status()["video_confirmed"] is True


def test_a_failed_verification_changes_nothing(world):
    job_id = _reconnect(world, username="admin", password="wrong").json()["job_id"]
    _appliance_confirms(world, job_id, success=False, message="The camera rejected the username or password.")
    assert _appliance_moves_address(world, job_id).status_code == 409
    assert _camera(world)["onvif_endpoint"] == OLD
    state = _owner(world["client"]).get(f"/api/customer/cameras/{world['camera_id']}/reconnect/{job_id}").json()
    assert state["status"] == "failed" and "rejected" in state["message"]


def test_the_identity_always_comes_from_the_camera_not_the_browser(world):
    job_id = _reconnect(world, username="admin", password="p", device_key=OTHER).json()["job_id"]
    assert _sql(world["db"], "SELECT device_key FROM camera_provisioning_requests WHERE id=?", (job_id,))[0]["device_key"] == RIGHT


def test_saved_credentials_can_be_confirmed_instead_of_retyped(world):
    blob = encrypt_camera_credentials("admin", "saved-pass")
    _sql(world["db"], "INSERT INTO camera_credentials(camera_id,encrypted_blob,created_at,updated_at) VALUES(?,?,?,?)",
         (world["camera_id"], blob, "2026-10-01", "2026-10-01"))
    assert _rediscover(world, [RIGHT])["has_saved_credentials"] is True
    job_id = _reconnect(world, use_saved_credentials=True).json()["job_id"]
    queued = _sql(world["db"], "SELECT encrypted_credentials FROM camera_provisioning_requests WHERE id=?", (job_id,))[0]
    assert decrypt_camera_credentials(queued["encrypted_credentials"]) == {"username": "admin", "password": "saved-pass"}


def test_confirming_saved_credentials_when_there_are_none_asks_for_them(world):
    response = _reconnect(world, use_saved_credentials=True)
    assert response.status_code == 400 and "Enter them" in response.json()["detail"]


def test_a_second_click_returns_the_same_pending_request(world):
    first = _reconnect(world, username="admin", password="p").json()["job_id"]
    assert _reconnect(world, username="admin", password="p").json()["job_id"] == first


# --------------------------------------------- address-change safety rails

def test_without_a_reconnect_job_an_existing_address_is_never_replaced(world):
    response = _appliance(world, "post", f"/api/appliance/AIC-SLOT1/cameras/{world['camera_id']}/media-uri",
                          json={"device_key": RIGHT, "rtsp_uri": NEW})
    assert response.json()["updated"] is False and _camera(world)["onvif_endpoint"] == OLD


def test_only_the_host_may_change(world):
    job_id = _reconnect(world, username="admin", password="p").json()["job_id"]
    _appliance_confirms(world, job_id)
    for uri in (f"rtsp://192.168.0.43:8554{PATH}", "rtsp://192.168.0.43:554/Streaming/Channels/102", f"rtsps://192.168.0.43:554{PATH}"):
        assert _appliance_moves_address(world, job_id, rtsp_uri=uri).status_code in {400, 409}, uri
    assert _camera(world)["onvif_endpoint"] == OLD


def test_a_job_that_is_not_yet_verified_does_not_authorize_a_change(world):
    job_id = _reconnect(world, username="admin", password="p").json()["job_id"]
    assert _appliance_moves_address(world, job_id).status_code == 409
    assert _camera(world)["onvif_endpoint"] == OLD


def test_an_expired_reconnect_does_not_authorize_a_change(world):
    job_id = _reconnect(world, username="admin", password="p").json()["job_id"]
    _appliance_confirms(world, job_id)
    _sql(world["db"], "UPDATE camera_provisioning_requests SET updated_at=? WHERE id=?",
         ((datetime.now() - timedelta(hours=2)).isoformat(), job_id))
    assert _appliance_moves_address(world, job_id).status_code == 409
    assert _camera(world)["onvif_endpoint"] == OLD


def test_a_reconnect_job_cannot_move_a_different_camera(world):
    job_id = _reconnect(world, username="admin", password="p").json()["job_id"]
    _appliance_confirms(world, job_id)
    _sql(world["db"], "INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,status,created_at,device_key,onvif_endpoint,camera_number) "
                      "VALUES('cam-x','cust-1','site-1','appl-1','Garage','configured','2026-10-01',?,?,9)",
         (OTHER, f"rtsp://192.168.0.69:554{PATH}"))
    response = _appliance(world, "post", "/api/appliance/AIC-SLOT1/cameras/cam-x/media-uri",
                          json={"device_key": OTHER, "rtsp_uri": f"rtsp://192.168.0.70:554{PATH}", "provisioning_job_id": job_id})
    assert response.status_code == 409
    assert _sql(world["db"], "SELECT onvif_endpoint FROM cameras WHERE id='cam-x'")[0]["onvif_endpoint"] == f"rtsp://192.168.0.69:554{PATH}"


# ----------------------------------------------------------- access rules

def test_another_customers_camera_is_not_found(world):
    other = {"anyaicam_partner_session": _owner_cookie(customer_id="cust-2", email="other@example.test")}
    world["client"].cookies.clear()
    assert world["client"].post(f"/api/customer/cameras/{world['camera_id']}/rediscover", cookies=other).status_code in {403, 404}
    assert world["client"].post(f"/api/customer/cameras/{world['camera_id']}/reconnect", json={}, cookies=other).status_code in {403, 404}


def test_a_removed_camera_cannot_be_reconnected(world):
    assert _remove(world["client"], world["camera_id"]).status_code == 200
    assert _reconnect(world, username="admin", password="p").status_code == 404


def test_on_the_appliance_itself_cameras_are_managed_in_the_portal(world, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    assert _owner(world["client"]).post(f"/api/customer/cameras/{world['camera_id']}/rediscover").status_code == 409
    assert _reconnect(world, username="admin", password="p").status_code == 409


# --------------------------------------------- Delete + Add stays the fallback

def test_remove_then_add_still_works_and_makes_a_new_camera(world):
    old_id = world["camera_id"]
    assert _remove(world["client"], old_id).status_code == 200
    assert _add(world["client"], world["credential"], RIGHT).status_code == 200
    assert _camera_id(world["db"], RIGHT) != old_id


# ----------------------------------------------------- the appliance side

def test_the_appliance_picks_up_the_new_address_and_the_new_password(world, tmp_path, monkeypatch):
    edge_db = tmp_path / "edge.db"
    with override_target(sqlite_path=str(edge_db)):
        initialize_database()
    monkeypatch.setattr(edge_camera_sync, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(edge_camera_sync, "CLOUD_URL", "https://portal.example")
    identity = {"appliance_id": "appl-1", "cloud_id": "AIC-SLOT1", "credential": world["credential"], "customer_id": "cust-1", "site_id": "site-1"}
    monkeypatch.setattr("appliance_activation.load_persisted_identity", lambda: identity)

    def sync(pending_password=None):
        configuration = _appliance(world, "get", "/api/appliance/configuration").json()
        monkeypatch.setattr(edge_camera_sync, "_control_plane_get", lambda path, appliance_id, credential: configuration)
        with override_target(sqlite_path=str(edge_db)):
            if pending_password:
                con = sqlite3.connect(edge_db)
                con.execute("INSERT INTO pending_camera_credentials(device_key,encrypted_blob,created_at) VALUES(?,?,?)",
                            (RIGHT, encrypt_camera_credentials("admin", pending_password), datetime.now().isoformat()))
                con.commit(); con.close()
            assert edge_camera_sync.sync_provisioned_cameras()["status"] == "ok"
            con = sqlite3.connect(edge_db); con.row_factory = sqlite3.Row
            camera = dict(con.execute("SELECT id,camera_number,onvif_endpoint FROM cameras WHERE device_key=?", (RIGHT,)).fetchone())
            blob = con.execute("SELECT encrypted_blob FROM camera_credentials WHERE camera_id=?", (camera["id"],)).fetchone()
            con.close()
            return camera, decrypt_camera_credentials(blob[0]) if blob else None

    camera, creds = sync(pending_password="old-pass")
    assert camera["onvif_endpoint"] == OLD and creds["password"] == "old-pass"
    job_id = _reconnect(world, username="admin", password="new-pass").json()["job_id"]
    _appliance_confirms(world, job_id)
    assert _appliance_moves_address(world, job_id).json()["updated"] is True
    moved, creds = sync(pending_password="new-pass")  # the agent hands the verified login to the VMS
    assert moved == {**camera, "onvif_endpoint": NEW}
    assert creds["password"] == "new-pass"


# ---------------------------------------------------------- the Cameras tab

def test_the_cameras_tab_offers_reconnect_to_owners_on_the_portal():
    import live_view_page
    script = live_view_page._CAMERA_MANAGE_JS
    for needle in ("/rediscover`", "/reconnect`", "use_saved_credentials", "video_confirmed",
                   "never connects a different device in place of this camera", "remove it and use Add camera"):
        assert needle in script, needle
    assert "document.querySelectorAll('.reconnect-camera').forEach(button=>{button.hidden=!manage;});" in script
    assert "use Reconnect instead" in script


def test_a_camera_can_only_be_moved_to_a_local_network_address(world):
    """Red review: a compromised or buggy appliance must not repoint a camera,
    and so its stored credentials, at a public host, a name or loopback."""
    job_id = _reconnect(world, username="admin", password="p").json()["job_id"]
    _appliance_confirms(world, job_id)
    for host in ("8.8.8.8", "203.0.113.7", "198.51.100.7", "192.0.2.5", "240.0.0.1", "198.18.0.1",
                 "camera.example.com", "127.0.0.1", "0.0.0.0", "224.0.0.1", "[2001:db8::1]", "[::1]"):
        response = _appliance_moves_address(world, job_id, rtsp_uri=f"rtsp://{host}:554{PATH}")
        assert response.status_code in {400, 409}, host
    assert _camera(world)["onvif_endpoint"] == OLD
    assert _appliance_moves_address(world, job_id, rtsp_uri=f"rtsp://100.64.10.20:554{PATH}").json()["updated"] is True


def test_the_local_network_allowlist_is_explicit():
    import ipaddress
    import appliance_cloud  # noqa: F401  (the constant lives in its route registration)
    allowed = ("10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.0.43", "169.254.1.1", "100.64.10.20", "fd00::5", "fe80::1")
    refused = ("172.32.0.1", "100.128.0.1", "203.0.113.7", "198.51.100.7", "192.0.2.5", "240.0.0.1", "198.18.0.1", "2001:db8::1")
    networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10', 'fc00::/7', 'fe80::/10')
    inside = lambda ip: any(ipaddress.ip_address(ip) in ipaddress.ip_network(n) for n in networks
                            if ipaddress.ip_network(n).version == ipaddress.ip_address(ip).version)
    assert all(inside(ip) for ip in allowed) and not any(inside(ip) for ip in refused)
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("appliance_cloud.py").read_text(encoding="utf-8")
    assert "RECONNECT_LOCAL_NETWORKS=" + repr(networks).replace(" ", "") in source.replace(" ", "")


# ------------------------------------------------ the driveway scenario

LEFT = "urn:uuid:d45ec000-f254-11b4-839a-142ffd606e23"


def test_both_driveway_cameras_reconnect_after_the_oct_12_dhcp_change(client, db_path, monkeypatch):
    """Driveway Right .44 -> .43 and Driveway Left .70 -> .69, each reconnected
    as itself: identities, numbers and names kept, no plan place used, and a
    reconnect of one never moves the other."""
    monkeypatch.setenv("ANYAICAM_CAMERA_CREDENTIAL_KEY", KEY)
    monkeypatch.delenv("ANYAICAM_RUNTIME_ROLE", raising=False)
    monkeypatch.setattr("appliance_readiness.enrollment_ready", lambda appliance_id: True)
    _seed_tenant(db_path)
    _per_camera_plan(db_path, 2)
    credential = _activated(client, db_path)
    cams = {}
    for key, name, old in ((RIGHT, "Driveway Right", "192.168.0.44"), (LEFT, "Driveway Left", "192.168.0.70")):
        assert _add(client, credential, key).status_code == 200
        cid = _camera_id(db_path, key)
        assert client.post(f"/api/appliance/AIC-SLOT1/cameras/{cid}/media-uri", json={"device_key": key, "rtsp_uri": f"rtsp://{old}:554{PATH}"},
                           headers=_appliance_headers("appl-1", credential)).json()["updated"]
        _sql(db_path, "UPDATE cameras SET name=? WHERE id=?", (name, cid))
        cams[key] = cid
    before = {k: _sql(db_path, "SELECT * FROM cameras WHERE id=?", (cid,))[0] for k, cid in cams.items()}
    for key, new in ((RIGHT, "192.168.0.43"), (LEFT, "192.168.0.69")):
        world = {"client": client, "db": db_path, "credential": credential, "camera_id": cams[key]}
        assert _rediscover(world, [RIGHT, LEFT])["found"] is True
        job_id = _reconnect(world, username="admin", password="pw").json()["job_id"]
        _appliance_confirms(world, job_id)
        moved = _appliance(world, "post", f"/api/appliance/AIC-SLOT1/cameras/{cams[key]}/media-uri",
                           json={"device_key": key, "rtsp_uri": f"rtsp://{new}:554{PATH}", "provisioning_job_id": job_id})
        assert moved.json()["updated"] is True, moved.text
    after = {k: _sql(db_path, "SELECT * FROM cameras WHERE id=?", (cid,))[0] for k, cid in cams.items()}
    assert after[RIGHT]["onvif_endpoint"] == f"rtsp://192.168.0.43:554{PATH}"
    assert after[LEFT]["onvif_endpoint"] == f"rtsp://192.168.0.69:554{PATH}"
    for key in cams:
        assert {k: v for k, v in after[key].items() if k != "onvif_endpoint"} == {k: v for k, v in before[key].items() if k != "onvif_endpoint"}
    assert _sql(db_path, "SELECT COUNT(*) AS n FROM cameras WHERE device_key IS NOT NULL")[0]["n"] == 2


def test_only_a_fresh_verified_credential_replaces_a_saved_one():
    """A reconnect's handoff (written moments ago) replaces the saved login;
    a stale or unparseable pending row never does (see test_edge_camera_sync:
    test_existing_local_credential_is_never_overwritten_by_a_stale_pending_row)."""
    fresh = edge_camera_sync._fresh_pending_credential
    assert fresh(datetime.now().isoformat()) is True
    assert fresh((datetime.now() - timedelta(seconds=edge_camera_sync.PENDING_CREDENTIAL_REPLACE_SECONDS + 5)).isoformat()) is False
    assert fresh("2026-09-12T05:50:00") is False
    assert fresh((datetime.now() + timedelta(hours=1)).isoformat()) is False  # clock nonsense is not "fresh"
    assert fresh(None) is False and fresh("not a time") is False
