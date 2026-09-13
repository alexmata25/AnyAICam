"""Staging Live View transport (2026-09-13): regression coverage for
live_playlist.py's routing decision -- the exact code that produced the
confirmed-live 503 root cause (GET .../live/playlist.m3u8 always fails on
a cloud-role process because require_local_camera() unconditionally
rejects any runtime_role outside {edge, combined}, and the CloudFront
relay path was never configured).

Proves, with a fully mocked CloudFront signer (no real AWS call):

  * once CloudFront IS configured, a cloud-role request is served from
    live_manifest_store + signed segment URLs and NEVER reaches
    local_context()/require_local_camera() at all -- the "impossible
    edge-local path" a cloud process can never satisfy is simply never
    attempted once the relay path is live;
  * render_playlist() produces a playable, correctly-shaped .m3u8 whose
    segment URIs are the signer's own signed URLs;
  * a cloud-role request with CloudFront still unconfigured (today's
    real, understood-correct state) still fails closed with 503 -- this
    lock-in test must keep passing until the day CloudFront is actually
    configured, at which point the OTHER test above is what proves the
    fix;
  * edge/combined local-HLS behavior for a real, activated appliance is
    completely unchanged by any of this.
"""

import os
import time
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from database_backend import override_target

with override_target(sqlite_path="/tmp/test_live_playlist_cloud_relay_path.db"):
    import live_playlist
    from partner_db import connection
    import partner_portal
    from cloud_config import settings


@contextmanager
def _runtime_role(role: str):
    """cloud_config.settings is a frozen dataclass singleton -- there is
    no supported setter, and the rest of this codebase's own tests that
    vary it do so via env var + importlib.reload(main), which is both
    expensive and irrelevant here (live_playlist.py reads
    cloud_config.settings directly, not main.RUNTIME_ROLE). This is the
    standard, narrowly-scoped escape hatch for a frozen dataclass in
    tests only -- restores the original value unconditionally."""
    original = settings.runtime_role
    object.__setattr__(settings, "runtime_role", role)
    try:
        yield
    finally:
        object.__setattr__(settings, "runtime_role", original)


def _fake_rsa_signer(message: bytes) -> bytes:
    return b"fake-signature-not-real"


def _seed(db, *, device_key="urn:uuid:fake"):
    now = "2026-09-13T00:00:00"
    db.execute("INSERT OR IGNORE INTO partners(id,name,created_at) VALUES('partner-1','Test Partner',?)", (now,))
    db.execute("INSERT OR IGNORE INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust-1','partner-1','C','c@example.test','active',?)", (now,))
    db.execute("INSERT OR IGNORE INTO sites(id,customer_id,name,created_at) VALUES('site-1','cust-1','Main',?)", (now,))
    db.execute("INSERT OR IGNORE INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES('appl-1','cust-1','site-1','AIC-1',?)", (now,))
    db.execute(
        "INSERT OR IGNORE INTO cameras(id,customer_id,site_id,appliance_id,camera_number,device_key,status,name,created_at) "
        "VALUES('cam-1','cust-1','site-1','appl-1',1,?,'configured','Camera 1',?)", (device_key, now)
    )
    db.execute(
        "INSERT OR IGNORE INTO partner_users(id,email,role,customer_id,password_hash,camera_access_mode,created_at) "
        "VALUES('user-1','owner@example.test','customer_owner','cust-1','x','all',?)", (now,)
    )
    db.commit()


def _owner_cookie():
    return partner_portal._token("owner@example.test", "customer_owner", None, "cust-1", None)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_live_playlist_cloud_relay_path.db"


def _client(hls_folder=None, local_identity=lambda: None):
    app = FastAPI()
    live_playlist.register_live_playlist_routes(app, hls_folder=hls_folder, local_identity=local_identity)
    return TestClient(app)


# ------------------------------------------------- cloud role, CloudFront configured (the fix)


def test_cloud_role_with_cloudfront_configured_never_touches_local_context(db_path, monkeypatch, tmp_path):
    monkeypatch.setenv(live_playlist.CLOUDFRONT_URL_ENV, "https://d123456.cloudfront.net")
    monkeypatch.setenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, "APKAFAKEKEYPAIRID")
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: _fake_rsa_signer)

    def _explode(*a, **k):
        raise AssertionError("local_context()/require_local_camera() must never be reached once CloudFront is configured")
    monkeypatch.setattr(live_playlist, "require_local_camera", _explode)

    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
        client = _client(hls_folder=None)  # even an absent local HLS folder must not matter on this path
        with override_target(sqlite_path=str(db_path)), _runtime_role("cloud"):
            response = client.get(
                "/api/customer/cameras/cam-1/live/playlist.m3u8",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
            )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "#EXTM3U" in response.text


