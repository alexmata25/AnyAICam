param(
    [Parameter(Mandatory=$true)][string]$Version,
    # 2026-10-20 (owner): the customer-facing AnyAiCam VMS release (1.2.7, as on Linux). -Version stays the Windows
    # setup's own, always-rising version (1.3.2) that the downgrade guard orders by.
    [Parameter(Mandatory=$true)][string]$ReleaseVersion,
    # Requirement 9 (1.2.7): the cloud's entitlement-signing PUBLIC keyset and its SHA-256, pinned out of band
    # (docs/entitlement-signing-keys.md). Without it a Windows install trusts no signed capacity: 0 cameras.
    [Parameter(Mandatory=$true)][string]$EntitlementKeyset,
    [Parameter(Mandatory=$true)][string]$EntitlementKeysetSha256,
    # LIC-W1: the same entitlement trust registry and pinned SHA-256 as the Linux release (docs/entitlement-signing-keys.md).
    # There is no registry-less build: every pinned key must be in it, active and with the same key material.
    [Parameter(Mandatory=$true)][string]$EntitlementTrustRegistry,
    [Parameter(Mandatory=$true)][string]$EntitlementTrustRegistrySha256,
    [string[]]$EntitlementRetiredKeyIds = @(),
    # Optional explicit build interpreter. This avoids relying on a machine-wide
    # Python Launcher registration when the approved interpreter is per-user.
    [string]$PythonExe = '',
    [switch]$Sign,
    [switch]$AzureSign,
    # Explicit, isolated Azure-signed internal test mode. Never enabled by default.
    [switch]$InternalTestAzureSign
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'publisher-pin.ps1')
if (($Sign -and $AzureSign) -or ($Sign -and $InternalTestAzureSign) -or ($AzureSign -and $InternalTestAzureSign)) {
    throw 'Choose exactly one signing mode.'
}
$azureSigning = $AzureSign -or $InternalTestAzureSign
$outputName = if ($InternalTestAzureSign) { 'output-internal-test' } else { 'output' }
$outputDir = Join-Path $PSScriptRoot $outputName
# Internal evidence is never reused or overwritten. This check is before any generated output is written.
if ($InternalTestAzureSign -and (Test-Path -LiteralPath $outputDir)) { throw "Internal-test output already exists; choose a new isolated source clone: $outputDir" }
# Three or four decimal parts (1.3.2, or a hotfix 1.3.2.1): exactly what install-version-guard.ps1 accepts.
if ($Version -notmatch '^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2,3}$') { throw "Version must look like 1.3.2 or 1.3.2.1 (got '$Version')." }
if ($ReleaseVersion -notmatch '^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2}$') { throw "ReleaseVersion must look like 1.2.7 (got '$ReleaseVersion')." }
$iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7\ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) { throw "Inno Setup compiler not found: $iscc" }
# The setup is built from a clean, committed tree: its name and the
# ANYAICAM_BUILD_ID it installs both carry this exact commit.
$repo = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$sourceCommit = (& git -C $repo rev-parse HEAD).Trim()
if ($sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'Could not read the source commit.' }
# Every packaged repository folder (read from the .iss, so appliance-agent
# is included) must be exactly the commit: see source-integrity.ps1.
. (Join-Path $PSScriptRoot 'source-integrity.ps1')
$sourceProblems = @(Get-PackagedSourceProblems $repo (Join-Path $PSScriptRoot 'AnyAiCam-VMS.iss'))
if ($sourceProblems.Count) { throw ("The packaged sources are not exactly commit $sourceCommit; build only from a committed tree:`n  " + ($sourceProblems -join "`n  ")) }
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
if ($azureSigning) {
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
if ($InternalTestAzureSign) {
    # The directory was verified absent at entry. Creating it here gives the catalog stage a private destination;
    # prepare-keyset.py later writes the two pinned inputs into this same newly-created directory.
    New-Item -ItemType Directory -Path $outputDir -ErrorAction Stop | Out-Null
    $outputItem = Get-Item -LiteralPath $outputDir -Force
    if (($outputItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Internal-test output cannot be a reparse point.' }
} else { New-Item -ItemType Directory -Force -Path $outputDir | Out-Null }
$internalTranscript = $null
if ($InternalTestAzureSign) {
    $internalTranscript = Join-Path $outputDir 'build-transcript.log'
    Start-Transcript -LiteralPath $internalTranscript -Force | Out-Null
}
try {
$catalog = Join-Path $outputDir 'AnyAiCamRuntime.cat'
$nativeArchives = @($lockedWheels | ForEach-Object { Join-Path $wheelRoot $_ }) + @('python-3.12.10-embed-amd64.zip', 'ffmpeg-8.1.2-essentials_build.zip', 'mediamtx_v1.21.0_windows_amd64.zip', 'tesseract-5.5.2-win64-anyaicam.zip' | ForEach-Object { Join-Path $vendor $_ })
$catalogStage = Join-Path $outputDir 'runtime-catalog'
if (Test-Path -LiteralPath $catalogStage) { throw 'Final runtime catalog stage already exists; refusing to overwrite it.' }
$catalogTempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
if (-not (Test-Path -LiteralPath $catalogTempRoot -PathType Container)) { throw 'A usable temporary directory is required for the short-path runtime catalog stage.' }
if ([IO.Path]::GetPathRoot($catalogTempRoot) -ine [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($outputDir))) { throw 'Runtime catalog temp staging must be on the same volume as the isolated output.' }
$catalogWorkStage = Join-Path $catalogTempRoot ('AnyAiCamCatalog-' + [Guid]::NewGuid().ToString('N').Substring(0, 10))
if (Test-Path -LiteralPath $catalogWorkStage) { throw 'Runtime catalog temp-stage collision; preserve and stop without retry.' }
$makeCat = if ($env:ANYAICAM_MAKECAT_PATH) { $env:ANYAICAM_MAKECAT_PATH } else { 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\makecat.exe' }
try {
    [void](Export-NativeFiles $catalogWorkStage $nativeArchives @(Join-Path $vendor 'WinSW-x64.exe'))
    $catalogued = New-RuntimeCatalog $catalogWorkStage $catalog $makeCat
    Move-Item -LiteralPath $catalogWorkStage -Destination $catalogStage -ErrorAction Stop
} catch {
    if (Test-Path -LiteralPath $catalogWorkStage -PathType Container) {
        Write-Output ("Runtime catalog work stage preserved for diagnosis: " + $catalogWorkStage)
    }
    throw
}
Write-Output "Runtime catalog: $catalogued native files."
if ($Sign) { & $signTool sign /sha1 $thumbprint /fd SHA256 /tr $timestampUrl /td SHA256 $catalog }
if ($azureSigning) { & $signTool sign /fd SHA256 /tr $timestampUrl /td SHA256 /dlib $dlib /dmdf $metadata $catalog }
if ($Sign -or $AzureSign -or $InternalTestAzureSign) {
    if ($LASTEXITCODE -ne 0) { throw "Signing the runtime catalog failed with exit code $LASTEXITCODE" }
    if ((Get-AuthenticodeSignature -LiteralPath $catalog).Status -ne 'Valid') { throw 'The runtime catalog signature is not valid.' }
    if ($azureSigning) {
        $catalogSignature = Get-AuthenticodeSignature -LiteralPath $catalog
        $publisherProblem = Get-AnyAiCamPublisherProblem $catalogSignature $catalog
        if ($publisherProblem) { throw "Publisher pin failed for the runtime catalog: $publisherProblem" }
        # Offline installs (2026-10-20): the catalog must CARRY its issuing CA, not rely on this PC's certificate cache.
        $issuer = $catalogSignature.SignerCertificate.Issuer
        if (-not (@(Get-AnyAiCamEmbeddedCertificates $catalog) | Where-Object { $_.Subject -eq $issuer })) { throw "The runtime catalog does not embed its issuing CA ($issuer); it would be refused offline." }
    }
    & $signTool verify /pa $catalog | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'SignTool verification of the runtime catalog failed.' }
    # Every catalogued file must verify against it.
    $verifiedNativeFiles = Test-RuntimeCatalogFiles $catalogStage $catalog $signTool
    if ($verifiedNativeFiles -ne $catalogued) { throw "Runtime catalog coverage mismatch: catalogued=$catalogued verified=$verifiedNativeFiles" }
    Write-Output 'Runtime catalog signed and verified.'
}
# The same validation as the Linux release build (installer/build_release_installer.read_entitlement_signing_keys):
# public keys only, well-formed Ed25519, canonical bytes hashing to the pinned SHA-256.
if ($EntitlementKeysetSha256 -notmatch '^[0-9a-f]{64}$') { throw 'EntitlementKeysetSha256 must be a 64-character lowercase SHA-256.' }
if ($EntitlementTrustRegistrySha256 -notmatch '^[0-9a-f]{64}$') { throw 'EntitlementTrustRegistrySha256 must be a 64-character lowercase SHA-256.' }
$keysetOut = Join-Path $outputDir 'entitlement-signing-public-keys.json'
$registryOut = Join-Path $outputDir 'entitlement-trust-registry.json'
# Existing signed modes require the production registry. The separate internal mode is bound to synthetic registry inputs.
$registryEnvironment = if ($InternalTestAzureSign) { 'internal-test' } elseif ($Sign -or $AzureSign) { 'production' } else { 'any' }
# A rotation build names the keys kept for verification only. Passed only when there are some: PowerShell 5.1 drops an
# empty argument. prepare-keyset.py stops the build on a retired key not named here.
$retiredKeyArgument = @()
if ($EntitlementRetiredKeyIds.Count) { $retiredKeyArgument = @($EntitlementRetiredKeyIds -join ',') }
if ($PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { throw "PythonExe does not exist: $PythonExe" }
    $pythonVersion = (& $PythonExe --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne 'Python 3.12.10') { throw "PythonExe must be the verified Python 3.12.10 interpreter (reported '$pythonVersion')." }
    & $PythonExe (Join-Path $PSScriptRoot 'prepare-keyset.py') $EntitlementKeyset $EntitlementKeysetSha256 $keysetOut $EntitlementTrustRegistry $EntitlementTrustRegistrySha256 $registryOut $registryEnvironment @retiredKeyArgument
} else {
    & py -3.11 (Join-Path $PSScriptRoot 'prepare-keyset.py') $EntitlementKeyset $EntitlementKeysetSha256 $keysetOut $EntitlementTrustRegistry $EntitlementTrustRegistrySha256 $registryOut $registryEnvironment @retiredKeyArgument
}
if ($LASTEXITCODE -ne 0) { throw 'The entitlement keyset or trust registry was refused; nothing was built.' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $keysetOut).Hash.ToLowerInvariant() -ne $EntitlementKeysetSha256) { throw 'The prepared entitlement keyset does not match its pinned SHA-256.' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $registryOut).Hash.ToLowerInvariant() -ne $EntitlementTrustRegistrySha256) { throw 'The prepared entitlement trust registry does not match its pinned SHA-256.' }
$compilerArguments += "/DKeysetSha256=$EntitlementKeysetSha256"
$compilerArguments += "/DRegistrySha256=$EntitlementTrustRegistrySha256"
$compilerArguments += "/DAppVersion=$Version"
$compilerArguments += "/DReleaseVersion=$ReleaseVersion"
$compilerArguments += "/DSourceCommit=$sourceCommit"
$compilerArguments += "/DBuildOutputDir=$outputName"
if ($InternalTestAzureSign) { $compilerArguments += '/DInternalTestOnly=1' }
$compilerArguments += $installerScript
& $iscc @compilerArguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
. (Join-Path $PSScriptRoot 'installer-size.ps1')
$setupSuffix = if ($InternalTestAzureSign) { '-INTERNAL-TEST-DO-NOT-SHIP' } else { '' }
$setupPath = Join-Path $outputDir ('AnyAiCam-VMS-Setup-' + $Version + '-' + $sourceCommit.Substring(0, 7) + $setupSuffix + '.exe')
$setupBytes = Assert-InstallerSize $setupPath
Write-Output "Installer size accepted: $setupBytes bytes (limit 750000000)."
if ($Sign -or $AzureSign -or $InternalTestAzureSign) {
    $setup = Get-Item -LiteralPath $setupPath
    # Inno Setup stores the signed uninstaller under SignedUninstallerDir; the installed unins000.exe is also
    # checked during the separately authorized Sandbox acceptance.
    $signedUninstallers = @(Get-ChildItem (Join-Path $outputDir 'signed-uninstallers') -Filter '*.exe' -ErrorAction Stop)
    if ($InternalTestAzureSign -and $signedUninstallers.Count -lt 1) {
        throw 'Internal-test signing produced no cached signed uninstaller to verify.'
    }
    foreach ($file in @($setup) + $signedUninstallers) {
        $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
        if ($signature.Status -ne 'Valid') { throw "Authenticode verification failed: $($file.FullName)" }
        if ($Sign -and $signature.SignerCertificate.Thumbprint -ne $thumbprint) { throw "Signer certificate mismatch: $($file.FullName)" }
        if ($azureSigning) {
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
if ($InternalTestAzureSign) {
    # Close the transcript before hashing it. Any failure before here retains the partial transcript in finally.
    Stop-Transcript | Out-Null
    $internalTranscript = $null
    $branchName = (& git -C $repo branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $branchName) { throw 'Could not record the source branch in the internal evidence manifest.' }
    $records = @(Get-ChildItem -LiteralPath $outputDir -Recurse -File | Sort-Object FullName | ForEach-Object {
        $signatureStatus = 'NotApplicable'
        $relativePath = $_.FullName.Substring($outputDir.Length + 1)
        if ($relativePath -like 'runtime-catalog\*') { $signatureStatus = 'CatalogCoverageVerified' }
        elseif ($_.Extension -in @('.exe', '.cat')) { $signatureStatus = [string](Get-AuthenticodeSignature -LiteralPath $_.FullName).Status }
        [ordered]@{ path = $relativePath; size = $_.Length;
                    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant();
                    authenticode = $signatureStatus }
    })
    $evidence = [ordered]@{
        schema_version = 1
        result = 'INTERNAL_TEST_BUILD_VERIFIED'
        signing_mode = 'Azure Artifact Signing; internal-test-only'
        source_branch = $branchName
        source_commit = $sourceCommit
        windows_setup_version = $Version
        vms_release_version = $ReleaseVersion
        trust_registry_mode = 'synthetic'
        runtime_catalog_native_files = $catalogued
        publisher_subject = $AnyAiCamPublisherSubject
        artifacts = $records
    }
    $manifestPath = Join-Path $outputDir 'build-evidence.json'
    [IO.File]::WriteAllText($manifestPath, ($evidence | ConvertTo-Json -Depth 8) + "`n", (New-Object Text.UTF8Encoding $false))
    $sumLines = @(Get-ChildItem -LiteralPath $outputDir -Recurse -File | Where-Object { $_.Name -ne 'SHA256SUMS' } |
        Sort-Object FullName | ForEach-Object {
            $relative = $_.FullName.Substring($outputDir.Length + 1) -replace '\\', '/'
            (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() + '  ' + $relative
        })
    [IO.File]::WriteAllLines((Join-Path $outputDir 'SHA256SUMS'), $sumLines, (New-Object Text.UTF8Encoding $false))
    Write-Output 'Internal-test build evidence manifest and SHA256SUMS written.'
}
} finally {
    if ($internalTranscript) { try { Stop-Transcript | Out-Null } catch { } }
}
