"""Regression coverage for a defect discovered TWICE independently in
this project: an empty AWS_REGION/AWS_DEFAULT_REGION silently builds a
malformed S3 endpoint (https://s3..amazonaws.com -- the double dot is
the empty region), and every upload then fails deep inside boto3 with a
cryptic "Invalid endpoint" ValueError that gives no indication why.

First found and documented (but never fixed in source) during Phase C's
real-hardware motion-cloud validation, in recording_uploader.py. Then
rediscovered independently -- same root cause, different module -- while
validating live-relay Live View on the same real appliance, in
live_relay_uploader.py. Both modules now fail loud and specific, before
ever constructing an S3 client, the moment an upload is attempted with
no region configured -- closing this class of defect in both places at
once rather than leaving the second module to rediscover it a third
time.
"""

import pytest

import recording_uploader as ru
import live_relay_uploader as lru


# --------------------------------------------------- recording_uploader.py


def test_ensure_client_refuses_empty_aws_region(monkeypatch):
    monkeypatch.setattr(ru, "AWS_REGION", "")
    monkeypatch.setattr(ru, "boto3", object())  # present, so the boto3-missing branch isn't what's tested here
    ru._clients.pop(1, None)  # ensure no cached client from another test masks this one
    with pytest.raises(RuntimeError, match="AWS_REGION"):
        ru._ensure_client(1, {"credentials": {"access_key_id": "x", "secret_access_key": "x", "session_token": "x"}})


def test_ensure_client_proceeds_with_a_real_region(monkeypatch):
    monkeypatch.setattr(ru, "AWS_REGION", "us-east-1")
    ru._clients.pop(1, None)
    session = {"credentials": {"access_key_id": "x", "secret_access_key": "x", "session_token": "x"}}
    # A real boto3 client construction with fake credentials succeeds
    # (boto3 doesn't validate credentials at construction time) -- this
    # confirms the guard doesn't false-positive on a legitimately
    # configured region, only on an empty one.
    client = ru._ensure_client(1, session)
    assert client is not None
    ru._clients.pop(1, None)


# --------------------------------------------------- live_relay_uploader.py


def test_upload_segment_refuses_empty_aws_region(monkeypatch, tmp_path):
    monkeypatch.setattr(lru, "AWS_REGION", "")
    local_path = tmp_path / "camera1_000000001.ts"
    local_path.write_bytes(b"not real video, just needs to exist for the path check upstream")
    session = {
        "credentials": {"access_key_id": "x", "secret_access_key": "x", "session_token": "x"},
        "bucket": "test-bucket",
        "key_prefix": "live/cust/site/appl/cam/",
    }
    with pytest.raises(RuntimeError, match="AWS_REGION"):
        lru._upload_segment(session, local_path)
