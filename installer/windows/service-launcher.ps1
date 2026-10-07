# AnyAiCam VMS Windows service entry point (run by WinSW as LocalSystem).
#
# 2026-10-07 (current VMS): the data locations are exactly the ones 0.1.3
# used, so an upgrade keeps recordings, accounts and settings. Every folder
# the VMS writes is set here (app/runtime_paths.py) -- never left to a Linux
# container default such as /app/recordings, which on Windows would mean
# C:\app\recordings.
$ErrorActionPreference = 'Stop'
$installRoot = Split-Path -Parent $PSScriptRoot
$dataRoot = Join-Path $env:ProgramData 'AnyAiCam'
$environmentFile = Join-Path $dataRoot 'config\vms.env'
$settings = @{}
if (Test-Path -LiteralPath $environmentFile) {
    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) {
            $settings[$parts[0]] = $parts[1]
            [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
        }
    }
}

$env:ANYAICAM_STATIC_FOLDER = Join-Path $installRoot 'app\static'
$env:ANYAICAM_RECORDINGS_FOLDER = Join-Path $dataRoot 'recordings'
$env:ANYAICAM_HLS_FOLDER = Join-Path $dataRoot 'hls'
$env:ANYAICAM_PARTNER_DB = Join-Path $dataRoot 'database\partner_portal.db'
$env:ANYAICAM_MEDIAMTX_BINARY = Join-Path $installRoot 'runtime\tools\mediamtx\mediamtx.exe'
$env:ANYAICAM_MEDIAMTX_CONFIG = Join-Path $dataRoot 'mediamtx\anyaicam-mediamtx.yml'
$env:PYTHONUNBUFFERED = '1'
$env:PATH = (Join-Path $installRoot 'runtime\tools\ffmpeg') + ';' + $env:PATH
foreach ($folder in @($env:ANYAICAM_RECORDINGS_FOLDER, $env:ANYAICAM_HLS_FOLDER,
                      (Split-Path -Parent $env:ANYAICAM_PARTNER_DB), (Split-Path -Parent $env:ANYAICAM_MEDIAMTX_CONFIG))) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}

# Reachable from phones and computers on the local network, like the Linux
# appliance (its port 8000 is published on every interface). The installer's
# firewall rules (firewall.ps1) admit only the local subnet and Tailscale, on
# private/domain networks.
$bindHost = if ($settings['ANYAICAM_BIND_HOST']) { $settings['ANYAICAM_BIND_HOST'] } else { '0.0.0.0' }
$port = if ($settings['ANYAICAM_HTTP_PORT'] -match '^\d{2,5}$') { $settings['ANYAICAM_HTTP_PORT'] } else { '8000' }
Set-Location (Join-Path $installRoot 'app')
& (Join-Path $installRoot 'runtime\python\python.exe') -m uvicorn main:app --host $bindHost --port $port --proxy-headers
exit $LASTEXITCODE
