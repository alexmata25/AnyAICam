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

# Validate before folders, stale-process cleanup or job limits are touched.
. (Join-Path $installRoot 'installer\gateway-edition.ps1')
Assert-GatewayRuntimeConfiguration $env:ANYAICAM_GATEWAY_EDITION $env:ANYAICAM_RUNTIME_ROLE
$env:ANYAICAM_STATIC_FOLDER = Join-Path $installRoot 'app\static'
$env:ANYAICAM_RECORDINGS_FOLDER = Join-Path $dataRoot 'recordings'
$env:ANYAICAM_HLS_FOLDER = Join-Path $dataRoot 'hls'
$env:ANYAICAM_PARTNER_DB = Join-Path $dataRoot 'database\partner_portal.db'
$env:ANYAICAM_MEDIAMTX_BINARY = Join-Path $installRoot 'runtime\tools\mediamtx\mediamtx.exe'
$env:ANYAICAM_MEDIAMTX_CONFIG = Join-Path $dataRoot 'mediamtx\anyaicam-mediamtx.yml'
# 1.3.2: the cloud agent's state folder (agent-launcher.ps1 sets the same one). The VMS's Hybrid workers (cloud
# backup, event media, analytics/facial sync, live relay, P2P, talk-down) read the agent's credential.json,
# live_relay_commands.json and lan_addresses.json from it; left unset they looked in C:\var\lib\anyaicam and every
# Hybrid feature stayed off. Set after vms.env, so it is never taken from a file.
$env:ANYAICAM_STATE_DIR = Join-Path $dataRoot 'agent'
$env:PYTHONUNBUFFERED = '1'
$env:PATH = (Join-Path $installRoot 'runtime\tools\ffmpeg') + ';' + $env:PATH
# 1.3.4: the bundled Tesseract OCR engine for licence plate reading (install-runtime.ps1), and its model folder.
$env:PATH = (Join-Path $installRoot 'runtime\tools\tesseract') + ';' + $env:PATH
$env:TESSDATA_PREFIX = Join-Path $installRoot 'runtime\tools\tesseract\tessdata'
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
# 1.3.3 D4: a VMS left over from an earlier service run (an orphan that survived a stop) holds port 8000 with the old
# product mode, so every new VMS would fail to bind and the service would restart in a loop. Stop it first.
try { & (Join-Path $PSScriptRoot 'stop-stale-vms.ps1') -InstallRoot $installRoot } catch { Write-Output "AnyAiCam VMS launcher: stale-process check failed: $($_.Exception.Message)" }
# 1.3.4: one Job Object around the whole VMS (vms-job-limits.ps1): a CPU cap, an optional memory limit. Python and
# its FFmpeg/MediaMTX/Tesseract children inherit it. Fail-open: without it the VMS still starts and records.
try { . (Join-Path $PSScriptRoot 'vms-job-limits.ps1'); Write-Output (Set-AnyAiCamVmsJobLimits $settings) } catch { Write-Output "AnyAiCam VMS launcher: resource limits unavailable: $($_.Exception.Message)" }
Set-Location (Join-Path $installRoot 'app')
& (Join-Path $installRoot 'runtime\python\python.exe') -m uvicorn main:app --host $bindHost --port $port --proxy-headers
exit $LASTEXITCODE
