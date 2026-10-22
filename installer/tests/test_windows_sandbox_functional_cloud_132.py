"""1.2.7 / Windows 1.3.2 functional Sandbox scenarios F-L / F-H (Blue, 2026-10-21): the disposable test cloud's
--functional routes. Harness tooling only (installer/windows/sandbox is never packaged). Each case builds the test cloud
in a FRESH interpreter (the app modules keep process-wide state) on a temporary database, with a synthetic key."""
import base64
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "installer" / "windows" / "sandbox"
TEST_CLOUD = SANDBOX / "test-cloud.py"

DRIVER = textwrap.dedent(r"""
    import importlib.util, json, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('test_cloud', sys.argv[1])
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    data, app_dir, key, functional = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5] == '1'
    app = module.build_app(data, app_dir, key, functional=functional)
    from fastapi.testclient import TestClient
    client = TestClient(app)
    owner = {'X-Test-Owner': 'yes'}
    out = {}
    def call(name, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        try: body = response.json()
        except ValueError: body = response.text
        out[name] = {'status': response.status_code, 'body': body}
    call('owner_no_header', 'POST', '/test/owner', json={'password': 'Synthetic-Cloud-Pass-1'})
    call('owner', 'POST', '/test/owner', json={'password': 'Synthetic-Cloud-Pass-1'}, headers=owner)
    call('owner_short', 'POST', '/test/owner', json={'password': 'short'}, headers=owner)
    call('expire', 'POST', '/test/expire-pending-claims', headers=owner)
    call('revoke_none', 'POST', '/test/revoke-appliance', headers=owner)
    call('cameras', 'GET', '/test/cameras', headers=owner)
    call('ent_local', 'POST', '/test/entitlement', json={'product': 'camera_slots_local', 'quantity': 2}, headers=owner)
    call('ent_hybrid', 'POST', '/test/entitlement', json={'product': 'camera_slots_hybrid', 'quantity': 2}, headers=owner)
    import customer_entitlements
    out['mode_after_hybrid'] = customer_entitlements.product_mode_for_customer('test-customer')
    call('cancel_hybrid', 'POST', '/test/entitlement-status', json={'product': 'camera_slots_hybrid', 'status': 'cancelled'}, headers=owner)
    out['mode_after_cancel'] = customer_entitlements.product_mode_for_customer('test-customer')
    call('status_bad', 'POST', '/test/entitlement-status', json={'product': 'stripe_live', 'status': 'active'}, headers=owner)
    call('scan_unknown_appliance', 'POST', '/api/customer/appliances/no-such-appliance/scan', json={}, headers=owner)
    call('scan_no_owner', 'POST', '/api/customer/appliances/no-such-appliance/scan', json={})
    import partner_db
    with partner_db.connection() as db:
        user = db.execute("SELECT role, approved, customer_id, password_hash FROM partner_users WHERE email='owner@example.test'").fetchone()
        grants = db.execute("SELECT role, scope_type, scope_id FROM identity_grants WHERE user_id='test-owner' AND revoked_at IS NULL").fetchall()
    out['user'] = None if user is None else {'role': user['role'], 'approved': user['approved'], 'customer_id': user['customer_id'],
                                             'password_ok': partner_db.verify_password('Synthetic-Cloud-Pass-1', user['password_hash'])}
    out['grants'] = [dict(g) for g in grants]
    print('RESULT ' + json.dumps(out))
""")


def _synthetic_key(folder: Path) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    path = folder / "signing-key.json"
    path.write_text(json.dumps({
        "key_id": "sandbox-test-hosttest",
        "private_key_b64": base64.b64encode(key.private_bytes_raw()).decode(),
        "public_key_b64": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
    }), encoding="ascii")
    return path


def _run(tmp_path: Path, functional: bool) -> dict:
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    data = tmp_path / ("cloud-f" if functional else "cloud")
    data.mkdir()
    env = dict(os.environ)
    for name in ("ANYAICAM_PARTNER_DB", "ANYAICAM_RECORDINGS_FOLDER", "ANYAICAM_RUNTIME_ROLE"):
        env.pop(name, None)
    done = subprocess.run([sys.executable, str(driver), str(TEST_CLOUD), str(data), str(ROOT / "app"),
                           str(_synthetic_key(tmp_path)), "1" if functional else "0"],
                          capture_output=True, text=True, timeout=300, env=env, cwd=str(tmp_path))
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, f"driver failed (exit {done.returncode}):\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"
    return json.loads(line[len("RESULT "):])


