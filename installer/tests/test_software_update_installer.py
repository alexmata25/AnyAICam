"""Software Update (2026-10-03), installer side: the release builder carries
the dotted release version and the signing PUBLIC key (never a private one),
the installer provisions that key root-owned outside every directory the
anyaicam user owns, the root-only applier is installed root-owned, and the
offline signer refuses anything an appliance would refuse."""
import base64
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
AGENT = REPO / "appliance-agent"
sys.path.insert(0, str(INSTALLER))
sys.path.insert(0, str(AGENT))
sys.path.insert(0, str(AGENT / "tests"))

import build_release_installer as builder  # noqa: E402
import sign_update_release as signer  # noqa: E402
from software_update_helpers import (BUILD_A, BUILD_B, MIGRATIONS_DESTRUCTIVE, generate_keypair,  # noqa: E402
                                     release_files, write_tarball)

BASH = os.environ.get("TEST_BASH") or shutil.which("bash")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class BuilderTests(unittest.TestCase):
    def _run(self, extra):
        cmd = [sys.executable, str(INSTALLER / "build_release_installer.py"), "--vms-commit", "0" * 40,
               "--vms-repo", "/nonexistent-repo-path-never-reached", "--no-mediamtx"] + extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_the_signing_key_is_a_deliberate_choice_like_mediamtx(self):
        neither = self._run([])
        self.assertNotEqual(neither.returncode, 0)
        self.assertIn("--update-signing-public-key", neither.stderr + neither.stdout)
        both = self._run(["--update-signing-public-key", "k.pem", "--no-update-signing-key"])
        self.assertIn("mutually exclusive", both.stderr + both.stdout)

    def test_release_versions_are_dotted(self):
        self.assertEqual(builder.validate_release_version("1.2.0"), "1.2.0")
        for bad in ("1.2.0-rc1", "v1", "", "1"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                builder.validate_release_version(bad)

    def test_only_a_public_key_can_ever_be_packaged(self):
        private_key, public_pem = generate_keypair()
        from cryptography.hazmat.primitives import serialization
        private_pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption())
        with tempfile.TemporaryDirectory() as tmp:
            good, bad, junk = Path(tmp, "pub.pem"), Path(tmp, "priv.pem"), Path(tmp, "junk.pem")
            good.write_bytes(public_pem)
            bad.write_bytes(private_pem)
            junk.write_bytes(b"not a key")
            self.assertIn(b"BEGIN PUBLIC KEY", builder.read_update_signing_public_key(str(good)))
            with self.assertRaises(SystemExit) as raised:
                builder.read_update_signing_public_key(str(bad))
            self.assertIn("PRIVATE", str(raised.exception))
            with self.assertRaises(SystemExit):
                builder.read_update_signing_public_key(str(junk))

    def test_the_new_install_step_ships_in_every_installer(self):
        self.assertIn("12-update-signing-key.sh", builder.INSTALLER_RUNTIME_FILES)
        install = _text(INSTALLER / "install.sh")
        self.assertIn('source "$INSTALLER_DIR/12-update-signing-key.sh"', install)
        self.assertLess(install.index("identity_provision \"$INSTALL_STATE\""), install.index("provision_update_signing_key"))


