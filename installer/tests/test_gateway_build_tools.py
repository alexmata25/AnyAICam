"""PowerShell helpers and source ordering only; never invokes build.ps1."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WIN = ROOT / 'installer/windows'


class GatewayBuildToolsTests(unittest.TestCase):
    def test_wheel_lock_rejects_empty_malformed_duplicate_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / 'wheels.lock.sha256'
            script = r"""
            $ErrorActionPreference='Stop'
            . '%s'
            $lock='%s'
            $hash='a'*64
            Set-Content -LiteralPath $lock -Value "$hash  valid-1.0-py3-none-any.whl" -Encoding ASCII
            $items=@(Read-BuildWheelLock $lock)
            if($items.Count -ne 1 -or $items[0].Name -ne 'valid-1.0-py3-none-any.whl') { throw 'valid lock refused' }
            foreach($bad in @('', 'bad', "$hash  ../outside.whl", "$hash  C:\outside.whl", "$hash  pkg.whl`n$hash  PKG.whl")) {
                Set-Content -LiteralPath $lock -Value $bad -Encoding ASCII
                $refused=$false
                try { [void]@(Read-BuildWheelLock $lock) } catch { $refused=$true }
                if(-not $refused) { throw 'invalid wheel lock accepted' }
            }
            """ % (WIN / 'build-tools.ps1', lock)
            result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_existing_wheel_lock_is_accepted_before_any_build(self):
        script = ". '%s'; $entries=@(Read-BuildWheelLock '%s'); if($entries.Count -lt 1){throw 'empty lock'}" % (WIN / 'build-tools.ps1', WIN / 'wheels.lock.sha256')
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_compiler_guard_refuses_missing_unpinned_and_non_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / 'ISCC.exe'
            fake.write_bytes(b'not executable; must never be run')
            script = """
            $ErrorActionPreference = 'Stop'
            . '%s'
            foreach ($path in @('%s','%s','%s')) {
                $refused = $false
                try { Assert-BuildInnoCompiler $path } catch { $refused = $true }
                if (-not $refused) { throw 'unverified compiler accepted' }
            }
            """ % (WIN / 'build-tools.ps1', fake, Path(folder) / 'missing.exe', folder)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_compiler_manifest_rejects_malformed_duplicate_and_unsafe_entries(self):
        digest = 'a' * 64
        cases = ['', 'not a manifest', f'{digest}  ../ISCC.exe', f'{digest}  ISCC.exe\n{digest}  ISCC.exe',
                 f'{digest}  ISCmplr.dll', f'{digest.upper()}  ISCC.exe', f'{digest} ISCC.exe']
        with tempfile.TemporaryDirectory() as folder:
            for index, text in enumerate(cases):
                (Path(folder) / f'm{index}.sha256').write_text(text, encoding='ascii')
            script = """
            $ErrorActionPreference = 'Stop'
            . '%s'
            [void](Read-BuildInnoManifest '%s')
            foreach ($index in 0..%d) {
                $refused = $false
                try { [void](Read-BuildInnoManifest (Join-Path '%s' "m$index.sha256")) } catch { $refused = $true }
                if (-not $refused) { throw "unsafe manifest $index accepted" }
            }
            """ % (WIN / 'build-tools.ps1', WIN / 'inno-setup-7.1.0.sha256', len(cases) - 1, folder)
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_compiler_signature_requires_pinned_publisher_and_valid_status(self):
        script = """
        $ErrorActionPreference = 'Stop'
        . '%s'
        if (-not (Get-BuildInnoSignatureProblem $null)) { throw 'missing signature accepted' }
        foreach ($status in @('NotSigned','HashMismatch','NotTrusted','UnknownError')) {
            if (-not (Get-BuildInnoSignatureProblem ([pscustomobject]@{Status=$status;SignerCertificate=$null}))) { throw "$status accepted" }
        }
        $other = [pscustomobject]@{Thumbprint='0000000000000000000000000000000000000000'}
        if (-not (Get-BuildInnoSignatureProblem ([pscustomobject]@{Status='Valid';SignerCertificate=$other}))) { throw 'other publisher accepted' }
        if (-not (Get-BuildInnoSignatureProblem ([pscustomobject]@{Status='Valid';SignerCertificate=$null}))) { throw 'no signer accepted' }
        """ % (WIN / 'build-tools.ps1')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_compiler_copy_is_accepted_and_every_tamper_is_refused(self):
        installed = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Inno Setup 7'
        if not (installed / 'ISCC.exe').is_file():
            self.skipTest('Inno Setup 7.1.0 is not installed on this machine')
        with tempfile.TemporaryDirectory() as folder:
            script = """
            $ErrorActionPreference = 'Stop'
            . '%s'
            $source = '%s'; $root = '%s'
            function Copy-Compiler([string]$Name) {
                $target = Join-Path $root $Name
                Copy-Item -LiteralPath $source -Destination $target -Recurse
                return $target
            }
            function Assert-Refused([string]$Folder, [string]$Expected) {
                $problems = @(Get-BuildInnoCompilerProblems (Join-Path $Folder 'ISCC.exe') '%s')
                if (-not ($problems -match [regex]::Escape($Expected))) { throw "tamper not refused ($Expected): $($problems -join '; ')" }
            }
            $clean = Copy-Compiler 'clean'
            Assert-BuildInnoCompiler (Join-Path $clean 'ISCC.exe')
            $changed = Copy-Compiler 'changed'
            $bytes = [IO.File]::ReadAllBytes((Join-Path $changed 'Setup.e64')); $bytes[1024] = $bytes[1024] -bxor 1
            [IO.File]::WriteAllBytes((Join-Path $changed 'Setup.e64'), $bytes)
            Assert-Refused $changed 'does not match the pinned Inno Setup 7.1.0: Setup.e64'
            $planted = Copy-Compiler 'planted'
            Copy-Item -LiteralPath (Join-Path $planted 'ISPP.dll') -Destination (Join-Path $planted 'version.dll')
            Assert-Refused $planted 'unexpected file in the compiler folder: version.dll'
            $missing = Copy-Compiler 'missing'
            Remove-Item -LiteralPath (Join-Path $missing 'Languages') -Recurse
            Assert-Refused $missing 'pinned compiler file is missing: Languages/'
            """ % (WIN / 'build-tools.ps1', installed, folder, WIN / 'inno-setup-7.1.0.sha256')
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=300)
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
