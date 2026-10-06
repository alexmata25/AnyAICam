"""Paid-feature entitlement trust anchor (2026-10-05, staging finding on
1f66bcd): the release builder packages the cloud's entitlement-signing PUBLIC
keyset (never private material), the installer provisions it root-owned in
/etc/anyaicam-update/ only after checking it against release.env, and the VMS
container reads it read-only. Nothing the cloud sends at runtime is a trust
anchor (app/appliance_entitlements.py)."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1]
REPO = INSTALLER.parent
sys.path.insert(0, str(INSTALLER))

import build_release_installer as builder  # noqa: E402

BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
PUBLIC = base64.b64encode(bytes(range(32))).decode()


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class BuilderTests(unittest.TestCase):
    def _run(self, extra):
        cmd = [sys.executable, str(INSTALLER / "build_release_installer.py"), "--vms-commit", "0" * 40,
               "--vms-repo", "/nonexistent-repo-path-never-reached", "--no-mediamtx", "--no-update-signing-key"] + extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def _keys_file(self, content):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "keys.json"
        path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode())
        return str(path)

    def test_the_keyset_is_a_deliberate_choice(self):
        neither = self._run([])
        self.assertNotEqual(neither.returncode, 0)
        self.assertIn("--entitlement-signing-public-keys", neither.stderr + neither.stdout)
        both = self._run(["--entitlement-signing-public-keys", "k.json", "--no-entitlement-signing-keys"])
        self.assertIn("mutually exclusive", both.stderr + both.stdout)

    def test_the_cloud_signing_keys_response_is_packaged_in_canonical_form(self):
        second = base64.b64encode(bytes(range(32, 64))).decode()
        packaged = builder.read_entitlement_signing_keys(self._keys_file({"keys": {"k2": second, "k1": PUBLIC}}))
        self.assertEqual(packaged, (json.dumps({"keys": {"k1": PUBLIC, "k2": second}}, sort_keys=True, indent=2) + "\n").encode())
        crlf = builder.read_entitlement_signing_keys(self._keys_file(
            json.dumps({"keys": {"k1": PUBLIC, "k2": second}}).replace(",", ",\r\n").encode()))
        self.assertEqual(crlf, packaged)  # stable SHA-256 whatever the input formatting

    def test_only_public_keys_can_ever_be_packaged(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private = Ed25519PrivateKey.generate()
        private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
        cases = {
            "private PEM": private_pem,
            "private key field": {"keys": {"k1": PUBLIC}, "private_key_b64": "AAAA"},
            "64-byte private+public": {"keys": {"k1": base64.b64encode(bytes(64)).decode()}},
            "raw private key bytes as a key": {"keys": {"k1": base64.b64encode(private.private_bytes_raw() + b"x").decode()}},
            "not JSON": b"not json",
            "no keys": {"keys": {}},
            "flat dict": {"k1": PUBLIC},
            "bad id": {"keys": {"bad id": PUBLIC}},
            "short key": {"keys": {"k1": "AAAA"}},
            "not base64": {"keys": {"k1": "not base64!"}},
        }
        for label, content in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                builder.read_entitlement_signing_keys(self._keys_file(content))

    def test_the_install_step_ships_and_runs_after_the_update_key(self):
        self.assertIn("13-entitlement-signing-keys.sh", builder.INSTALLER_RUNTIME_FILES)
        install = _text(INSTALLER / "install.sh")
        self.assertIn('source "$INSTALLER_DIR/13-entitlement-signing-keys.sh"', install)
        self.assertLess(install.index("    provision_update_signing_key\n"), install.index("    provision_entitlement_signing_keys\n"))
        self.assertIn("ENTITLEMENT_SIGNING_KEYS_SHA256", install)
        self.assertRegex(install, r'ENTITLEMENT_SIGNING_KEYS_SHA256.*=~ \^\[0-9a-f\]\{64\}\$')

    def test_release_env_and_the_manifest_carry_the_keyset_hash(self):
        source = _text(INSTALLER / "build_release_installer.py")
        self.assertIn('f"ENTITLEMENT_SIGNING_KEYS_SHA256={entitlement_keys_sha256}\\n"', source)
        self.assertIn('"entitlement_signing_keys_sha256": entitlement_keys_sha256', source)
        self.assertIn('package / "payload/keys/entitlement-signing-public-keys.json"', source)


@unittest.skipUnless(BASH, "bash is required")
class KeysetProvisioningTests(unittest.TestCase):
    """13-entitlement-signing-keys.sh with `install` stubbed to record owner/mode."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "install"
        stub.write_text(textwrap.dedent('''\
            #!/usr/bin/env bash
            echo "install $*" >> "$STUB_LOG"
            args=("$@"); last="${args[-1]}"
            if [[ "$1" == "-d" ]]; then for a in "$@"; do [[ "$a" == /* || "$a" == ?:* ]] && mkdir -p "$a"; done; exit 0; fi
            cp "${args[-2]}" "$last"
            '''), newline="\n")
        stub.chmod(0o755)
        self.payload = self.tmp / "payload"
        (self.payload / "keys").mkdir(parents=True)
        self.log = self.tmp / "stub.log"
        self.anchor = self.tmp / "etc-anyaicam-update" / "entitlement_signing_keys.json"

    def bash_path(self, path):
        text = Path(path).as_posix()
        return "/" + text[0].lower() + text[2:] if os.name == "nt" and text[1:2] == ":" else text

    def run_step(self, keys_bytes, sha):
        if keys_bytes is not None:
            (self.payload / "keys" / "entitlement-signing-public-keys.json").write_bytes(keys_bytes)
        script = (f'set -e; log(){{ echo "$*"; }}; PAYLOAD_DIR="{self.bash_path(self.payload)}"; '
                  f'ENTITLEMENT_SIGNING_KEYS_SHA256="{sha}"; '
                  f'source "{self.bash_path(INSTALLER / "13-entitlement-signing-keys.sh")}"; provision_entitlement_signing_keys')
        env = dict(os.environ, PATH=self.bash_path(self.bin) + os.pathsep + os.environ.get("PATH", ""), STUB_LOG=str(self.log),
                   ANYAICAM_UPDATE_KEY_DIR=self.bash_path(self.tmp / "etc-anyaicam-update"))
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)

    def keyset(self):
        return (json.dumps({"keys": {"k1": PUBLIC}}, sort_keys=True, indent=2) + "\n").encode()

    def test_the_keyset_is_installed_root_owned(self):
        data = self.keyset()
        result = self.run_step(data, hashlib.sha256(data).hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertIn("install -m 0644 -o root -g root", log)
        self.assertIn("entitlement_signing_keys.json", log)
        self.assertIn("-d -m 0755 -o root -g root", log)
        self.assertEqual(self.anchor.read_bytes(), data)

    def test_a_keyset_that_does_not_match_release_env_is_refused(self):
        result = self.run_step(self.keyset(), "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertFalse(self.anchor.exists())

    def test_private_key_material_is_refused(self):
        data = b'{"keys": {"k1": "%s"}, "private_key_b64": "AAAA"}' % PUBLIC.encode()
        result = self.run_step(data, hashlib.sha256(data).hexdigest())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private key", result.stderr)
        self.assertFalse(self.anchor.exists())

    def test_a_named_keyset_missing_from_the_package_is_refused(self):
        result = self.run_step(None, "a" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not contain it", result.stderr)

    def test_a_release_without_a_keyset_keeps_the_existing_one_and_warns_when_none(self):
        none = self.run_step(None, "")
        self.assertEqual(none.returncode, 0, none.stderr)
        self.assertIn("Talk Down and AAC Voice Call stay unavailable", none.stdout)
        self.assertFalse(self.anchor.exists())
        self.anchor.parent.mkdir(parents=True, exist_ok=True)
        self.anchor.write_bytes(self.keyset())
        kept = self.run_step(None, "")
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertIn("left unchanged", kept.stdout)
        self.assertEqual(self.anchor.read_bytes(), self.keyset())


class TrustBoundaryTests(unittest.TestCase):
    def test_the_vms_container_reads_the_anchor_read_only(self):
        compose = _text(REPO / "docker-compose.yml")
        self.assertIn("- /etc/anyaicam-update:/etc/anyaicam-update:ro", compose)
        self.assertNotRegex(compose, r"/etc/anyaicam-update:/etc/anyaicam-update(:rw)?\n")

    def test_the_app_reads_the_installed_path(self):
        source = _text(REPO / "app" / "appliance_entitlements.py")
        self.assertIn('TRUST_ANCHOR_FILE = Path("/etc/anyaicam-update/entitlement_signing_keys.json")', source)
        step = _text(INSTALLER / "13-entitlement-signing-keys.sh")
        self.assertIn('ENTITLEMENT_KEYS_DIR="${ANYAICAM_UPDATE_KEY_DIR:-/etc/anyaicam-update}"', step)
        self.assertIn('ENTITLEMENT_KEYS_FILE="$ENTITLEMENT_KEYS_DIR/entitlement_signing_keys.json"', step)

    def test_the_agent_service_cannot_write_the_anchor(self):
        unit = _text(REPO / "appliance-agent" / "systemd" / "anyaicam-agent.service")
        writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
        self.assertNotIn("/etc/anyaicam-update ", writable + " ")

    def test_validate_checks_the_anchor(self):
        script = _text(INSTALLER / "validate.sh")
        self.assertIn("entitlement_signing_keys_ok", script)
        self.assertIn('[[ "$(stat -c %a "$keys")" == "644" ]]', script)


if __name__ == "__main__":
    unittest.main()
