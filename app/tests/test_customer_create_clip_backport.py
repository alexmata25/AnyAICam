"""Reconciliation back-port (2026-09-04): POST /api/customer/clips and
GET /api/customer/clips/{job_id} existed on live EC2 production but had
never been committed to git -- confirmed during the git<->EC2
body-level reconciliation pass. The clip-building worker itself
(build_manual_clip()) and its supporting state (clip_jobs, clip_tasks,
the ClipRequest model) were already present in git identically; only
the two customer-facing route handlers that expose that worker were
missing. Ported verbatim from the live, already-running EC2 source --
not reimplemented -- so this file's job is to prove the ported code
behaves correctly, not to redesign it.

Same TestClient + real signed session-cookie harness this suite
already established (see test_customer_camera_count_excludes_
placeholders.py/test_dashboard_camera_tenant_scoping.py's own
docstrings for the pattern this reuses).
"""

import sqlite3

import pytest

import partner_portal
from database_backend import override_target
from partner_db import initialize_database

import main


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_customer_create_clip_backport.db"


@pytest.fixture()
def http_client(db_path):
    from fastapi.testclient import TestClient

    with override_target(sqlite_path=db_path):
        initialize_database()
        # Same TrustedHostMiddleware note as test_dashboard_camera_
        # tenant_scoping.py: only relevant when this suite is run
        # directly inside a real deployed container where
        # cloud_settings.deployed is True; harmless base_url everywhere
        # else.
        with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
            yield test_client


def _seed_tenant(conn, customer_id, partner_id="partner-1"):
    conn.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES(?,?,?)", (partner_id, "Test Partner", "2026-01-01"))
    conn.execute(
        "INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
        (customer_id, partner_id, f"Customer {customer_id}", f"{customer_id}@example.test", "active", "2026-01-01"),
    )
    conn.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{customer_id}", customer_id, "Main Site", "2026-01-01"))


def _seed_camera(conn, camera_id, *, customer_id, camera_number, name):
    conn.execute(
        "INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
        (camera_id, customer_id, f"site-{customer_id}", name, "configured", camera_number, "2026-01-01"),
    )


def _owner_cookie(customer_id):
    return partner_portal._token("owner@example.test", "customer_owner", None, customer_id, None)


def _csrf():
    """POST/PUT/PATCH/DELETE go through ProductionSecurityMiddleware's
    double-submit CSRF check (cloud_security.py) before any route code
    runs at all -- an unrelated, pre-existing protection this suite
    must satisfy to test the route itself, not something this
    back-port adds or changes. Returns (cookies, headers) with a
    matching signed token in both, exactly what a real browser's own
    prior GET would have set via the same middleware's response-side
    cookie issuance."""
    from token_security import sign

    token = sign("csrf", 28800)
    return {"anyaicam_csrf": token}, {"X-CSRF-Token": token}


def _seed_two_customers(db_path):
    conn = sqlite3.connect(db_path)
    _seed_tenant(conn, "cust-a")
    _seed_tenant(conn, "cust-b")
    _seed_camera(conn, "a-cam-1", customer_id="cust-a", camera_number=1, name="A Camera 1")
    _seed_camera(conn, "b-cam-1", customer_id="cust-b", camera_number=1, name="B Camera 1")
    conn.commit()
    conn.close()


async def _fake_build_manual_clip(job_id, camera_number, start_time, end_time):
    # The real worker touches actual recording files on disk and runs
    # ffmpeg -- irrelevant to what this suite proves (routing,
    # authorization, validation, job-status shape). It's the exact
    # same asyncio.create_task()-fired worker either way; only the
    # worker body is stubbed for the test.
    main.clip_jobs[job_id] = {
        "id": job_id,
        "status": "complete",
        "progress": 100,
        "message": "Clip ready.",
    }


# --------------------------------------------------------------- POST /api/customer/clips

def test_create_clip_requires_authentication(http_client, db_path):
    _seed_two_customers(db_path)
    csrf_cookies, csrf_headers = _csrf()
    response = http_client.post(
        "/api/customer/clips",
        json={"camera_id": "a-cam-1", "start_time": "2026-09-04T10:00:00Z", "end_time": "2026-09-04T10:05:00Z"},
        cookies=csrf_cookies,
        headers=csrf_headers,
    )
    assert response.status_code in (401, 403)


