param([switch]$Sign, [switch]$AzureSign)
$ErrorActionPreference = 'Stop'
if ($Sign -and $AzureSign) { throw 'Choose either certificate-store signing or Azure Artifact Signing.' }
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
$compilerArguments += $installerScript
& $iscc @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
if ($Sign -or $AzureSign) {
    $setup = Get-Item (Join-Path $PSScriptRoot 'output\AnyAiCam-VMS-Setup-0.1.3-ec5272f.exe')
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
