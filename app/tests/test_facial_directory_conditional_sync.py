"""Conditional facial-directory sync (2026-09-24).

GET /api/appliance/facial-directory used to return every enrolled
embedding (~28.5 KB of JSON each) to the edge every 5 minutes whether or
not anything changed, and the edge deleted and re-inserted its local
copy every time. Now the cloud returns a directory_version (content hash
of the customer's snapshot); the edge sends back the version it applied
and gets a tiny "unchanged" reply when nothing changed.

Covers correctness (additions, updates, removals, tenant isolation,
safety resync, offline), plus a measured bandwidth comparison. Software
only: no physical unlock, no AWS inference, no device involved.
"""
import json
import random
import secrets
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_facial_directory_conditional_sync_import.db"):
    import appliance_cloud
    import facial_embedding_sync as fes
    import facial_people
    from partner_db import connection, initialize_database, password_hash

NOW = "2026-09-24T00:00:00"


def _headers(appliance_id="appl-1", credential="cred-1"):
    return {
        "X-Appliance-Id": appliance_id,
        "X-Request-Timestamp": str(int(time.time())),
        "X-Request-Nonce": secrets.token_hex(16),
        "Authorization": f"Bearer {credential}",
    }


def _seed_tenant(db, suffix):
    db.execute("INSERT OR IGNORE INTO partners(id,name,approval_status,source,created_at) VALUES('p1','P','approved','real',?)", (NOW,))
    db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES(?,?,?,?,?,?,?)", (f"cust-{suffix}", "p1", "C", f"{suffix}@example.test", "active", "real", NOW))
    db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{suffix}", f"cust-{suffix}", "Site", NOW))
    db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", (f"appl-{suffix}", f"cust-{suffix}", f"site-{suffix}", f"AIC-{suffix}", NOW))
    db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES(?,?,?,?)", (f"c-{suffix}", f"appl-{suffix}", password_hash(f"cred-{suffix}"), NOW))


def _vector(seed):
    rng = random.Random(seed)
    return tuple(rng.uniform(-1, 1) for _ in range(512))  # realistic SFace-sized embedding


@pytest.fixture()
def cloud_db(tmp_path):
    path = tmp_path / "cloud.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            _seed_tenant(db, "1")
            _seed_tenant(db, "2")
    return path


@pytest.fixture()
def client(cloud_db, monkeypatch):
    monkeypatch.setattr(appliance_cloud, "FACIAL_EMBEDDING_SYNC_ENABLED", True)
    with override_target(sqlite_path=str(cloud_db)):
        app = FastAPI()
        appliance_cloud.register_appliance_cloud_routes(app, shell=lambda *a, **k: "")
        with TestClient(app) as test_client:
            yield test_client


def _enroll(cloud_db, customer_id, name, *, faces=1, seed=0):
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            person_id = facial_people.enroll_person(db, customer_id=customer_id, display_name=name, now=NOW)
            for index in range(faces):
                facial_people.add_reference_image(db, customer_id=customer_id, person_id=person_id, embedding=_vector(seed * 100 + index), engine="onnx_yunet_sface", engine_version="1", now=NOW)
    return person_id


def _directory(client, cloud_db, *, if_version=None, appliance="1"):
    path = "/api/appliance/facial-directory" + (f"?if_version={if_version}" if if_version else "")
    with override_target(sqlite_path=str(cloud_db)):
        response = client.get(path, headers=_headers(f"appl-{appliance}", f"cred-{appliance}"))
    assert response.status_code == 200
    return response


# ------------------------------------------------------------------ cloud


def test_the_directory_carries_a_deterministic_version(client, cloud_db):
    _enroll(cloud_db, "cust-1", "Alice", faces=2, seed=1)
    first = _directory(client, cloud_db).json()
    second = _directory(client, cloud_db).json()
    assert first["directory_version"] and first["directory_version"] == second["directory_version"]
    assert len(first["embeddings"]) == 2


def test_a_matching_version_gets_an_unchanged_reply_with_no_embeddings(client, cloud_db):
    _enroll(cloud_db, "cust-1", "Alice", faces=3, seed=1)
    version = _directory(client, cloud_db).json()["directory_version"]
    body = _directory(client, cloud_db, if_version=version).json()
    assert body == {"customer_id": "cust-1", "directory_version": version, "unchanged": True}


