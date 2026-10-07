param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [Parameter(Mandatory=$true)][string]$PythonArchive,
    [Parameter(Mandatory=$true)][string]$GetPipScript,
    [Parameter(Mandatory=$true)][string]$WheelRoot,
    [Parameter(Mandatory=$true)][string]$FFmpegArchive,
    [Parameter(Mandatory=$true)][string]$MediaMtxArchive,
    [Parameter(Mandatory=$true)][string]$AppVersion
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
& $python $GetPipScript --no-index --find-links $WheelRoot --disable-pip-version-check --no-warn-script-location
if ($LASTEXITCODE -ne 0) { throw 'Private pip bootstrap failed.' }
& $python -m pip install --no-index --find-links $WheelRoot --disable-pip-version-check --no-warn-script-location -r (Join-Path $InstallRoot 'installer\requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
# The VMS imports cv2/ultralytics/torch at startup; their DLLs need the Visual
# C++ runtime the setup installs first. Stop here, before any service exists,
# if anything fails to load (the setup log has the preflight's output).
& $python (Join-Path $InstallRoot 'installer\runtime-preflight.py')
if ($LASTEXITCODE -ne 0) { throw 'AnyAiCam runtime preflight failed: the bundled Python cannot load the VMS packages.' }

$ffmpegRoot = Join-Path $InstallRoot 'runtime\tools\ffmpeg'
if (-not (Test-Path (Join-Path $ffmpegRoot 'ffmpeg.exe'))) {
    $ffmpegTemp = Join-Path $env:TEMP ('anyaicam-ffmpeg-' + [guid]::NewGuid().ToString('N'))
    try {
        Expand-Archive -LiteralPath $FFmpegArchive -DestinationPath $ffmpegTemp -Force
        $ffmpeg = Get-ChildItem -LiteralPath $ffmpegTemp -Filter ffmpeg.exe -Recurse | Select-Object -First 1
        if (-not $ffmpeg) { throw 'FFmpeg archive does not contain ffmpeg.exe.' }
        New-Item -ItemType Directory -Force -Path $ffmpegRoot | Out-Null
        Copy-Item -Path (Join-Path $ffmpeg.Directory.FullName '*') -Destination $ffmpegRoot -Recurse -Force
    } finally { if (Test-Path $ffmpegTemp) { Remove-Item $ffmpegTemp -Recurse -Force } }
}

# MediaMTX (2026-10-07): the WebRTC live-view relay the VMS starts itself
# (app/webrtc_publisher.py; ANYAICAM_MEDIAMTX_BINARY in service-launcher.ps1).
# Replaced on every install so an upgrade always runs the pinned version.
$mediamtxRoot = Join-Path $InstallRoot 'runtime\tools\mediamtx'
$mediamtxTemp = Join-Path $env:TEMP ('anyaicam-mediamtx-' + [guid]::NewGuid().ToString('N'))
try {
    Expand-Archive -LiteralPath $MediaMtxArchive -DestinationPath $mediamtxTemp -Force
    if (-not (Test-Path -LiteralPath (Join-Path $mediamtxTemp 'mediamtx.exe'))) { throw 'MediaMTX archive does not contain mediamtx.exe.' }
    New-Item -ItemType Directory -Force -Path $mediamtxRoot | Out-Null
    Copy-Item -Path (Join-Path $mediamtxTemp 'mediamtx.exe'), (Join-Path $mediamtxTemp 'LICENSE') -Destination $mediamtxRoot -Force
} finally { if (Test-Path $mediamtxTemp) { Remove-Item $mediamtxTemp -Recurse -Force } }

foreach ($directory in @('config', 'database', 'recordings', 'hls', 'logs', 'mediamtx')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot $directory) | Out-Null
}
# The data folder holds the app secrets (config\vms.env), the account database
# and recordings: only the service (LocalSystem) and administrators may read it.
# ProgramData's default would let every local user read and create files here.
& icacls.exe $DataRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to $DataRoot (icacls exit $LASTEXITCODE)." }
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
# What /version reports (the Linux installer writes the same key).
$values['ANYAICAM_VERSION'] = $AppVersion
$content = @('# Managed by the AnyAiCam Windows installer. Persistent across repair and uninstall.')
$content += $values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
[IO.File]::WriteAllLines($environmentFile, $content, [Text.UTF8Encoding]::new($false))

# Local-network access to the VMS and live view (see firewall.ps1).
& (Join-Path $InstallRoot 'installer\firewall.ps1') -Apply -InstallRoot $InstallRoot
