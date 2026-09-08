$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$required = @(
    'app\main.py',
    'installer\windows\AnyAiCam-VMS.iss',
    'installer\windows\AnyAiCamVMS.xml',
    'installer\windows\install-runtime.ps1',
    'installer\windows\service-launcher.ps1'
)
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) { throw "Missing $relative" }
}
$tokens = $null; $errors = $null
foreach ($relative in @('installer\windows\install-runtime.ps1', 'installer\windows\service-launcher.ps1', 'installer\windows\build.ps1')) {
    [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $root $relative), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell syntax error in $relative`: $($errors[0].Message)" }
}
$main = Get-Content -Raw -LiteralPath (Join-Path $root 'app\main.py')
foreach ($name in @('ANYAICAM_STATIC_FOLDER', 'ANYAICAM_RECORDINGS_FOLDER', 'ANYAICAM_HLS_FOLDER')) {
    if (-not $main.Contains($name)) { throw "main.py is missing $name Windows path support" }
}
$iss = Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\AnyAiCam-VMS.iss')
foreach ($text in @('uninsneveruninstall', 'AnyAiCamVMS.exe', 'python-3.12.10-embed-amd64.zip', 'get-pip.py', 'vendor\wheels\*', 'ffmpeg-8.1.2-essentials_build.zip', 'Start-Sleep -Seconds 3', 'PrivilegesRequired=admin')) {
    if (-not $iss.Contains($text)) { throw "Installer manifest is missing $text" }
}
$runtime = Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\install-runtime.ps1')
if (-not $runtime.Contains('--no-index')) { throw 'Runtime dependency installation is not offline-only.' }
foreach ($forbidden in @('InstallAllUsers=', 'python-3.12.10-amd64.exe', '/uninstall')) {
    if ($iss.Contains($forbidden)) { throw "Installer manifest contains unsafe registered-Python operation: $forbidden" }
}
foreach ($signingText in @('SignTool=AnyAiCamSign', 'SignedUninstaller=yes', 'SignedUninstallerDir=', 'Get-AuthenticodeSignature', 'ANYAICAM_TIMESTAMP_URL')) {
    if (-not ((Get-Content -Raw (Join-Path $root 'installer\windows\AnyAiCam-VMS.iss')) + (Get-Content -Raw (Join-Path $root 'installer\windows\build.ps1'))).Contains($signingText)) { throw "Signing support is missing $signingText" }
}
$xml = [xml](Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\AnyAiCamVMS.xml'))
if ($xml.service.startmode -ne 'Automatic') { throw 'Service is not automatic.' }
if (-not $xml.service.onfailure) { throw 'Service has no restart-on-failure policy.' }
'Windows installer source checks passed.'
