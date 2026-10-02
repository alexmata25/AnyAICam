"""Hybrid transfer-cost audit (docs/hybrid-transfer-cost-reduction-audit.md):
_upload_recording() previously uploaded both the recording and its
thumbnail with only a ContentType, no CacheControl -- so nothing ever told
a browser (or a future CDN) it was safe to reuse either object's bytes
after the first fetch. A finished recording (and its thumbnail) is
written once and never modified afterward, so a long-lived, immutable
CacheControl is always safe. This proves both uploads now carry it.
"""
from datetime import datetime
from pathlib import Path

import recording_uploader as ru


class _FakeS3Client:
    def __init__(self):
        self.upload_calls = []

    def upload_file(self, path, bucket, key, ExtraArgs=None, Config=None):
        self.upload_calls.append({"path": path, "bucket": bucket, "key": key, "extra_args": ExtraArgs or {}})


def test_recording_upload_carries_the_immutable_cache_control(tmp_path, monkeypatch):
    # Thumbnail generation shells out to real ffmpeg -- not needed to
    # prove the mp4 upload's own ExtraArgs, and skipping it keeps this
    # test independent of whether ffmpeg is installed in the test
    # environment.
    monkeypatch.setattr(ru, "_create_recording_thumbnail", lambda mp4_path, camera_number: None)

    local_path = tmp_path / "camera1_20260918_120000.mp4"
    local_path.write_bytes(b"fake-mp4-bytes")

    client = _FakeS3Client()
    session = {"bucket": "fake-bucket", "key_prefix": "recordings/tenant/"}

    ru._upload_recording(client, session, local_path, datetime(2026, 9, 18, 12, 0, 0), camera_number=1)

    assert len(client.upload_calls) == 1
    call = client.upload_calls[0]
    assert call["extra_args"]["ContentType"] == "video/mp4"
    assert call["extra_args"]["CacheControl"] == ru.RECORDING_MEDIA_CACHE_CONTROL


def test_recording_and_thumbnail_both_carry_the_cache_control(tmp_path, monkeypatch):
    thumbnail_path = tmp_path / "camera1_20260918_120000.jpg"
    thumbnail_path.write_bytes(b"fake-jpg-bytes")
    monkeypatch.setattr(ru, "_create_recording_thumbnail", lambda mp4_path, camera_number: thumbnail_path)

    local_path = tmp_path / "camera1_20260918_120000.mp4"
    local_path.write_bytes(b"fake-mp4-bytes")

    client = _FakeS3Client()
    session = {"bucket": "fake-bucket", "key_prefix": "recordings/tenant/"}

    ru._upload_recording(client, session, local_path, datetime(2026, 9, 18, 12, 0, 0), camera_number=1)

    assert len(client.upload_calls) == 2
    content_types = {c["extra_args"]["ContentType"] for c in client.upload_calls}
    assert content_types == {"video/mp4", "image/jpeg"}
    assert all(c["extra_args"]["CacheControl"] == ru.RECORDING_MEDIA_CACHE_CONTROL for c in client.upload_calls)
