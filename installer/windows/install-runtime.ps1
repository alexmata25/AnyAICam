param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [Parameter(Mandatory=$true)][string]$PythonArchive,
    [Parameter(Mandatory=$true)][string]$GetPipScript
)
$ErrorActionPreference = 'Stop'
$pythonRoot = Join-Path $InstallRoot 'runtime\python'
$python = Join-Path $pythonRoot 'python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    New-Item -ItemType Directory -Force -Path $pythonRoot | Out-Null
    Expand-Archive -LiteralPath $PythonArchive -DestinationPath $pythonRoot -Force
}
if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python extraction failed: $python" }
$pathFile = Get-ChildItem -LiteralPath $pythonRoot -Filter 'python*._pth' | Select-Object -First 1
if (-not $pathFile) { throw 'Bundled Python path configuration was not found.' }
$pathContent = Get-Content -LiteralPath $pathFile.FullName
$pathContent = $pathContent | ForEach-Object { if ($_ -eq '#import site') { 'import site' } else { $_ } }
[IO.File]::WriteAllLines($pathFile.FullName, $pathContent, [Text.UTF8Encoding]::new($false))
& $python $GetPipScript --disable-pip-version-check --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw 'Private pip bootstrap failed.' }
& $python -m pip install --disable-pip-version-check --no-warn-script-location -r (Join-Path $InstallRoot 'installer\requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

foreach ($directory in @('config', 'database', 'recordings', 'hls', 'logs')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot $directory) | Out-Null
}
$environmentFile = Join-Path $DataRoot 'config\vms.env'
$values = [ordered]@{}
if (Test-Path -LiteralPath $environmentFile) {
    foreach ($line in Get-Content -LiteralPath $environmentFile) {
        if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $values[$parts[0]] = $parts[1] }
    }
}
if (-not $values.Contains('ANYAICAM_APP_SECRETS')) {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    $values['ANYAICAM_APP_SECRETS'] = ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}
if (-not $values.Contains('ANYAICAM_CAMERA_CREDENTIAL_KEY')) {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    $values['ANYAICAM_CAMERA_CREDENTIAL_KEY'] = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
}
$values['ANYAICAM_RUNTIME_ROLE'] = 'edge'
$values['ANYAICAM_ENV'] = 'production'
$values['ANYAICAM_FORCE_HTTPS'] = 'false'
$values['ANYAICAM_SECURE_COOKIES'] = 'false'
$values['ANYAICAM_PUBLIC_URL'] = 'http://127.0.0.1:8000'
$values['ANYAICAM_VMS_COMMIT'] = $SourceCommit
$values['ANYAICAM_BUILD_ID'] = $SourceCommit
$content = @('# Managed by the AnyAiCam Windows installer. Persistent across repair and uninstall.')
$content += $values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
[IO.File]::WriteAllLines($environmentFile, $content, [Text.UTF8Encoding]::new($false))
