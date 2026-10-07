$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$required = @(
    'app\main.py',
    'app\runtime_paths.py',
    'installer\windows\AnyAiCam-VMS.iss',
    'installer\windows\AnyAiCamVMS.xml',
    'installer\windows\install-runtime.ps1',
    'installer\windows\service-launcher.ps1',
    'installer\windows\firewall.ps1',
    'installer\windows\runtime-preflight.py',
    'installer\windows\cloud-link.ps1',
    'installer\windows\agent-launcher.ps1',
    'installer\windows\agent-main.py',
    'installer\windows\AnyAiCamAgent.xml',
    'appliance-agent\anyaicam_agent\windows.py'
)
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) { throw "Missing $relative" }
}
$tokens = $null; $errors = $null
foreach ($relative in @('installer\windows\install-runtime.ps1', 'installer\windows\service-launcher.ps1', 'installer\windows\build.ps1', 'installer\windows\firewall.ps1', 'installer\windows\cloud-link.ps1', 'installer\windows\agent-launcher.ps1')) {
    [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $root $relative), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw "PowerShell syntax error in $relative`: $($errors[0].Message)" }
}
# 2026-10-07: the data/static roots live in app/runtime_paths.py; HLS stays in main.py.
$paths = (Get-Content -Raw -LiteralPath (Join-Path $root 'app\runtime_paths.py')) + (Get-Content -Raw -LiteralPath (Join-Path $root 'app\main.py'))
foreach ($name in @('ANYAICAM_STATIC_FOLDER', 'ANYAICAM_RECORDINGS_FOLDER', 'ANYAICAM_HLS_FOLDER')) {
    if (-not $paths.Contains($name)) { throw "The VMS is missing $name Windows path support" }
}
$iss = Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\AnyAiCam-VMS.iss')
foreach ($text in @('uninsneveruninstall', 'AnyAiCamVMS.exe', 'python-3.12.10-embed-amd64.zip', 'get-pip.py', 'vendor\wheels\*', 'ffmpeg-8.1.2-essentials_build.zip', 'mediamtx_v1.21.0_windows_amd64.zip', 'firewall.ps1', '[InstallDelete]', 'Start-Sleep -Seconds 3', 'PrivilegesRequired=admin', 'vc_redist.x64-14.44.35211.exe', 'runtime-preflight.py')) {
    if (-not $iss.Contains($text)) { throw "Installer manifest is missing $text" }
}
$runtime = Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\install-runtime.ps1')
if (-not $runtime.Contains('--no-index')) { throw 'Runtime dependency installation is not offline-only.' }
if (-not $runtime.Contains('runtime-preflight.py')) { throw 'Runtime install does not run the preflight before the service is installed.' }
foreach ($forbidden in @('InstallAllUsers=', 'python-3.12.10-amd64.exe', '/uninstall')) {
    if ($iss.Contains($forbidden)) { throw "Installer manifest contains unsafe registered-Python operation: $forbidden" }
}
foreach ($signingText in @('SignTool=AnyAiCamSign', 'SignedUninstaller=yes', 'SignedUninstallerDir=', 'Get-AuthenticodeSignature', 'ANYAICAM_TIMESTAMP_URL')) {
    if (-not ((Get-Content -Raw (Join-Path $root 'installer\windows\AnyAiCam-VMS.iss')) + (Get-Content -Raw (Join-Path $root 'installer\windows\build.ps1'))).Contains($signingText)) { throw "Signing support is missing $signingText" }
}
foreach ($azureSigningText in @('AzureSign', '/dlib', '/dmdf', 'timestamp.acs.microsoft.com')) {
    if (-not (Get-Content -Raw (Join-Path $root 'installer\windows\build.ps1')).Contains($azureSigningText)) { throw "Azure signing support is missing $azureSigningText" }
}
$requirements = Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\requirements-windows.txt')
foreach ($requiredPin in @('torch==2.5.1+cpu', 'torchvision==0.20.1+cpu', 'ultralytics==8.3.40', 'setuptools==')) {
    if (-not $requirements.Contains($requiredPin)) { throw "CPU AI manifest is missing $requiredPin" }
}
# pytesseract is pinned by the Linux image too (requirements.txt); only its external OCR
# engine is not bundled (DEPENDENCIES.md). GPU packages must never reach a CPU install.
foreach ($forbiddenPackage in @('nvidia-', 'nvidia_', 'ultralytics-platform==')) {
    if ($requirements.Contains($forbiddenPackage)) { throw "CPU AI manifest contains forbidden package $forbiddenPackage" }
}
# 2026-10-07: version and commit are passed in by build.ps1, never stale literals.
foreach ($sourceText in @('#ifndef AppVersion', '#ifndef SourceCommit', 'AnyAiCam-VMS-Setup-{#AppVersion}-{#ShortCommit}')) {
    if (-not $iss.Contains($sourceText)) { throw "Installer source metadata is missing $sourceText" }
}
if ($iss.Contains('0.1.3')) { throw 'Installer source still names version 0.1.3.' }
$xml = [xml](Get-Content -Raw -LiteralPath (Join-Path $root 'installer\windows\AnyAiCamVMS.xml'))
if ($xml.service.startmode -ne 'Automatic') { throw 'Service is not automatic.' }
if (-not $xml.service.onfailure) { throw 'Service has no restart-on-failure policy.' }
'Windows installer source checks passed.'
