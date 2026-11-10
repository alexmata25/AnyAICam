# Isolated DirectDellTestCandidate entry point. This service never starts the
# production cloud agent and binds only to loopback.
$ErrorActionPreference = 'Stop'
$installRoot = Split-Path -Parent $PSScriptRoot
$expectedInstall = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'AnyAiCam VMS Direct Test')).TrimEnd('\')
if ([IO.Path]::GetFullPath($installRoot).TrimEnd('\') -ine $expectedInstall) { throw 'DIRECT_TEST_INSTALL_PATH_REFUSED' }
$profilePath = Join-Path $installRoot 'direct-test-profile.json'
if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) { throw 'DIRECT_TEST_PROFILE_MISSING' }
if ((Get-Item -LiteralPath $profilePath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'DIRECT_TEST_PROFILE_REPARSE' }
$profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
if ($profile.schema -ne 1 -or $profile.run_id -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' -or
    $profile.service_name -cne 'AnyAiCamVmsDirectTest' -or $profile.http_port -ne 18767) { throw 'DIRECT_TEST_PROFILE_INVALID' }
$expectedRoot = Join-Path (Join-Path $env:ProgramData 'AnyAiCam-Test') $profile.run_id
$normalize = { param($p) [IO.Path]::GetFullPath([string]$p).TrimEnd('\') }
if ((& $normalize $profile.data_root) -ine (& $normalize $expectedRoot)) { throw 'DIRECT_TEST_ROOT_MISMATCH' }
$dataRoot = [string]$expectedRoot
. (Join-Path $installRoot 'installer\data-root-guard.ps1')
Assert-AnyAiCamDataRootHasNoLinks $dataRoot
if (-not (Test-AnyAiCamDataRootTrusted $dataRoot)) { throw 'DIRECT_TEST_ROOT_ACL_UNTRUSTED' }

$config = Join-Path $dataRoot 'config'
$recordings = Join-Path $dataRoot 'recordings'
$env:ANYAICAM_INTERNAL_TEST_BUILD = '1'
$env:ANYAICAM_TEST_RUN_ID = $profile.run_id
$env:ANYAICAM_CONFIG_DIR = $config
$env:ANYAICAM_FEATURE_ENTITLEMENTS_FILE = Join-Path $config 'feature_entitlements.json'
$env:ANYAICAM_APPLIANCE_IDENTITY_FILE = Join-Path $config 'appliance_identity.json'
$env:ANYAICAM_PRODUCT_MODE_STATE_FILE = Join-Path $config 'product_mode.json'
$env:ANYAICAM_STATE_DIR = Join-Path $dataRoot 'agent'
$env:ANYAICAM_LOG_DIR = Join-Path $dataRoot 'logs'
$env:ANYAICAM_RECORDING_PATH = $recordings
$env:ANYAICAM_VMS_RECORDINGS_PATH = $recordings
$env:ANYAICAM_RECORDINGS_FOLDER = $recordings
$env:ANYAICAM_HLS_FOLDER = Join-Path $dataRoot 'hls'
$env:ANYAICAM_PARTNER_DB = Join-Path $dataRoot 'database\partner_portal.db'
$env:ANYAICAM_MEDIAMTX_CONFIG = Join-Path $dataRoot 'mediamtx\anyaicam-mediamtx.yml'
$env:ANYAICAM_STATIC_FOLDER = Join-Path $installRoot 'app\static'
$env:ANYAICAM_BIND_HOST = '127.0.0.1'
$env:ANYAICAM_HTTP_PORT = '18767'
$env:ANYAICAM_PUBLIC_URL = 'http://127.0.0.1:18767'
$env:ANYAICAM_VMS_LOCAL_URL = 'http://127.0.0.1:18767'
$env:ANYAICAM_VMS_LOCAL_HEALTH_URL = 'http://127.0.0.1:18767/health'
$env:PYTHONUNBUFFERED = '1'
$env:PATH = (Join-Path $installRoot 'runtime\tools\ffmpeg') + ';' + $env:PATH
$env:PATH = (Join-Path $installRoot 'runtime\tools\tesseract') + ';' + $env:PATH
$env:TESSDATA_PREFIX = Join-Path $installRoot 'runtime\tools\tesseract\tessdata'
foreach ($folder in @($env:ANYAICAM_STATE_DIR, $env:ANYAICAM_LOG_DIR, $env:ANYAICAM_HLS_FOLDER,
                      (Split-Path -Parent $env:ANYAICAM_PARTNER_DB), (Split-Path -Parent $env:ANYAICAM_MEDIAMTX_CONFIG))) {
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) { throw 'DIRECT_TEST_DIRECTORY_MISSING' }
}
$envFile = Join-Path $config 'vms.env'
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'DIRECT_TEST_ENV_MISSING' }
foreach ($line in Get-Content -LiteralPath $envFile) {
    if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
    $parts = $line.Split('=', 2)
    if ($parts.Count -eq 2 -and $parts[0] -in @('ANYAICAM_ENV', 'ANYAICAM_BUILD_ID', 'ANYAICAM_VERSION', 'ANYAICAM_WINDOWS_SETUP_VERSION', 'WEB_CONCURRENCY')) {
        [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
    }
}
# The internal test profile has no portal or account-link path. Product-mode
# behavior is tested locally from the synthetic, signed entitlement fixture.
$env:ANYAICAM_AGENT_MODE = 'disabled'
$env:ANYAICAM_RUNTIME_ROLE = 'edge'
$env:ANYAICAM_ENV = 'local'
$env:ANYAICAM_PRODUCT_MODE = 'local'
$env:ANYAICAM_PRODUCT_MODE_BOOTSTRAP = 'local'
$env:ANYAICAM_CLOUD_URL = ''
$env:ANYAICAM_PORTAL_URL = ''
$env:ANYAICAM_ANALYTICS_SYNC_ENABLED = 'false'
$env:ANYAICAM_EVENT_MEDIA_UPLOAD_ENABLED = 'false'
$env:ANYAICAM_FACIAL_EMBEDDING_SYNC_ENABLED = 'false'
$env:ANYAICAM_LIVE_RELAY_ENABLED = 'false'
$env:ANYAICAM_RECORDING_UPLOAD_ENABLED = 'false'
$env:ANYAICAM_LIVE_P2P_ENABLED = 'false'
$env:ANYAICAM_PRODUCT_MODE_SELF_RESTART = 'false'
$env:ANYAICAM_TURN_URL = ''
$env:ANYAICAM_TURN_USERNAME = ''
$env:ANYAICAM_TURN_CREDENTIAL = ''
$env:ANYAICAM_STUN_URL = ''
$env:ANYAICAM_LIVE_STUN_SERVERS = ''
$env:ANYAICAM_LIVE_TURN_SERVERS = ''
$env:ANYAICAM_LIVE_TURN_USERNAME = ''
$env:ANYAICAM_LIVE_TURN_CREDENTIAL = ''
$env:WEB_CONCURRENCY = '1'
try { . (Join-Path $PSScriptRoot 'vms-job-limits.ps1'); Write-Output (Set-AnyAiCamVmsJobLimits @{}) } catch { throw 'DIRECT_TEST_RESOURCE_POLICY_FAILED' }
Set-Location (Join-Path $installRoot 'app')
& (Join-Path $installRoot 'runtime\python\python.exe') -m uvicorn main:app --host 127.0.0.1 --port 18767 --workers 1
exit $LASTEXITCODE
