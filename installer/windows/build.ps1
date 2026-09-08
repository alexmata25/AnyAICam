$ErrorActionPreference = 'Stop'
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) { throw "Inno Setup compiler not found: $iscc" }
$vendor = Join-Path $PSScriptRoot 'vendor'
foreach ($file in @('python-3.12.10-amd64.exe', 'WinSW-x64.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $vendor $file))) { throw "Missing vendor file: $file" }
}
& $iscc (Join-Path $PSScriptRoot 'AnyAiCam-VMS.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
