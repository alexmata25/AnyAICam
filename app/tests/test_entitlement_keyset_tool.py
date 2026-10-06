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


@pytest.mark.parametrize("bad", ["no-equals", "bad id=" + NEXT_KEY, "k=AAAA", "k=" + base64.b64encode(bytes(64)).decode()])
def test_the_tool_refuses_anything_but_a_public_key(cloud_db, bad):
    _cloud_private_keys(cloud_db)
    with pytest.raises(SystemExit):
        _run_tool(cloud_db, "--add", bad)
