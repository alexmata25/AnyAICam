$ErrorActionPreference = 'Stop'
$installRoot = Split-Path -Parent $PSScriptRoot
$dataRoot = Join-Path $env:ProgramData 'AnyAiCam'
$environmentFile = Join-Path $dataRoot 'config\vms.env'

if (Test-Path -LiteralPath $environmentFile) {
    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process') }
    }
}

$env:ANYAICAM_STATIC_FOLDER = Join-Path $installRoot 'app\static'
$env:ANYAICAM_RECORDINGS_FOLDER = Join-Path $dataRoot 'recordings'
$env:ANYAICAM_HLS_FOLDER = Join-Path $dataRoot 'hls'
$env:ANYAICAM_PARTNER_DB = Join-Path $dataRoot 'database\partner_portal.db'
$env:PYTHONUNBUFFERED = '1'
Set-Location (Join-Path $installRoot 'app')
& (Join-Path $installRoot 'runtime\python\python.exe') -m uvicorn main:app --host 127.0.0.1 --port 8000 --proxy-headers
exit $LASTEXITCODE
