"""/recordings serves recorded media only (2026-10-01 security fix).

Found during the staging installer E2E: any signed-in customer of any
account could fetch audit_log.jsonl, billing_invoices.json, the
email-preview mailbox, face crops, backups and object storage (including
the licensed installer) from the /recordings static mount by name."""
from pathlib import Path

import pytest

from recordings_guard import recordings_path_allowed
from test_pricing_ff_commission import OWNER, _cookie, db_path, portal  # noqa: F401 -- fixtures


@pytest.mark.parametrize("path", [
    "camera1/camera1_2026-10-01_19-01-55.mkv", "camera12/segment.ts", "camera3/live.m3u8",
    "clips/motion/motion_abc.mp4", "clips/clip.mp4", "media/snapshots/home/camera1/2026-10-01/a.jpg",
    "media/ai/2026/10/01/x.jpeg", "media/motion/2026/10/01/y.png", "camera1" + chr(92) + "x.mp4",
])
def test_recorded_media_is_allowed(path):
    assert recordings_path_allowed(path)


@pytest.mark.parametrize("path", [
    "audit_log.jsonl", "billing_invoices.json", "license_state.json", "analytics_events.json",
    "storage/downloads/vms-installer/latest.json",
    "storage/downloads/vms-installer/567b7f29f545/anyaicam-appliance-installer-1.1.0-vms-567b7f29f545.tar.gz",
    "email-preview/20261001-192958-685247.json", "aac_faces/p1/face.jpg", "backups/x.mp4", "db/staging.db",
    "partner_portal.db", "partner_portal-pre-2e1086246293-20261001T144520Z.db", "appliance_identity.json",
    "camera1/notes.json", "camera1/../audit_log.jsonl", "camera1/.hidden.mp4", "media/", "camera1", "",
    "_media_cache/a.mp4", "cameraX/a.mp4", "clips/motion/x.mp4.json",
])
def test_everything_else_is_refused(path):
    assert not recordings_path_allowed(path)


def _mount_dir():
    import main
    mount = next(r for r in main.app.routes if getattr(r, "path", "") == "/recordings")
    return Path(mount.app.directory)


def test_the_live_mount_refuses_data_files_but_serves_media(portal, db_path):
    client, _, _ = portal
    root = _mount_dir()
    try:
        (root / "camera97").mkdir(parents=True, exist_ok=True)
        media = root / "camera97" / "guard-test.mp4"
        media.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        data = root / "guard-test-secret.json"
        data.write_text('{"secret": true}', encoding="utf-8")
        (root / "storage" / "downloads").mkdir(parents=True, exist_ok=True)
        stored = root / "storage" / "downloads" / "guard-test.tar.gz"
        stored.write_bytes(b"installer")
    except OSError:
        pytest.skip("recordings directory is not writable here")
    try:
        cookies = _cookie(*OWNER)
        assert client.get("/recordings/camera97/guard-test.mp4", cookies=cookies).status_code == 200
        for refused in ("/recordings/guard-test-secret.json", "/recordings/storage/downloads/guard-test.tar.gz",
                        "/recordings/camera97/../guard-test-secret.json"):
            response = client.get(refused, cookies=cookies)
            assert response.status_code == 404, refused
            assert b"secret" not in response.content and b"installer" not in response.content
    finally:
        for path in (media, data, stored):
            path.unlink(missing_ok=True)
        for folder in (root / "camera97", root / "storage" / "downloads"):
            try:
                folder.rmdir()
            except OSError:
                pass


# ------------------------------------------------------------------ tenant isolation matrix
import sqlite3  # noqa: E402

from test_pricing_ff_commission import _seed  # noqa: E402

CUSTOMER_A = ("owner@example.test", "customer_owner", "cust-1")
CUSTOMER_B = ("b-owner@example.test", "customer_owner", "cust-2")
UNLICENSED = ("u-owner@example.test", "customer_owner", "cust-3")
SECRET = b"guard-secret-bytes"


@pytest.fixture()
def tenants(portal, db_path):
    client, _, _ = portal
    _seed(db_path)  # cust-1 (A): cam-1..cam-3
    conn = sqlite3.connect(db_path)
    for cid, email in (("cust-2", "b-owner@example.test"), ("cust-3", "u-owner@example.test")):
        conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                     (cid, "partner-1", cid, email, "active", "2026-01-01"))
        conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"site-{cid}", cid, "S", "2026-01-01"))
    conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,camera_number,created_at) VALUES('b-cam','cust-2','site-cust-2','B cam',1,'2026-01-01')")
    for event_id, customer, camera in (("evt-a", "cust-1", "cam-1"), ("evt-b", "cust-2", "b-cam")):
        conn.execute("INSERT INTO detection_events(id,customer_id,site_id,appliance_id,camera_id,local_event_id,event_type,confidence,"
                     "object_count,event_timestamp,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (event_id, customer, f"site-{customer}", "app", camera, event_id, "motion", 0.9, 1, "2026-10-01T10:00:00", "2026-10-01T10:00:00"))
        conn.execute("INSERT INTO detection_event_media(id,detection_event_id,customer_id,camera_id,s3_key,started_at,ended_at,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (f"m-{event_id}", event_id, customer, camera, f"{customer}/{event_id}.mp4",
                                                 "2026-10-01T10:00:00", "2026-10-01T10:00:10", "2026-10-01T10:00:00"))
    conn.commit(); conn.close()
    root = _mount_dir()
    created = []
    try:
        for rel, data in (("camera1/b-customer-clip.mp4", SECRET), ("clips/motion/motion_evt-b.mp4", SECRET),
                          ("media/snapshots/site-cust-2/camera1/2026-10-01/b.jpg", SECRET), ("guard-secret.json", SECRET)):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
                created.append(path)
    except OSError:
        pytest.skip("recordings directory is not writable here")
    yield client
    for path in created:
        path.unlink(missing_ok=True)


