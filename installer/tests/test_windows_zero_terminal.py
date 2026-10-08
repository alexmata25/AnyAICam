"""Windows zero-terminal linking (installer/windows, 2026-10-08).

* Build integrity: build.ps1 checked only app/ and installer/windows/ while
  the .iss also packages appliance-agent\\anyaicam_agent\\*, so an edited,
  untracked or ignored agent file shipped inside a setup named after a clean
  commit (source-integrity.ps1 now reads the packaged folders from the .iss).
* Elevated browser: the finish page's links opened from the elevated setup
  (no runasoriginaluser).
* No visible code: the finish page printed the claim code and a Start menu
  entry showed it again; the link was a [Run] Filename, which Inno writes to
  the setup log. Now the link opens without showing the code, its Parameters
  are not logged, and only an administrator can open it again later.
* No local link page on Windows (ANYAICAM_LINK_PORT=0).

Nothing here installs anything: the PowerShell tests run the real scripts
against temporary folders / a temporary git repository, and the Inno test
only compiles (ISCC /O-, no setup is written).
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / "installer" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
GIT = shutil.which("git")
ISCC = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 7" / "ISCC.exe"
COMMIT = "aedecb6bdaac32aece3ede39510a078fe8bfa9a6"
CODE = r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}"


def read(name):
    return (WIN / name).read_text(encoding="utf-8")


def section(text, name):
    body = text.split(f"\n[{name}]\n", 1)[1]
    return re.split(r"\n\[[A-Za-z]+\]\n", body, maxsplit=1)[0]


def ps(command, cwd=None):
    return subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                          capture_output=True, text=True, timeout=120, cwd=cwd)


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


# ---------------------------------------------------------------- build integrity
@unittest.skipUnless(POWERSHELL and GIT, "PowerShell and git are required")
class BuildIntegrityTests(unittest.TestCase):
    """A throwaway repository with the real .iss and source-integrity.ps1."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="anyaicam-integrity-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        files = {
            "app/main.py": "print('vms')\n",
            "app/tests/test_main.py": "def test(): pass\n",
            "appliance-agent/anyaicam_agent/service.py": "def main(): pass\n",
            "appliance-agent/tests/test_service.py": "def test(): pass\n",
            ".gitignore": "__pycache__/\n*.local\n*.db\n",
        }
        for name, text in files.items():
            self.write(name, text)
        (self.repo / "installer/windows").mkdir(parents=True)
        for name in ("AnyAiCam-VMS.iss", "source-integrity.ps1", ".gitignore"):
            shutil.copy(WIN / name, self.repo / "installer/windows" / name)
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-qm", "base")

    def write(self, name, text="x\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def git(self, *args):
        return subprocess.run([GIT, "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def problems(self):
        script = self.repo / "installer/windows/source-integrity.ps1"
        iss = self.repo / "installer/windows/AnyAiCam-VMS.iss"
        result = ps(f". {quote(script)}; Get-PackagedSourceProblems {quote(self.repo)} {quote(iss)} | ForEach-Object {{ $_ }}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [line for line in result.stdout.splitlines() if line.strip()]

    def test_a_clean_tree_passes(self):
        self.assertEqual(self.problems(), [])

    def test_an_edited_agent_file_fails_where_the_old_check_passed(self):
        self.write("appliance-agent/anyaicam_agent/service.py", "def main(): import os; os.system('x')\n")
        # The aedecb6 check (git status --porcelain -- app installer/windows) saw nothing:
        self.assertEqual(self.git("status", "--porcelain", "--", "app", "installer/windows"), "")
        self.assertEqual(self.problems(), ["uncommitted change (M): appliance-agent/anyaicam_agent/service.py"])

    def test_an_untracked_agent_file_fails(self):
        self.write("appliance-agent/anyaicam_agent/extra.py")
        self.assertEqual(self.problems(), ["untracked: appliance-agent/anyaicam_agent/extra.py"])

    def test_an_ignored_but_packaged_file_fails(self):
        # The .iss copies whatever is on disk, ignored or not.
        self.write("appliance-agent/anyaicam_agent/secrets.local")
        self.write("app/static/data.db")
        self.assertEqual(sorted(self.problems()), ["ignored but packaged: app/static/data.db",
                                                   "ignored but packaged: appliance-agent/anyaicam_agent/secrets.local"])

    def test_a_staged_change_and_a_deletion_fail(self):
        self.write("app/main.py", "print('changed')\n")
        self.git("add", "app/main.py")
        (self.repo / "appliance-agent/anyaicam_agent/service.py").unlink()
        self.assertEqual(sorted(self.problems()), ["uncommitted change (D): appliance-agent/anyaicam_agent/service.py",
                                                   "uncommitted change (M): app/main.py"])

    def test_what_the_iss_excludes_or_pins_is_allowed(self):
        self.write("appliance-agent/anyaicam_agent/__pycache__/service.cpython-312.pyc")
        self.write("app/__pycache__/main.cpython-312.pyc")
        self.write("app/tests/fixtures/scratch.db")        # app tests are not packaged
        self.write("installer/windows/vendor/get-pip.py")   # pinned by SHA-256 in build.ps1
        self.write("installer/windows/output/AnyAiCam-VMS-Setup-1.3.0-x.exe")
        # left by the installer tests; installer/windows is packaged file by file
        self.write("installer/windows/__pycache__/runtime-preflight.cpython-311.pyc")
        self.assertEqual(self.problems(), [])

    def test_a_changed_or_untracked_installer_file_still_fails(self):
        gitignore = self.repo / "installer/windows/.gitignore"
        gitignore.write_text(gitignore.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8", newline="\n")
        self.write("installer/windows/extra.ps1")
        self.assertEqual(sorted(self.problems()), ["uncommitted change (M): installer/windows/.gitignore",
                                                   "untracked: installer/windows/extra.ps1"])

    def test_an_unpackaged_folder_is_not_checked(self):
        self.write("appliance-agent/tests/test_service.py", "def test(): assert 0\n")
        self.write("docs/notes.md")
        self.assertEqual(self.problems(), [])

    def test_a_changed_or_untracked_app_test_still_fails(self):
        # As before 2026-10-08: anything not ignored under a packaged folder.
        self.write("app/tests/test_new.py")
        self.assertEqual(self.problems(), ["untracked: app/tests/test_new.py"])

    def test_folders_come_from_the_iss(self):
        script = WIN / "source-integrity.ps1"
        result = ps(f". {quote(script)}; $f = Get-PackagedRepoFolders {quote(WIN / 'AnyAiCam-VMS.iss')}; "
                    "$f.Keys | ForEach-Object { $_ + '=' + ($f[$_] -join ',') }")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["app=tests,__pycache__", "appliance-agent/anyaicam_agent=__pycache__"])

    def test_an_excludes_pattern_it_does_not_understand_stops_the_build(self):
        iss = self.repo / "installer/windows/AnyAiCam-VMS.iss"
        iss.write_text(iss.read_text(encoding="utf-8").replace('Excludes: "tests\\*,__pycache__\\*"', 'Excludes: "*.pyc"'), encoding="utf-8")
        result = ps(f". {quote(self.repo / 'installer/windows/source-integrity.ps1')}; Get-PackagedSourceProblems {quote(self.repo)} {quote(iss)}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not understand the Excludes pattern", result.stdout + result.stderr)

    def test_vendor_holds_only_pinned_files_and_locked_wheels(self):
        vendor = self.repo / "vendor"
        for name in ("get-pip.py", "wheels/a-1.whl", "wheels/b-2.whl", "wheels/extra-9.whl", "notes.txt"):
            (vendor / name).parent.mkdir(parents=True, exist_ok=True)
            (vendor / name).write_text("x")
        result = ps(f". {quote(WIN / 'source-integrity.ps1')}; Get-VendorProblems {quote(vendor)} @('get-pip.py') @('a-1.whl','b-2.whl') | ForEach-Object {{ $_ }}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.split("\n")[:-1]), ["not pinned: vendor/notes.txt", "not pinned: vendor/wheels/extra-9.whl"])


class BuildScriptTests(unittest.TestCase):
    def test_build_uses_the_full_check_before_compiling(self):
        build = read("build.ps1")
        self.assertNotIn("status --porcelain -- app installer/windows", build)
        check = build.index("Get-PackagedSourceProblems $repo (Join-Path $PSScriptRoot 'AnyAiCam-VMS.iss')")
        self.assertLess(build.index("$sourceCommit = "), check)
        self.assertLess(check, build.index("& $iscc @compilerArguments"))
        self.assertLess(build.index("Get-VendorProblems $vendor @($pinned.Keys) $lockedWheels"), build.index("& $iscc @compilerArguments"))
        self.assertIn("if ($sourceProblems.Count) { throw", build)
        self.assertIn("if ($vendorProblems.Count) { throw", build)


# ---------------------------------------------------------------- finish page and Start menu
class InstallerLinkTests(unittest.TestCase):
    def setUp(self):
        self.iss = read("AnyAiCam-VMS.iss")
        self.run = section(self.iss, "Run")
        self.icons = section(self.iss, "Icons")
        self.code = self.iss.split("// ------------------------------------------------------------ claim code", 1)[1]

    def test_every_finish_page_entry_runs_as_the_original_user(self):
        entries = [line for line in self.run.splitlines() if line.startswith("Filename:")]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            flags = re.search(r'Flags: ([^;]+)', entry).group(1).split()
            self.assertIn("postinstall", flags)
            self.assertIn("runasoriginaluser", flags, entry)
            self.assertIn("skipifsilent", flags, entry)

    def test_the_link_is_a_parameter_that_is_never_logged(self):
        link = next(line for line in self.run.splitlines() if "Link this computer to my AnyAiCam account" in line)
        self.assertTrue(link.startswith('Filename: "{sys}\\WindowsPowerShell\\v1.0\\powershell.exe"; Parameters: "'), link)
        self.assertIn("Start-Process -FilePath '{code:ClaimLink}'", link)
        for flag in ("dontlogparameters", "runhidden", "nowait"):
            self.assertIn(flag, link)
        self.assertIn("Check: HasClaimLink", link)
        # Inno logs every [Run]/[Icons] Filename: the code may never be one.
        for line in (self.run + self.icons).splitlines():
            self.assertNotRegex(line, r'Filename: "\{code:')

    def test_the_finish_page_and_start_menu_never_show_the_code(self):
        self.assertNotIn("ClaimLabelValue('Claim code:')", self.iss)
        self.assertNotIn("notepad", self.iss.lower())
        self.assertNotIn("Show AnyAiCam claim code", self.icons)
        self.assertIn('Type: files; Name: "{group}\\Show AnyAiCam claim code.lnk"', section(self.iss, "InstallDelete"))
        finish = self.code.split("procedure CurPageChanged", 1)[1]
        self.assertNotIn("ClaimLink(", finish)
        self.assertNotIn("ClaimLabelValue(", finish)
        self.assertIn("There is no code to type.", finish)
        self.assertIn("This computer is linked to your AnyAiCam account.", finish)
        self.assertNotIn("Log(", self.code)

    def test_a_linked_pc_offers_no_link(self):
        has = self.code.split("function HasClaimLink", 1)[1].split("end;", 1)[0]
        self.assertIn("(not PostInstallFailed) and (not IsLinked) and (ClaimLink('') <> '')", has)
        self.assertIn("FileExists(ExpandConstant('{commonappdata}\\AnyAiCam\\agent\\credential.json'))", self.code)

    def test_the_link_is_checked_before_it_reaches_powershell(self):
        link = self.code.split("function ClaimLink", 1)[1].split("\nend;", 1)[0]
        self.assertIn("(Pos('https://', Link) <> 1) or (Pos('/claim#label=', Link) = 0)", link)
        allowed = re.search(r"Pos\(Copy\(Link, I, 1\), '([^']*)'\) = 0", link).group(1)
        self.assertEqual(set(allowed) - set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"), set(".:/-#="))
        self.assertIn("Result := Link + '&link=windows';", link)

    def test_start_menu_link_entry(self):
        self.assertIn('Name: "{group}\\Link this computer to AnyAiCam"; Filename: "{sys}\\WindowsPowerShell\\v1.0\\powershell.exe"; '
                      'Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\\installer\\open-claim-link.ps1"""',
                      self.icons)
        self.assertIn('Source: "open-claim-link.ps1"; DestDir: "{app}\\installer"; Flags: ignoreversion', self.iss)

    def test_no_local_link_page_on_windows(self):
        launcher = read("agent-launcher.ps1")
        self.assertIn("$env:ANYAICAM_LINK_PORT = '0'", launcher)
        self.assertIn("@('ANYAICAM_PORTAL_URL', 'ANYAICAM_AGENT_MODE')", launcher)  # agent.env cannot turn it on
        self.assertLess(launcher.index("$env:ANYAICAM_LINK_PORT = '0'"), launcher.index("& (Join-Path $installRoot 'runtime\\python\\python.exe')"))


@unittest.skipUnless(ISCC.is_file(), "Inno Setup 7 is required")
class InnoCompileTests(unittest.TestCase):
    def test_the_setup_script_compiles(self):
        """The real .iss without [Files] (the vendor inputs are not in the
        repository), compiled with /O- so no setup is written."""
        text = read("AnyAiCam-VMS.iss")
        files = text.index("\n[Files]\n")
        text = text[:files] + text[text.index("\n[Icons]\n"):]
        with tempfile.TemporaryDirectory(prefix="anyaicam-iss-") as folder:
            script = Path(folder) / "AnyAiCam-VMS.iss"
            script.write_text(text, encoding="utf-8")
            result = subprocess.run([str(ISCC), "/O-", "/Q", "/DAppVersion=1.3.0", f"/DSourceCommit={COMMIT}", str(script)],
                                    capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


# ---------------------------------------------------------------- open-claim-link.ps1
@unittest.skipUnless(POWERSHELL, "PowerShell is required")
class OpenClaimLinkTests(unittest.TestCase):
    SCRIPT = WIN / "open-claim-link.ps1"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="anyaicam-open-link-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def call(self, expression):
        result = ps(f". {quote(self.SCRIPT)}; {expression}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def label(self, *lines):
        path = self.root / "claim-label.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_builds_the_link_from_the_real_cloud_link_output(self):
        provision = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                    str(WIN / "cloud-link.ps1"), "-DataRoot", str(self.root), "-AppVersion", "1.3.0", "-SourceCommit", COMMIT],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(provision.returncode, 0, provision.stdout + provision.stderr)
        label = (self.root / "label" / "claim-label.txt").read_text(encoding="utf-8-sig")
        code = re.search(r"^Claim code: (" + CODE + ")$", label, re.M).group(1)
        link = self.call(f"Get-ClaimLink {quote(self.root / 'label' / 'claim-label.txt')}")
        self.assertEqual(link, f"https://app.anyaicam.com/claim#label={code}&link=windows")

    def test_dot_sourcing_runs_nothing(self):
        # (the tests rely on it; running it would elevate and open a browser)
        self.assertEqual(self.call("'loaded'"), "loaded")

    def test_refuses_anything_but_a_well_formed_https_claim_link(self):
        for bad in ("http://app.anyaicam.com/claim#label=ABCD-EFGH-JKMN",
                    "https://app.anyaicam.com/claim#label=ABCD-EFGH-JKMN' ; calc ; '",
                    "https://app.anyaicam.com/claim#label=ABCD-EFGH-JKMN&x=1",
                    "https://app.anyaicam.com/other#label=ABCD-EFGH-JKMN",
                    "https://app.anyaicam.com/claim#label=abcd-efgh-jkmn",
                    "https://app.anyaicam.com/claim#label=ABCD-EFGH-JKMO",
                    "https://evil example/claim#label=ABCD-EFGH-JKMN",
                    "https://user@app.anyaicam.com/claim#label=ABCD-EFGH-JKMN"):
            path = self.label("Claim code: ABCD-EFGH-JKMN", "QR code: " + bad)
            self.assertEqual(self.call(f"Get-ClaimLink {quote(path)}"), "", bad)
        self.assertEqual(self.call(f"Get-ClaimLink {quote(self.root / 'missing.txt')}"), "")
        self.assertEqual(self.call(f"Get-ClaimLink {quote(self.label('Claim code: ABCD-EFGH-JKMN'))}"), "")

    def test_link_state(self):
        self.assertEqual(self.call(f"Get-LinkState {quote(self.root)}"), "unlinked")
        (self.root / "agent").mkdir()
        (self.root / "agent" / "credential.json").write_text("{}")
        self.assertEqual(self.call(f"Get-LinkState {quote(self.root)}"), "linked")

    def test_only_an_administrator_reads_the_code_and_the_browser_is_not_elevated(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        main = text.split("function Invoke-OpenClaimLink", 1)[1]
        self.assertLess(main.index("if (-not (Test-Administrator))"), main.index("Get-ClaimLink"))
        self.assertIn("-Verb RunAs", main.split("Get-ClaimLink", 1)[0])
        self.assertIn("Start-Process -FilePath (Join-Path $env:SystemRoot 'explorer.exe') -ArgumentList $link", main)
        self.assertNotIn("Start-Process -FilePath $link", text)
        self.assertLess(main.index("(Get-LinkState $dataRoot) -eq 'linked'"), main.index("Get-ClaimLink"))

    def test_never_writes_or_prints_the_code(self):
        text = self.SCRIPT.read_text(encoding="utf-8")
        for statement in ("Write-Host", "Write-Output", "Write-Verbose", "Write-Information", "Out-File", "Set-Content",
                          "Add-Content", "Start-Transcript", "Show-Message $link", "Show-Message \"$link"):
            self.assertNotIn(statement, text)


if __name__ == "__main__":
    unittest.main()
