"""Hybrid cloud EVENT retention is enforced for billing v2 accounts (2026-10-05,
after Codex review of 453bd1c).

The retention sweeper read only the legacy plans table, which a billing v2
Hybrid account does not have, so its event clips were never expired. Event
media now follows the customer's cloud-event retention: an explicit RDM
override (7/14/30), else the v2 entitlement's 14 days (also after the plan
ends), else the legacy plan as before. Continuous recordings are unchanged.
The sweeper stays behind its own off-by-default flag and delete-only role.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from database_backend import override_target
from partner_db import initialize_database
from test_recording_retention_sweep import _seed_detection_event, _seed_event_media, _seed_plan, _seed_recording, _seed_tenant

import recording_retention_sweep as rrs

NOW = datetime(2026, 10, 30, 12, 0, 0)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "hybrid_retention.db"


@pytest.fixture()
def conn(db_path, monkeypatch):
    import per_camera_billing as billing
    import stripe_state
    for plan_key in billing.PLANS:
        monkeypatch.setenv(billing.PRICE_ENV[plan_key], f"price_r_{plan_key}")
    monkeypatch.setattr(stripe_state, "subscription_payment_reversal", lambda _s: None)
    with override_target(sqlite_path=db_path):
        initialize_database()
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        _seed_tenant(connection, "cust-h", "h")
        connection.commit()
        yield connection
        connection.close()


def _v2(conn, plan_key, status="active"):
    import per_camera_billing as billing
    billing._upsert_current({"id": "sub_r", "customer": "cus_r", "status": "active", "metadata": {"anyaicam_customer_id": "cust-h"},
                             "items": {"data": [{"id": "si", "quantity": 2, "price": {"id": f"price_r_{plan_key}"}}]}})
    if status != "active":
        conn.execute("UPDATE camera_plan_entitlements_v2 SET status=? WHERE customer_id='cust-h'", (status,))
        conn.commit()


def _clip(conn, media_id, days_old):
    _seed_detection_event(conn, f"evt-{media_id}", "cust-h", "site-h", "appl-h", "cam-h", "motion", f"local-{media_id}")
    _seed_event_media(conn, media_id, f"evt-{media_id}", "cust-h", "cam-h", (NOW - timedelta(days=days_old)).isoformat())
    conn.commit()


def _expired(conn):
    return {c["id"] for c in rrs._expired_candidates(conn, NOW)}


def test_hybrid_event_clips_expire_after_14_days(conn):
    _v2(conn, "hybrid")
    _clip(conn, "old", 15)
    _clip(conn, "new", 13)
    assert _expired(conn) == {"old"}


def test_an_rdm_override_still_wins(conn):
    _v2(conn, "hybrid")
    conn.execute("INSERT INTO customer_cloud_policy(customer_id,daily_cloud_seconds,retention_days,updated_at,updated_by) "
                 "VALUES('cust-h',NULL,7,'2026-10-01','ops@example.test')")
    conn.commit()
    _clip(conn, "eight", 8)
    _clip(conn, "six", 6)
    assert _expired(conn) == {"eight"}


@pytest.mark.parametrize("status", ["cancelled", "suspended"])
def test_media_of_an_ended_or_paused_hybrid_plan_still_expires(conn, status):
    _v2(conn, "hybrid", status=status)
    _clip(conn, "old", 20)
    assert _expired(conn) == {"old"}


def test_a_plan_without_cloud_events_falls_back_to_the_legacy_rule(conn):
    _v2(conn, "ai_local")
    _clip(conn, "stray", 40)
    assert _expired(conn) == set()  # no cloud-event retention and no legacy plan: never guessed


def test_continuous_recordings_still_follow_only_the_legacy_plan(conn):
    _v2(conn, "hybrid")
    _seed_recording(conn, "rec-1", "cust-h", "site-h", "appl-h", "cam-h", (NOW - timedelta(days=30)).isoformat())
    conn.commit()
    assert "rec-1" not in _expired(conn)
    _seed_plan(conn, "cust-h", retention_days=7)
    conn.commit()
    assert "rec-1" in _expired(conn)


def test_legacy_event_media_is_unchanged(conn):
    _seed_plan(conn, "cust-h", retention_days=2)
    conn.commit()
    _clip(conn, "legacy", 3)
    assert _expired(conn) == {"legacy"}


# ------------------------------------------------------------ host readiness (no AWS call)

def test_readiness_lists_everything_a_cloud_host_still_needs(monkeypatch):
    monkeypatch.setattr(rrs, "RUNTIME_ROLE", "edge")
    monkeypatch.setattr(rrs, "RETENTION_SWEEP_ENABLED", False)
    for name in ("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "ANYAICAM_RECORDING_S3_BUCKET", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    result = rrs.readiness()
    assert result["ready"] is False
    joined = " ".join(result["missing"])
    for needed in ("ANYAICAM_RUNTIME_ROLE", "ANYAICAM_RECORDING_RETENTION_SWEEP_ENABLED=true", "ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN",
                   "ANYAICAM_RECORDING_S3_BUCKET", "AWS_REGION"):
        assert needed in joined


def test_readiness_is_satisfied_by_complete_configuration(monkeypatch):
    pytest.importorskip("boto3")
    monkeypatch.setattr(rrs, "RUNTIME_ROLE", "cloud")
    monkeypatch.setattr(rrs, "RETENTION_SWEEP_ENABLED", True)
    monkeypatch.setenv("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "arn:aws:iam::880690594006:role/anyaicam-recording-lifecycle")
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "example-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert rrs.readiness() == {"ready": True, "missing": []}


def test_a_malformed_role_arn_is_not_accepted(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_LIFECYCLE_ROLE_ARN", "anyaicam-recording-lifecycle")
    assert any("ROLE_ARN" in item for item in rrs.readiness()["missing"])