@pytest.mark.parametrize("change", ["add_person", "add_face", "rename", "remove_person", "watchlist"])
def test_every_kind_of_change_produces_a_new_version_and_the_full_directory(client, cloud_db, change):
    alice = _enroll(cloud_db, "cust-1", "Alice", faces=1, seed=1)
    version = _directory(client, cloud_db).json()["directory_version"]
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            if change == "add_person":
                pass
            elif change == "add_face":
                facial_people.add_reference_image(db, customer_id="cust-1", person_id=alice, embedding=_vector(99), engine="onnx_yunet_sface", engine_version="1", now=NOW)
            elif change == "rename":
                db.execute("UPDATE facial_people SET display_name='Alice B' WHERE id=?", (alice,))
            elif change == "remove_person":
                facial_people.delete_person(db, customer_id="cust-1", person_id=alice)
            elif change == "watchlist":
                watchlist = facial_people.create_watchlist(db, customer_id="cust-1", name="VIP", now=NOW)
                facial_people.add_watchlist_member(db, customer_id="cust-1", watchlist_id=watchlist, person_id=alice, now=NOW)
    if change == "add_person":
        _enroll(cloud_db, "cust-1", "Bob", faces=1, seed=2)
    body = _directory(client, cloud_db, if_version=version).json()
    assert body.get("unchanged") is not True
    assert body["directory_version"] != version
    assert "people" in body and "embeddings" in body


def test_another_tenants_version_never_yields_unchanged_or_leaks_data(client, cloud_db):
    _enroll(cloud_db, "cust-1", "Alice", seed=1)
    _enroll(cloud_db, "cust-2", "Mallory", seed=2)
    cust1_version = _directory(client, cloud_db, appliance="1").json()["directory_version"]
    body = _directory(client, cloud_db, if_version=cust1_version, appliance="2").json()
    assert body.get("unchanged") is not True
    assert body["customer_id"] == "cust-2"
    assert [p["display_name"] for p in body["people"]] == ["Mallory"]


def test_a_garbage_version_gets_the_full_directory(client, cloud_db):
    _enroll(cloud_db, "cust-1", "Alice", seed=1)
    body = _directory(client, cloud_db, if_version="not-a-real-version").json()
    assert body.get("unchanged") is not True and len(body["people"]) == 1


# ------------------------------------------------------------------- edge


@pytest.fixture()
def edge_db(tmp_path, monkeypatch):
    path = tmp_path / "edge.db"
    with override_target(sqlite_path=str(path)):
        initialize_database()
        with connection() as db:
            _seed_tenant(db, "1")  # edge_camera_sync materializes this locally in production
    credential_file = tmp_path / "credential.json"
    credential_file.write_text(json.dumps({"appliance_id": "appl-1", "credential": "cred-1"}), encoding="utf-8")
    monkeypatch.setattr(fes, "CREDENTIAL_FILE", credential_file)
    return path


def _route_edge_to_cloud(monkeypatch, client, cloud_db, log):
    def fetch(path):
        with override_target(sqlite_path=str(cloud_db)):
            response = client.get(path, headers=_headers())
        log.append((path, len(response.content)))
        return response.json() if response.status_code == 200 else None
    monkeypatch.setattr(fes, "_control_plane_get", fetch)


def _sync(edge_db):
    with override_target(sqlite_path=str(edge_db)):
        return fes.sync_facial_directory()


def _local_people(edge_db):
    with override_target(sqlite_path=str(edge_db)):
        with connection() as db:
            return sorted(row["display_name"] for row in db.execute("SELECT display_name FROM facial_people").fetchall())


def test_edge_skips_the_transfer_and_the_local_rewrite_when_nothing_changed(client, cloud_db, edge_db, monkeypatch):
    _enroll(cloud_db, "cust-1", "Alice", faces=2, seed=1)
    log = []
    _route_edge_to_cloud(monkeypatch, client, cloud_db, log)
    assert _sync(edge_db)["status"] == "synced"
    rewrites = []
    real_replace = fes._replace_local_directory
    monkeypatch.setattr(fes, "_replace_local_directory", lambda *a, **k: rewrites.append(1) or real_replace(*a, **k))
    assert _sync(edge_db) == {"status": "unchanged"}
    assert rewrites == []
    assert "if_version=" not in log[0][0] and "if_version=" in log[1][0]
    assert _local_people(edge_db) == ["Alice"]  # recognition data untouched