def test_render_playlist_produces_a_playable_manifest_with_signed_segment_urls():
    manifest = {
        "segments": [
            {"key": "live/cust-1/site-1/appl-1/cam-1/camera1_000001.ts", "sequence": 1},
            {"key": "live/cust-1/site-1/appl-1/cam-1/camera1_000002.ts", "sequence": 2},
        ],
        "updated_at": time.time(),
    }
    playlist = live_playlist.render_playlist(
        manifest,
        expected_prefix="live/cust-1/site-1/appl-1/cam-1/",
        cloudfront_base_url="https://d123456.cloudfront.net",
        customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-1",
        key_id="APKAFAKEKEYPAIRID", rsa_signer=_fake_rsa_signer,
    )
    assert playlist.startswith("#EXTM3U")
    assert "#EXT-X-MEDIA-SEQUENCE:1" in playlist
    assert playlist.count("#EXTINF:") == 2
    lines = playlist.splitlines()
    segment_urls = [line for line in lines if line.startswith("https://d123456.cloudfront.net/")]
    assert len(segment_urls) == 2
    for url in segment_urls:
        assert "Policy=" in url or "Signature=" in url or "Key-Pair-Id=" in url
    # No #EXT-X-ENDLIST -- this must still read as a live, ongoing stream.
    assert "#EXT-X-ENDLIST" not in playlist


def test_render_playlist_drops_a_segment_outside_this_cameras_own_prefix():
    """A manifest entry whose key does not start with this camera's own
    prefix (corrupted state, or a future bug elsewhere) is silently
    skipped -- never signed, never leaked into another camera's
    playlist -- one bad entry must not take down the whole render."""
    manifest = {
        "segments": [
            {"key": "live/cust-1/site-1/appl-1/cam-1/camera1_000001.ts", "sequence": 1},
            {"key": "live/cust-1/site-1/appl-1/OTHER-CAMERA/camera1_000002.ts", "sequence": 2},
        ],
        "updated_at": time.time(),
    }
    playlist = live_playlist.render_playlist(
        manifest,
        expected_prefix="live/cust-1/site-1/appl-1/cam-1/",
        cloudfront_base_url="https://d123456.cloudfront.net",
        customer_id="cust-1", site_id="site-1", appliance_id="appl-1", camera_id="cam-1",
        key_id="APKAFAKEKEYPAIRID", rsa_signer=_fake_rsa_signer,
    )
    assert playlist.count("#EXTINF:") == 1
    assert "OTHER-CAMERA" not in playlist


# ------------------------------------------------- cloud role, CloudFront NOT configured (today's real state)


def test_cloud_role_without_cloudfront_configured_fails_closed_with_503(db_path, monkeypatch):
    """Lock-in of today's real, confirmed-live, understood-correct
    behavior (portal-staging has ANYAICAM_RUNTIME_ROLE=cloud and no
    ANYAICAM_CLOUDFRONT_* env set) -- this must keep failing exactly this
    way until CloudFront is actually provisioned and configured. Even a
    fully valid-looking local identity is deliberately supplied here (an
    appliance whose own credentials genuinely match this camera) to prove
    it is specifically settings.runtime_role=='cloud' that closes this
    path -- not merely a missing/absent identity, which a separate real
    edge box would never have."""
    monkeypatch.delenv(live_playlist.CLOUDFRONT_URL_ENV, raising=False)
    monkeypatch.delenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, raising=False)
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: None)
    identity = {"credential": "cred", "appliance_id": "appl-1", "cloud_id": "AIC-1"}

    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
            db.execute("UPDATE appliances SET cloud_id='AIC-1' WHERE id='appl-1'")
        client = _client(hls_folder="/nonexistent", local_identity=lambda: identity)
        with override_target(sqlite_path=str(db_path)), _runtime_role("cloud"):
            response = client.get(
                "/api/customer/cameras/cam-1/live/playlist.m3u8",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
            )
    assert response.status_code == 503


# ------------------------------------------------- edge/combined local-HLS unchanged


def test_edge_role_local_playlist_behavior_is_unchanged(db_path, monkeypatch, tmp_path):
    monkeypatch.delenv(live_playlist.CLOUDFRONT_URL_ENV, raising=False)
    monkeypatch.delenv(live_playlist.CLOUDFRONT_KEY_PAIR_ID_ENV, raising=False)
    monkeypatch.setattr(live_playlist, "get_configured_signer", lambda: None)

    hls_folder = tmp_path / "hls"
    hls_folder.mkdir()
    (hls_folder / "camera1.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\ncamera1_000001.ts\n"
    )
    (hls_folder / "camera1_000001.ts").write_bytes(b"fake-ts-bytes")

    identity = {"credential": "cred", "appliance_id": "appl-1", "cloud_id": "AIC-1"}

    with override_target(sqlite_path=str(db_path)):
        from partner_db import initialize_database
        initialize_database()
        with connection() as db:
            _seed(db)
            db.execute("UPDATE appliances SET cloud_id='AIC-1' WHERE id='appl-1'")
        client = _client(hls_folder=str(hls_folder), local_identity=lambda: identity)
        with override_target(sqlite_path=str(db_path)), _runtime_role("edge"):
            response = client.get(
                "/api/customer/cameras/cam-1/live/playlist.m3u8",
                cookies={partner_portal.SESSION_COOKIE: _owner_cookie()},
            )
    assert response.status_code == 200
    assert "#EXTINF" in response.text
    assert "/api/customer/cameras/cam-1/live/segments/camera1_000001.ts" in response.text
