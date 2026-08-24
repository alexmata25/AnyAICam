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


def teardown_function(_):
    main._recording_read_credentials_cache = None


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
    assert fake_sts.assume_role.call_count == 1  # 25 presigns, one real AssumeRole -- the actual fix
    assert fake_s3.generate_presigned_url.call_count == 25
