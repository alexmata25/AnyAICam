# AnyAiCam appliance agent Windows service entry point (run by WinSW as
# LocalSystem, 2026-10-07). The agent links this PC to the AnyAiCam cloud
# (headless claim with the installer's claim code), then sends heartbeats and
# camera status and runs the portal's commands (windows.py: restarts only).
#
# Every folder is set here: the Linux defaults (/etc/anyaicam, /var/lib/...)
# mean nothing on Windows. config is the VMS's own config folder, so the
# agent's ANYAICAM_CLOUD_URL update reaches the vms.env the VMS loads, and the
# VMS reads the cloud identity the agent writes to recordings\.
$ErrorActionPreference = 'Stop'
$installRoot = Split-Path -Parent $PSScriptRoot
$dataRoot = Join-Path $env:ProgramData 'AnyAiCam'
$config = Join-Path $dataRoot 'config'

$env:ANYAICAM_CONFIG_DIR = $config
$env:ANYAICAM_STATE_DIR = Join-Path $dataRoot 'agent'
$env:ANYAICAM_LOG_DIR = Join-Path $dataRoot 'logs'
$env:ANYAICAM_RECORDING_PATH = Join-Path $dataRoot 'recordings'
$env:ANYAICAM_VMS_RECORDINGS_PATH = Join-Path $dataRoot 'recordings'
$env:ANYAICAM_VMS_HLS_PATH = Join-Path $dataRoot 'hls'
$env:ANYAICAM_VMS_RELEASE_MARKER = Join-Path $config 'vms_release.json'
$env:PYTHONUNBUFFERED = '1'

function Import-EnvFile([string]$Path, [string[]]$Keys) {
    $values = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
            $parts = $line.Split('=', 2)
            if ($parts.Count -eq 2 -and $Keys -contains $parts[0]) { $values[$parts[0]] = $parts[1] }
        }
    }
    return $values
}
# The cloud portal before activation (agent.json takes over once linked).
$agent = Import-EnvFile (Join-Path $config 'agent.env') @('ANYAICAM_PORTAL_URL', 'ANYAICAM_AGENT_MODE')
foreach ($key in $agent.Keys) { [Environment]::SetEnvironmentVariable($key, $agent[$key], 'Process') }
# The VMS's local address (same port override as service-launcher.ps1).
$vms = Import-EnvFile (Join-Path $config 'vms.env') @('ANYAICAM_HTTP_PORT')
$port = if ($vms['ANYAICAM_HTTP_PORT'] -match '^\d{2,5}$') { $vms['ANYAICAM_HTTP_PORT'] } else { '8000' }
$env:ANYAICAM_VMS_LOCAL_URL = "http://127.0.0.1:$port"
$env:ANYAICAM_VMS_LOCAL_HEALTH_URL = "http://127.0.0.1:$port/health"

New-Item -ItemType Directory -Force -Path $env:ANYAICAM_STATE_DIR | Out-Null
& (Join-Path $installRoot 'runtime\python\python.exe') (Join-Path $installRoot 'agent\agent-main.py')
exit $LASTEXITCODE
