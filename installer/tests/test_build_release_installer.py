"""Regression coverage for run_git()'s core.autocrlf=false override in
build_release_installer.py.

Confirmed live: on a Windows host with the very common core.autocrlf=true
setting, `git archive` invoked via Python's subprocess (not through a
shell/pty) silently re-introduces CRLF into every exported file, even
though the actual committed blobs are correctly LF-only (git show / a
plain shell's own `git archive` both return clean bytes on the same
host and commit). Without a fix, this made a release build's shell-LF
validation (ensure_lf_and_modes()) fail non-deterministically depending
entirely on the *building operator's* global git config -- something
this tool must never depend on. run_git() now passes
`-c core.autocrlf=false` as an in-process override on every git
invocation, so the build is deterministic regardless of the host's or
operator's own git configuration, without ever reading or writing the
repo's own .git/config.

Two independent checks:
  1. The constructed command line always carries the override (a pure,
     platform-independent guard against someone removing it later).
  2. A real git round-trip: a temporary repo with core.autocrlf=true
     explicitly set, archived via run_git() -- the output must be
     byte-for-byte LF-only, proving the override actually neutralizes
     that config value rather than merely being present on the command
     line.
"""
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_release_installer import INSTALLER_RUNTIME_FILES, run_git, write_deterministic_tar  # noqa: E402


class RunGitAutocrlfOverrideTests(unittest.TestCase):
    def test_every_invocation_carries_the_autocrlf_override(self):
        with patch("build_release_installer.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"")
            run_git(Path("."), "rev-parse", "HEAD")
        (cmd,), _kwargs = mock_run.call_args
        self.assertEqual(cmd[0], "git")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "core.autocrlf=false")

    def test_archive_is_lf_clean_even_when_the_repo_is_configured_autocrlf_true(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            # The exact condition confirmed to trigger the bug: the repo
            # (not just some global operator setting) has autocrlf=true.
            subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", "true"], check=True)
            script = repo / "script.sh"
            script.write_bytes(b"#!/usr/bin/env bash\necho hi\n")
            subprocess.run(["git", "-C", str(repo), "add", "script.sh"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add script"], check=True)

            archive = run_git(repo, "archive", "--format=tar", "HEAD", "--", "script.sh")

        self.assertNotIn(b"\r", archive, "run_git()'s archive output must be LF-clean regardless of the repo's own core.autocrlf setting")


class DeterministicTarExecutableBitTests(unittest.TestCase):
    """Regression coverage for a second confirmed-live release blocker:
    the artifact printed "shell_executable=PASS" (previously a hardcoded
    string with no check behind it at all) while every extracted script
    was actually mode 0644 on the real Ubuntu target -- `sudo
    ./install.sh` failed with Permission denied on Samsung. Root cause:
    write_deterministic_tar() trusted gettarinfo()'s auto-detected mode,
    which reads the *building host's* os.stat() -- on Windows, os.chmod
    (..., 0o755) (see ensure_lf_and_modes()) cannot actually confer a
    POSIX executable bit, so the tar silently inherited Windows' own
    fabricated, non-executable mode regardless of what chmod "set".

    Every mode is now assigned explicitly in write_deterministic_tar(),
    independent of any host stat() call -- these tests build a real
    tarball with write_deterministic_tar() itself (not a mock) and prove
    the executable bit survives two ways: reading the archive's own
    stored TarInfo.mode directly (meaningful on any host, including this
    one), and actually extracting to disk and checking the resulting
    file's real permissions (meaningful wherever POSIX permissions
    exist -- skipped on Windows, which cannot represent them at all,
    exactly the platform gap that let this bug ship undetected)."""

    def _build_sample_tar(self, tmp_path: Path) -> Path:
        source = tmp_path / "package"
        source.mkdir()
        (source / "install.sh").write_text("#!/usr/bin/env bash\necho install\n", newline="\n")
        (source / "validate.sh").write_text("#!/usr/bin/env bash\necho validate\n", newline="\n")
        (source / "README.md").write_text("not a script\n", newline="\n")
        output = tmp_path / "sample.tar.gz"
        write_deterministic_tar(source, output, mtime=0, executable_paths=frozenset({"install.sh", "validate.sh"}))
        return output

    def test_executable_paths_are_stored_with_the_exec_bit_in_the_archive_itself(self):
        with tempfile.TemporaryDirectory() as td:
            output = self._build_sample_tar(Path(td))
            with tarfile.open(output, "r:gz") as tf:
                modes = {member.name: member.mode for member in tf.getmembers()}
        self.assertTrue(modes["install.sh"] & 0o111, "install.sh must be stored with an executable bit")
        self.assertTrue(modes["validate.sh"] & 0o111, "validate.sh must be stored with an executable bit")
        self.assertFalse(modes["README.md"] & 0o111, "README.md must NOT be stored executable")

    @unittest.skipIf(os.name == "nt", "Windows cannot represent real POSIX executable bits at all -- this is exactly the platform gap the fix works around, not something extraction-based assertions can meaningfully re-check here.")
    def test_extracted_files_are_actually_executable_without_a_manual_chmod(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            output = self._build_sample_tar(tmp_path)
            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()
            with tarfile.open(output, "r:gz") as tf:
                tf.extractall(extract_dir)  # noqa: S202 -- trusted, just-built local archive
            self.assertTrue(os.access(extract_dir / "install.sh", os.X_OK), "install.sh must be executable immediately after extraction, with no manual chmod")
            self.assertTrue(os.access(extract_dir / "validate.sh", os.X_OK), "validate.sh must be executable immediately after extraction, with no manual chmod")

    def test_all_installer_runtime_shell_scripts_are_covered_by_the_executable_set(self):
        # Guards against someone adding a new top-level installer .sh
        # file without it ever landing in the executable set computed
        # from INSTALLER_RUNTIME_FILES in main().
        expected = {name for name in INSTALLER_RUNTIME_FILES if name.endswith(".sh")}
        self.assertIn("install.sh", expected)
        self.assertIn("validate.sh", expected)
        self.assertIn("uninstall.sh", expected)
        self.assertNotIn("README.md", expected)


if __name__ == "__main__":
    unittest.main()
