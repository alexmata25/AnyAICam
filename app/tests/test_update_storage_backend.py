"""Software Update storage isolation (2026-10-03): update packages/catalog can
use S3 (ANYAICAM_UPDATE_STORAGE_BACKEND=s3, prefix updates/) while every other
storage category keeps the general backend (ANYAICAM_STORAGE_BACKEND)."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import object_storage
import updates_storage
from update_storage_fakes import use_fake_s3_update_storage

PACKAGE = b"installer-tarball-bytes"


def _manifest(**overrides):
    manifest = {"update_id": "1.2.0-bbbbbbbbbbbb", "version": "1.2.0", "build_id": "b" * 40,
                "sha256": hashlib.sha256(PACKAGE).hexdigest(), "package_size_bytes": len(PACKAGE),
                "target": "anyaicam-appliance", "channel": "stable", "platform": "ubuntu", "architecture": "x86_64",
                "issued_at": "2026-10-03T00:00:00Z", "migration_safety": "additive"}
    manifest.update(overrides)
    return manifest


@pytest.fixture()
def offline_key(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = tmp_path / "public.pem"
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv("ANYAICAM_UPDATE_SIGNING_PUBLIC_KEY_FILE", str(public))
    return key


def _sign(key, manifest):
    return key.sign(updates_storage.canonical_manifest_bytes(manifest), padding.PKCS1v15(), hashes.SHA256())


@pytest.fixture()
def general_local(tmp_path, monkeypatch):
    """The general backend stays local (staging today)."""
    assert object_storage.settings.storage_backend == "local"
    local = object_storage.LocalStorage(root=tmp_path / "general")
    monkeypatch.setattr(object_storage, "get_storage", lambda: local)
    monkeypatch.setattr(updates_storage, "get_storage", lambda: local)
    return local


def test_updates_use_s3_while_everything_else_stays_local(general_local, offline_key, monkeypatch):
    client = use_fake_s3_update_storage(monkeypatch)
    updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=PACKAGE,
                                    signature=_sign(offline_key, _manifest()))
    # Update objects went to S3, under updates/ only.
    keys = sorted(key for (bucket, key) in client.objects)
    assert keys == ["updates/anyaicam-appliance/stable/1.2.0-bbbbbbbbbbbb/package.bin", "updates/anyaicam-appliance/stable/latest.json"]
    assert {bucket for (bucket, _key) in client.objects} == {"anyaicam2026"}
    # Nothing was written to the general (local) store ...
    assert not (general_local.root / "updates").exists()
    # ... and the general store still behaves exactly as before for everything else.
    stored = object_storage.get_storage().put("thumbnails", "cam-1/frame.jpg", b"jpeg", content_type="image/jpeg")
    assert stored["backend"] == "local" and (general_local.root / "thumbnails/cam-1/frame.jpg").read_bytes() == b"jpeg"
    assert object_storage.get_storage().url("thumbnails", "cam-1/frame.jpg") == "/storage/thumbnails/cam-1/frame.jpg"
    assert updates_storage.get_latest_release("anyaicam-appliance", "stable")["manifest"] == _manifest()


def test_the_appliance_download_link_is_an_absolute_presigned_s3_url(monkeypatch):
    """A real boto3 client signs locally (no network) with dummy credentials."""
    monkeypatch.setenv("ANYAICAM_UPDATE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("ANYAICAM_UPDATE_S3_BUCKET", "anyaicam2026")
    monkeypatch.setenv("ANYAICAM_UPDATE_S3_REGION", "us-east-1")
    monkeypatch.delenv("ANYAICAM_S3_ENDPOINT", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLEONLY00000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret-not-real")
    url = updates_storage.package_download_url({"package_key": "anyaicam-appliance/stable/1.2.0-x/package.bin"}, expires_seconds=300)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https" and "anyaicam2026" in parsed.netloc + parsed.path
    assert parsed.path.endswith("/updates/anyaicam-appliance/stable/1.2.0-x/package.bin")
    assert query["X-Amz-Expires"] == ["300"] and "X-Amz-Signature" in query
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]


def test_unset_update_backend_keeps_the_previous_behaviour(general_local, monkeypatch):
    monkeypatch.delenv("ANYAICAM_UPDATE_STORAGE_BACKEND", raising=False)
    assert updates_storage.get_update_storage() is general_local
    monkeypatch.setenv("ANYAICAM_UPDATE_STORAGE_BACKEND", "default")
    assert updates_storage.get_update_storage() is general_local


def test_a_local_update_store_never_hands_out_an_undownloadable_link(general_local, monkeypatch):
    monkeypatch.delenv("ANYAICAM_UPDATE_STORAGE_BACKEND", raising=False)
    with pytest.raises(updates_storage.UpdateStorageError):
        updates_storage.package_download_url({"package_key": "anyaicam-appliance/stable/x/package.bin"})


@pytest.mark.parametrize("setting, env", [
    ("s3", {"ANYAICAM_UPDATE_S3_BUCKET": "", "ANYAICAM_S3_BUCKET": ""}),   # no bucket
    ("ftp", {}),                                                            # unknown backend
])
def test_misconfigured_update_storage_fails_safe(general_local, offline_key, monkeypatch, setting, env):
    monkeypatch.setenv("ANYAICAM_UPDATE_STORAGE_BACKEND", setting)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(updates_storage.UpdateStorageError):
        updates_storage.get_update_storage()
    # Publishing refuses (nothing stored), and no release is offered.
    with pytest.raises(updates_storage.PublishError):
        updates_storage.publish_release("anyaicam-appliance", "stable", manifest=_manifest(), package_bytes=PACKAGE,
                                        signature=_sign(offline_key, _manifest()))
    assert updates_storage.get_latest_release("anyaicam-appliance", "stable") is None
    assert not (general_local.root / "updates").exists()


def test_missing_boto3_fails_safe(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_boto3(name, *args, **kwargs):
        if name == "boto3" or name.startswith("botocore"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setenv("ANYAICAM_UPDATE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("ANYAICAM_UPDATE_S3_BUCKET", "anyaicam2026")
    monkeypatch.setattr(builtins, "__import__", no_boto3)
    with pytest.raises(updates_storage.UpdateStorageError):
        updates_storage.get_update_storage()


def test_update_storage_refuses_any_other_category(monkeypatch):
    storage = updates_storage.UpdateS3Storage.__new__(updates_storage.UpdateS3Storage)
    storage.bucket, storage.client = "anyaicam2026", None
    for category in ("thumbnails", "clips", "downloads"):
        with pytest.raises(ValueError):
            storage._key(category, "x")


@pytest.mark.parametrize("tamper", ["signature", "package", "manifest"])
def test_signed_release_verification_is_unchanged_on_s3(general_local, offline_key, monkeypatch, tamper):
    client = use_fake_s3_update_storage(monkeypatch)
    manifest, package, signature = _manifest(), PACKAGE, _sign(offline_key, _manifest())
    if tamper == "signature":
        signature = bytes([signature[0] ^ 1]) + signature[1:]
    elif tamper == "package":
        package = b"other"
    else:
        manifest = _manifest(version="9.9.9")
    with pytest.raises(updates_storage.PublishError):
        updates_storage.publish_release("anyaicam-appliance", "stable", manifest=manifest, package_bytes=package, signature=signature)
    assert client.objects == {}


def test_no_private_key_is_introduced_or_required():
    import inspect
    import publish_update_release
    for module in (updates_storage, publish_update_release):
        source = inspect.getsource(module)
        assert "load_pem_private_key" not in source and "PRIVATE KEY" not in source
    assert not hasattr(updates_storage, "load_signing_key") and not hasattr(updates_storage, "sign_manifest")
