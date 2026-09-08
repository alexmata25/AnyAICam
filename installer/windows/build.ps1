$ErrorActionPreference = 'Stop'
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) { throw "Inno Setup compiler not found: $iscc" }
$vendor = Join-Path $PSScriptRoot 'vendor'
foreach ($file in @('python-3.12.10-embed-amd64.zip', 'get-pip.py', 'WinSW-x64.exe', 'ffmpeg-8.1.2-essentials_build.zip')) {
    if (-not (Test-Path -LiteralPath (Join-Path $vendor $file))) { throw "Missing vendor file: $file" }
}
$wheelRoot = Join-Path $vendor 'wheels'
foreach ($line in Get-Content (Join-Path $PSScriptRoot 'wheels.lock.sha256')) {
    $parts = $line -split '  ', 2
    $wheel = Join-Path $wheelRoot $parts[1]
    if (-not (Test-Path $wheel) -or (Get-FileHash -Algorithm SHA256 $wheel).Hash.ToLowerInvariant() -ne $parts[0]) { throw "Wheel checksum failed: $($parts[1])" }
}
& $iscc (Join-Path $PSScriptRoot 'AnyAiCam-VMS.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