RAW_MEDIA = ["/recordings/camera1/b-customer-clip.mp4", "/recordings/clips/motion/motion_evt-b.mp4",
             "/recordings/media/snapshots/site-cust-2/camera1/2026-10-01/b.jpg"]
TRAVERSALS = [
    "/recordings/camera1/%2e%2e/guard-secret.json", "/recordings/camera1/..%2fguard-secret.json",
    "/recordings/camera1/%252e%252e/guard-secret.json", "/recordings/camera1/..%5cguard-secret.json",
    "/recordings/camera1/b-customer-clip.mp4%00.json", "/recordings//guard-secret.json",
    "/recordings/camera1/./../guard-secret.json", "/recordings/%2e%2e/app/main.py",
    "/recordings/clips/../guard-secret.json", "/recordings/media/%2e%2e%2fguard-secret.json",
]


def _never_leaks(response):
    assert response.status_code != 200, response.request.url
    assert SECRET not in response.content


def test_cloud_serves_no_raw_media_to_anyone(tenants, monkeypatch):
    """On the cloud a /recordings path names no customer, so nothing is served:
    signed out, unlicensed, customer A or customer B."""
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    client = tenants
    for url in RAW_MEDIA + TRAVERSALS:
        _never_leaks(client.get(url))
        for who in (UNLICENSED, CUSTOMER_A, CUSTOMER_B):
            response = client.get(url, cookies=_cookie(*who))
            _never_leaks(response)
            assert response.status_code in (400, 404), (url, who, response.status_code)


def test_customer_a_reaches_its_own_media_only_through_the_tenant_checked_route(tenants, monkeypatch):
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "cloud")
    import main
    signed = []
    monkeypatch.setattr(main, "_presigned_recording_url", lambda key: signed.append(key) or f"https://s3.example.test/{key}?sig=x")
    client = tenants
    own = client.get("/api/customer/events/cam-1/evt-a/media/url", cookies=_cookie(*CUSTOMER_A))
    assert own.status_code == 200 and own.json()["url"].startswith("https://s3.example.test/cust-1/evt-a.mp4")
    # Customer B's camera, B's event, or B's event under A's own camera: refused.
    assert client.get("/api/customer/events/b-cam/evt-b/media/url", cookies=_cookie(*CUSTOMER_A)).status_code == 403
    assert client.get("/api/customer/events/cam-1/evt-b/media/url", cookies=_cookie(*CUSTOMER_A)).status_code == 404
    assert client.get("/api/customer/events/b-cam/evt-b/media/url", cookies=_cookie(*UNLICENSED)).status_code == 403
    assert client.get("/api/customer/events/cam-1/evt-a/media/url").status_code in (401, 403)
    # ...and B reaches B's own, never A's.
    assert client.get("/api/customer/events/b-cam/evt-b/media/url", cookies=_cookie(*CUSTOMER_B)).status_code == 200
    assert client.get("/api/customer/events/cam-1/evt-a/media/url", cookies=_cookie(*CUSTOMER_B)).status_code == 403
    # Only each customer's own object was ever signed.
    assert signed == ["cust-1/evt-a.mp4", "cust-2/evt-b.mp4"]


def test_appliance_serves_media_but_never_escapes_the_media_folders(tenants, monkeypatch):
    """An appliance belongs to one customer; its own pages link raw media."""
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    client = tenants
    signed_out = client.get(RAW_MEDIA[0])
    _never_leaks(signed_out)
    assert client.get(RAW_MEDIA[0], cookies=_cookie(*CUSTOMER_A)).status_code == 200
    for url in TRAVERSALS:
        response = client.get(url, cookies=_cookie(*CUSTOMER_A))
        _never_leaks(response)


def test_signed_in_files_are_never_kept_by_a_shared_cache(tenants, monkeypatch):
    """Cloudflare cached a signed-in .tar.gz and served it to anyone (staging,
    2026-10-01): every /recordings answer -- served or refused -- and the
    licensed download route say private, no-store to every cache."""
    monkeypatch.setenv("ANYAICAM_RUNTIME_ROLE", "edge")
    client = tenants
    for url in (RAW_MEDIA[0], "/recordings/guard-secret.json", "/recordings/camera1/%2e%2e/guard-secret.json"):
        response = client.get(url, cookies=_cookie(*CUSTOMER_A))
        assert "no-store" in response.headers.get("cache-control", ""), url
        assert response.headers.get("cdn-cache-control") == "no-store", url
    download = client.get("/api/customer/downloads/vms-installer", cookies=_cookie(*CUSTOMER_A))
    assert "no-store" in download.headers.get("cache-control", "") and download.headers.get("cdn-cache-control") == "no-store"
