"""Notification fan-out recovers from partial failure (2026-10-02, Codex
finding on daca1d7).

The event-level dedupe claim was committed before any recipient's
notification existed, so a failure after the claim -- or after only some
recipients -- left the event "already processed" and the missing people were
never notified. Fan-out is now idempotent per recipient (UNIQUE(user_id,
dedupe_key), created in the same transaction as that person's bookkeeping),
both routes re-run it on replay, and a failed fan-out is answered 503 so the
appliance retries.
"""
import notification_engine
from test_notification_idempotency_and_lpr import (  # noqa: F401 -- fixtures and helpers
    _analytics,
    _legacy,
    _notifications,
    cloud,
)


def _fail_once(monkeypatch, name, on_call):
    real = getattr(notification_engine, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == on_call:
            raise RuntimeError("injected failure")
        return real(*args, **kwargs)
    monkeypatch.setattr(notification_engine, name, flaky)


def _per_person():
    people = {}
    for user_id, _event_type in _notifications():
        people[user_id] = people.get(user_id, 0) + 1
    return people


def test_a_failure_before_any_recipient_is_recovered_by_the_retry(cloud, monkeypatch):
    _fail_once(monkeypatch, "_quiet_hours_clock", 1)        # right where the old event-level claim had been committed
    assert _analytics(cloud, "evt-a").status_code == 503    # stored; the appliance is asked to retry
    assert _notifications() == []
    assert _analytics(cloud, "evt-a").json()["status"] == "duplicate"  # the retry
    assert _per_person() == {"owner-1": 1, "member-1": 1}


def test_a_failure_after_the_first_recipient_fills_in_the_rest_on_replay(cloud, monkeypatch):
    _fail_once(monkeypatch, "_enqueue_mobile_push", 2)       # the second person's transaction fails
    assert _analytics(cloud, "evt-b").status_code == 503
    assert sum(_per_person().values()) == 1
    assert _analytics(cloud, "evt-b").status_code == 200    # analytics-route replay
    assert _per_person() == {"owner-1": 1, "member-1": 1}
    assert _analytics(cloud, "evt-b").status_code == 200    # further replays change nothing
    assert _per_person() == {"owner-1": 1, "member-1": 1}


def test_the_legacy_route_replay_also_recovers_and_converges(cloud, monkeypatch):
    _fail_once(monkeypatch, "_enqueue_mobile_push", 2)
    assert _legacy(cloud, "evt-c").status_code == 503
    assert sum(_per_person().values()) == 1
    assert _legacy(cloud, "evt-c").status_code == 200       # legacy replay (appliance_events says duplicate)
    assert _per_person() == {"owner-1": 1, "member-1": 1}
    assert _analytics(cloud, "evt-c").status_code == 200    # the same event through the other route
    assert _per_person() == {"owner-1": 1, "member-1": 1}


def test_every_authorized_household_member_is_independent(cloud):
    from partner_db import connection
    with connection() as db:
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,camera_access_mode,created_at) "
                   "VALUES('member-2','p1','member-2@example.test','m2','customer_viewer','x',1,'cust-1','active','all','2026-10-02')")
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,camera_access_mode,created_at) "
                   "VALUES('member-3','p1','member-3@example.test','m3','customer_viewer','x',1,'cust-1','active','selected','2026-10-02')")
    _legacy(cloud, "evt-d")
    _analytics(cloud, "evt-d")
    assert _per_person() == {"owner-1": 1, "member-1": 1, "member-2": 1}  # member-3 has no camera access


def test_another_customers_identical_event_id_is_not_suppressed(cloud):
    import secrets
    import time
    from partner_db import connection, password_hash
    with connection() as db:
        db.execute("INSERT INTO customers(id,partner_id,name,email,status,source,created_at) VALUES('cust-2','p1','C2','c2@example.test','active','real','2026-10-02')")
        db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site-2','cust-2','Home','2026-10-02')")
        db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-2','cust-2','site-2','AIC-2','2026-10-02')")
        db.execute("INSERT INTO appliance_credentials(id,appliance_id,credential_hash,created_at) VALUES('c2','appl-2',?,'2026-10-02')", (password_hash("cred-2"),))
        db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,name,created_at) VALUES('cam-2','cust-2','site-2','appl-2','Gate','2026-10-02')")
        db.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,account_status,camera_access_mode,created_at) "
                   "VALUES('owner-2','p1','owner-2@example.test','o2','customer_owner','x',1,'cust-2','active','all','2026-10-02')")
    _analytics(cloud, "same-local-id")
    headers = {"X-Appliance-Id": "appl-2", "X-Request-Timestamp": str(int(time.time())),
               "X-Request-Nonce": secrets.token_hex(16), "Authorization": "Bearer cred-2"}
    response = cloud.post("/api/appliance/analytics/cam-2/events", headers=headers, json={
        "local_event_id": "same-local-id", "event_type": "person", "confidence": 0.9, "object_count": 1,
        "detections": [], "event_timestamp": "2026-10-02T12:00:00"})
    assert response.status_code == 200
    assert _per_person() == {"owner-1": 1, "member-1": 1, "owner-2": 1}