@pytest.fixture(scope="module")
def functional(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("functional"), True)


@pytest.fixture(scope="module")
def default(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("default"), False)


def test_without_the_flag_no_functional_route_exists(default):
    for name in ("owner", "expire", "revoke_none", "cameras", "cancel_hybrid", "scan_unknown_appliance"):
        assert default[name]["status"] == 404, (name, default[name])
    assert default["user"] is None


def test_every_functional_route_is_test_owner_only(functional):
    assert functional["owner_no_header"]["status"] == 403
    assert functional["scan_no_owner"]["status"] in (401, 403)


def test_owner_seeding_makes_a_real_customer_owner_account_with_a_customer_grant(functional):
    assert functional["owner"]["status"] == 200
    assert functional["owner_short"]["status"] == 400
    assert functional["user"] == {"role": "customer_owner", "approved": 1, "customer_id": "test-customer", "password_ok": True}
    assert functional["grants"] == [{"role": "customer_owner", "scope_type": "customer", "scope_id": "test-customer"}]


def test_claim_expiry_revocation_and_cameras_answer_on_an_empty_cloud(functional):
    assert functional["expire"] == {"status": 200, "body": {"expired": 0}}
    assert functional["revoke_none"]["status"] == 409
    assert functional["cameras"] == {"status": 200, "body": {"cameras": []}}


def test_cancelling_the_hybrid_entitlement_turns_the_customer_back_to_local(functional):
    assert functional["mode_after_hybrid"] == "hybrid"
    assert functional["cancel_hybrid"]["body"]["rows"] == 1
    assert functional["mode_after_cancel"] == "local"
    assert functional["status_bad"]["status"] == 400


def test_the_real_customer_camera_routes_are_mounted_for_the_test_owner(functional):
    # Authenticated as the test owner, the real scan route looks the appliance up (404), it does not refuse the caller.
    assert functional["scan_unknown_appliance"]["status"] == 404, functional["scan_unknown_appliance"]


def _main(args, username):
    env = dict(os.environ, USERNAME=username)
    return subprocess.run([sys.executable, str(TEST_CLOUD), "--app", str(ROOT / "app"), "--data", "unused", *args],
                          capture_output=True, text=True, timeout=120, env=env)


def test_functional_is_refused_without_the_synthetic_key():
    done = _main(["--functional"], "WDAGUtilityAccount")
    assert done.returncode != 0 and "--functional needs the synthetic --signing-key" in done.stderr


def test_functional_is_refused_outside_windows_sandbox(tmp_path):
    done = _main(["--functional", "--signing-key", str(_synthetic_key(tmp_path))], "Alejandro Mata")
    assert done.returncode != 0 and "only inside Windows Sandbox" in done.stderr


def test_a_non_synthetic_signing_key_is_still_refused(tmp_path):
    key = tmp_path / "prod-like.json"
    key.write_text(json.dumps({"key_id": "anyaicam-identity-49cfda63a27f", "private_key_b64": "x", "public_key_b64": "y"}), encoding="ascii")
    driver = textwrap.dedent(r"""
        import importlib.util, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('test_cloud', sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        m.build_app(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), functional=True)
    """)
    (tmp_path / "d.py").write_text(driver, encoding="utf-8")
    (tmp_path / "data").mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("ANYAICAM_PARTNER_DB", "ANYAICAM_RECORDINGS_FOLDER")}
    done = subprocess.run([sys.executable, str(tmp_path / "d.py"), str(TEST_CLOUD), str(tmp_path / "data"), str(ROOT / "app"), str(key)],
                          capture_output=True, text=True, timeout=300, env=env, cwd=str(tmp_path))
    assert done.returncode != 0 and "not a synthetic sandbox-test key" in (done.stdout + done.stderr)
