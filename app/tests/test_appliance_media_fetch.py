"""appliance_media_fetch.py (2026-09-19): the shared HMAC token
mint()/verify() and response_looks_like_real_video() the real
authenticated event-clip direct-fetch path depends on. Pure, dependency-
free unit tests -- no FastAPI, no network, no filesystem beyond
load_local_secret()'s own narrow file-read test."""

import json

import pytest

import appliance_media_fetch as amf


def test_mint_then_verify_succeeds():
    expires, token = amf.mint("secret-a", "/recordings/clips/motion/x.mp4", now=1000.0)
    assert amf.verify("secret-a", "/recordings/clips/motion/x.mp4", expires, token, now=1010.0)


def test_verify_rejects_wrong_secret():
    expires, token = amf.mint("secret-a", "/recordings/clips/motion/x.mp4", now=1000.0)
    assert not amf.verify("secret-b", "/recordings/clips/motion/x.mp4", expires, token, now=1010.0)


def test_verify_rejects_wrong_path():
    expires, token = amf.mint("secret-a", "/recordings/clips/motion/x.mp4", now=1000.0)
    assert not amf.verify("secret-a", "/recordings/clips/motion/y.mp4", expires, token, now=1010.0)


def test_verify_rejects_expired_token():
    expires, token = amf.mint("secret-a", "/recordings/clips/motion/x.mp4", now=1000.0)
    # TOKEN_TTL_SECONDS default is 60 -- well past expiry.
    assert not amf.verify("secret-a", "/recordings/clips/motion/x.mp4", expires, token, now=1200.0)


def test_verify_rejects_tampered_token():
    expires, token = amf.mint("secret-a", "/recordings/clips/motion/x.mp4", now=1000.0)
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert not amf.verify("secret-a", "/recordings/clips/motion/x.mp4", expires, tampered, now=1010.0)


def test_load_local_secret_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(amf, "MEDIA_FETCH_SECRET_FILE", tmp_path / "does-not-exist.json")
    assert amf.load_local_secret() is None


def test_load_local_secret_reads_real_file(tmp_path, monkeypatch):
    secret_file = tmp_path / "media_fetch_secret.json"
    secret_file.write_text(json.dumps({"secret": "real-secret-value"}), encoding="utf-8")
    monkeypatch.setattr(amf, "MEDIA_FETCH_SECRET_FILE", secret_file)
    assert amf.load_local_secret() == "real-secret-value"


def test_load_local_secret_malformed_json_returns_none(tmp_path, monkeypatch):
    secret_file = tmp_path / "media_fetch_secret.json"
    secret_file.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(amf, "MEDIA_FETCH_SECRET_FILE", secret_file)
    assert amf.load_local_secret() is None


def test_load_local_secret_wrong_shape_returns_none(tmp_path, monkeypatch):
    secret_file = tmp_path / "media_fetch_secret.json"
    secret_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setattr(amf, "MEDIA_FETCH_SECRET_FILE", secret_file)
    assert amf.load_local_secret() is None


# ---------------------------------------------------------------------------
# response_looks_like_real_video -- the real fix for the false-positive
# ---------------------------------------------------------------------------

def _real_mp4_bytes(size: int) -> bytes:
    # A minimal, real-shaped MP4 header (ftyp box) padded to size --
    # exactly what response_looks_like_real_video() is designed to accept.
    header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    return header + b"\x00" * max(0, size - len(header))


def test_real_video_bytes_pass():
    body = _real_mp4_bytes(5000)
    assert amf.response_looks_like_real_video(body)


def test_html_login_page_rejected():
    html = b"<!doctype html><html><head><title>Local emergency recovery sign-in</title></head></html>" + b" " * 2000
    assert not amf.response_looks_like_real_video(html)


def test_json_error_body_rejected():
    body = b'{"detail": "Invalid or expired media-fetch token."}' + b" " * 2000
    assert not amf.response_looks_like_real_video(body)


def test_too_short_response_rejected():
    assert not amf.response_looks_like_real_video(_real_mp4_bytes(200))


def test_missing_ftyp_signature_rejected():
    body = b"\x00" * 5000  # right size, no real MP4 signature anywhere
    assert not amf.response_looks_like_real_video(body)


def test_size_mismatch_beyond_tolerance_rejected():
    body = _real_mp4_bytes(1000)
    assert not amf.response_looks_like_real_video(body, expected_size=1_000_000)


def test_size_within_tolerance_accepted():
    body = _real_mp4_bytes(100_000)
    assert amf.response_looks_like_real_video(body, expected_size=101_000)
