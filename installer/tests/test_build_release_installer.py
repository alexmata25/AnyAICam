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
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_release_installer import run_git  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
