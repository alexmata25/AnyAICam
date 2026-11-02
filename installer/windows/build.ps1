param(
    [Parameter(Mandatory=$true)][string]$Version,
    # 2026-10-20 (owner): the customer-facing AnyAiCam VMS release (1.2.7, as on Linux). -Version stays the Windows
    # setup's own, always-rising version (1.3.2) that the downgrade guard orders by.
    [Parameter(Mandatory=$true)][string]$ReleaseVersion,
    # Requirement 9 (1.2.7): the cloud's entitlement-signing PUBLIC keyset and its SHA-256, pinned out of band
    # (docs/entitlement-signing-keys.md). Without it a Windows install trusts no signed capacity: 0 cameras.
    [Parameter(Mandatory=$true)][string]$EntitlementKeyset,
    [Parameter(Mandatory=$true)][string]$EntitlementKeysetSha256,
    # Optional explicit build interpreter. This avoids relying on a machine-wide
    # Python Launcher registration when the approved interpreter is per-user.
    [string]$PythonExe = '',
    [string]$GitExe = '',
    [switch]$Sign,
    [switch]$AzureSign
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build-tools.ps1')
$GitExe = Resolve-BuildGit $GitExe
. (Join-Path $PSScriptRoot 'publisher-pin.ps1')
if ($Sign -and $AzureSign) { throw 'Choose either certificate-store signing or Azure Artifact Signing.' }
# Three or four decimal parts (1.3.2, or a hotfix 1.3.2.1): exactly what install-version-guard.ps1 accepts.
if ($Version -notmatch '^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2,3}$') { throw "Version must look like 1.3.2 or 1.3.2.1 (got '$Version')." }
if ($ReleaseVersion -notmatch '^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2}$') { throw "ReleaseVersion must look like 1.2.7 (got '$ReleaseVersion')." }
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'
Assert-BuildInnoCompiler $iscc
# The setup is built from a clean, committed tree: its name and the
# ANYAICAM_BUILD_ID it installs both carry this exact commit.
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$sourceCommit = (& $GitExe -C $repo rev-parse HEAD).Trim()
if ($sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'Could not read the source commit.' }
# Every packaged repository folder (read from the .iss, so appliance-agent
# is included) must be exactly the commit: see source-integrity.ps1.
. (Join-Path $PSScriptRoot 'source-integrity.ps1')
$sourceProblems = @(Get-PackagedSourceProblems $repo (Join-Path $PSScriptRoot 'AnyAiCam-VMS.iss') -GitExe $GitExe)
if ($sourceProblems.Count) { throw ("The packaged sources are not exactly commit $sourceCommit; build only from a committed tree:`n  " + ($sourceProblems -join "`n  ")) }
# The same validation as the Linux release build (installer/build_release_installer.read_entitlement_signing_keys):
# public keys only, well-formed Ed25519, canonical bytes hashing to the pinned SHA-256.
if ($EntitlementKeysetSha256 -notmatch '^[0-9a-f]{64}$') { throw 'EntitlementKeysetSha256 must be a 64-character lowercase SHA-256.' }
$keysetOut = Join-Path $PSScriptRoot 'output\entitlement-signing-public-keys.json'
if ($PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "PythonExe does not exist: $PythonExe" }
    $pythonVersion = (& $PythonExe --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne 'Python 3.12.10') { throw "PythonExe must be the verified Python 3.12.10 interpreter (reported '$pythonVersion')." }
    & $PythonExe (Join-Path $PSScriptRoot 'prepare-keyset.py') $EntitlementKeyset $EntitlementKeysetSha256 $keysetOut
} else {
    & py -3.11 (Join-Path $PSScriptRoot 'prepare-keyset.py') $EntitlementKeyset $EntitlementKeysetSha256 $keysetOut
}
if ($LASTEXITCODE -ne 0) { throw 'The entitlement keyset was refused; nothing was built.' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $keysetOut).Hash.ToLowerInvariant() -ne $EntitlementKeysetSha256) { throw 'The prepared entitlement keyset does not match its pinned SHA-256.' }
# Every vendored input is pinned by SHA-256 (DEPENDENCIES.md), not just present.
$vendor = Join-Path $PSScriptRoot 'vendor'
$pinned = [ordered]@{
    'python-3.12.10-embed-amd64.zip'        = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
    'get-pip.py'                            = 'fb24e693bab954209a063d90953621412ccad4a500905a726286e038f508ddf6'
    'WinSW-x64.exe'                         = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
    'ffmpeg-8.1.2-essentials_build.zip'     = 'db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec'
    'mediamtx_v1.21.0_windows_amd64.zip'    = '8a58a9b8c25ee99a96c23dc0a17f39ace3072c01d2e148329073c64ddf83493d'
    'vc_redist.x64-14.44.35211.exe'         = 'cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b'
    # 1.3.2 Voice Call listening: the Linux Dockerfiles' pin (alphacephei.com/vosk/models, Apache-2.0).
    'vosk-model-small-en-us-0.15.zip'       = '30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498'
    # 1.3.4 LPR parity: Tesseract 5.5.2 + its DLL closure + tessdata_fast eng, assembled deterministically from pinned
    # conda-forge packages by prepare-tesseract-bundle.py (tesseract-bundle.lock.json; licences inside the zip).
    'tesseract-5.5.2-win64-anyaicam.zip'    = '221b4599a9310737d8f3a2d8a93477eb3c495d41b5eca8af6ed7b79e0781fbf7'
}
foreach ($file in $pinned.Keys) {
    $path = Join-Path $vendor $file
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing vendor file: $file" }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $pinned[$file]) { throw "Vendor checksum failed: $file" }
}
# The Visual C++ runtime installer runs elevated on customer machines: besides
# its pin it must carry Microsoft's valid Authenticode signature.
$vcRedist = Get-AuthenticodeSignature -LiteralPath (Join-Path $vendor 'vc_redist.x64-14.44.35211.exe')
if ($vcRedist.Status -ne 'Valid' -or $vcRedist.SignerCertificate.Subject -notmatch '^CN=Microsoft Corporation,') { throw 'vc_redist.x64-14.44.35211.exe is not validly signed by Microsoft Corporation.' }
$wheelRoot = Join-Path $vendor 'wheels'
$lockedWheels = @()
foreach ($line in Get-Content (Join-Path $PSScriptRoot 'wheels.lock.sha256')) {
    $parts = $line -split '  ', 2
    $wheel = Join-Path $wheelRoot $parts[1]
    if (-not (Test-Path $wheel) -or (Get-FileHash -Algorithm SHA256 $wheel).Hash.ToLowerInvariant() -ne $parts[0]) { throw "Wheel checksum failed: $($parts[1])" }
    $lockedWheels += $parts[1]
}
$vendorProblems = @(Get-VendorProblems $vendor @($pinned.Keys) $lockedWheels)
if ($vendorProblems.Count) { throw ("vendor/ holds files that are not pinned (they would be packaged):`n  " + ($vendorProblems -join "`n  ")) }
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
# Runtime catalog (2026-10-08, runtime-catalog.ps1): Smart App Control blocks
# the runtime's unsigned native files (wheels, WinSW, FFmpeg, MediaMTX, Tesseract). One
# catalog of their hashes, signed with the same certificate, is registered by
# the setup. Built from exactly the pinned inputs verified above.
. (Join-Path $PSScriptRoot 'runtime-catalog.ps1')
$outputDir = Join-Path $PSScriptRoot 'output'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$catalog = Join-Path $outputDir 'AnyAiCamRuntime.cat'
$nativeArchives = @($lockedWheels | ForEach-Object { Join-Path $wheelRoot $_ }) + @('python-3.12.10-embed-amd64.zip', 'ffmpeg-8.1.2-essentials_build.zip', 'mediamtx_v1.21.0_windows_amd64.zip', 'tesseract-5.5.2-win64-anyaicam.zip' | ForEach-Object { Join-Path $vendor $_ })
$catalogStage = Join-Path $outputDir 'runtime-catalog'
[void](Export-NativeFiles $catalogStage $nativeArchives @(Join-Path $vendor 'WinSW-x64.exe'))
$makeCat = if ($env:ANYAICAM_MAKECAT_PATH) { $env:ANYAICAM_MAKECAT_PATH } else { 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makecat.exe' }
$catalogued = New-RuntimeCatalog $catalogStage $catalog $makeCat
Write-Output "Runtime catalog: $catalogued native files."
if ($Sign) { & $signTool sign /sha1 $thumbprint /fd SHA256 /tr $timestampUrl /td SHA256 $catalog }
if ($AzureSign) { & $signTool sign /fd SHA256 /tr $timestampUrl /td SHA256 /dlib $dlib /dmdf $metadata $catalog }
if ($Sign -or $AzureSign) {
    if ($LASTEXITCODE -ne 0) { throw "Signing the runtime catalog failed with exit code $LASTEXITCODE" }
    if ((Get-AuthenticodeSignature -LiteralPath $catalog).Status -ne 'Valid') { throw 'The runtime catalog signature is not valid.' }
    if ($AzureSign) {
        $catalogSignature = Get-AuthenticodeSignature -LiteralPath $catalog
        $publisherProblem = Get-AnyAiCamPublisherProblem $catalogSignature $catalog
        if ($publisherProblem) { throw "Publisher pin failed for the runtime catalog: $publisherProblem" }
        # Offline installs (2026-10-20): the catalog must CARRY its issuing CA, not rely on this PC's certificate cache.
        $issuer = $catalogSignature.SignerCertificate.Issuer
        if (-not (@(Get-AnyAiCamEmbeddedCertificates $catalog) | Where-Object { $_.Subject -eq $issuer })) { throw "The runtime catalog does not embed its issuing CA ($issuer); it would be refused offline." }
    }
    & $signTool verify /pa $catalog | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'SignTool verification of the runtime catalog failed.' }
    # Verify every staged native file against the catalog, not only one sample.
    $catalogMembers = @(Get-ChildItem -LiteralPath $catalogStage -Recurse -File)
    if (-not $catalogMembers.Count) { throw 'The runtime catalog has no staged members.' }
    foreach ($member in $catalogMembers) {
        & $signTool verify /pa /c $catalog $member.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "A catalogued file does not verify against the runtime catalog: $($member.FullName)" }
    }
    Write-Output "Runtime catalog signed and verified for all $($catalogMembers.Count) native files."
}
$compilerArguments += "/DKeysetSha256=$EntitlementKeysetSha256"
$compilerArguments += "/DAppVersion=$Version"
$compilerArguments += "/DReleaseVersion=$ReleaseVersion"
$compilerArguments += "/DSourceCommit=$sourceCommit"
$compilerArguments += $installerScript
& $iscc @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
. (Join-Path $PSScriptRoot 'installer-size-policy.ps1')
$setupPath = Join-Path $PSScriptRoot ('output\AnyAiCam-VMS-Setup-' + $Version + '-' + $sourceCommit.Substring(0, 7) + '.exe')
$sizeProblem = Get-AnyAiCamSetupSizeProblem $setupPath
if ($sizeProblem) { throw "Installer size policy: $sizeProblem" }
Write-Output "Installer size gate passed: $((Get-Item -LiteralPath $setupPath).Length) / $AnyAiCamSetupMaxBytes bytes (750 MB)."
if ($Sign -or $AzureSign) {
    $setup = Get-Item -LiteralPath $setupPath
    # (Inno Setup 7 keeps no signed-uninstaller cache; the installed
    # unins000.exe is verified by the Windows Sandbox validation.)
    $signedUninstallers = @(Get-ChildItem (Join-Path $PSScriptRoot 'output\signed-uninstallers') -Filter '*.exe' -ErrorAction Stop)
    foreach ($file in @($setup) + $signedUninstallers) {
        $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
        if ($signature.Status -ne 'Valid') { throw "Authenticode verification failed: $($file.FullName)" }
        if ($Sign -and $signature.SignerCertificate.Thumbprint -ne $thumbprint) { throw "Signer certificate mismatch: $($file.FullName)" }
        if ($AzureSign) {
            $publisherProblem = Get-AnyAiCamPublisherProblem $signature $file.FullName
            if ($publisherProblem) { throw "Publisher pin failed for $($file.FullName): $publisherProblem" }
            $issuer = $signature.SignerCertificate.Issuer
            if (-not (@(Get-AnyAiCamEmbeddedCertificates $file.FullName) | Where-Object { $_.Subject -eq $issuer })) { throw "$($file.Name) does not embed its issuing CA ($issuer); it would be refused offline." }
        }
        & $signTool verify /pa /all $file.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "SignTool verification failed: $($file.FullName)" }
    }
    Write-Output "Verified Authenticode signatures on setup and $($signedUninstallers.Count) cached uninstaller(s)."
}
