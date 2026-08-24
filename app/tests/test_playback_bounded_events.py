"""Bounded Playback events: _customer_camera_events()/GET /api/customer/
events/{camera_id} -- the same bounded-load fix as recordings, for a
second dataset. The timeline used to embed a customer's entire
detection_events history for every camera (12,000+ rows measured for
one real account) to plot a single 24h axis. This queries exactly one
camera and one calendar date, and returns only the two fields the
timeline's own JS reads (event_type, timestamp).

Direct function calls for the query layer; the route itself is called
directly too (see test_playback_bounded_load.py's own note on why
TestClient is unreliable in this production container specifically --
not a new constraint, same one that file already documents).
"""

import sqlite3
from types import SimpleNamespace

import pytest

import main
from database_backend import override_target
from partner_db import initialize_database


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_bounded_events.db"


def _seed_base_tenant(conn):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','Test Co','test@example.com','active','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-TEST','2026-01-01')")
    conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-1','cust-1','site-1','appl-1',1,'Front Door','2026-01-01')")
    conn.commit()


def _seed_event(conn, event_id, camera_id, event_type, timestamp):
    conn.execute(
        "INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, "cust-1", "site-1", "appl-1", camera_id, event_id, event_type, timestamp, timestamp),
    )
    conn.commit()


def test_only_the_requested_date_is_returned(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_event(conn, "ev-1", "cam-1", "person", "2026-08-20T10:00:00")
        _seed_event(conn, "ev-2", "cam-1", "car", "2026-08-21T10:00:00")
        result = main._customer_camera_events("cam-1", "2026-08-20")
    assert result == [{"event_type": "person", "timestamp": "2026-08-20T10:00:00"}]


def test_only_the_requested_camera_is_returned(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        conn.execute("INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES('cam-2','cust-1','site-1','appl-1',2,'Other','2026-01-01')")
        conn.commit()
        _seed_event(conn, "ev-1", "cam-1", "person", "2026-08-20T10:00:00")
        _seed_event(conn, "ev-2", "cam-2", "car", "2026-08-20T10:05:00")
        result = main._customer_camera_events("cam-1", "2026-08-20")
    assert len(result) == 1
    assert result[0]["event_type"] == "person"


def test_returns_only_event_type_and_timestamp(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_event(conn, "ev-1", "cam-1", "truck", "2026-08-20T10:00:00")
        result = main._customer_camera_events("cam-1", "2026-08-20")
    assert set(result[0].keys()) == {"event_type", "timestamp"}


def test_empty_date_returns_empty_list(db_path):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        _seed_event(conn, "ev-1", "cam-1", "person", "2026-08-20T10:00:00")
        result = main._customer_camera_events("cam-1", "2026-09-01")
    assert result == []


def _fake_request(date=None):
    return SimpleNamespace(query_params=SimpleNamespace(get=lambda key, default=None: date if key == "date" else default))


def test_route_defaults_to_today_and_requires_authorization(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        result = main.customer_camera_events("cam-1", _fake_request())
    assert result == {"events": []}  # no seeded events for today -- proves it ran without error, not a stub


def test_route_rejects_unauthorized_camera(db_path, monkeypatch):
    with override_target(sqlite_path=db_path):
        initialize_database()
        conn = sqlite3.connect(db_path)
        _seed_base_tenant(conn)
        monkeypatch.setattr(main, "_customer_playback_cameras", lambda request: [{"id": "cam-1", "name": "Front Door", "camera_number": 1}])
        with pytest.raises(Exception) as excinfo:
            main.customer_camera_events("cam-2-does-not-belong", _fake_request())
    assert getattr(excinfo.value, "status_code", None) == 403
