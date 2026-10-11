"""Verified preloaded VMS image (2026-10-11).

A release built with build_release_installer.py --preload-vms-image ships the
VMS image (docker save, compressed) inside the installer, recorded in
release.env. prepare_vms_image() then runs `docker load` instead of `docker
build` -- but only when the archive SHA-256, staged payload digest, loaded
image ID and labels all match the release; anything else falls back to the
existing build. These tests source the production 06-deploy-vms.sh with
docker/df stubs on PATH (no real Docker) and temporary folders only.
"""
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_release_installer as builder  # noqa: E402

BASH = os.environ.get("TEST_BASH") or shutil.which("bash")
RSYNC = shutil.which("rsync")
COMMIT = "c" * 40
SHORT = COMMIT[:12]
IMAGE_ID = "sha256:" + "1" * 64
CONFIG_DIGEST = "sha256:" + "2" * 64
GIB = 1024 ** 3


def bash_path(path: Path) -> str:
    value = path.as_posix()
    if os.name == "nt" and len(value) > 1 and value[1] == ":":
        return "/" + value[0].lower() + value[2:]
    return value


def q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


DOCKER_STUB = textwrap.dedent(r'''\
    #!/usr/bin/env bash
    printf 'docker %s\n' "$*" >> "$PL_CALL_LOG"
    case "$1" in
      info) printf '%s\n' "$PL_DOCKER_ROOT"; exit 0 ;;
      build) exit "${PL_BUILD_STATUS:-0}" ;;
      load)
        file="${@: -1}"
        sha256sum "$file" | awk '{print "loaded-sha " $1}' >> "$PL_CALL_LOG"
        [[ "${PL_LOAD:-ok}" == ok ]] || { echo 'simulated load failure' >&2; exit 1; }
        touch "$PL_LOADED_MARK"; echo "Loaded image: $PL_REF"; exit 0 ;;
      image)
        if [[ "$2" == inspect ]]; then
          [[ -f "$PL_LOADED_MARK" || "${PL_PREEXISTING:-0}" == 1 ]] || exit 1
          case "$4" in
            *release-commit*) printf '%s\n' "$PL_LABEL_COMMIT" ;;
            *payload-sha256*) printf '%s\n' "$PL_LABEL_PAYLOAD" ;;
            *) printf '%s\n' "$PL_LOADED_ID" ;;
          esac
          exit 0
        fi
        exit 0 ;;
      tag) exit 0 ;;
    esac
    exit 0
    ''')

DF_STUB = textwrap.dedent(r'''\
    #!/usr/bin/env bash
    printf 'df %s\n' "$*" >> "$PL_CALL_LOG"
    case "${@: -1}" in
      *containerd*) printf 'Avail\n%s\n' "${PL_FREE_CONTAINERD:-$PL_FREE_BYTES}" ;;
      *) printf 'Avail\n%s\n' "$PL_FREE_BYTES" ;;
    esac
    ''')