@unittest.skipUnless(BASH, "bash is required")
class KeyProvisioningTests(unittest.TestCase):
    """12-update-signing-key.sh with `install` stubbed to record owner/mode."""

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

    def bash_path(self, path):
        text = Path(path).as_posix()
        return "/" + text[0].lower() + text[2:] if os.name == "nt" and text[1:2] == ":" else text

    def run_step(self, key_bytes, sha):
        (self.payload / "keys" / "update-signing-public-key.pem").write_bytes(key_bytes)
        script = (f'set -e; log(){{ echo "$*"; }}; PAYLOAD_DIR="{self.bash_path(self.payload)}"; '
                  f'UPDATE_SIGNING_KEY_SHA256="{sha}"; '
                  f'source "{self.bash_path(INSTALLER / "12-update-signing-key.sh")}"; provision_update_signing_key')
        env = dict(os.environ, PATH=self.bash_path(self.bin) + os.pathsep + os.environ.get("PATH", ""), STUB_LOG=str(self.log),
                   ANYAICAM_UPDATE_KEY_DIR=self.bash_path(self.tmp / "etc-anyaicam-update"),
                   ANYAICAM_UPDATE_STATE_DIR=self.bash_path(self.tmp / "var-lib-anyaicam-update"))
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)

    def test_the_key_and_root_state_are_installed_root_owned(self):
        import hashlib
        _private, public_pem = generate_keypair()
        result = self.run_step(public_pem, hashlib.sha256(public_pem).hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text()
        self.assertIn("install -m 0644 -o root -g root", log)
        self.assertIn("trusted_signing_key.pem", log)
        self.assertIn("-d -m 0755 -o root -g root", log)
        self.assertIn("-d -m 0700 -o root -g root", log)  # root's work area
        self.assertEqual((self.tmp / "etc-anyaicam-update" / "trusted_signing_key.pem").read_bytes(), public_pem)

    def test_a_key_that_does_not_match_release_env_is_refused(self):
        _private, public_pem = generate_keypair()
        result = self.run_step(public_pem, "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)
        self.assertFalse((self.tmp / "etc-anyaicam-update" / "trusted_signing_key.pem").exists())

    def test_a_private_key_is_refused(self):
        import hashlib
        data = b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        result = self.run_step(data, hashlib.sha256(data).hexdigest())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("private key", result.stderr)


class PrivilegeBoundaryTests(unittest.TestCase):
    def test_the_agent_install_root_is_root_owned(self):
        script = _text(AGENT / "scripts" / "install.sh")
        self.assertIn("install -d -m 0755 -o root -g root /opt/anyaicam-agent", script)
        self.assertNotRegex(script, r"install -d [^\n]*-o anyaicam[^\n]*/opt/anyaicam-agent")

    def test_the_applier_and_its_checks_are_installed_root_only(self):
        script = _text(AGENT / "scripts" / "lib-privileged-watcher.sh")
        self.assertIn('install -m 0700 -o root -g root "$applier_src" "$watcher_dir/apply_release.py"', script)
        self.assertIn('install -m 0600 -o root -g root "$checks_src" "$watcher_dir/anyaicam_release_checks.py"', script)
        self.assertIn('install -d -m 0700 -o root -g root "$watcher_dir"', script)

    def test_the_agent_service_cannot_write_root_update_state_or_the_key(self):
        unit = _text(AGENT / "systemd" / "anyaicam-agent.service")
        writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
        for path in ("/var/lib/anyaicam-update", "/etc/anyaicam-update", "/opt/anyaicam"):
            self.assertNotIn(path + " ", writable + " ")
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("NoNewPrivileges=true", unit)

    def test_validate_checks_the_release_version_and_the_trust_anchor(self):
        script = _text(INSTALLER / "validate.sh")
        self.assertIn("vms_reports_release_version", script)
        self.assertIn("update_signing_key_ok", script)

    def test_repairs_stop_the_vms_before_rewriting_its_bind_mounted_tree(self):
        script = _text(INSTALLER / "06-deploy-vms.sh")
        stop = script.index("systemctl stop anyaicam-vms.service")
        self.assertLess(stop, script.index('rsync -a --no-owner --no-group --delete'))


class OfflineSignerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.private_key, self.public_pem = generate_keypair()
        from cryptography.hazmat.primitives import serialization
        self.key_file = self.tmp / "offline-signing-key.pem"
        self.key_file.write_bytes(self.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                                 serialization.NoEncryption()))

    def _installer(self, version, build, migrations=None, key=None):
        files = release_files(version, build, **({"migrations": migrations} if migrations else {}))
        files["payload/keys/update-signing-public-key.pem"] = key if key is not None else self.public_pem
        listing = json.loads(files.pop("artifact-files.json"))
        import hashlib
        listing.append({"path": "payload/keys/update-signing-public-key.pem",
                        "sha256": hashlib.sha256(files["payload/keys/update-signing-public-key.pem"]).hexdigest()})
        files["artifact-files.json"] = json.dumps(listing).encode()
        return write_tarball(self.tmp / f"installer-{version}.tar.gz", files)

    def test_a_signed_release_verifies_the_way_appliances_check_it(self):
        out = self.tmp / "out"
        code = signer.main(["--installer", str(self._installer("1.2.0", BUILD_B)), "--previous-installer",
                            str(self._installer("1.1.0", BUILD_A)), "--signing-key", str(self.key_file), "--out-dir", str(out)])
        self.assertEqual(code, 0)
        manifest = json.loads((out / "manifest.json").read_text())
        signature = base64.b64decode((out / "manifest.sig").read_bytes())
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        serialization.load_pem_public_key(self.public_pem).verify(signature, signer.canonical_manifest_bytes(manifest),
                                                                  padding.PKCS1v15(), hashes.SHA256())
        self.assertEqual((manifest["version"], manifest["build_id"], manifest["migration_safety"]), ("1.2.0", BUILD_B, "additive"))

    def test_destructive_migrations_are_never_signed(self):
        with self.assertRaises(SystemExit) as raised:
            signer.main(["--installer", str(self._installer("1.2.0", BUILD_B, migrations=MIGRATIONS_DESTRUCTIVE)),
                         "--previous-installer", str(self._installer("1.1.0", BUILD_A)), "--signing-key", str(self.key_file),
                         "--out-dir", str(self.tmp / "out")])
        self.assertIn("database", str(raised.exception))

    def test_a_key_that_appliances_would_not_trust_is_refused(self):
        _other, other_public = generate_keypair()
        with self.assertRaises(SystemExit) as raised:
            signer.main(["--installer", str(self._installer("1.2.0", BUILD_B, key=other_public)), "--first-release",
                         "--signing-key", str(self.key_file), "--out-dir", str(self.tmp / "out")])
        self.assertIn("does not match", str(raised.exception))

    def test_a_signing_key_inside_the_repository_is_refused(self):
        inside = REPO / "installer" / "tests" / "_must_not_exist_signing_key.pem"
        inside.write_bytes(self.key_file.read_bytes())
        self.addCleanup(inside.unlink)
        with self.assertRaises(SystemExit) as raised:
            signer.main(["--installer", str(self._installer("1.2.0", BUILD_B)), "--first-release", "--signing-key",
                         str(inside), "--out-dir", str(self.tmp / "out")])
        self.assertIn("inside the repository", str(raised.exception))

    def test_a_previous_release_is_required_for_the_migration_check(self):
        with self.assertRaises(SystemExit):
            signer.main(["--installer", str(self._installer("1.2.0", BUILD_B)), "--signing-key", str(self.key_file),
                         "--out-dir", str(self.tmp / "out")])


if __name__ == "__main__":
    unittest.main()
