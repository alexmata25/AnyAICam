"""Paid-feature entitlement trust anchor (2026-10-05, staging finding on
1f66bcd; provenance and path findings from the Codex review of 5c1556e): the
release builder packages the cloud's entitlement-signing PUBLIC keyset only
when it matches a digest obtained independently on the cloud host, the
installer provisions it root-owned at the one fixed path the VMS reads (no
environment override), validate.sh applies the runtime's own ownership/mode
rules, and the VMS container reads it read-only. Nothing the cloud sends at
runtime is a trust anchor (app/appliance_entitlements.py)."""
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
SECOND = base64.b64encode(bytes(range(32, 64))).decode()


def _digest(keys: dict) -> str:
    return hashlib.sha256(builder.canonical_entitlement_keyset(keys)).hexdigest()


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

    def test_the_digest_is_required_with_a_keyset(self):
        missing = self._run(["--entitlement-signing-public-keys", "k.json"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("requires --entitlement-signing-keys-sha256", missing.stderr + missing.stdout)
        stray = self._run(["--no-entitlement-signing-keys", "--entitlement-signing-keys-sha256", "a" * 64])
        self.assertNotEqual(stray.returncode, 0)
        self.assertIn("only used with --entitlement-signing-public-keys", stray.stderr + stray.stdout)
        for bad in ("", "A" * 64, "a" * 63, "not-a-digest"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                builder.read_entitlement_signing_keys(self._keys_file({"keys": {"k1": PUBLIC}}), bad)

    def test_the_keyset_matching_the_independent_digest_is_packaged_in_canonical_form(self):
        keys = {"k2": SECOND, "k1": PUBLIC}
        packaged = builder.read_entitlement_signing_keys(self._keys_file({"keys": keys}), _digest(keys))
        self.assertEqual(packaged, (json.dumps({"keys": {"k1": PUBLIC, "k2": SECOND}}, sort_keys=True, indent=2) + "\n").encode())
        self.assertEqual(hashlib.sha256(packaged).hexdigest(), _digest(keys))
        crlf = builder.read_entitlement_signing_keys(self._keys_file(
            json.dumps({"keys": keys}).replace(",", ",\r\n").encode()), _digest(keys))
        self.assertEqual(crlf, packaged)  # the digest covers the canonical form, not the input formatting

    def test_a_keyset_that_does_not_match_the_digest_is_refused(self):
        expected = _digest({"k1": PUBLIC})
        for label, keys in {"another key": {"k1": SECOND}, "an extra key": {"k1": PUBLIC, "k2": SECOND},
                            "a renamed key": {"k9": PUBLIC},
                            "a private seed in place of the public key": {"k1": base64.b64encode(bytes(32)).decode()}}.items():
            with self.subTest(label), self.assertRaises(SystemExit) as raised:
                builder.read_entitlement_signing_keys(self._keys_file({"keys": keys}), expected)
            self.assertIn("does not match", str(raised.exception))

    def test_private_keys_and_containers_are_refused(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private = Ed25519PrivateKey.generate()
        encodings = {
            "PKCS8 PEM": private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()),
            "OpenSSH": private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                             serialization.NoEncryption()),
            "public PEM container": private.public_key().public_bytes(serialization.Encoding.PEM,
                                                                      serialization.PublicFormat.SubjectPublicKeyInfo),
            "private field": json.dumps({"keys": {"k1": PUBLIC}, "private_key_b64": "AAAA"}).encode(),
            "PEM inside the keyset": json.dumps({"keys": {"k1": private.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}}).encode(),
            "extra field": json.dumps({"keys": {"k1": PUBLIC}, "d": PUBLIC}).encode(),
        }
        for label, content in encodings.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                builder.read_entitlement_signing_keys(self._keys_file(content), _digest({"k1": PUBLIC}))

    def test_malformed_keysets_are_refused(self):
        cases = {
            "64-byte value": {"keys": {"k1": base64.b64encode(bytes(64)).decode()}},
            "33-byte value": {"keys": {"k1": base64.b64encode(bytes(33)).decode()}},
            "not JSON": b"not json",
            "no keys": {"keys": {}},
            "flat dict": {"k1": PUBLIC},
            "list": [PUBLIC],
            "bad id": {"keys": {"bad id": PUBLIC}},
            "short key": {"keys": {"k1": "AAAA"}},
            "not base64": {"keys": {"k1": "not base64!"}},
            "non-string key": {"keys": {"k1": 123}},
        }
        for label, content in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                builder.read_entitlement_signing_keys(self._keys_file(content), _digest({"k1": PUBLIC}))

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
        # The step's destination is fixed; this harness re-points the shell
        # variables after sourcing so the test never writes the real /etc.
        anchor_dir = self.bash_path(self.anchor.parent)
        script = (f'set -e; log(){{ echo "$*"; }}; PAYLOAD_DIR="{self.bash_path(self.payload)}"; '
                  f'ENTITLEMENT_SIGNING_KEYS_SHA256="{sha}"; '
                  f'source "{self.bash_path(INSTALLER / "13-entitlement-signing-keys.sh")}"; '
                  f'ENTITLEMENT_KEYS_DIR="{anchor_dir}"; ENTITLEMENT_KEYS_FILE="{anchor_dir}/entitlement_signing_keys.json"; '
                  f'provision_entitlement_signing_keys')
        env = dict(os.environ, PATH=self.bash_path(self.bin) + os.pathsep + os.environ.get("PATH", ""), STUB_LOG=str(self.log))
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


@unittest.skipUnless(BASH, "bash is required")
class FixedPathTests(unittest.TestCase):
    """No environment variable can move the trust anchor (Codex review of 5c1556e)."""

    def test_the_environment_cannot_redirect_the_step(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = (f'source "{KeysetProvisioningTests.bash_path(None, INSTALLER / "13-entitlement-signing-keys.sh")}"; '
                  'echo "$ENTITLEMENT_KEYS_DIR|$ENTITLEMENT_KEYS_FILE"')
        redirect = KeysetProvisioningTests.bash_path(None, tmp / "attacker")
        env = dict(os.environ, ANYAICAM_UPDATE_KEY_DIR=redirect, ENTITLEMENT_KEYS_DIR=redirect,
                   ENTITLEMENT_KEYS_FILE=redirect + "/keys.json")
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "/etc/anyaicam-update|/etc/anyaicam-update/entitlement_signing_keys.json")

    def test_the_step_installs_only_to_the_fixed_path(self):
        """A log-only `install` stub: the destination it is given is the fixed
        path even with the redirecting environment set."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_dir, payload, log = tmp / "bin", tmp / "payload", tmp / "install.log"
        bin_dir.mkdir()
        (payload / "keys").mkdir(parents=True)
        data = (json.dumps({"keys": {"k1": PUBLIC}}, sort_keys=True, indent=2) + "\n").encode()
        (payload / "keys" / "entitlement-signing-public-keys.json").write_bytes(data)
        stub = bin_dir / "install"
        stub.write_text('#!/usr/bin/env bash\necho "install $*" >> "$STUB_LOG"\n', newline="\n")
        stub.chmod(0o755)
        to_bash = lambda path: KeysetProvisioningTests.bash_path(None, path)  # noqa: E731
        script = (f'set -e; log(){{ echo "$*"; }}; PAYLOAD_DIR="{to_bash(payload)}"; '
                  f'ENTITLEMENT_SIGNING_KEYS_SHA256="{hashlib.sha256(data).hexdigest()}"; '
                  f'source "{to_bash(INSTALLER / "13-entitlement-signing-keys.sh")}"; provision_entitlement_signing_keys')
        env = dict(os.environ, PATH=to_bash(bin_dir) + os.pathsep + os.environ.get("PATH", ""), STUB_LOG=str(log),
                   ANYAICAM_UPDATE_KEY_DIR=to_bash(tmp / "attacker"), ENTITLEMENT_KEYS_DIR=to_bash(tmp / "attacker"))
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text().splitlines()
        self.assertEqual(calls[0], "install -d -m 0755 -o root -g root /etc/anyaicam-update")
        self.assertTrue(calls[-1].startswith("install -m 0644 -o root -g root "))
        self.assertTrue(calls[-1].endswith(" /etc/anyaicam-update/entitlement_signing_keys.json"))
        self.assertNotIn("attacker", log.read_text())


@unittest.skipUnless(BASH, "bash is required")
class ValidateTrustAnchorTests(unittest.TestCase):
    """validate.sh's entitlement_signing_keys_ok, with `stat` stubbed to report
    the owner/mode a real host would, applies the runtime's own rules
    (app/appliance_entitlements.py ownership_problem)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "anchor"
        self.dir.mkdir()
        self.data = (json.dumps({"keys": {"k1": PUBLIC}}, sort_keys=True, indent=2) + "\n").encode()
        (self.dir / "entitlement_signing_keys.json").write_bytes(self.data)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "stat"
        # stat -c %U|%a PATH -> STUB_<DIR|FILE>_<OWNER|MODE>
        stub.write_text(textwrap.dedent('''\
            #!/usr/bin/env bash
            fmt="$2"; path="$3"
            case "$path" in *.json) kind=FILE ;; *) kind=DIR ;; esac
            if [[ "$fmt" == "%U" ]]; then var="STUB_${kind}_OWNER"; else var="STUB_${kind}_MODE"; fi
            echo "${!var}"
            '''), newline="\n")
        stub.chmod(0o755)

    def validate(self, dir_owner="root", dir_mode="755", file_owner="root", file_mode="644", sha=None):
        to_bash = lambda path: KeysetProvisioningTests.bash_path(None, path)  # noqa: E731
        script = (f'source "{to_bash(INSTALLER / "validate.sh")}"; '
                  f'ENTITLEMENT_KEYS_FILE="{to_bash(self.dir / "entitlement_signing_keys.json")}"; '
                  f'ENTITLEMENT_SIGNING_KEYS_SHA256="{sha or hashlib.sha256(self.data).hexdigest()}"; '
                  'entitlement_signing_keys_ok')
        env = dict(os.environ, PATH=to_bash(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                   STUB_DIR_OWNER=dir_owner, STUB_DIR_MODE=dir_mode, STUB_FILE_OWNER=file_owner, STUB_FILE_MODE=file_mode)
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env).returncode

    def test_a_root_owned_safe_anchor_passes(self):
        self.assertEqual(self.validate(), 0)
        self.assertEqual(self.validate(dir_mode="700", file_mode="444"), 0)

    def test_an_unsafe_directory_fails(self):
        for label, overrides in {"group-writable directory": {"dir_mode": "775"},
                                 "world-writable directory": {"dir_mode": "757"},
                                 "sticky world-writable directory": {"dir_mode": "1777"},
                                 "directory not owned by root": {"dir_owner": "anyaicam"}}.items():
            with self.subTest(label):
                self.assertNotEqual(self.validate(**overrides), 0)

    def test_an_unsafe_keyset_fails(self):
        for label, overrides in {"group-writable keyset": {"file_mode": "664"},
                                 "world-writable keyset": {"file_mode": "646"},
                                 "keyset not owned by root": {"file_owner": "anyaicam"},
                                 "unreadable mode": {"file_mode": "rw-r--r--"},
                                 "another keyset": {"sha": "0" * 64}}.items():
            with self.subTest(label):
                self.assertNotEqual(self.validate(**overrides), 0)

    def test_a_missing_keyset_fails(self):
        (self.dir / "entitlement_signing_keys.json").unlink()
        self.assertNotEqual(self.validate(), 0)


class TrustBoundaryTests(unittest.TestCase):
    def test_the_vms_container_reads_the_anchor_read_only(self):
        compose = _text(REPO / "docker-compose.yml")
        self.assertIn("- /etc/anyaicam-update:/etc/anyaicam-update:ro", compose)
        self.assertNotRegex(compose, r"/etc/anyaicam-update:/etc/anyaicam-update(:rw)?\n")

    def test_the_app_reads_the_installed_path(self):
        source = _text(REPO / "app" / "appliance_entitlements.py")
        self.assertIn('TRUST_ANCHOR_FILE = Path("/etc/anyaicam-update/entitlement_signing_keys.json")', source)
        step = _text(INSTALLER / "13-entitlement-signing-keys.sh")
        self.assertIn('ENTITLEMENT_KEYS_DIR="/etc/anyaicam-update"\n', step)
        self.assertNotIn("ANYAICAM_UPDATE_KEY_DIR", step)
        self.assertIn('ENTITLEMENT_KEYS_FILE="$ENTITLEMENT_KEYS_DIR/entitlement_signing_keys.json"', step)

    def test_the_agent_service_cannot_write_the_anchor(self):
        unit = _text(REPO / "appliance-agent" / "systemd" / "anyaicam-agent.service")
        writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
        self.assertNotIn("/etc/anyaicam-update ", writable + " ")

    def test_validate_checks_the_anchor(self):
        script = _text(INSTALLER / "validate.sh")
        self.assertIn('check "Entitlement-signing keyset is provisioned root-owned" entitlement_signing_keys_ok', script)
        self.assertIn('trust_anchor_entry_ok "$(dirname "$keys")" dir', script)
        self.assertNotIn("${ENTITLEMENT_KEYS_FILE:-", script)


if __name__ == "__main__":
    unittest.main()