@unittest.skipUnless(BASH and RSYNC, "bash and rsync are required")
class PreloadedImageInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-preload-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("df", DF_STUB)):
            path = self.bin / name
            # Shebang at byte 0: GNU timeout execs the stub directly, and
            # without one execvp falls back to /bin/sh (no bash syntax).
            body = textwrap.dedent(body.split("\n", 1)[1]) if body.startswith("\\") else body
            path.write_text(body, encoding="utf-8", newline="\n")
            path.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.installer = self.tmp / "installer"
        self.payload = self.installer / "payload" / "vms"
        (self.payload / "app" / "static").mkdir(parents=True)
        (self.payload / "app" / "main.py").write_text("print('vms')\n")
        (self.payload / "app" / "static" / "release-identity.json").write_text('{"build_id":"%s"}\n' % COMMIT)
        (self.payload / "Dockerfile").write_text("FROM scratch\n")
        (self.payload / "requirements.txt").write_text("fastapi\n")
        (self.payload / "docker-compose.yml").write_text("services: {}\n")
        self.image = self.installer / "images" / f"anyaicam-vms-{SHORT}.tar.zst"
        self.image.parent.mkdir(parents=True)
        self.image.write_bytes(os.urandom(4096))
        self.payload_sha = builder.vms_payload_digest(self.payload)
        self.staging = self.tmp / "staging"
        self.docker_root = self.tmp / "docker-root"
        self.docker_root.mkdir()
        self.loaded_mark = self.tmp / "loaded"
        self.containerd_root = self.tmp / "containerd-root"
        self.containerd_root.mkdir()

    def release(self, **overrides):
        values = {
            "VMS_PRELOAD_IMAGE_FILE": f"images/{self.image.name}",
            "VMS_PRELOAD_IMAGE_SHA256": hashlib.sha256(self.image.read_bytes()).hexdigest(),
            "VMS_PRELOAD_IMAGE_BYTES": str(self.image.stat().st_size),
            "VMS_PRELOAD_IMAGE_UNPACKED_BYTES": "8192",
            "VMS_PRELOAD_IMAGE_COMPRESSION": "zstd",
            "VMS_PRELOAD_IMAGE_REF": f"anyaicam-vms-preload:{SHORT}",
            "VMS_PRELOAD_IMAGE_ID": IMAGE_ID,
            "VMS_PRELOAD_IMAGE_CONFIG_DIGEST": CONFIG_DIGEST,
            "VMS_PRELOAD_IMAGE_PAYLOAD_SHA256": self.payload_sha,
            "VMS_PRELOAD_IMAGE_PLATFORM": "linux/amd64",
        }
        values.update(overrides)
        return values

    def run_prepare(self, release=None, extra_env=None, prelude="", **stub):
        release = self.release() if release is None else release
        lines = "\n".join(f"{key}={q(value)}" for key, value in release.items())
        script = textwrap.dedent(f'''\
            set -euo pipefail
            log() {{ printf 'LOG %s\\n' "$*"; }}
            uname() {{ [[ "${{1:-}}" == -m ]] && {{ echo x86_64; return 0; }}; command uname "$@"; }}
            source {q(bash_path(ROOT / "06-deploy-vms.sh"))}
            INSTALLER_DIR={q(bash_path(self.installer))}
            VMS_PAYLOAD_DIR={q(bash_path(self.payload))}
            VMS_RELEASE_COMMIT={COMMIT}
            ''') + prelude + "\n" + lines + "\nprepare_vms_image\necho \"PREPARED=$PREPARED_VMS_IMAGE_TAG\"\n"
        env = dict(os.environ)
        env.update({
            "PATH": bash_path(self.bin) + ":" + env.get("PATH", ""),
            "PL_CALL_LOG": bash_path(self.log),
            "PL_DOCKER_ROOT": bash_path(self.docker_root),
            "PL_LOADED_MARK": bash_path(self.loaded_mark),
            "PL_REF": f"anyaicam-vms-preload:{SHORT}",
            "PL_LOADED_ID": IMAGE_ID,
            "PL_LABEL_COMMIT": COMMIT,
            "PL_LABEL_PAYLOAD": self.payload_sha,
            "PL_FREE_BYTES": str(100 * GIB),
            "ANYAICAM_CONTAINERD_ROOT": bash_path(self.containerd_root),
            "ANYAICAM_INSTALLER_STAGING_ROOT": bash_path(self.staging),
        })
        env.update(stub)
        env.update(extra_env or {})
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env, timeout=120)
        calls = self.log.read_text() if self.log.exists() else ""
        return result, calls

    def assert_built_not_loaded(self, result, calls, *, loaded=False):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"PREPARED=anyaicam-vms:release-{SHORT}", result.stdout)
        self.assertIn("docker build", calls)
        self.assertEqual("docker load" in calls, loaded)
        self.assertNotIn(f"docker tag {IMAGE_ID}", calls)
        self.assertEqual(list(self.staging.iterdir()), [], "staging copies must be cleaned up")

    def test_valid_preload_loads_tags_and_never_builds(self):
        result, calls = self.run_prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"PREPARED=anyaicam-vms:release-{SHORT}", result.stdout)
        self.assertNotIn("docker build", calls)
        self.assertIn("docker load -q -i", calls)
        self.assertIn("loaded-sha " + hashlib.sha256(self.image.read_bytes()).hexdigest(), calls)
        self.assertIn(f"docker tag {IMAGE_ID} anyaicam-vms:release-{SHORT}", calls)
        self.assertIn(f"docker image rm anyaicam-vms-preload:{SHORT}", calls)
        self.assertNotIn(bash_path(self.image), calls.split("docker load", 1)[1].splitlines()[0],
                         "the root-only verified copy is loaded, not the file in the installer folder")
        self.assertIn("no image build needed", result.stdout)
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_a_classic_store_image_id_equal_to_the_config_digest_is_accepted(self):
        result, calls = self.run_prepare(PL_LOADED_ID=CONFIG_DIGEST)
        self.assertNotIn("docker build", calls)
        self.assertIn(f"docker tag {CONFIG_DIGEST} anyaicam-vms:release-{SHORT}", calls)

    def test_sha_mismatch_falls_back_to_build_and_never_loads_the_archive(self):
        result, calls = self.run_prepare(self.release(VMS_PRELOAD_IMAGE_SHA256="0" * 64))
        self.assert_built_not_loaded(result, calls)
        self.assertIn("SHA-256", result.stdout)
        self.assertIn("NOT loaded", result.stdout)

    def test_archive_replaced_after_verification_is_caught_by_the_copy_check(self):
        release = self.release()
        self.image.write_bytes(os.urandom(4096))   # same size, different bytes
        result, calls = self.run_prepare(release)
        self.assert_built_not_loaded(result, calls)

    def test_image_id_mismatch_after_load_falls_back_and_untags(self):
        result, calls = self.run_prepare(PL_LOADED_ID="sha256:" + "9" * 64)
        self.assert_built_not_loaded(result, calls, loaded=True)
        self.assertIn(f"docker image rm anyaicam-vms-preload:{SHORT}", calls)
        self.assertIn("image ID", result.stdout)

    def test_commit_label_mismatch_falls_back(self):
        result, calls = self.run_prepare(PL_LABEL_COMMIT="d" * 40)
        self.assert_built_not_loaded(result, calls, loaded=True)
        self.assertIn("labels", result.stdout)

    def test_payload_label_mismatch_falls_back(self):
        result, calls = self.run_prepare(PL_LABEL_PAYLOAD="e" * 64)
        self.assert_built_not_loaded(result, calls, loaded=True)
        self.assertIn("labels", result.stdout)

    def test_staged_payload_that_differs_from_the_image_payload_falls_back_without_loading(self):
        release = self.release()
        (self.payload / "app" / "main.py").write_text("print('changed')\n")
        result, calls = self.run_prepare(release)
        self.assert_built_not_loaded(result, calls)
        self.assertIn("not the one the image was built from", result.stdout)

    def test_missing_archive_builds_as_today(self):
        release = self.release()
        self.image.unlink()
        result, calls = self.run_prepare(release)
        self.assert_built_not_loaded(result, calls)
        self.assertIn("missing", result.stdout)

    def test_release_without_a_preloaded_image_builds_exactly_as_before(self):
        result, calls = self.run_prepare(release={})
        self.assert_built_not_loaded(result, calls)
        self.assertNotIn("Preloaded", result.stdout)
        self.assertNotIn("docker info", calls)

    def test_low_disk_space_falls_back_with_a_message(self):
        result, calls = self.run_prepare(PL_FREE_BYTES=str(GIB // 2))
        self.assert_built_not_loaded(result, calls)
        self.assertIn("not enough free disk space", result.stdout)

    def test_load_failure_falls_back(self):
        result, calls = self.run_prepare(PL_LOAD="fail")
        self.assert_built_not_loaded(result, calls, loaded=True)
        self.assertIn("docker load failed", result.stdout)

    def test_wrong_platform_falls_back(self):
        result, calls = self.run_prepare(self.release(VMS_PRELOAD_IMAGE_PLATFORM="linux/arm64"))
        self.assert_built_not_loaded(result, calls)

    def test_malformed_metadata_falls_back(self):
        for key, value in (("VMS_PRELOAD_IMAGE_ID", "latest"), ("VMS_PRELOAD_IMAGE_FILE", "../evil.tar.zst"),
                           ("VMS_PRELOAD_IMAGE_REF", "anyaicam-vms:latest"), ("VMS_PRELOAD_IMAGE_BYTES", "0")):
            with self.subTest(key=key):
                self.log.unlink(missing_ok=True)
                result, calls = self.run_prepare(self.release(**{key: value}))
                self.assert_built_not_loaded(result, calls)

    def test_explicit_archive_inside_the_bundle_is_used_and_outside_is_refused(self):
        inside = self.installer / "images" / "copy.tar.zst"
        shutil.copy(self.image, inside)
        result, calls = self.run_prepare(extra_env={"ANYAICAM_VMS_IMAGE_TAR": bash_path(inside)})
        self.assertNotIn("docker build", calls)
        self.log.unlink()
        outside = self.tmp / "outside.tar.zst"
        shutil.copy(self.image, outside)
        result, calls = self.run_prepare(extra_env={"ANYAICAM_VMS_IMAGE_TAR": bash_path(outside)})
        self.assert_built_not_loaded(result, calls)
        self.assertIn("outside the verified installer folder", result.stdout)

    def test_preload_can_be_disabled(self):
        result, calls = self.run_prepare(extra_env={"ANYAICAM_VMS_PRELOAD": "0"})
        self.assert_built_not_loaded(result, calls)

    # A stat shim reporting the containerd root as its own filesystem.
    SEPARATE_CONTAINERD_FS = textwrap.dedent("""\
        stat() {
            if [[ "${1:-}" == -c && "${2:-}" == %d && "${@: -1}" == *containerd* ]]; then echo 4242; return 0; fi
            command stat "$@"
        }
        """)

    def test_low_space_on_a_separate_containerd_filesystem_falls_back(self):
        result, calls = self.run_prepare(prelude=self.SEPARATE_CONTAINERD_FS, PL_FREE_CONTAINERD=str(GIB // 2))
        self.assert_built_not_loaded(result, calls)
        self.assertIn("not enough free disk space", result.stdout)
        self.assertIn("containerd-root", result.stdout)
        self.assertIn("df --output=avail -B1 " + bash_path(self.containerd_root), calls)

    def test_enough_space_on_a_separate_containerd_filesystem_loads(self):
        result, calls = self.run_prepare(prelude=self.SEPARATE_CONTAINERD_FS, PL_FREE_CONTAINERD=str(3 * GIB))
        self.assertNotIn("docker build", calls)
        self.assertIn("df --output=avail -B1 " + bash_path(self.containerd_root), calls)

    def test_one_filesystem_counts_the_image_once(self):
        release = self.release()
        archive, unpacked = int(release["VMS_PRELOAD_IMAGE_BYTES"]), int(release["VMS_PRELOAD_IMAGE_UNPACKED_BYTES"])
        exact = (archive + GIB) + (archive + unpacked + GIB)   # archive copy + image, not image twice
        result, calls = self.run_prepare(release, PL_FREE_BYTES=str(exact))
        self.assertNotIn("docker build", calls)
        self.assertEqual(calls.count("df --output=avail"), 1, "one filesystem is checked once")
        self.log.unlink()
        self.loaded_mark.unlink()
        result, calls = self.run_prepare(release, PL_FREE_BYTES=str(exact - 1))
        self.assert_built_not_loaded(result, calls)

    def test_a_preload_tag_that_existed_before_this_run_is_never_removed(self):
        for stub in ({"PL_LOAD": "fail"}, {"PL_LOADED_ID": "sha256:" + "9" * 64}, {}):
            with self.subTest(stub=stub):
                self.log.unlink(missing_ok=True)
                self.loaded_mark.unlink(missing_ok=True)
                result, calls = self.run_prepare(PL_PREEXISTING="1", **stub)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("docker image rm", calls)

    def test_a_preload_tag_created_by_this_run_is_removed_on_load_failure(self):
        result, calls = self.run_prepare(PL_LOAD="fail")
        self.assertIn(f"docker image rm anyaicam-vms-preload:{SHORT}", calls)

    def test_build_failure_after_a_rejected_preload_still_fails_closed(self):
        result, calls = self.run_prepare(self.release(VMS_PRELOAD_IMAGE_SHA256="0" * 64), PL_BUILD_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PREPARED=", result.stdout)
        self.assertNotIn("docker load", calls)


@unittest.skipUnless(BASH, "bash is required")
class PreloadedImageTrustTests(unittest.TestCase):
    """The archive and the release.env values describing it are only trusted
    because artifact-files.json lists them: verify_installer_payload refuses
    the installer, as it does for any other file, when either was changed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-preload-trust-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "images").mkdir()
        (self.tmp / "images" / f"anyaicam-vms-{SHORT}.tar.zst").write_bytes(b"image bytes")
        (self.tmp / "release.env").write_text(f"VMS_PRELOAD_IMAGE_SHA256={'a' * 64}\n")
        rows = builder.file_manifest(self.tmp)
        (self.tmp / "artifact-files.json").write_text(json.dumps(rows))

    def verify(self):
        script = (f"log() {{ :; }}; source {q(bash_path(ROOT / '01-preflight.sh'))}; "
                  f"verify_installer_payload {q(bash_path(self.tmp / 'artifact-files.json'))}")
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True)

    def test_untouched_package_verifies(self):
        self.assertEqual(self.verify().returncode, 0)

    def test_changed_image_archive_is_refused(self):
        (self.tmp / "images" / f"anyaicam-vms-{SHORT}.tar.zst").write_bytes(b"other bytes")
        self.assertNotEqual(self.verify().returncode, 0)

    def test_changed_recorded_image_metadata_is_refused(self):
        (self.tmp / "release.env").write_text(f"VMS_PRELOAD_IMAGE_SHA256={'b' * 64}\n")
        self.assertNotEqual(self.verify().returncode, 0)

    def test_install_verifies_the_package_before_preparing_the_image(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertLess(install.index("    verify_installer_payload || return 1\n"),
                        install.index("    prepare_vms_image\n"))
        # Only release.env can set the preload metadata: install.sh resets it first.
        self.assertLess(install.index('VMS_PRELOAD_IMAGE_SHA256=""'), install.index("load_release_metadata() {"))


@unittest.skipUnless(BASH, "bash is required")
class ReleaseEnvParserTests(unittest.TestCase):
    """release.env is parsed as data, never sourced: a modified file can never
    run a command, and only the builder's own KEY=VALUE lines are accepted."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="anyaicam-release-env-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.marker = self.tmp / "PWNED"
        self.env_file = self.tmp / "release.env"

    def parse(self, content: bytes):
        self.env_file.write_bytes(content)
        script = (f"source {q(bash_path(ROOT / 'install.sh'))}; set +e; "
                  f"parse_release_env {q(bash_path(self.env_file))}; rc=$?; "
                  'echo "RC=$rc COMMIT=${VMS_RELEASE_COMMIT:-} SHA=${VMS_PRELOAD_IMAGE_SHA256:-} PATHV=$PATH"')
        return subprocess.run([BASH, "-c", script], capture_output=True, text=True, cwd=self.tmp)

    def builder_release_env(self) -> bytes:
        # Exactly the lines build_release_installer.py writes (see its main()).
        fields = {
            "VMS_RELEASE_COMMIT": COMMIT, "VMS_RELEASE_SHA256": "a" * 64, "INSTALLER_SOURCE_COMMIT": "b" * 40,
            "MEDIAMTX_INCLUDED": "false", "MEDIAMTX_SHA256": "", "RELEASE_VERSION": "1.2.5",
            "UPDATE_SIGNING_KEY_SHA256": "", "ENTITLEMENT_SIGNING_KEYS_SHA256": "c" * 64,
            "VMS_PRELOAD_IMAGE_FILE": f"images/anyaicam-vms-{SHORT}.tar.zst", "VMS_PRELOAD_IMAGE_SHA256": "d" * 64,
            "VMS_PRELOAD_IMAGE_BYTES": "866721451", "VMS_PRELOAD_IMAGE_UNPACKED_BYTES": "3405931699",
            "VMS_PRELOAD_IMAGE_COMPRESSION": "zstd", "VMS_PRELOAD_IMAGE_REF": f"anyaicam-vms-preload:{SHORT}",
            "VMS_PRELOAD_IMAGE_ID": IMAGE_ID, "VMS_PRELOAD_IMAGE_CONFIG_DIGEST": CONFIG_DIGEST,
            "VMS_PRELOAD_IMAGE_PAYLOAD_SHA256": "e" * 64, "VMS_PRELOAD_IMAGE_PLATFORM": "linux/amd64",
        }
        return "".join(f"{key}={value}\n" for key, value in fields.items()).encode()

    def test_builder_output_parses(self):
        result = self.parse(self.builder_release_env())
        self.assertIn(f"RC=0 COMMIT={COMMIT} SHA={'d' * 64}", result.stdout, result.stderr)

    def test_every_builder_key_is_accepted(self):
        import re
        source = (ROOT / "build_release_installer.py").read_text(encoding="utf-8")
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        keys = set(re.findall(r'f"([A-Z0-9_]+)=\{', source)) | set(re.findall(r'"(VMS_PRELOAD_IMAGE_[A-Z_]+)"', source))
        self.assertGreater(len(keys), 15)
        allowed = install.split('RELEASE_ENV_KEYS="', 1)[1].split('"', 1)[0].replace("\\\n", " ").split()
        self.assertEqual(sorted(keys - set(allowed)), [])

    def test_malicious_content_is_refused_without_running_anything(self):
        m = bash_path(self.marker)
        cases = {
            "command substitution": f"RELEASE_VERSION=$(touch {m})\n",
            "backticks": f"RELEASE_VERSION=`touch {m}`\n",
            "command after ;": f"RELEASE_VERSION=1.2.5; touch {m}\n",
            "bare command": f"touch {m}\n",
            "unknown key": "PATH=/tmp/evil\n",
            "unknown key LD_PRELOAD": "LD_PRELOAD=/tmp/evil.so\n",
            "quoted value": 'RELEASE_VERSION="1.2.5"\n',
            "export prefix": "export RELEASE_VERSION=1.2.5\n",
            "space in value": "RELEASE_VERSION=1.2.5 touch\n",
            "variable expansion": "RELEASE_VERSION=${HOME}\n",
            "CRLF line": "RELEASE_VERSION=1.2.5\r\n",
            "duplicate key": "RELEASE_VERSION=1.2.5\nRELEASE_VERSION=1.2.6\n",
        }
        base = self.builder_release_env().replace(b"RELEASE_VERSION=1.2.5\n", b"")
        for name, text in cases.items():
            with self.subTest(name=name):
                result = self.parse(base + text.encode())
                self.assertIn("RC=1", result.stdout, result.stderr)
                self.assertIn("[ERROR] release.env", result.stderr)
                self.assertFalse(self.marker.exists(), "release.env content was executed")
                self.assertNotIn("PATHV=/tmp/evil", result.stdout)

    def test_install_verifies_the_package_before_reading_release_env_and_never_sources_it(self):
        install = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn('source "$RELEASE_ENV_FILE"', install)
        self.assertNotIn('. "$RELEASE_ENV_FILE"', install)
        body = install.split("run_install() {", 1)[1]
        self.assertLess(body.index("verify_installer_payload || return 1"), body.index("load_release_metadata"))

    def test_load_release_metadata_refuses_a_malicious_file(self):
        installer = self.tmp / "installer"
        shutil.copytree(ROOT, installer, ignore=shutil.ignore_patterns("tests", "windows", "__pycache__"))
        (installer / "release.env").write_text(f"VMS_RELEASE_COMMIT=$(touch {bash_path(self.marker)})\n")
        script = f"source {q(bash_path(installer / 'install.sh'))}; set +e; load_release_metadata; echo RC=$?"
        result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
        self.assertIn("RC=1", result.stdout, result.stderr)
        self.assertFalse(self.marker.exists())


class PayloadDigestParityTests(unittest.TestCase):
    """The builder and the installer must compute the same payload digest."""

    @unittest.skipUnless(BASH, "bash is required")
    def test_builder_and_installer_digests_agree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "vms"
            (root / "app" / "static" / "js").mkdir(parents=True)
            (root / "app" / "main.py").write_text("x\n")
            (root / "app" / "static" / "js" / "a b.js").write_text("y\n")
            (root / "app" / "Z.py").write_text("z\n")
            (root / "Dockerfile").write_bytes(b"FROM scratch\r\n")
            (root / "empty-dir").mkdir()
            script = f"source {q(bash_path(ROOT / '06-deploy-vms.sh'))}; vms_payload_digest {q(bash_path(root))}"
            shell = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(shell, builder.vms_payload_digest(root))


class BuilderPreloadTests(unittest.TestCase):
    """build_preloaded_vms_image() with docker calls faked (no real Docker)."""

    def fake_save(self, raw: Path, *, tags, config_hex="2" * 64, index_digest=IMAGE_ID, images=1):
        with tarfile.open(raw, "w") as tf:
            def add(name, data):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            layers = ["blobs/sha256/" + "3" * 64, "blobs/sha256/" + "4" * 64]
            add("manifest.json", json.dumps([{"Config": f"blobs/sha256/{config_hex}", "RepoTags": tags,
                                               "Layers": layers}] * images).encode())
            add("index.json", json.dumps({"schemaVersion": 2, "manifests": [{"digest": index_digest}]}).encode())
            add(f"blobs/sha256/{config_hex}", b"{}")
            # One gzip layer (containerd store) of 100000 bytes, one plain of 500.
            add(layers[0], gzip.compress(b"\0" * 100000))
            add(layers[1], b"x" * 500)

    def run_builder(self, *, built_id=IMAGE_ID, labels=None, tags=None, compression="gzip"):
        calls = []
        tmp = Path(tempfile.mkdtemp(prefix="anyaicam-preload-builder-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        context = tmp / "package" / "payload" / "vms"
        (context / "app").mkdir(parents=True)
        (context / "app" / "main.py").write_text("x\n")
        (context / "Dockerfile").write_text("FROM scratch\n")
        payload_sha = builder.vms_payload_digest(context)
        ref = f"anyaicam-vms-preload:{SHORT}"
        labels = labels if labels is not None else {builder.PRELOAD_LABEL_COMMIT: COMMIT,
                                                    builder.PRELOAD_LABEL_PAYLOAD: payload_sha}

        def fake_run(cmd, check=False, stdout=None, text=False, **kwargs):
            calls.append(cmd)
            out = ""
            if cmd[1] == "image" and cmd[4] == "{{.Id}}":
                out = built_id + "\n"
            elif cmd[1] == "image":
                out = json.dumps(labels)
            elif cmd[1] == "save":
                self.fake_save(Path(cmd[3]), tags=tags if tags is not None else [ref])
            return subprocess.CompletedProcess(cmd, 0, stdout=out)

        with mock.patch.object(builder.subprocess, "run", side_effect=fake_run):
            fields = builder.build_preloaded_vms_image(
                docker="docker", context=context, images_dir=tmp / "package" / "images", scratch=tmp,
                vms_commit=COMMIT, release_version="1.2.5", platform="linux/amd64", compression=compression,
                no_cache=False)
        return fields, calls, tmp, payload_sha

    def test_records_everything_the_installer_verifies(self):
        fields, calls, tmp, payload_sha = self.run_builder()
        build = calls[0]
        self.assertEqual(build[:2], ["docker", "build"])
        self.assertIn(f"{builder.PRELOAD_LABEL_COMMIT}={COMMIT}", build)
        self.assertIn(f"{builder.PRELOAD_LABEL_PAYLOAD}={payload_sha}", build)
        self.assertTrue(build[-1].endswith("payload" + os.sep + "vms") or build[-1].endswith("payload/vms"))
        archive = tmp / "package" / fields["VMS_PRELOAD_IMAGE_FILE"]
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_FILE"], f"images/anyaicam-vms-{SHORT}.tar.gz")
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_SHA256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_BYTES"], str(archive.stat().st_size))
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_ID"], IMAGE_ID)
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_CONFIG_DIGEST"], CONFIG_DIGEST)
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_PAYLOAD_SHA256"], payload_sha)
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_REF"], f"anyaicam-vms-preload:{SHORT}")
        with gzip.open(archive) as handle, tarfile.open(fileobj=handle) as tf:
            self.assertIn("manifest.json", tf.getnames())
        self.assertFalse((tmp / "vms-image.tar").exists())
        # Extracted layer size (gzip layers decompressed), used for the disk check.
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_UNPACKED_BYTES"], str(100000 + 500))

    def test_classic_store_id_equal_to_config_digest_is_accepted(self):
        fields, *_ = self.run_builder(built_id=CONFIG_DIGEST)
        self.assertEqual(fields["VMS_PRELOAD_IMAGE_CONFIG_DIGEST"], CONFIG_DIGEST)

    def test_saved_image_that_is_not_the_built_one_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_builder(built_id="sha256:" + "7" * 64)

    def test_missing_labels_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_builder(labels={})

    def test_unexpected_tags_in_the_saved_archive_are_refused(self):
        with self.assertRaises(SystemExit):
            self.run_builder(tags=["anyaicam-vms:latest"])

    def test_release_env_and_manifest_carry_the_fields(self):
        source = (ROOT / "build_release_installer.py").read_text(encoding="utf-8")
        self.assertIn('release_env += "".join(f"{key}={value}\\n" for key, value in preload.items())', source)
        self.assertIn('manifest["vms_preloaded_image"]', source)
        # The image archive lives outside payload/ (never scanned as source) but
        # inside the package, so artifact-files.json lists it.
        self.assertIn('images_dir=package / "images"', source)


if __name__ == "__main__":
    unittest.main()