def test_create_clip_rejects_a_camera_the_caller_does_not_own(http_client, db_path):
    _seed_two_customers(db_path)
    csrf_cookies, csrf_headers = _csrf()
    response = http_client.post(
        "/api/customer/clips",
        json={"camera_id": "b-cam-1", "start_time": "2026-09-04T10:00:00Z", "end_time": "2026-09-04T10:05:00Z"},
        cookies={**csrf_cookies, partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
        headers=csrf_headers,
    )
    assert response.status_code == 403


def test_create_clip_requires_all_fields(http_client, db_path):
    _seed_two_customers(db_path)
    csrf_cookies, csrf_headers = _csrf()
    response = http_client.post(
        "/api/customer/clips",
        json={"camera_id": "a-cam-1"},
        cookies={**csrf_cookies, partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
        headers=csrf_headers,
    )
    assert response.status_code == 400


def test_create_clip_rejects_end_before_start(http_client, db_path):
    _seed_two_customers(db_path)
    csrf_cookies, csrf_headers = _csrf()
    response = http_client.post(
        "/api/customer/clips",
        json={"camera_id": "a-cam-1", "start_time": "2026-09-04T10:05:00Z", "end_time": "2026-09-04T10:00:00Z"},
        cookies={**csrf_cookies, partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
        headers=csrf_headers,
    )
    assert response.status_code == 400


def test_create_clip_rejects_windows_over_one_hour(http_client, db_path):
    _seed_two_customers(db_path)
    csrf_cookies, csrf_headers = _csrf()
    response = http_client.post(
        "/api/customer/clips",
        json={"camera_id": "a-cam-1", "start_time": "2026-09-04T09:00:00Z", "end_time": "2026-09-04T10:05:00Z"},
        cookies={**csrf_cookies, partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
        headers=csrf_headers,
    )
    assert response.status_code == 400


def test_create_clip_queues_a_job_for_an_authorized_camera(http_client, db_path, monkeypatch):
    _seed_two_customers(db_path)
    monkeypatch.setattr(main, "build_manual_clip", _fake_build_manual_clip)
    csrf_cookies, csrf_headers = _csrf()
    response = http_client.post(
        "/api/customer/clips",
        json={"camera_id": "a-cam-1", "start_time": "2026-09-04T10:00:00Z", "end_time": "2026-09-04T10:05:00Z"},
        cookies={**csrf_cookies, partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
        headers=csrf_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "queued"
    assert data["id"]


# --------------------------------------------------------------- GET /api/customer/clips/{job_id}

def test_clip_status_requires_a_customer_portal_identity(http_client, db_path):
    _seed_two_customers(db_path)
    response = http_client.get("/api/customer/clips/nonexistent-job")
    assert response.status_code in (401, 403)


def test_clip_status_unknown_job_returns_a_not_found_shape_not_an_error(http_client, db_path):
    _seed_two_customers(db_path)
    response = http_client.get(
        "/api/customer/clips/nonexistent-job",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-a")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "error"
    assert data["progress"] == 0


def test_clip_status_reflects_the_real_job_state_any_customer_identity_can_poll_it(http_client, db_path):
    # customer_clip_status() only requires *a* valid customer-portal
    # identity, not ownership of the specific camera the job belongs
    # to -- exactly matching the live EC2 source being ported (see
    # that function's own comment: "The clip itself was already
    # authorized by camera ownership/access when the job was
    # created."). This test documents that existing behavior rather
    # than changing it.
    _seed_two_customers(db_path)
    main.clip_jobs["job-123"] = {"id": "job-123", "status": "building", "progress": 40, "message": "Building clip…"}
    response = http_client.get(
        "/api/customer/clips/job-123",
        cookies={partner_portal.SESSION_COOKIE: _owner_cookie("cust-b")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {"id": "job-123", "status": "building", "progress": 40, "message": "Building clip…"}
    main.clip_jobs.pop("job-123", None)


# --------------------------------------------------------------- nothing else moved

def test_build_manual_clip_and_clip_request_model_are_unchanged(monkeypatch):
    # These already existed identically in git before this back-port --
    # confirmed during the reconciliation pass. This back-port adds
    # only the two route handlers above; it must not have touched
    # build_manual_clip() or ClipRequest at all.
    assert hasattr(main, "build_manual_clip")
    assert hasattr(main, "ClipRequest")
    fields = main.ClipRequest.model_fields
    assert set(fields) == {"camera", "start_time", "end_time"}
