param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$SourceCommit
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $InstallRoot 'runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Bundled Python installation failed: $python" }
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
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $values['ANYAICAM_APP_SECRETS'] = [Convert]::ToHexString($bytes).ToLowerInvariant()
}
if (-not $values.Contains('ANYAICAM_CAMERA_CREDENTIAL_KEY')) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
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
