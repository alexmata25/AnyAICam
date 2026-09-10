from datetime import datetime

import event_media_outbox as outbox
import event_media_uploader as uploader
import recording_retention_sweep as sweep
from database_backend import override_target
from partner_db import connection, initialize_database


def test_outbox_is_atomic_and_deduplicates_event_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_FILE", tmp_path / "outbox.json")
    outbox.put({"event_id": "evt-1", "camera_number": 1})
    outbox.put({"event_id": "evt-1", "camera_number": 2})
    assert outbox.load() == [{"event_id": "evt-1", "camera_number": 2}]
    outbox.remove("evt-1")
    assert outbox.load() == []


def test_retry_keeps_failed_job_and_removes_completed_job(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_FILE", tmp_path / "outbox.json")
    job = {"event_id": "evt-1", "camera_number": 1, "event_start": "2026-01-01T00:00:00", "event_end": "2026-01-01T00:00:01", "clip_url": "/recordings/a.mp4", "thumbnail_url": None}
    outbox.put(job)
    monkeypatch.setattr(uploader, "upload_motion_event_media", lambda **_: False)
    assert uploader.retry_pending_event_media() == {"attempted": 1, "completed": 0, "pending": 1}
    monkeypatch.setattr(uploader, "upload_motion_event_media", lambda **kwargs: outbox.remove(kwargs["event_id"]) or True)
    assert uploader.retry_pending_event_media()["completed"] == 1
    assert outbox.load() == []


def test_retention_candidate_includes_event_media_with_the_customer_plan(tmp_path):
    database = tmp_path / "retention.db"
    with override_target(sqlite_path=str(database)):
        initialize_database()
        with connection() as db:
            now = "2026-01-01T00:00:00"
            db.execute("INSERT INTO partners(id,name,approval_status,source,created_at) VALUES(?,?,?,?,?)", ("partner", "P", "approved", "real", now))
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)", ("cust", "partner", "C", "c@example.test", "active", now))
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", ("site", "cust", "S", now))
            db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,?,?,?,?)", ("appliance", "cust", "site", "AIC-T", now))
            db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES(?,?,?,?,?,?)", ("cam", "cust", "site", "appliance", "Cam", now))
            db.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?)", ("event", "cust", "site", "appliance", "cam", "local", "motion", now, now))
            db.execute("INSERT INTO plans(id,customer_id,retention_days,created_at) VALUES(?,?,?,?)", ("plan", "cust", 7, now))
            db.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?)", ("media", "event", "cust", "cam", "recordings/c", "2026-01-01T00:00:00", now, now))
            candidates = sweep._expired_candidates(db, datetime(2026, 1, 9))
    assert candidates == [{"id": "media", "customer_id": "cust", "s3_key": "recordings/c", "started_at": "2026-01-01T00:00:00", "kind": "event_media"}]
