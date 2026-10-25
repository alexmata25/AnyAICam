"""The keyset digest an operator obtains on the cloud host (app/
entitlement_keyset.py, Codex review of 5c1556e) is what the release builder
verifies the downloaded keyset against: two independent sources must agree
before a trust anchor is packaged."""
import base64
import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import entitlement_keyset
from database_backend import override_target
from test_aac_voice_call_cloud_edge_flow import appliance_cloud, cloud_client, cloud_db, connection  # noqa: F401

_INSTALLER = Path(__file__).resolve().parents[2] / "installer"
if not (_INSTALLER / "build_release_installer.py").is_file():  # the VMS image carries app/ only
    pytest.skip("the release builder (installer/) is not part of this tree", allow_module_level=True)
sys.path.insert(0, str(_INSTALLER))
import build_release_installer as builder  # noqa: E402

NEXT_KEY = base64.b64encode(bytes(range(32))).decode()


def _run_tool(cloud_db, *args):
    out = io.StringIO()
    with override_target(sqlite_path=str(cloud_db)):
        with redirect_stdout(out):
            code = entitlement_keyset.main(list(args))
    return code, out.getvalue()


def _run_with_public_keys(monkeypatch, public_keys, *args):
    out = io.StringIO()
    monkeypatch.setattr(entitlement_keyset, "cloud_public_keys", lambda: dict(public_keys))
    with redirect_stdout(out):
        code = entitlement_keyset.main(list(args))
    return code, out.getvalue()


def _cloud_private_keys(cloud_db):
    import appliance_identity
    with override_target(sqlite_path=str(cloud_db)):
        with connection() as db:
            appliance_identity.ensure_signing_key(db)
            return [row["private_key_b64"] for row in db.execute("SELECT private_key_b64 FROM identity_signing_keys")]


def test_the_cloud_host_digest_verifies_the_downloaded_keyset(cloud_client, cloud_db, tmp_path):
    private_keys = _cloud_private_keys(cloud_db)
    with override_target(sqlite_path=str(cloud_db)):
        downloaded = cloud_client.get("/api/appliance/signing-keys").json()  # the public endpoint
    code, printed = _run_tool(cloud_db)  # on the cloud host, over the admin channel
    assert code == 0
    digest = printed.strip().splitlines()[-1].split()[-1]
    keyset_file = tmp_path / "entitlement-signing-public-keys.json"
    keyset_file.write_text(json.dumps(downloaded), encoding="utf-8")
    packaged = builder.read_entitlement_signing_keys(str(keyset_file), digest)
    assert hashlib.sha256(packaged).hexdigest() == digest
    assert printed.rsplit("sha256 ", 1)[0].encode() == packaged  # the tool prints exactly the canonical keyset
    for private in private_keys:
        assert private not in printed  # public keys only


def test_a_swapped_download_fails_against_the_cloud_host_digest(cloud_client, cloud_db, tmp_path):
    _cloud_private_keys(cloud_db)
    _code, printed = _run_tool(cloud_db)
    digest = printed.strip().splitlines()[-1].split()[-1]
    with override_target(sqlite_path=str(cloud_db)):
        downloaded = cloud_client.get("/api/appliance/signing-keys").json()
    key_id = next(iter(downloaded["keys"]))
    swapped = tmp_path / "swapped.json"
    swapped.write_text(json.dumps({"keys": {key_id: NEXT_KEY}}), encoding="utf-8")  # attacker key under the real id
    with pytest.raises(SystemExit, match="does not match"):
        builder.read_entitlement_signing_keys(str(swapped), digest)


def test_rotation_digest_includes_the_pre_generated_next_key(cloud_db):
    _cloud_private_keys(cloud_db)
    _code, current = _run_tool(cloud_db)
    code, rotated = _run_tool(cloud_db, "--add", f"next-key={NEXT_KEY}")
    assert code == 0
    keys = json.loads(rotated.rsplit("sha256 ", 1)[0])["keys"]
    assert keys["next-key"] == NEXT_KEY and len(keys) == len(json.loads(current.rsplit("sha256 ", 1)[0])["keys"]) + 1
    assert rotated.strip().splitlines()[-1] == f"sha256 {entitlement_keyset.digest(keys)}"


def test_legacy_flat_keyset_refuses_rebinding_an_existing_id(monkeypatch):
    replacement = base64.b64encode(bytes(reversed(range(32)))).decode()
    with pytest.raises(SystemExit, match="no rebinding"):
        _run_with_public_keys(monkeypatch, {"existing-key": NEXT_KEY}, "--add", f"existing-key={replacement}")


