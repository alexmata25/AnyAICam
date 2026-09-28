"""Local Recording device-bound licence core (docs/local-recording-licensing-design.md)."""
import base64
import json
import socket

import pytest

import local_license as ll

FP_A = ll.compute_fingerprint("machine-a", "vol-a", "aa:bb:cc:dd:ee:01", "appl-a")
FP_B = ll.compute_fingerprint("machine-b", "vol-a", "aa:bb:cc:dd:ee:01", "appl-a")


@pytest.fixture(scope="module")
def keys():
    return ll.generate_signing_keypair()


@pytest.fixture()
def cert(keys):
    return ll.issue_certificate(keys[0], license_key="ABCDE-12345-FGHIJ-67890", fingerprint_hash=FP_A)


def test_a_valid_certificate_verifies_offline(keys, cert, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("verification must not touch the network")
    monkeypatch.setattr(socket, "create_connection", no_network)
    result = ll.verify_certificate(cert, keys[1], FP_A)
    assert result.valid and result.reason == "ok" and result.local_id.startswith("LID-")


def test_copying_the_certificate_to_another_machine_fails(keys, cert):
    assert ll.verify_certificate(cert, keys[1], FP_B).reason == "fingerprint_mismatch"


@pytest.mark.parametrize("field,value", [
    ("fingerprint_hash", FP_B), ("license_key", "ZZZZZ-00000-ZZZZZ-00000"), ("local_id", "LID-FORGED"),
    ("product", "hybrid"), ("issued_at", "2099-01-01T00:00:00+00:00"),
])
def test_editing_any_field_breaks_the_signature(keys, cert, field, value):
    forged = json.loads(json.dumps(cert))
    forged["certificate"][field] = value
    assert ll.verify_certificate(forged, keys[1], FP_B if field == "fingerprint_hash" else FP_A).reason == "signature_invalid"


def test_a_certificate_signed_by_another_key_is_rejected(keys):
    other_private, _ = ll.generate_signing_keypair()
    forged = ll.issue_certificate(other_private, license_key="ABCDE-12345-FGHIJ-67890", fingerprint_hash=FP_A)
    assert ll.verify_certificate(forged, keys[1], FP_A).reason == "signature_invalid"


@pytest.mark.parametrize("document,reason", [
    (None, "certificate_missing"), ("", "certificate_missing"), ("{not json", "certificate_corrupt"),
    ({"certificate": {}}, "certificate_corrupt"), ({"certificate": {"version": 1}, "signature": "!!"}, "certificate_corrupt"),
])
def test_missing_or_corrupt_certificates(keys, document, reason):
    assert ll.verify_certificate(document, keys[1], FP_A).reason == reason


def test_fingerprint_is_a_hash_and_never_contains_raw_inputs():
    fp = ll.compute_fingerprint("secret-machine-id", "vol-uuid-123", "aa:bb:cc:dd:ee:ff", "appl-9")
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)
    for raw in ("secret-machine-id", "vol-uuid-123", "aa:bb:cc:dd:ee:ff", "appl-9"):
        assert raw not in fp
    assert fp == ll.compute_fingerprint(" SECRET-MACHINE-ID ", "VOL-UUID-123", "AA:BB:CC:DD:EE:FF", "appl-9")  # normalized
    assert ll.compute_fingerprint("ab", "c", "", "") != ll.compute_fingerprint("a", "bc", "", "")  # no concatenation collisions
    with pytest.raises(ValueError):
        ll.compute_fingerprint("", "", "", "")


def test_saved_certificate_round_trips_and_verifies_from_disk(keys, tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "collect_fingerprint", lambda appliance_id: FP_A)
    doc = ll.issue_certificate(keys[0], license_key=ll.new_license_key(), fingerprint_hash=FP_A)
    path = tmp_path / "license" / "local_license.json"
    ll.save_certificate(path, doc)
    assert ll.load_and_verify(path, keys[1], "appl-a").valid
    assert ll.load_and_verify(tmp_path / "missing.json", keys[1], "appl-a").reason == "certificate_missing"


def test_issuing_requires_a_real_fingerprint(keys):
    with pytest.raises(ValueError):
        ll.issue_certificate(keys[0], license_key="K", fingerprint_hash="short")


def test_private_key_is_never_part_of_the_certificate(keys, cert):
    blob = json.dumps(cert)
    assert "PRIVATE" not in blob and base64.b64encode(keys[0]).decode()[:40] not in blob


def test_a_weak_container_fingerprint_is_refused(monkeypatch):
    """Inside the VMS container there is no machine-id, no volume UUID and
    no physical NIC (verified on the Ryzen): never bind to appliance_id alone."""
    monkeypatch.setattr(ll, "_read", lambda path: "")
    monkeypatch.setattr(ll, "_root_volume_uuid", lambda: "")
    monkeypatch.setattr(ll, "_primary_mac", lambda: "")
    with pytest.raises(ll.FingerprintUnavailable):
        ll.collect_fingerprint("appl-a")


def test_host_inputs_produce_a_stable_fingerprint(monkeypatch):
    monkeypatch.setattr(ll, "_read", lambda path: "0123456789abcdef" if path == "/etc/machine-id" else "")
    monkeypatch.setattr(ll, "_root_volume_uuid", lambda: "1111-2222")
    monkeypatch.setattr(ll, "_primary_mac", lambda: "aa:bb:cc:00:11:22")
    assert ll.collect_fingerprint("appl-a") == ll.collect_fingerprint("appl-a") == ll.compute_fingerprint(
        "0123456789abcdef", "1111-2222", "aa:bb:cc:00:11:22", "appl-a")
