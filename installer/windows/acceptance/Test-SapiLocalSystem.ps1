<#
Windows 1.3.4.1 acceptance A3: the AAC Voice Call greeting is synthesized by Windows SAPI under LocalSystem (the VMS
service account), with the product's own code (app/aac_voice_call_greeting.synthesize_speech_sapi) and runtime.

  R430 test VM (elevated):  .\Test-SapiLocalSystem.ps1 -OutputDir C:\Acceptance\A3
  local run:                -RunAs CurrentUser -PythonExe <python 3.12> -AppDir <repo>\app

Checks: ran as SYSTEM (LocalSystem mode); at least one SAPI voice visible to that account (Server editions may have
none); the product's engine selection is "windows-sapi"; 22,050 Hz PCM16; 1-15 s; at least 0.3 s of speech;
levelled to the "medium" target (-28 dBFS +/- 3). The camera's two-way audio path is NOT covered here (physical test).
Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED.
#>
param([string]$InstallRoot = (Join-Path $env:ProgramFiles 'AnyAiCam'),
      [ValidateSet('LocalSystem', 'CurrentUser')][string]$RunAs = 'LocalSystem',
      [string]$PythonExe = '', [string]$AppDir = '',
      [string]$OutputDir = (Join-Path $env:TEMP 'AnyAiCamAcceptance'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'acceptance-common.ps1')
if (-not $PythonExe) { $PythonExe = Join-Path $InstallRoot 'runtime\python\python.exe' }
if (-not $AppDir) { $AppDir = Join-Path $InstallRoot 'app' }
$result = New-AcceptanceResult 'A3-sapi-greeting-localsystem' $RunAs
$output = Join-Path $OutputDir 'A3-sapi-greeting-localsystem.json'
try {
    foreach ($path in @($PythonExe, (Join-Path $AppDir 'aac_voice_call_greeting.py'))) {
        if (-not (Test-Path -LiteralPath $path)) { Complete-AcceptanceResult $result $output "missing: $path" | Out-Null; "A3 BLOCKED: missing $path"; exit 2 }
    }
    if (-not (Select-String -LiteralPath (Join-Path $AppDir 'aac_voice_call_greeting.py') -Pattern 'def synthesize_speech_sapi' -Quiet)) {
        Complete-AcceptanceResult $result $output 'the installed app has no SAPI greeting (pre-1.3.4.1 build)' | Out-Null; 'A3 BLOCKED: no SAPI greeting in this build'; exit 2
    }
    $probe = Invoke-AcceptanceProbe -ProbeScript (Join-Path $PSScriptRoot 'probe-sapi.ps1') -Arguments @{ PythonExe = $PythonExe; AppDir = $AppDir } -RunAs $RunAs -TimeoutSeconds 240
    $result.probe = $probe
    if ($RunAs -eq 'LocalSystem') { Add-AcceptanceCheck $result 'probe ran as LocalSystem' ($probe.sid -eq 'S-1-5-18') $probe.identity }
    Add-AcceptanceCheck $result 'a SAPI voice is available to this account' (@($probe.voices).Count -gt 0) ((@($probe.voices) -join '; ') + $probe.voices_error)
    $audio = $probe.audio
    if (-not $audio) { Add-AcceptanceCheck $result 'the product synthesized a greeting' $false "$($probe.python_output) $($probe.error)" }
    else {
        Add-AcceptanceCheck $result 'engine selected by the product is windows-sapi' ($audio.engine -eq 'windows-sapi') "$($audio.engine)"
        Add-AcceptanceCheck $result 'PCM16 at 22,050 Hz' ($audio.rate -eq 22050) "$($audio.rate) Hz"
        Add-AcceptanceCheck $result 'duration 1-15 s' ($audio.seconds -gt 1 -and $audio.seconds -lt 15) "$($audio.seconds) s"
        Add-AcceptanceCheck $result 'audible speech (>= 0.3 s above the gate)' ($audio.speech_seconds -ge 0.3) "$($audio.speech_seconds) s, raw $($audio.raw_dbfs) dBFS"
        Add-AcceptanceCheck $result 'levelled to the medium target (-28 dBFS +/- 3)' ($null -ne $audio.levelled_dbfs -and [math]::Abs($audio.levelled_dbfs + 28) -le 3) "$($audio.levelled_dbfs) dBFS"
    }
} catch {
    Complete-AcceptanceResult $result $output $_.Exception.Message | Out-Null
    Write-Output "A3 BLOCKED: $($_.Exception.Message)"; exit 2
}
$final = Complete-AcceptanceResult $result $output
$final.checks | ForEach-Object { '{0} {1} {2}' -f ($(if ($_.passed) { 'PASS' } else { 'FAIL' })), $_.name, $_.detail }
"A3 $($final.result) ($RunAs) -> $output"
exit $(if ($final.result -eq 'PASS') { 0 } else { 1 })
