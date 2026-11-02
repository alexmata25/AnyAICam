"""PowerShell helpers and source ordering only; never invokes build.ps1."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / 'installer/windows'


class GatewayBuildToolsTests(unittest.TestCase):
    def test_compiler_metadata_refuses_wrong_patch_missing_metadata_and_non_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'ISCC.exe'
            fake.write_bytes(b'not executable; must never be run')
            script = """
            $ErrorActionPreference = 'Stop'
            . '%s'
            $accepted = [pscustomobject]@{FileMajorPart=7;FileMinorPart=1;FileBuildPart=0;FilePrivatePart=0}
            if (Get-BuildInnoVersionProblem $accepted) { throw 'approved metadata refused' }
            foreach ($field in @('FileMajorPart','FileMinorPart','FileBuildPart','FilePrivatePart')) {
                $changed = $accepted.PSObject.Copy()
                $changed.$field = $changed.$field + 1
                if (-not (Get-BuildInnoVersionProblem $changed)) { throw 'other compiler version accepted' }
            }
            if (-not (Get-BuildInnoVersionProblem $null)) { throw 'absent metadata accepted' }
            if (-not (Get-BuildInnoVersionProblem ([pscustomobject]@{FileMajorPart=7}))) { throw 'incomplete metadata accepted' }
            foreach ($path in @('%s','%s','%s')) {
                $refused = $false
                try { Assert-BuildInnoCompiler $path } catch { $refused = $true }
                if (-not $refused) { throw 'unverified compiler accepted' }
            }
            """ % (WIN / 'build-tools.ps1', fake, Path(folder) / 'missing.exe', folder)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_compiler_guard_precedes_staging_and_compilation(self):
        source = (WIN / 'build.ps1').read_text(encoding='utf-8')
        self.assertLess(source.index('Assert-BuildInnoCompiler $iscc'), source.index('Export-NativeFiles'))
        self.assertLess(source.index('Assert-BuildInnoCompiler $iscc'), source.index('& $iscc @compilerArguments'))

    def test_explicit_git_and_source_integrity_guard_without_path_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'fake git.ps1'
            fake.write_text("'?? app/unsafe.py'\nexit 0\n")
            script = """
            $ErrorActionPreference = 'Stop'
            . '%s'
            . '%s'
            $before = $env:PATH
            $gitTool = Resolve-BuildGit '%s'
            $problems = @(Get-PackagedSourceProblems '%s' '%s' -GitExe $gitTool)
            if ($problems -notcontains 'untracked: app/unsafe.py') { throw 'source gate bypassed' }
            if ($env:PATH -cne $before) { throw 'PATH changed' }
            $refused = $false
            try { Resolve-BuildGit '%s' } catch { $refused = $true }
            if (-not $refused) { throw 'missing explicit Git accepted' }
            """ % (WIN / 'build-tools.ps1', WIN / 'source-integrity.ps1', fake, folder,
                       WIN / 'AnyAiCam-VMS.iss', Path(folder) / 'missing.exe')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_interpreter_and_keyset_fail_before_expensive_staging(self):
        source = (WIN / 'build.ps1').read_text(encoding='utf-8')
        self.assertLess(source.index('$pythonVersion ='), source.index('Export-NativeFiles'))
        self.assertLess(source.index('The prepared entitlement keyset does not match'), source.index('# Every vendored input'))
        self.assertIn('-GitExe $GitExe', source)
        self.assertIn('& $GitExe -C $repo rev-parse HEAD', source)
        self.assertIn("$pythonVersion -ne 'Python 3.12.10'", source)
        self.assertIn('& py -3.11', source)  # Blue's existing helper-only fallback

    def test_edited_powershell_sources_parse_without_running(self):
        paths = [WIN / name for name in ('build.ps1', 'build-tools.ps1', 'source-integrity.ps1')]
        script = "$ErrorActionPreference='Stop'\n" + '\n'.join(
            "$parseErrors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$parseErrors); if($parseErrors.Count){throw 'PowerShell parse failure'}" % path
            for path in paths)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
