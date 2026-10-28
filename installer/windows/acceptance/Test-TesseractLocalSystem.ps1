<#
Windows 1.3.4.1 acceptance A1: the bundled Tesseract launches and finds its language model as LocalSystem, the account
the AnyAiCam VMS service (WinSW) runs as, with exactly the PATH/TESSDATA_PREFIX service-launcher.ps1 gives it.

  R430 test VM (elevated):  .\Test-TesseractLocalSystem.ps1 -OutputDir C:\Acceptance\A1
  optional OCR check:       -Image C:\Inputs\ocr-tests\plate1.png -Expected ABC1234
  local/mocked run:         -RunAs CurrentUser -InstallRoot <fake install>   (no LocalSystem evidence; marked as such)

Checks: installed files match the bundle manifest (none missing, none altered); bundled licences present; the probe ran
as SYSTEM (LocalSystem mode); `tesseract` resolves through PATH to the bundled exe; --version exit 0 and "tesseract 5.";
--list-langs lists eng; not blocked by App Control; optional OCR text matches. Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED.
#>
param([string]$InstallRoot = (Join-Path $env:ProgramFiles 'AnyAiCam'),
      [ValidateSet('LocalSystem', 'CurrentUser')][string]$RunAs = 'LocalSystem',
      [string]$OutputDir = (Join-Path $env:TEMP 'AnyAiCamAcceptance'), [string]$Image = '', [string]$Expected = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'acceptance-common.ps1')
$result = New-AcceptanceResult 'A1-tesseract-localsystem' $RunAs
$output = Join-Path $OutputDir 'A1-tesseract-localsystem.json'
$root = Join-Path $InstallRoot 'runtime\tools\tesseract'
try {
    $manifestPath = Join-Path $root 'BUNDLE-MANIFEST.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { Complete-AcceptanceResult $result $output "Tesseract is not installed: $manifestPath missing" | Out-Null; exit 2 }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    $missing = @(); $changed = @(); $count = 0
    foreach ($entry in $manifest.files.PSObject.Properties | Where-Object { $_.Name -like 'tesseract/*' }) {
        $count++
        $path = Join-Path $root ($entry.Name.Substring('tesseract/'.Length) -replace '/', '\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $missing += $entry.Name; continue }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.Value.sha256) { $changed += $entry.Name }
    }
    Add-AcceptanceCheck $result 'installed engine files match the bundle manifest' ($count -gt 0 -and -not $missing -and -not $changed) "$count files; missing: $($missing -join ', '); altered: $($changed -join ', ')"
    $licences = Join-Path $InstallRoot 'licenses\tesseract'
    Add-AcceptanceCheck $result 'bundled licences installed' ((Test-Path -LiteralPath $licences) -and @(Get-ChildItem -LiteralPath $licences -Recurse -File).Count -gt 0) $licences

    $arguments = @{ InstallRoot = $InstallRoot }
    if ($Image) { $arguments.Image = $Image; $arguments.Expected = $Expected }
    $probe = Invoke-AcceptanceProbe -ProbeScript (Join-Path $PSScriptRoot 'probe-tesseract.ps1') -Arguments $arguments -RunAs $RunAs
    $result.probe = $probe
    if ($RunAs -eq 'LocalSystem') { Add-AcceptanceCheck $result 'probe ran as LocalSystem' ($probe.sid -eq 'S-1-5-18') $probe.identity }
    $expectedExe = Join-Path $root 'tesseract.exe'
    Add-AcceptanceCheck $result 'tesseract resolves through PATH to the bundled engine' ([string]$probe.resolved -ieq $expectedExe -or ([string]$probe.resolved).StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) "$($probe.resolved)"
    Add-AcceptanceCheck $result 'TESSDATA_PREFIX is the bundled model folder' ($probe.tessdata_prefix -ieq (Join-Path $root 'tessdata')) "$($probe.tessdata_prefix)"
    Add-AcceptanceCheck $result 'not blocked by App Control (error 4551)' (-not $probe.blocked_by_app_control) ''
    Add-AcceptanceCheck $result 'tesseract --version runs (Tesseract 5)' ($probe.version_exit -eq 0 -and $probe.version -match 'tesseract 5\.') ("exit $($probe.version_exit): " + ([string]$probe.version -split "`n")[0])
    Add-AcceptanceCheck $result 'English model found (--list-langs has eng)' ($probe.langs_exit -eq 0 -and @($probe.languages) -contains 'eng') ("languages: " + (@($probe.languages) -join ','))
    if ($Image) { Add-AcceptanceCheck $result "OCR of $(Split-Path -Leaf $Image) as LPR calls it" ($probe.ocr_exit -eq 0 -and $probe.ocr_text -eq $Expected) "got '$($probe.ocr_text)', expected '$Expected'" }
    if ($probe.error) { Add-AcceptanceCheck $result 'probe completed without error' $false $probe.error }
} catch {
    Complete-AcceptanceResult $result $output $_.Exception.Message | Out-Null
    Write-Output "A1 BLOCKED: $($_.Exception.Message)"; exit 2
}
$final = Complete-AcceptanceResult $result $output
$final.checks | ForEach-Object { '{0} {1} {2}' -f ($(if ($_.passed) { 'PASS' } else { 'FAIL' })), $_.name, $_.detail }
"A1 $($final.result) ($RunAs) -> $output"
exit $(if ($final.result -eq 'PASS') { 0 } else { 1 })
