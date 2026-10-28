<#
Windows 1.3.4.1 acceptance A6 (lifecycle): a READ-ONLY snapshot of an AnyAiCam VMS installation, taken before and after
each upgrade / repair / uninstall step and compared by Compare-AnyAiCamInstallState.ps1.

  .\Get-AnyAiCamInstallState.ps1 -OutputPath C:\Acceptance\A6\01-before-upgrade.json     (elevated: ProgramData is admin-only)

Secrets are never recorded: from vms.env only KEY NAMES are kept, plus the VALUES of an allowlist of non-secret switches;
identity files (agent credential, appliance identity) are recorded as SHA-256 only, to prove they survived unchanged.
Non-elevated runs record ProgramData as "access denied" instead of failing.
#>
param([Parameter(Mandatory=$true)][string]$OutputPath,
      [string]$InstallRoot = (Join-Path $env:ProgramFiles 'AnyAiCam'),
      [string]$DataRoot = (Join-Path $env:ProgramData 'AnyAiCam'))
$ErrorActionPreference = 'Stop'
$AppId = '{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}_is1'
$PublicEnvKeys = @('ANYAICAM_BUILD_ID', 'ANYAICAM_VMS_COMMIT', 'ANYAICAM_PRODUCT_MODE', 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP',
    'ANYAICAM_RUNTIME_ROLE', 'ANYAICAM_ENV', 'ANYAICAM_LPR_ENABLED', 'PEOPLE_COUNTING_ENABLED', 'CUSTOMER_ANALYTICS_RULES_ENABLED',
    'ANYAICAM_PPE_ENABLED', 'ANYAICAM_FACIAL_RECOGNITION_ENABLED', 'ANYAICAM_VMS_CPU_LIMIT_PERCENT', 'ANYAICAM_VMS_MEMORY_LIMIT_MB',
    'ANYAICAM_HTTP_PORT', 'ANYAICAM_BIND_HOST')
$IdentityFiles = @('agent\credential.json', 'config\appliance_identity.json', 'recordings\appliance_identity.json', 'config\feature_entitlements.json')

function Get-Sha([string]$Path) { try { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() } catch { 'unreadable' } }

$state = [ordered]@{ taken = (Get-Date).ToUniversalTime().ToString('o'); computer = $env:COMPUTERNAME; install_root = $InstallRoot; data_root = $DataRoot }
$uninstall = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall') |
    ForEach-Object { Get-ItemProperty -LiteralPath (Join-Path $_ $AppId) -ErrorAction SilentlyContinue } | Select-Object -First 1
$state.registered = [bool]$uninstall
$state.version = if ($uninstall) { [string]$uninstall.DisplayVersion } else { $null }
$state.display_name = if ($uninstall) { [string]$uninstall.DisplayName } else { $null }
$state.services = [ordered]@{}
foreach ($name in 'AnyAiCamVMS', 'AnyAiCamAgent') {
    $s = Get-CimInstance Win32_Service -Filter "Name='$name'" -ErrorAction SilentlyContinue
    $state.services[$name] = if ($s) { [ordered]@{ state = $s.State; start_mode = $s.StartMode; account = $s.StartName; path = $s.PathName } } else { $null }
}
$state.program_folder_exists = Test-Path -LiteralPath $InstallRoot
if ($state.program_folder_exists) {
    $files = @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Force -ErrorAction SilentlyContinue)
    $rootFull = (Get-Item -LiteralPath $InstallRoot).FullName.TrimEnd('\') + '\'
    $state.program_files = [ordered]@{ count = $files.Count; bytes = [long](($files | Measure-Object Length -Sum).Sum) }
    $state.program_file_list = @($files | ForEach-Object { $_.FullName.Substring($rootFull.Length) } | Sort-Object)
    $state.scripts = [ordered]@{}
    foreach ($f in $files | Where-Object { $_.Extension -eq '.ps1' -and $_.DirectoryName -match '\\(service|installer)$' }) { $state.scripts[$f.FullName.Substring($rootFull.Length)] = Get-Sha $f.FullName }
    $manifest = Join-Path $InstallRoot 'runtime\tools\tesseract\BUNDLE-MANIFEST.json'
    $state.tesseract_manifest = if (Test-Path -LiteralPath $manifest) { Get-Sha $manifest } else { $null }
}
$catalog = Join-Path $env:SystemRoot 'System32\CatRoot\{F750E6C3-38EE-11D1-85E5-00C04FC295EE}\AnyAiCamRuntime.cat'
$state.runtime_catalog = if (Test-Path -LiteralPath $catalog) { Get-Sha $catalog } else { $null }
$state.firewall_rules = @(Get-NetFirewallRule -Group 'AnyAiCam VMS' -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Name)|$($_.Enabled)" } | Sort-Object)
$state.data = [ordered]@{ readable = $true }
try {
    $null = Get-ChildItem -LiteralPath $DataRoot -ErrorAction Stop
    $envFile = Join-Path $DataRoot 'config\vms.env'
    if (Test-Path -LiteralPath $envFile) {
        $keys = [ordered]@{}; $public = [ordered]@{}
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if (-not $line -or $line.TrimStart().StartsWith('#') -or -not $line.Contains('=')) { continue }
            $key, $value = $line.Split('=', 2)
            $keys[$key] = $true
            if ($PublicEnvKeys -contains $key) { $public[$key] = $value }
        }
        $state.data.vms_env_keys = @($keys.Keys | Sort-Object); $state.data.vms_env_public = $public
    } else { $state.data.vms_env_keys = $null }
    $state.data.identity_sha256 = [ordered]@{}
    foreach ($relative in $IdentityFiles) { $p = Join-Path $DataRoot $relative; if (Test-Path -LiteralPath $p) { $state.data.identity_sha256[$relative] = Get-Sha $p } }
    $db = Join-Path $DataRoot 'database\partner_portal.db'
    $state.data.database = if (Test-Path -LiteralPath $db) { [ordered]@{ bytes = (Get-Item -LiteralPath $db).Length } } else { $null }
    $recordings = @(Get-ChildItem -LiteralPath (Join-Path $DataRoot 'recordings') -Recurse -File -Include *.mp4, *.mkv -ErrorAction SilentlyContinue)
    $state.data.recordings = [ordered]@{ count = $recordings.Count; bytes = [long](($recordings | Measure-Object Length -Sum).Sum) }
    $state.data.folders = @(Get-ChildItem -LiteralPath $DataRoot -Directory -ErrorAction SilentlyContinue | ForEach-Object { $_.Name } | Sort-Object)
} catch [UnauthorizedAccessException] { $state.data = [ordered]@{ readable = $false; reason = 'access denied (run elevated)' } }
catch { if ($_.Exception -is [System.Management.Automation.ItemNotFoundException]) { $state.data = [ordered]@{ readable = $true; missing = $true } } else { $state.data = [ordered]@{ readable = $false; reason = $_.Exception.Message } } }
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
$state | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath $OutputPath
"snapshot: registered=$($state.registered) version=$($state.version) files=$($state.program_files.count) catalog=$([bool]$state.runtime_catalog) data_readable=$($state.data.readable) -> $OutputPath"