def test_edge_applies_additions_updates_and_removals(client, cloud_db, edge_db, monkeypatch):
    alice = _enroll(cloud_db, "cust-1", "Alice", seed=1)
    _route_edge_to_cloud(monkeypatch, client, cloud_db, [])
    _sync(edge_db)
    _enroll(cloud_db, "cust-1", "Bob", seed=2)
    assert _sync(edge_db)["status"] == "synced" and _local_people(edge_db) == ["Alice", "Bob"]
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            db.execute("UPDATE facial_people SET display_name='Alice B' WHERE id=?", (alice,))
    assert _sync(edge_db)["status"] == "synced" and _local_people(edge_db) == ["Alice B", "Bob"]
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            facial_people.delete_person(db, customer_id="cust-1", person_id=alice)
    assert _sync(edge_db)["status"] == "synced" and _local_people(edge_db) == ["Bob"]
    assert _sync(edge_db)["status"] == "unchanged"


def test_a_mismatched_unchanged_reply_is_not_trusted_and_forces_a_full_sync(edge_db, monkeypatch):
    replies = iter([
        {"customer_id": "cust-1", "directory_version": "v1", "people": [], "embeddings": [], "watchlists": [], "watchlist_members": []},
        {"customer_id": "cust-9", "directory_version": "v1", "unchanged": True},
    ])
    paths = []
    monkeypatch.setattr(fes, "_control_plane_get", lambda path: paths.append(path) or next(replies))
    assert _sync(edge_db)["status"] == "synced"
    assert _sync(edge_db)["status"] == "fetch_failed"
    monkeypatch.setattr(fes, "_control_plane_get", lambda path: paths.append(path) or None)
    _sync(edge_db)
    assert "if_version=" not in paths[-1]  # version was dropped


def test_an_older_cloud_without_versions_keeps_the_original_full_sync(edge_db, monkeypatch):
    paths = []
    monkeypatch.setattr(fes, "_control_plane_get", lambda path: paths.append(path) or {"customer_id": "cust-1", "people": [], "embeddings": [], "watchlists": [], "watchlist_members": []})
    _sync(edge_db)
    _sync(edge_db)
    assert all("if_version=" not in path for path in paths)


def test_a_full_resync_still_happens_after_the_safety_interval(edge_db, monkeypatch):
    paths = []
    monkeypatch.setattr(fes, "_control_plane_get", lambda path: paths.append(path) or {"customer_id": "cust-1", "directory_version": "v1", "people": [], "embeddings": [], "watchlists": [], "watchlist_members": []})
    _sync(edge_db)
    _sync(edge_db)
    with fes._state_lock:
        fes._applied_directory["applied_at"] -= fes.FULL_RESYNC_SECONDS + 1
    _sync(edge_db)
    assert ["if_version=" in path for path in paths] == [False, True, False]


def test_offline_the_local_directory_is_kept_and_recognition_data_survives(client, cloud_db, edge_db, monkeypatch):
    _enroll(cloud_db, "cust-1", "Alice", seed=1)
    _route_edge_to_cloud(monkeypatch, client, cloud_db, [])
    _sync(edge_db)
    monkeypatch.setattr(fes, "_control_plane_get", lambda path: None)
    assert _sync(edge_db) == {"status": "fetch_failed"}
    assert _local_people(edge_db) == ["Alice"]


def test_bandwidth_for_an_unchanged_directory_drops_by_over_99_percent(client, cloud_db, edge_db, monkeypatch):
    """10 people x 7 reference faces (512-d vectors) -- a typical home."""
    for index in range(10):
        _enroll(cloud_db, "cust-1", f"Person {index}", faces=7, seed=index)
    log = []
    _route_edge_to_cloud(monkeypatch, client, cloud_db, log)
    _sync(edge_db)
    for _poll in range(5):
        assert _sync(edge_db)["status"] == "unchanged"
    full_bytes = log[0][1]
    unchanged_bytes = max(size for _path, size in log[1:])
    assert full_bytes > 500_000
    assert unchanged_bytes < full_bytes * 0.01