def test_legacy_flat_keyset_allows_same_binding_and_new_ids(monkeypatch):
    replacement = base64.b64encode(bytes(reversed(range(32)))).decode()
    code, printed = _run_with_public_keys(
        monkeypatch,
        {"existing-key": NEXT_KEY},
        "--add", f"existing-key={NEXT_KEY}",
        "--add", f"next-key={replacement}",
    )
    assert code == 0
    keys = json.loads(printed.rsplit("sha256 ", 1)[0])["keys"]
    assert keys == {"existing-key": NEXT_KEY, "next-key": replacement}


@pytest.mark.parametrize("bad", ["no-equals", "bad id=" + NEXT_KEY, "k=AAAA", "k=" + base64.b64encode(bytes(64)).decode()])
def test_the_tool_refuses_anything_but_a_public_key(cloud_db, bad):
    _cloud_private_keys(cloud_db)
    with pytest.raises(SystemExit):
        _run_tool(cloud_db, "--add", bad)


# ------------------------------------------------------------------ TRUST-1 (1.2.7.1): the trust registry
def _registry(printed: str) -> tuple[dict, str]:
    body, _, tail = printed.rpartition("sha256 ")
    return json.loads(body), tail.strip()


def test_the_first_registry_holds_every_active_cloud_key_and_verifies_in_the_builder(cloud_db, tmp_path):
    _cloud_private_keys(cloud_db)
    code, printed = _run_tool(cloud_db, "--trust-registry")
    assert code == 0
    document, digest = _registry(printed)
    assert document["type"] == "anyaicam.entitlement_trust_registry" and document["generation"] == 1
    assert document["environment"] == "production"
    assert all(entry["state"] == "active" for entry in document["keys"].values())
    path = tmp_path / "registry.json"
    path.write_bytes(printed.rpartition("sha256 ")[0].encode("ascii"))
    registry_bytes, keyset_bytes = builder.read_entitlement_trust_registry(str(path), digest)
    assert hashlib.sha256(registry_bytes).hexdigest() == digest
    assert set(json.loads(keyset_bytes)["keys"]) == set(document["keys"])


def test_rotation_retires_the_old_key_adds_the_next_and_never_drops_a_binding(cloud_db, tmp_path):
    _cloud_private_keys(cloud_db)
    _code, first = _run_tool(cloud_db, "--trust-registry")
    previous = tmp_path / "gen1.json"
    previous.write_bytes(first.rpartition("sha256 ")[0].encode("ascii"))
    old_id = next(iter(_registry(first)[0]["keys"]))
    code, second = _run_tool(cloud_db, "--trust-registry", "--previous", str(previous),
                             "--add", f"anyaicam-identity-next={NEXT_KEY}", "--retire", old_id)
    assert code == 0
    document, _ = _registry(second)
    assert document["generation"] == 2
    assert document["keys"][old_id]["state"] == "retired" and document["keys"][old_id]["changed_generation"] == 2
    assert document["keys"]["anyaicam-identity-next"]["state"] == "active"
    gen2 = tmp_path / "gen2.json"
    gen2.write_bytes(second.rpartition("sha256 ")[0].encode("ascii"))
    code, third = _run_tool(cloud_db, "--trust-registry", "--previous", str(gen2), "--revoke", old_id)
    assert code == 0 and _registry(third)[0]["keys"][old_id]["state"] == "revoked"
    gen3 = tmp_path / "gen3.json"
    gen3.write_text(third.rpartition("sha256 ")[0], encoding="ascii")
    with pytest.raises(SystemExit, match="lifecycle_backwards|revoked"):
        _run_tool(cloud_db, "--trust-registry", "--previous", str(gen3), "--retire", old_id)


@pytest.mark.parametrize("bad", ["lab-signer=" + NEXT_KEY, "test-key=" + NEXT_KEY])
def test_the_tool_refuses_lab_or_test_keys_in_a_production_registry(cloud_db, bad):
    _cloud_private_keys(cloud_db)
    with pytest.raises(SystemExit, match="lab|test"):
        _run_tool(cloud_db, "--trust-registry", "--add", bad)


def test_retire_and_revoke_need_the_registry_mode(cloud_db):
    _cloud_private_keys(cloud_db)
    with pytest.raises(SystemExit):
        _run_tool(cloud_db, "--retire", "anything")
