"""Card-sized event previews (2026-09-25): /thumbnail?size=card returns the
same stored snapshot scaled to 480 px (made in memory, nothing stored),
cached per process and privately in the browser; without ?size=card, and
whenever the resize can't be made, the route behaves exactly as before
(302 to the presigned full snapshot)."""
import sqlite3

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
import partner_portal
from database_backend import override_target
from partner_db import initialize_database



def _jpeg(width=1280, height=720):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, : width // 2] = (40, 160, 220)
    ok, data = cv2.imencode(".jpg", image)
    assert ok
    return data.tobytes()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    with override_target(sqlite_path=tmp_path / "thumbs.db"):
        initialize_database()
        conn = sqlite3.connect(tmp_path / "thumbs.db")
        conn.execute("INSERT INTO partners(id,name,created_at) VALUES('p1','P','x')")
        for customer in ("cust-1", "cust-2"):
            conn.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES(?,?,?,?,?,?)",
                         (customer, "p1", customer, f"{customer}@example.test", "active", "x"))
            conn.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES(?,?,?,?)", (f"s-{customer}", customer, "Main", "x"))
        for camera, customer in (("cam-1", "cust-1"), ("cam-other", "cust-2")):
            conn.execute("INSERT INTO cameras(id,customer_id,site_id,name,status,camera_number,created_at) VALUES(?,?,?,?,?,?,?)",
                         (camera, customer, f"s-{customer}", camera, "configured", 1, "x"))
        conn.execute("INSERT INTO partner_users(id,partner_id,email,name,role,password_hash,approved,customer_id,created_at,account_status,camera_access_mode) "
                     "VALUES('u','p1','o@c1.test','O','customer_owner','x',1,'cust-1','x','active','all')")
        conn.commit()
        conn.close()
        yield from _client(monkeypatch)


def _client(monkeypatch):
    fetches = []
    monkeypatch.setattr(main, "_customer_event_thumbnail_s3_key",
                        lambda camera_id, event_id: {"ev-1": "thumbs/ev-1.jpg", "ev-2": "thumbs/ev-2.jpg"}.get(event_id))
    monkeypatch.setattr(main, "_presigned_recording_url", lambda key: f"https://s3.example/{key}")
    monkeypatch.setattr(main, "_presigned_recording_url_and_ttl", lambda key: (f"https://s3.example/{key}", 300))

    def fetch(url, limit=8 * 1024 * 1024):
        fetches.append(url)
        return _jpeg() if url.endswith("ev-1.jpg") else None  # ev-2's object can't be read

    monkeypatch.setattr(main, "_fetch_presigned_bytes", fetch)
    main._card_thumbnail_cache.clear()
    with TestClient(main.app, base_url="https://app.anyaicam.com", follow_redirects=False) as test_client:
        test_client.cookies.set(partner_portal.SESSION_COOKIE, partner_portal._token("o@c1.test", "customer_owner", None, "cust-1", None))
        test_client.fetches = fetches
        yield test_client
    main._card_thumbnail_cache.clear()


def test_card_size_is_the_stored_snapshot_scaled_to_480px(client):
    response = client.get("/api/customer/events/cam-1/ev-1/thumbnail?size=card")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=86400"  # never shared/CDN-cacheable
    image = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (270, 480)  # aspect ratio kept
    assert len(response.content) < len(_jpeg())


def test_card_size_is_cached_in_process(client):
    for _ in range(3):
        assert client.get("/api/customer/events/cam-1/ev-1/thumbnail?size=card").status_code == 200
    assert client.fetches == ["https://s3.example/thumbs/ev-1.jpg"]


def test_without_card_size_the_route_is_unchanged(client):
    response = client.get("/api/customer/events/cam-1/ev-1/thumbnail")
    assert response.status_code == 302 and response.headers["location"] == "https://s3.example/thumbs/ev-1.jpg"
    assert client.fetches == []


def test_unreadable_snapshot_falls_back_to_the_full_redirect(client):
    response = client.get("/api/customer/events/cam-1/ev-2/thumbnail?size=card")
    assert response.status_code == 302 and response.headers["location"] == "https://s3.example/thumbs/ev-2.jpg"


def test_authorization_and_missing_snapshot_are_unchanged(client):
    assert client.get("/api/customer/events/cam-other/ev-1/thumbnail?size=card").status_code == 403
    assert client.get("/api/customer/events/cam-1/ev-none/thumbnail?size=card").status_code == 404
    assert client.fetches == []


def test_cache_is_bounded(client, monkeypatch):
    monkeypatch.setattr(main, "_CARD_THUMBNAIL_CACHE_MAX", 2)
    monkeypatch.setattr(main, "_fetch_presigned_bytes", lambda url, limit=0: _jpeg(640, 360))
    for key in ("a", "b", "c"):
        assert main._card_thumbnail_bytes(key)
    assert list(main._card_thumbnail_cache) == ["b", "c"]
