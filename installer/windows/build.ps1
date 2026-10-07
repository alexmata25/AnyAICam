param(
    [Parameter(Mandatory=$true)][string]$Version,
    [switch]$Sign,
    [switch]$AzureSign
)
$ErrorActionPreference = 'Stop'
if ($Sign -and $AzureSign) { throw 'Choose either certificate-store signing or Azure Artifact Signing.' }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like 1.2.4 (got '$Version')." }
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) { throw "Inno Setup compiler not found: $iscc" }
# The setup is built from a clean, committed tree: its name and the
# ANYAICAM_BUILD_ID it installs both carry this exact commit.
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$sourceCommit = (& git -C $repo rev-parse HEAD).Trim()
if ($sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'Could not read the source commit.' }
if (& git -C $repo status --porcelain -- app installer/windows) { throw 'app/ or installer/windows/ has uncommitted changes; build only from a committed tree.' }
# Every vendored input is pinned by SHA-256 (DEPENDENCIES.md), not just present.
$vendor = Join-Path $PSScriptRoot 'vendor'
$pinned = [ordered]@{
    'python-3.12.10-embed-amd64.zip'        = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
    'get-pip.py'                            = 'fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6'
    'WinSW-x64.exe'                         = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
    'ffmpeg-8.1.2-essentials_build.zip'     = 'db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec'
    'mediamtx_v1.21.0_windows_amd64.zip'    = '8a58a9b8c25ee99a96c23dc0a17f39ace3072c01d2e148329073c64ddf83493d'
}
foreach ($file in $pinned.Keys) {
    $path = Join-Path $vendor $file
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing vendor file: $file" }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $pinned[$file]) { throw "Vendor checksum failed: $file" }
}
$wheelRoot = Join-Path $vendor 'wheels'
foreach ($line in Get-Content (Join-Path $PSScriptRoot 'wheels.lock.sha256')) {
    $parts = $line -split '  ', 2
    $wheel = Join-Path $wheelRoot $parts[1]
    if (-not (Test-Path $wheel) -or (Get-FileHash -Algorithm SHA256 $wheel).Hash.ToLowerInvariant() -ne $parts[0]) { throw "Wheel checksum failed: $($parts[1])" }
}
$installerScript = Join-Path $PSScriptRoot 'AnyAiCam-VMS.iss'
$compilerArguments = @()
if ($Sign) {
    $signTool = $env:ANYAICAM_SIGNTOOL_PATH
    $thumbprint = ($env:ANYAICAM_SIGN_CERT_SHA1 -replace '\s','').ToUpperInvariant()
    $timestampUrl = $env:ANYAICAM_TIMESTAMP_URL
    if (-not (Test-Path -LiteralPath $signTool)) { throw 'ANYAICAM_SIGNTOOL_PATH must identify signtool.exe.' }
    if ($thumbprint -notmatch '^[0-9A-F]{40}$') { throw 'ANYAICAM_SIGN_CERT_SHA1 must be a 40-character certificate thumbprint.' }
    if ($timestampUrl -notmatch '^https?://') { throw 'ANYAICAM_TIMESTAMP_URL must be an HTTP(S) RFC 3161 endpoint.' }
    $certificate = Get-ChildItem Cert:\CurrentUser\My,Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
        Where-Object { $_.Thumbprint -eq $thumbprint -and $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) -and ($_.EnhancedKeyUsageList.ObjectId.Value -contains '1.3.6.1.5.5.7.3.3') } |
        Select-Object -First 1
    if (-not $certificate) { throw 'The requested valid code-signing certificate with private key was not found.' }
    $signCommand = '"' + $signTool + '" sign /sha1 ' + $thumbprint + ' /fd SHA256 /tr "' + $timestampUrl + '" /td SHA256 $f'
    $compilerArguments += '/DEnableSigning=1'
    $compilerArguments += '/SAnyAiCamSign=' + $signCommand
}
if ($AzureSign) {
    $signTool = if ($env:ANYAICAM_SIGNTOOL_PATH) { $env:ANYAICAM_SIGNTOOL_PATH } else { 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe' }
    $dlib = if ($env:ANYAICAM_AZURE_SIGNING_DLIB) { $env:ANYAICAM_AZURE_SIGNING_DLIB } else { Join-Path $env:LOCALAPPDATA 'Microsoft\MicrosoftArtifactSigningClientTools\Azure.CodeSigning.Dlib.dll' }
    $metadata = if ($env:ANYAICAM_AZURE_SIGNING_METADATA) { $env:ANYAICAM_AZURE_SIGNING_METADATA } else { 'C:\AnyAiCamSigning\metadata.json' }
    $timestampUrl = if ($env:ANYAICAM_TIMESTAMP_URL) { $env:ANYAICAM_TIMESTAMP_URL } else { 'http://timestamp.acs.microsoft.com' }
    foreach ($path in @($signTool, $dlib, $metadata)) { if (-not (Test-Path -LiteralPath $path)) { throw "Azure signing dependency not found: $path" } }
    $signCommand = '$q' + $signTool + '$q sign /fd SHA256 /tr $q' + $timestampUrl + '$q /td SHA256 /dlib $q' + $dlib + '$q /dmdf $q' + $metadata + '$q $f'
    $compilerArguments += '/DEnableSigning=1'
    $compilerArguments += '/SAnyAiCamSign=' + $signCommand
}
$compilerArguments += "/DAppVersion=$Version"
$compilerArguments += "/DSourceCommit=$sourceCommit"
$compilerArguments += $installerScript
& $iscc @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
if ($Sign -or $AzureSign) {
    $setup = Get-Item (Join-Path $PSScriptRoot ('output\AnyAiCam-VMS-Setup-' + $Version + '-' + $sourceCommit.Substring(0, 7) + '.exe'))
    $signedUninstallers = @(Get-ChildItem (Join-Path $PSScriptRoot 'output\signed-uninstallers') -Filter '*.exe' -ErrorAction Stop)
    foreach ($file in @($setup) + $signedUninstallers) {
        $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
        if ($signature.Status -ne 'Valid') { throw "Authenticode verification failed: $($file.FullName)" }
        if ($Sign -and $signature.SignerCertificate.Thumbprint -ne $thumbprint) { throw "Signer certificate mismatch: $($file.FullName)" }
        & $signTool verify /pa /all $file.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "SignTool verification failed: $($file.FullName)" }
    }
    Write-Output "Verified Authenticode signatures on setup and $($signedUninstallers.Count) cached uninstaller(s)."
}
