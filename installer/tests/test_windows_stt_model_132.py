"""Windows 1.3.2 (Blue, Codex FAIL on ef93b503): the Vosk speech model for Voice Call listening ships with the setup.

* Pins: build.ps1, the per-file manifest and both Linux Dockerfiles name the same archive SHA-256.
* Packaging: the .iss bundles the archive, the installer script, the manifest and the licence notice; install-runtime.ps1
  installs the model after the data root is locked down and before any service exists.
* Location: exactly where app/aac_voice_call_listen.py looks by default on Windows.
* Behaviour: install-stt-model.ps1 is EXECUTED with real PowerShell for a clean install, repair/upgrade with an intact
  model (kept), a corrupted / incomplete / extended model (replaced), a damaged archive (refused, model untouched),
  links (refused) and an interrupted earlier run; plus the real pinned archive end to end when the vendor copy exists.
"""
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
ISS = (WIN / "AnyAiCam-VMS.iss").read_text(encoding="utf-8")
# AAC_STT_SCRIPT_UNDER_TEST: run the execution tests against another copy (mutation check against an older version).
SCRIPT = Path(os.environ.get("AAC_STT_SCRIPT_UNDER_TEST") or (WIN / "install-stt-model.ps1"))
MANIFEST = WIN / "vosk-model-small-en-us-0.15.sha256"
NOTICE = WIN / "notices" / "vosk-model-small-en-us-0.15-NOTICE.txt"
ARCHIVE = WIN / "vendor" / "vosk-model-small-en-us-0.15.zip"
PIN = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
NAME = "vosk-model-small-en-us-0.15"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _q(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class PinsAndPackagingTests(unittest.TestCase):
    def test_one_pin_everywhere(self):
        self.assertIn(f"'vosk-model-small-en-us-0.15.zip'       = '{PIN}'", (WIN / "build.ps1").read_text(encoding="utf-8"))
        self.assertEqual(MANIFEST.read_text(encoding="utf-8").splitlines()[0], f"{PIN}  *archive* {NAME}.zip")
        for dockerfile in ("Dockerfile", "Dockerfile.production"):
            self.assertIn(f'echo "{PIN}  /tmp/vosk-model.zip"', (ROOT / dockerfile).read_text(encoding="utf-8"), dockerfile)

    def test_the_manifest_lists_the_whole_model(self):
        lines = MANIFEST.read_text(encoding="utf-8").splitlines()[1:]
        entries = dict(reversed(line.split("  ", 1)) for line in lines)
        self.assertEqual(len(entries), 14)
        for required in ("am/final.mdl", "conf/model.conf", "conf/mfcc.conf", "graph/HCLr.fst", "graph/Gr.fst",
                         "ivector/final.ie", "README"):
            self.assertIn(f"{NAME}/{required}", entries)
        for path, sha in entries.items():
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertNotIn("..", path)

    def test_the_setup_bundles_archive_script_manifest_and_notice(self):
        self.assertIn('Source: "vendor\\vosk-model-small-en-us-0.15.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall', ISS)
        self.assertIn('Source: "install-stt-model.ps1"; DestDir: "{app}\\installer"; Flags: ignoreversion', ISS)
        self.assertIn('Source: "vosk-model-small-en-us-0.15.sha256"; DestDir: "{app}\\installer"; Flags: ignoreversion', ISS)
        self.assertIn('Source: "notices\\vosk-model-small-en-us-0.15-NOTICE.txt"; DestDir: "{app}\\licenses"; Flags: ignoreversion', ISS)
        self.assertIn("-SttModelArchive \"' + ExpandConstant('{tmp}\\vosk-model-small-en-us-0.15.zip') + '\"", ISS)

    def test_install_runtime_installs_it_after_the_data_root_is_locked_down(self):
        text = (WIN / "install-runtime.ps1").read_text(encoding="utf-8")
        self.assertIn("[Parameter(Mandatory=$true)][string]$SttModelArchive,", text)
        call = text.index("Install-AnyAiCamSttModel -DataRoot $DataRoot -Archive $SttModelArchive -Manifest (Join-Path $PSScriptRoot 'vosk-model-small-en-us-0.15.sha256')")
        self.assertLess(text.index("/inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'"), call)
        self.assertLess(text.index("Assert-AnyAiCamDataRootHasNoLinks $DataRoot  # again, now that nothing below is user-writable"), call)
        self.assertIn("Assert-AnyAiCamDataRootHasNoLinks $DataRoot", text[call:call + 300])

    def test_the_vms_looks_exactly_where_the_setup_installs(self):
        listen = (ROOT / "app" / "aac_voice_call_listen.py").read_text(encoding="utf-8")
        self.assertIn(f'platform_paths.native_path("/opt/anyaicam-stt-model/{NAME}", "models", "stt", "{NAME}")', listen)
        self.assertIn("$models = Join-Path $DataRoot 'models'", SCRIPT.read_text(encoding="utf-8"))
        self.assertIn("$stt = Join-Path $models 'stt'", SCRIPT.read_text(encoding="utf-8"))

    def test_licence_notice_and_dependency_record(self):
        notice = NOTICE.read_text(encoding="utf-8")
        for needed in ("Copyright 2020 Alpha Cephei Inc", "Apache License, Version 2.0", PIN,
                       "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION", "9. Accepting Warranty or Additional Liability",
                       f"%ProgramData%\\AnyAiCam\\models\\stt\\{NAME}"):
            self.assertIn(needed, notice)
        self.assertFalse(re.search(r"[^\x00-\x7F]", notice))
        deps = (WIN / "DEPENDENCIES.md").read_text(encoding="utf-8")
        self.assertIn(f"| Vosk small US English speech model | {NAME}", deps)
        self.assertIn(PIN, deps)

    @unittest.skipUnless(ARCHIVE.exists(), "vendored model archive not present (fetched for a build)")
    def test_the_vendored_archive_matches_the_pin_and_the_manifest(self):
        self.assertEqual(hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(), PIN)
        entries = dict(reversed(line.split("  ", 1)) for line in MANIFEST.read_text(encoding="utf-8").splitlines()[1:])
        with zipfile.ZipFile(ARCHIVE) as archive:
            files = {info.filename: hashlib.sha256(archive.read(info)).hexdigest() for info in archive.infolist() if not info.is_dir()}
        self.assertEqual(files, entries)


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "executes install-stt-model.ps1 with Windows PowerShell")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aac-stt-"))
        self.data = self.tmp / "ProgramData" / "AnyAiCam"
        self.data.mkdir(parents=True)
        self.target = self.data / "models" / "stt" / NAME
        self.files = {f"{NAME}/am/final.mdl": b"acoustic model " * 64, f"{NAME}/conf/model.conf": b"--min-active=200\n",
                      f"{NAME}/graph/phones/word_boundary.int": b"1 nonword\n", f"{NAME}/README": b"test model\n"}
        self.archive, self.manifest = self._archive(self.files)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)   # never follows a junction (Python >= 3.8 on Windows)

    def _archive(self, files, *, name="model", manifest_files=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, data in files.items():
                archive.writestr(path, data)
        archive_path = self.tmp / f"{name}.zip"
        archive_path.write_bytes(buffer.getvalue())
        lines = [f"{hashlib.sha256(buffer.getvalue()).hexdigest()}  *archive* {NAME}.zip"]
        lines += [f"{hashlib.sha256(data).hexdigest()}  {path}" for path, data in sorted((manifest_files or self.files).items())]
        manifest = self.tmp / f"{name}.sha256"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return archive_path, manifest

    def install(self, archive=None, manifest=None, data=None, *, prelude="", caller_errors="Stop"):
        """prelude runs after the script is dot-sourced (e.g. a Move-AnyAiCamSttFolder that fails one step).
        caller_errors: the caller's $ErrorActionPreference; the function must enforce Stop itself."""
        # The error is printed as one unwrapped line (PowerShell's own error display wraps at the console width).
        script = (f". {_q(SCRIPT)}; {prelude}; try {{ Install-AnyAiCamSttModel -DataRoot {_q(data or self.data)} "
                  f"-Archive {_q(archive or self.archive)} -Manifest {_q(manifest or self.manifest)} }} "
                  "catch { [Console]::Out.WriteLine('INSTALL FAILED: ' + $_.Exception.Message); exit 1 }")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                                 f"$ErrorActionPreference = '{caller_errors}'; " + script], capture_output=True, text=True, timeout=300)
        return result.returncode, result.stdout + result.stderr

    def installed(self):
        return {f"{NAME}/" + p.relative_to(self.target).as_posix(): p.read_bytes() for p in self.target.rglob("*") if p.is_file()}

    def test_clean_install_extracts_and_verifies(self):
        code, output = self.install()
        self.assertEqual(code, 0, output)
        self.assertIn(f"Speech model {NAME}: installed and verified.", output)
        self.assertEqual(self.installed(), self.files)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], [NAME])          # no staging left behind

    def test_repair_or_upgrade_keeps_an_intact_model_untouched(self):
        self.assertEqual(self.install()[0], 0)
        before = {p: p.stat().st_mtime_ns for p in self.target.rglob("*")}
        code, output = self.install()
        self.assertEqual(code, 0, output)
        self.assertIn("present and verified (kept)", output)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.target.rglob("*")}, before)

    def test_a_corrupted_missing_or_extra_file_is_replaced(self):
        for damage in ("altered", "missing", "extra", "folder_gone"):
            with self.subTest(damage=damage):
                self.assertEqual(self.install()[0], 0)
                if damage == "altered":
                    (self.target / "am" / "final.mdl").write_bytes(b"corrupt")
                elif damage == "missing":
                    (self.target / "conf" / "model.conf").unlink()
                elif damage == "extra":
                    (self.target / "am" / "planted.pkl").write_bytes(b"x")
                else:
                    shutil.rmtree(self.target)
                code, output = self.install()
                self.assertEqual(code, 0, output)
                expected = {"altered": f"replaced (altered file {NAME}/am/final.mdl)",
                            "missing": f"replaced (missing file {NAME}/conf/model.conf)",
                            "extra": f"replaced (unexpected file {NAME}/am/planted.pkl)",
                            "folder_gone": "installed"}[damage]
                self.assertIn(f"Speech model {NAME}: {expected} and verified.", output)
                self.assertEqual(self.installed(), self.files)

    def test_an_archive_that_does_not_match_its_pin_changes_nothing(self):
        self.assertEqual(self.install()[0], 0)
        (self.target / "am" / "final.mdl").write_bytes(b"corrupt")          # needs a replacement...
        bad = self.tmp / "bad.zip"
        bad.write_bytes(self.archive.read_bytes() + b"tampered")              # ...from an archive that fails its pin
        code, output = self.install(archive=bad)
        self.assertNotEqual(code, 0)
        self.assertIn("does not match its pinned SHA-256", output)
        self.assertEqual((self.target / "am" / "final.mdl").read_bytes(), b"corrupt")   # left exactly as it was

    def test_an_archive_whose_content_does_not_match_the_manifest_changes_nothing(self):
        other = dict(self.files)
        other[f"{NAME}/am/final.mdl"] = b"different model"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, data in other.items():
                archive.writestr(path, data)
        rogue = self.tmp / "rogue.zip"
        rogue.write_bytes(buffer.getvalue())
        lines = self.manifest.read_text(encoding="utf-8").splitlines()
        lines[0] = f"{hashlib.sha256(buffer.getvalue()).hexdigest()}  *archive* {NAME}.zip"    # archive pin matches...
        manifest = self.tmp / "rogue.sha256"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        code, output = self.install(archive=rogue, manifest=manifest)
        self.assertNotEqual(code, 0)
        self.assertIn("does not match its manifest", output)                  # ...but a file does not
        self.assertFalse(self.target.exists())
        self.assertEqual([p.name for p in self.target.parent.iterdir()], [])

    def test_links_are_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        for where in ("models", "stt", "target"):
            with self.subTest(where=where):
                shutil.rmtree(self.data / "models", ignore_errors=True)
                link = {"models": self.data / "models", "stt": self.data / "models" / "stt", "target": self.target}[where]
                link.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
                code, output = self.install()
                self.assertNotEqual(code, 0)
                self.assertIn("Refusing a link", output)
                self.assertEqual(list(outside.iterdir()), [])                  # nothing written through the link
                os.rmdir(link)

    def test_leftovers_of_an_interrupted_run_are_removed(self):
        stale = self.data / "models" / "stt" / ".staging-deadbeef"
        stale.mkdir(parents=True)
        (stale / "half.bin").write_bytes(b"x")
        (self.data / "models" / "stt" / ".old-cafe").mkdir()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], [NAME])

    # ------------------------------------------------------------ recovery (Codex FAIL on d919e4c4)
    FAIL_STAGED_MOVE = ("function Move-AnyAiCamSttFolder([string]$From, [string]$To) { "
                        "if ($From -like '*\\.staging-*') { throw 'simulated failure moving the staged model' }; "
                        "Move-Item -LiteralPath $From -Destination $To -ErrorAction Stop }")
    FAIL_STAGED_AND_RESTORE = ("function Move-AnyAiCamSttFolder([string]$From, [string]$To) { "
                               "if ($From -like '*\\.staging-*' -or $From -like '*\\.old-*') { throw 'simulated failure' }; "
                               "Move-Item -LiteralPath $From -Destination $To -ErrorAction Stop }")

    def stt(self):
        return self.data / "models" / "stt"

    def asides(self):
        return sorted(p for p in self.stt().iterdir() if p.name.startswith(".old-"))

    def snapshot(self, folder):
        return {p.relative_to(folder).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}

    def corrupted_archive(self, source=None):
        bad = self.tmp / "corrupted.zip"
        bad.write_bytes((source or self.archive).read_bytes()[:-200] + b"corrupted")
        return bad

    def interrupt_after_moving_aside(self):
        """State an install leaves when it stops between 'previous model -> .old-*' and 'staged -> model'."""
        aside = self.stt() / ".old-20261020120000000-1b3c"
        os.replace(self.target, aside)
        staging = self.stt() / ".staging-feedface"
        (staging / NAME / "am").mkdir(parents=True)
        (staging / NAME / "am" / "final.mdl").write_bytes(b"half extracted")
        return aside

    def upgrade_files(self):
        newer = dict(self.files)
        newer[f"{NAME}/am/final.mdl"] = b"newer acoustic model " * 64
        newer[f"{NAME}/conf/model.conf"] = b"--min-active=300\n"
        return newer

    def test_interrupted_promotion_followed_by_a_corrupted_archive_restores_the_previous_model(self):
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot(self.target)
        aside = self.interrupt_after_moving_aside()
        self.assertFalse(self.target.exists())
        code, output = self.install(archive=self.corrupted_archive())
        self.assertEqual(code, 0, output)
        self.assertIn(f"Speech model {NAME}: restored from {aside.name} (an interrupted earlier install) and verified.", output)
        self.assertEqual(self.snapshot(self.target), before)                  # the very same files, untouched
        self.assertEqual([p.name for p in self.stt().iterdir()], [NAME])       # aside and staging cleaned only now

    def test_successful_retry_after_an_interruption(self):
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot(self.target)
        self.interrupt_after_moving_aside()
        code, output = self.install()                                          # the retry, good archive
        self.assertEqual(code, 0, output)
        self.assertIn("restored from .old-20261020120000000-1b3c", output)
        self.assertEqual(self.snapshot(self.target), before)
        code, output = self.install()                                          # and a later run simply keeps it
        self.assertEqual(code, 0, output)
        self.assertIn("present and verified (kept)", output)

    def test_retry_after_an_interruption_with_an_unusable_aside_installs_from_the_archive(self):
        self.assertEqual(self.install()[0], 0)
        aside = self.interrupt_after_moving_aside()
        (aside / "am" / "final.mdl").write_bytes(b"damaged")                  # the aside does not verify
        code, output = self.install(archive=self.corrupted_archive())         # ...and the archive is corrupted
        self.assertNotEqual(code, 0)
        self.assertIn("does not match its pinned SHA-256", output)
        self.assertTrue(aside.is_dir(), "the recovery folder must survive a failed attempt")
        code, output = self.install()                                          # a good archive later
        self.assertEqual(code, 0, output)
        self.assertIn(f"Speech model {NAME}: installed and verified.", output)
        self.assertEqual(self.installed(), self.files)
        self.assertEqual(self.asides(), [])                                    # removed only after the new model verified

    def test_a_failed_staged_to_target_move_puts_the_previous_model_back(self):
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot(self.target)
        archive, manifest = self._archive(self.upgrade_files(), name="newer", manifest_files=self.upgrade_files())
        code, output = self.install(archive=archive, manifest=manifest, prelude=self.FAIL_STAGED_MOVE, caller_errors="Continue")
        self.assertNotEqual(code, 0)
        self.assertIn("Promoting the new speech model failed (simulated failure moving the staged model); "
                      "the previous model was put back unchanged.", output)
        self.assertEqual(self.snapshot(self.target), before)
        self.assertEqual([p.name for p in self.stt().iterdir()], [NAME])       # no staging, no aside left

    def test_when_even_the_put_back_fails_the_recovery_folder_is_kept_and_the_next_run_recovers(self):
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot(self.target)
        archive, manifest = self._archive(self.upgrade_files(), name="newer", manifest_files=self.upgrade_files())
        code, output = self.install(archive=archive, manifest=manifest, prelude=self.FAIL_STAGED_AND_RESTORE)
        self.assertNotEqual(code, 0)
        self.assertIn("the previous model could not be put back; it is kept in", output)
        self.assertFalse(self.target.exists())
        [aside] = self.asides()
        self.assertEqual(self.snapshot(aside), before)                         # the only copy, intact
        # next attempt with the same release's archive corrupted: the kept copy comes back
        code, output = self.install(archive=self.corrupted_archive())
        self.assertEqual(code, 0, output)
        self.assertIn(f"restored from {aside.name}", output)
        self.assertEqual(self.snapshot(self.target), before)
        # and the upgrade itself succeeds once the move works
        code, output = self.install(archive=archive, manifest=manifest)
        self.assertEqual(code, 0, output)
        self.assertIn("replaced (altered file", output)
        self.assertEqual(self.installed(), self.upgrade_files())
        self.assertEqual(self.asides(), [])

    def test_a_valid_model_survives_a_failed_repair_or_upgrade(self):
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot(self.target)
        newer = self.upgrade_files()
        upgrade_archive, upgrade_manifest = self._archive(newer, name="newer", manifest_files=newer)
        mismatched, mismatched_manifest = self._archive(self.files, name="mismatch", manifest_files=newer)
        cases = {
            "repair, corrupted archive": dict(archive=self.corrupted_archive()),                       # kept: archive unused
            "upgrade, corrupted archive": dict(archive=self.corrupted_archive(upgrade_archive), manifest=upgrade_manifest),
            "upgrade, archive content != manifest": dict(archive=mismatched, manifest=mismatched_manifest),
            "upgrade, staged move fails": dict(archive=upgrade_archive, manifest=upgrade_manifest, prelude=self.FAIL_STAGED_MOVE),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                code, output = self.install(**kwargs)
                if label.startswith("repair"):
                    self.assertEqual(code, 0, output)
                    self.assertIn("(kept)", output)
                else:
                    self.assertNotEqual(code, 0, output)
                self.assertEqual(self.snapshot(self.target), before)            # same bytes AND same timestamps
                self.assertEqual([p.name for p in self.stt().iterdir()], [NAME])

    def test_stale_asides_are_removed_only_once_the_active_model_verifies(self):
        self.assertEqual(self.install()[0], 0)
        stale = self.stt() / ".old-20261001000000000-dead"
        (stale / "am").mkdir(parents=True)
        (stale / "am" / "final.mdl").write_bytes(b"an older release")          # does not verify
        code, output = self.install()
        self.assertEqual(code, 0, output)
        self.assertIn("(kept)", output)
        self.assertEqual(self.asides(), [])

    def test_the_model_keeps_the_protected_parent_permissions_after_restore_and_replace(self):
        self.assertEqual(self.install()[0], 0)
        self.interrupt_after_moving_aside()
        self.assertEqual(self.install()[0], 0)                                  # restored by a folder move
        (self.target / "am" / "final.mdl").write_bytes(b"corrupt")
        self.assertEqual(self.install()[0], 0)                                  # replaced by a folder move
        script = (f"$acl = Get-Acl -LiteralPath {_q(self.target)}; "
                  "if ($acl.AreAccessRulesProtected) { 'PROTECTED' } else { 'INHERITS' }; "
                  "@($acl.Access | Where-Object { -not $_.IsInherited }).Count")
        result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.stdout.split(), ["INHERITS", "0"], result.stdout + result.stderr)

    @unittest.skipUnless(ARCHIVE.exists(), "vendored model archive not present (fetched for a build)")
    def test_the_real_pinned_model_installs_and_verifies(self):
        code, output = self.install(archive=ARCHIVE, manifest=MANIFEST)
        self.assertEqual(code, 0, output)
        self.assertIn(f"Speech model {NAME}: installed and verified.", output)
        self.assertTrue((self.target / "am" / "final.mdl").is_file())
        code, output = self.install(archive=ARCHIVE, manifest=MANIFEST)
        self.assertEqual(code, 0, output)
        self.assertIn("(kept)", output)


if __name__ == "__main__":
    unittest.main()
