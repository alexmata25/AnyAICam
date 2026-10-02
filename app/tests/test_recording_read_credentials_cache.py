"""_recording_read_credentials(): tests for the STS AssumeRole caching
fix to the real production incident found while diagnosing a Playback
regression -- _presigned_recording_url() was assuming the read role
fresh for every single recording (192 recordings measured at ~15s for
one camera alone), which made a populated, correctly-shaped Playback
page feel completely broken once the catalog actually had real volume.
This does not change what a customer sees or how a URL is signed --
only how often STS gets called to make that possible.

Same import-inside-container constraint as test_customer_recordings_r4.py
(see that file's own module docstring).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import main


def _fake_assumed_role(access_key="AKIAFAKE", expires_in_seconds=900):
    return {
        "Credentials": {
            "AccessKeyId": access_key,
            "SecretAccessKey": "fake-secret",
            "SessionToken": "fake-token",
            "Expiration": datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
        }
    }


def setup_function(_):
    main._recording_read_credentials_cache = None
    main._presigned_url_cache = {}


def teardown_function(_):
    main._recording_read_credentials_cache = None
    main._presigned_url_cache = {}


def test_second_call_reuses_cached_credentials_without_a_new_assume_role():
    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = _fake_assumed_role()
    with patch("boto3.client", return_value=fake_sts):
        first = main._recording_read_credentials("arn:aws:iam::123456789012:role/fake", "us-east-1")
        second = main._recording_read_credentials("arn:aws:iam::123456789012:role/fake", "us-east-1")
    assert fake_sts.assume_role.call_count == 1  # the whole point of the fix
    assert first == second
    assert first["access_key_id"] == "AKIAFAKE"


def test_expired_credentials_trigger_a_real_refresh():
    fake_sts = MagicMock()
    fake_sts.assume_role.side_effect = [
        _fake_assumed_role("AKIAOLD", expires_in_seconds=30),  # inside the 60s safety margin already
        _fake_assumed_role("AKIANEW", expires_in_seconds=900),
    ]
    with patch("boto3.client", return_value=fake_sts):
        first = main._recording_read_credentials("arn:aws:iam::123456789012:role/fake", "us-east-1")
        second = main._recording_read_credentials("arn:aws:iam::123456789012:role/fake", "us-east-1")
    assert fake_sts.assume_role.call_count == 2
    assert first["access_key_id"] == "AKIAOLD"
    assert second["access_key_id"] == "AKIANEW"


def test_assume_role_failure_is_not_cached_and_retries_next_call():
    fake_sts = MagicMock()
    fake_sts.assume_role.side_effect = [Exception("network blip"), _fake_assumed_role("AKIARECOVERED")]
    with patch("boto3.client", return_value=fake_sts):
        first = main._recording_read_credentials("arn:aws:iam::123456789012:role/fake", "us-east-1")
        second = main._recording_read_credentials("arn:aws:iam::123456789012:role/fake", "us-east-1")
    assert first is None
    assert second["access_key_id"] == "AKIARECOVERED"
    assert fake_sts.assume_role.call_count == 2


def test_presigned_recording_url_uses_the_cache_across_many_calls(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_READ_ROLE_ARN", "arn:aws:iam::123456789012:role/fake")
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "fake-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = _fake_assumed_role()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://example.com/signed"

    def fake_client(service, **kwargs):
        return fake_sts if service == "sts" else fake_s3

    with patch("boto3.client", side_effect=fake_client):
        for _ in range(25):
            url = main._presigned_recording_url("recordings/some/key.mp4")
    assert url == "https://example.com/signed"
    assert fake_sts.assume_role.call_count == 1  # one real AssumeRole
    # Hybrid transfer-cost audit (docs/hybrid-transfer-cost-reduction-
    # audit.md): _presigned_recording_url() itself now reuses an
    # already-signed URL for PRESIGNED_URL_REUSE_SECONDS instead of
    # re-signing on every call -- 25 calls for the same key produce one
    # real S3 sign, not 25. This is the fix, not a relaxed assertion:
    # unlike the STS credential, a presigned URL was never re-derived
    # from anything that could legitimately change between calls for
    # the same object.
    assert fake_s3.generate_presigned_url.call_count == 1


def test_presigned_recording_url_signs_again_for_a_different_key(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_READ_ROLE_ARN", "arn:aws:iam::123456789012:role/fake")
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "fake-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = _fake_assumed_role()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: f"https://example.com/{Params['Key']}"

    def fake_client(service, **kwargs):
        return fake_sts if service == "sts" else fake_s3

    with patch("boto3.client", side_effect=fake_client):
        first = main._presigned_recording_url("recordings/a.mp4")
        second = main._presigned_recording_url("recordings/b.mp4")
        first_again = main._presigned_recording_url("recordings/a.mp4")
    assert first == "https://example.com/recordings/a.mp4"
    assert second == "https://example.com/recordings/b.mp4"
    assert first_again == first
    assert fake_s3.generate_presigned_url.call_count == 2  # one per distinct key, never per call


def test_presigned_recording_url_re_signs_once_the_reuse_window_elapses(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_READ_ROLE_ARN", "arn:aws:iam::123456789012:role/fake")
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "fake-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = _fake_assumed_role()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://example.com/signed"

    def fake_client(service, **kwargs):
        return fake_sts if service == "sts" else fake_s3

    fake_clock = {"now": 1_000_000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: fake_clock["now"])

    with patch("boto3.client", side_effect=fake_client):
        main._presigned_recording_url("recordings/a.mp4")
        fake_clock["now"] += main.PRESIGNED_URL_REUSE_SECONDS + 1
        main._presigned_recording_url("recordings/a.mp4")
    assert fake_s3.generate_presigned_url.call_count == 2  # the stale entry was not reused past its window


def test_presigned_recording_url_and_ttl_reports_decreasing_remaining_time(monkeypatch):
    monkeypatch.setenv("ANYAICAM_RECORDING_READ_ROLE_ARN", "arn:aws:iam::123456789012:role/fake")
    monkeypatch.setenv("ANYAICAM_RECORDING_S3_BUCKET", "fake-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    fake_sts = MagicMock()
    fake_sts.assume_role.return_value = _fake_assumed_role()
    fake_s3 = MagicMock()
    fake_s3.generate_presigned_url.return_value = "https://example.com/signed"

    def fake_client(service, **kwargs):
        return fake_sts if service == "sts" else fake_s3

    fake_clock = {"now": 1_000_000.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: fake_clock["now"])

    with patch("boto3.client", side_effect=fake_client):
        url, ttl = main._presigned_recording_url_and_ttl("recordings/a.mp4")
        assert ttl == main.PRESIGNED_URL_REUSE_SECONDS
        fake_clock["now"] += 120
        url2, ttl2 = main._presigned_recording_url_and_ttl("recordings/a.mp4")
    assert url == url2  # same cached URL, not re-signed
    assert ttl2 == main.PRESIGNED_URL_REUSE_SECONDS - 120


def test_presigned_recording_url_and_ttl_reports_zero_on_signing_failure(monkeypatch):
    monkeypatch.delenv("ANYAICAM_RECORDING_READ_ROLE_ARN", raising=False)
    url, ttl = main._presigned_recording_url_and_ttl("recordings/a.mp4")
    assert url is None
    assert ttl == 0


def test_cacheable_presigned_redirect_sets_a_private_cache_control_header(monkeypatch):
    monkeypatch.setattr(
        main, "_presigned_recording_url_and_ttl",
        lambda s3_key: ("https://example.com/signed", 300),
    )
    response = main._cacheable_presigned_redirect("recordings/a.jpg")
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/signed"
    # "private" is the whole point: this must never be reusable by a
    # shared/intermediate cache serving a different, unauthorized
    # request -- only the one browser that already proved authorization
    # to fetch this specific object may reuse its own cached copy.
    assert response.headers["cache-control"] == "private, max-age=300"


def test_cacheable_presigned_redirect_returns_none_on_signing_failure(monkeypatch):
    monkeypatch.setattr(main, "_presigned_recording_url_and_ttl", lambda s3_key: (None, 0))
    assert main._cacheable_presigned_redirect("recordings/missing.jpg") is None


def test_cacheable_presigned_redirect_omits_cache_control_when_ttl_is_zero(monkeypatch):
    # A URL can in principle come back with 0 reuse seconds left (e.g. the
    # cache entry was read at the exact instant it expired) -- must never
    # emit a max-age=0 (which some clients still treat as "cache it, but
    # revalidate," rather than "don't cache") when there is genuinely no
    # safe reuse window left.
    monkeypatch.setattr(
        main, "_presigned_recording_url_and_ttl",
        lambda s3_key: ("https://example.com/signed", 0),
    )
    response = main._cacheable_presigned_redirect("recordings/a.jpg")
    assert response.status_code == 302
    assert "cache-control" not in {k.lower() for k in response.headers.keys()}
