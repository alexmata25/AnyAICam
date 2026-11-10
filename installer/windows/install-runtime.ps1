param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [Parameter(Mandatory=$true)][string]$PythonArchive,
    [Parameter(Mandatory=$true)][string]$GetPipScript,
    [Parameter(Mandatory=$true)][string]$WheelRoot,
    [Parameter(Mandatory=$true)][string]$FFmpegArchive,
    [Parameter(Mandatory=$true)][string]$MediaMtxArchive,
    # 1.3.4: the Tesseract OCR engine bundle for licence plate reading (prepare-tesseract-bundle.py).
    [Parameter(Mandatory=$true)][string]$TesseractArchive,
    # 1.3.2: the pinned Vosk speech model for Voice Call listening (install-stt-model.ps1).
    [Parameter(Mandatory=$true)][string]$SttModelArchive,
    [Parameter(Mandatory=$true)][string]$AppVersion,
    # 2026-10-20 (owner): the customer-facing AnyAiCam VMS release (1.2.7). AppVersion stays this Windows setup's own
    # version (1.3.2), which install-version-guard.ps1 orders by.
    [Parameter(Mandatory=$true)][ValidatePattern('^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2}$')][string]$ReleaseVersion,
    # Setup's /PortalUrl=https://... (empty: keep the saved one, else the
    # AnyAiCam cloud); checked by cloud-link.ps1 before anything is written.
    [string]$PortalUrl = '',
    # 2026-10-08: the signed catalog of the runtime's native files
    # (runtime-catalog.ps1), registered before anything native is loaded, so
    # Smart App Control lets the bundled runtime run.
    [Parameter(Mandatory=$true)][string]$RuntimeCatalog,
    # Requirement 9 (1.2.7): the entitlement-signing PUBLIC keyset and its pinned SHA-256 (build.ps1).
    [Parameter(Mandatory=$true)][string]$EntitlementKeyset,
    [Parameter(Mandatory=$true)][string]$EntitlementKeysetSha256,
    # LIC-W1: the entitlement trust registry and its pinned SHA-256 (build.ps1); always installed before the flat keyset.
    [Parameter(Mandatory=$true)][string]$EntitlementTrustRegistry,
    [Parameter(Mandatory=$true)][string]$EntitlementTrustRegistrySha256,
    # Requirement 9 (1.2.7): Local or Hybrid, chosen in Setup (or /ProductMode= for a silent install). Empty on an
    # upgrade, which keeps the saved mode. Mirrors installer/03-product-mode.sh.
    [string]$ProductMode = '',
    [switch]$DirectTest,
    [string]$TestRunId = '',
    [string]$TestApplianceIdentity = '',
    [string]$TestApplianceIdentitySha256 = '',
    [string]$TestAcceptedCapacity = '',
    [string]$TestAcceptedCapacitySha256 = '',
    [string]$TestFeatureEntitlements = '',
    [string]$TestFeatureEntitlementsSha256 = ''
)
$ErrorActionPreference = 'Stop'
if ($DirectTest) {
    if ($TestRunId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') { throw 'DIRECT_TEST_RUN_ID_INVALID' }
    $profilePath = Join-Path $InstallRoot 'direct-test-profile.json'
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) { throw 'DIRECT_TEST_PROFILE_MISSING' }
    $profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
    $expectedRoot = Join-Path (Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'AnyAiCam-Test') $TestRunId
    if ($profile.run_id -cne $TestRunId -or [IO.Path]::GetFullPath([string]$profile.data_root) -ine [IO.Path]::GetFullPath($expectedRoot) -or
        [IO.Path]::GetFullPath($DataRoot) -ine [IO.Path]::GetFullPath($expectedRoot) -or
        $profile.service_name -cne 'AnyAiCamVmsDirectTest' -or $profile.http_port -ne 18767) { throw 'DIRECT_TEST_PROFILE_MISMATCH' }
    # Python trust helpers resolve their trust location from platform_paths at process startup.
    # Bind that resolver to the protected profile before any helper is invoked.
    $env:ANYAICAM_INTERNAL_TEST_BUILD = '1'
    $env:ANYAICAM_TEST_RUN_ID = $TestRunId
    if (($TestApplianceIdentity -and $TestApplianceIdentitySha256 -notmatch '^[0-9a-f]{64}$') -or
        (-not $TestApplianceIdentity -and $TestApplianceIdentitySha256)) { throw 'DIRECT_TEST_IDENTITY_PIN_INVALID' }
    if (($TestFeatureEntitlements -and $TestFeatureEntitlementsSha256 -notmatch '^[0-9a-f]{64}$') -or
        (-not $TestFeatureEntitlements -and $TestFeatureEntitlementsSha256)) { throw 'DIRECT_TEST_ENTITLEMENT_PIN_INVALID' }
    if (($TestAcceptedCapacity -and $TestAcceptedCapacitySha256 -notmatch '^[0-9a-f]{64}$') -or
        (-not $TestAcceptedCapacity -and $TestAcceptedCapacitySha256)) { throw 'DIRECT_TEST_CAPACITY_PIN_INVALID' }
} elseif ($TestRunId -or $TestApplianceIdentity -or $TestApplianceIdentitySha256 -or $TestAcceptedCapacity -or $TestAcceptedCapacitySha256 -or $TestFeatureEntitlements -or $TestFeatureEntitlementsSha256) {
    throw 'DIRECT_TEST_ARGUMENTS_REQUIRE_DIRECT_TEST_MODE'
}
. (Join-Path $PSScriptRoot 'runtime-catalog.ps1') -DirectTest:$DirectTest
Install-RuntimeCatalog $RuntimeCatalog
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
# pip's 33 distlib console-script EXEs are generated after catalog staging.
# They are not used by the VMS (which imports modules / invokes python -m) and
# are not part of the product runtime. Remove only top-level Scripts\*.exe;
# keep package modules, scripts and all other runtime files unchanged.
. (Join-Path $PSScriptRoot 'python-launchers.ps1')
$removedPythonLaunchers = Remove-AnyAiCamGeneratedPythonLaunchers $pythonRoot
Write-Output "Removed $removedPythonLaunchers pip-generated Python Scripts EXE launchers; package modules and runtime files are retained."
# The VMS imports cv2/ultralytics/torch at startup; their DLLs need the Visual
# C++ runtime the setup installs first. Stop here, before any service exists,
# if anything fails to load (the setup log has the preflight's output).
& $python (Join-Path $InstallRoot 'installer\runtime-preflight.py')
if ($LASTEXITCODE -ne 0) { throw 'AnyAiCam runtime preflight failed: the bundled Python cannot load the VMS packages.' }
# The cloud agent (2026-10-07) must load too, before its service exists.
if (-not $DirectTest) {
    & $python -c "import sys; sys.path.insert(0, sys.argv[1]); import anyaicam_agent.service, anyaicam_agent.windows; print('AnyAiCam agent preflight passed')" (Join-Path $InstallRoot 'agent')
    if ($LASTEXITCODE -ne 0) { throw 'AnyAiCam agent preflight failed: the bundled Python cannot load the cloud agent.' }
}

$ffmpegRoot = Join-Path $InstallRoot 'runtime\tools\ffmpeg'
# 1.3.4.1: FFmpeg's GPL licence sits beside bin\ in the archive, so the copy below never reached it. An upgrade over a
# copy without it opens the archive for the licence alone and leaves the programs as they are.
$ffmpegLicense = Join-Path $InstallRoot 'licenses\ffmpeg\LICENSE'
$ffmpegPresent = Test-Path (Join-Path $ffmpegRoot 'ffmpeg.exe')
if (-not $ffmpegPresent -or -not (Test-Path -LiteralPath $ffmpegLicense)) {
    $ffmpegTemp = Join-Path $env:TEMP ('anyaicam-ffmpeg-' + [guid]::NewGuid().ToString('N'))
    try {
        Expand-Archive -LiteralPath $FFmpegArchive -DestinationPath $ffmpegTemp -Force
        $ffmpeg = Get-ChildItem -LiteralPath $ffmpegTemp -Filter ffmpeg.exe -Recurse | Select-Object -First 1
        if (-not $ffmpeg) { throw 'FFmpeg archive does not contain ffmpeg.exe.' }
        $license = Join-Path $ffmpeg.Directory.Parent.FullName 'LICENSE'
        if (-not (Test-Path -LiteralPath $license)) { throw 'FFmpeg archive does not contain its LICENSE.' }
        if (-not $ffmpegPresent) {
            New-Item -ItemType Directory -Force -Path $ffmpegRoot | Out-Null
            Copy-Item -Path (Join-Path $ffmpeg.Directory.FullName '*') -Destination $ffmpegRoot -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ffmpegLicense) | Out-Null
        Copy-Item -LiteralPath $license -Destination $ffmpegLicense -Force
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

# Tesseract OCR (1.3.4, LPR parity): tesseract.exe, its DLLs and the English model, the same OCR Linux gets from
# Debian's tesseract-ocr. Every file is checked against the bundle's own manifest (and nothing unlisted may be in it)
# before it replaces the old copy; the folder is replaced as a whole on every install, so an upgrade never mixes
# versions. service-launcher.ps1 puts it on PATH (analytics_platform finds it there) and sets TESSDATA_PREFIX.
$tesseractRoot = Join-Path $InstallRoot 'runtime\tools\tesseract'
$tesseractTemp = Join-Path $env:TEMP ('anyaicam-tesseract-' + [guid]::NewGuid().ToString('N'))
try {
    Expand-Archive -LiteralPath $TesseractArchive -DestinationPath $tesseractTemp -Force
    $bundleManifest = Get-Content -LiteralPath (Join-Path $tesseractTemp 'BUNDLE-MANIFEST.json') -Raw | ConvertFrom-Json
    $listed = @($bundleManifest.files.PSObject.Properties)
    if ($listed.Count -lt 3) { throw 'The Tesseract bundle manifest lists no files.' }
    foreach ($file in $listed) {
        $path = Join-Path $tesseractTemp ($file.Name -replace '/', '\')
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Tesseract bundle file missing: $($file.Name)" }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $file.Value.sha256) { throw "Tesseract bundle file failed its checksum: $($file.Name)" }
    }
    # Cut relative names from the folder's own full spelling: TEMP may be an 8.3 short path (source-integrity.ps1).
    $tesseractTempFull = (Get-Item -LiteralPath $tesseractTemp).FullName.TrimEnd('\')
    $extra = @(Get-ChildItem -LiteralPath $tesseractTempFull -Recurse -File | ForEach-Object { $_.FullName.Substring($tesseractTempFull.Length + 1) -replace '\\', '/' } |
        Where-Object { $_ -ne 'BUNDLE-MANIFEST.json' -and -not $bundleManifest.files.PSObject.Properties[$_] })
    if ($extra.Count) { throw "The Tesseract bundle holds unlisted files: $($extra -join ', ')" }
    if (-not (Test-Path -LiteralPath (Join-Path $tesseractTemp 'tesseract\tesseract.exe'))) { throw 'The Tesseract bundle does not contain tesseract.exe.' }
    if (Test-Path -LiteralPath $tesseractRoot) { Remove-Item -LiteralPath $tesseractRoot -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tesseractRoot) | Out-Null
    Move-Item -LiteralPath (Join-Path $tesseractTemp 'tesseract') -Destination $tesseractRoot
    Copy-Item -LiteralPath (Join-Path $tesseractTemp 'BUNDLE-MANIFEST.json') -Destination $tesseractRoot -Force
    $tesseractLicenses = Join-Path $InstallRoot 'licenses\tesseract'
    if (Test-Path -LiteralPath $tesseractLicenses) { Remove-Item -LiteralPath $tesseractLicenses -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tesseractLicenses) | Out-Null
    Move-Item -LiteralPath (Join-Path $tesseractTemp 'licenses') -Destination $tesseractLicenses
} finally { if (Test-Path $tesseractTemp) { Remove-Item $tesseractTemp -Recurse -Force } }

# The data folder holds the app secrets (config\vms.env), the account database
# and recordings: only the service (LocalSystem) and administrators may read it.
# ProgramData's default would let every local user read and create files here.
# Codex 1.2.7 Windows review: a standard user may pre-create %ProgramData%\AnyAiCam (ProgramData lets users create
# folders) and plant a junction or symbolic link in it. Refuse links before creating child paths or running recursive
# ACL repair, then make Administrators the owner of the root (an owner can always rewrite a folder's permissions).
. (Join-Path $PSScriptRoot 'data-root-guard.ps1')
Assert-AnyAiCamDataRootHasNoLinks $DataRoot  # catch DataRoot itself if it was pre-planted as a junction
# Decided BEFORE the root is touched: was this already a genuine (admin-owned, protected) AnyAiCam data root? If
# not -- a first install, or a tree someone else prepared -- every entry is repaired below, not only the secret folders.
$rootWasTrusted = Test-AnyAiCamDataRootTrusted $DataRoot
if (-not (Test-Path -LiteralPath $DataRoot)) {
    New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
}
Assert-AnyAiCamDataRootHasNoLinks $DataRoot  # root creation must not have resolved to a reparse point
& icacls.exe $DataRoot /setowner '*S-1-5-32-544' /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not set the owner of $DataRoot (icacls exit $LASTEXITCODE)." }
# The root only (never /T): its inheritable SYSTEM/Administrators entries flow to every existing and future file and
# folder that keeps inheritance on. Done FIRST, so no new top-level entry can be planted while the tree is checked.
& icacls.exe $DataRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to $DataRoot (icacls exit $LASTEXITCODE)." }
Assert-AnyAiCamDataRootHasNoLinks $DataRoot
foreach ($directory in @('config', 'database', 'recordings', 'hls', 'logs', 'mediamtx')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot $directory) | Out-Null
}
$environmentFile = Join-Path $DataRoot 'config\vms.env'
# Remember whether the complete legacy tree was already repaired so the three
# secret folders are not walked twice on that path.
$repairedLegacyTree = $false
# Repair (2026-10-07): the unreleased 1.3.0 candidates ran the restriction
# below with /T, which on a reinstall left every existing file with an EMPTY
# permission list -- unreadable even by the service and administrators. Such an
# install is recognised by an unreadable vms.env: take ownership for
# Administrators and make every item inherit from the folder again. Healthy
# installs (and 0.1.3, whose files only inherit) skip this.
if (Test-Path -LiteralPath $environmentFile) {
    $readable = $true
    try { [void][IO.File]::ReadAllBytes($environmentFile) } catch { $readable = $false }
    if (-not $readable) {
        # Walk one entry at a time. The helper rejects reparse points, secures
        # each directory before enumerating it, and never gives icacls a tree
        # recursion flag that could follow an attacker-planted link.
        Set-AnyAiCamDescendantAcl $DataRoot
        & icacls.exe $DataRoot /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to $DataRoot (icacls exit $LASTEXITCODE)." }
        $repairedLegacyTree = $true
    }
}
# The secret-bearing folders (app secrets, account database, cloud-agent credential) must not keep anything a user
# planted before the install. Parent directories are restricted first; then each child is checked, owned and reset
# without recursive traversal before its descendants are enumerated.
if (-not $repairedLegacyTree -and -not $rootWasTrusted) {
    # First install or a pre-prepared tree: recordings, hls, logs and mediamtx may hold user-writable entries or
    # foreign owners too. Walk and repair the whole tree (no recursive ACL tool; links refused before any change).
    Set-AnyAiCamDescendantAcl $DataRoot
    $repairedLegacyTree = $true
}
if (-not $repairedLegacyTree) {
    foreach ($secretFolder in @('config', 'database', 'agent')) {
        $secretPath = Join-Path $DataRoot $secretFolder
        if (-not (Test-Path -LiteralPath $secretPath)) { continue }
        Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $secretPath -Force)
    }
}
Assert-AnyAiCamDataRootHasNoLinks $DataRoot  # again, now that nothing below is user-writable
# Trust anchors (Requirement 9, the Windows counterpart of Linux /etc/anyaicam-update): %ProgramData%\AnyAiCam\update
# holds the entitlement keyset and the accepted / fallback VMS capacity. The VMS (app/platform_paths.py) trusts an
# entry only if it is owned by SYSTEM or Administrators, nobody else may modify it, and it is not a link. A file an
# administrator creates is owned by that user, so ownership is set explicitly.
$updateRoot = Join-Path $DataRoot 'update'
if (-not (Test-Path -LiteralPath $updateRoot)) { New-Item -ItemType Directory -Path $updateRoot -Force | Out-Null }
Assert-AnyAiCamTrustFolderHasNoHardLinks $updateRoot
Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $updateRoot -Force)
if ($EntitlementKeysetSha256 -notmatch '^[0-9a-f]{64}$') { throw 'The pinned entitlement keyset SHA-256 is malformed.' }
$keysetBytes = [IO.File]::ReadAllBytes($EntitlementKeyset)
$keysetText = [Text.Encoding]::ASCII.GetString($keysetBytes).ToUpperInvariant()
if ($keysetText.Contains('PRIVATE') -or $keysetText.Contains('BEGIN') -or $keysetText.Contains('OPENSSH')) { throw 'The packaged entitlement keyset contains private key material; refusing to install it.' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $EntitlementKeyset).Hash.ToLowerInvariant() -ne $EntitlementKeysetSha256) { throw 'The packaged entitlement keyset does not match its pinned SHA-256; refusing to trust it.' }
# LIC-W1: entitlement_trust_registry.json, then entitlement_trust_state.json, then the accepted grant's signer fields,
# before the flat keyset, under the acceptance lock (trust_install_windows.py). Any refusal stops the setup with the
# trust files as they were. The VMS and the helper trust an entry only when SYSTEM or Administrators owns it, and a file
# an administrator creates is owned by that user, so the walk that repaired this folder above runs again. Both services
# are stopped here, so nothing reads these files before their owner is right.
if ($EntitlementTrustRegistrySha256 -notmatch '^[0-9a-f]{64}$') { throw 'The pinned entitlement trust registry SHA-256 is malformed.' }
if ($DirectTest) {
    $testRegistry = Get-Content -LiteralPath $EntitlementTrustRegistry -Raw | ConvertFrom-Json
    $testKeyset = Get-Content -LiteralPath $EntitlementKeyset -Raw | ConvertFrom-Json
    if ($testRegistry.environment -cne 'synthetic' -or -not $testRegistry.keys -or -not $testKeyset.keys) { throw 'DIRECT_TEST_SYNTHETIC_TRUST_REQUIRED' }
    foreach ($key in $testRegistry.keys.PSObject.Properties) {
        if ($key.Value.environment -cne 'synthetic') { throw 'DIRECT_TEST_NON_SYNTHETIC_TRUST_KEY_REFUSED' }
    }
    foreach ($key in $testKeyset.keys.PSObject.Properties) {
        if (-not $testRegistry.keys.($key.Name) -or $testRegistry.keys.($key.Name).environment -cne 'synthetic') { throw 'DIRECT_TEST_KEYSET_REGISTRY_MISMATCH' }
    }
}
& $python (Join-Path $InstallRoot 'agent\system\trust_install_windows.py') $EntitlementTrustRegistry $EntitlementTrustRegistrySha256 $EntitlementKeyset
if ($LASTEXITCODE -ne 0) { throw "The entitlement trust registry was not installed (exit $LASTEXITCODE); the setup log has the reason." }
Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $updateRoot -Force)
$keysetTarget = Join-Path $updateRoot 'entitlement_signing_keys.json'
$keysetTemp = Join-Path $updateRoot ('.entitlement_signing_keys.' + [guid]::NewGuid().ToString('N') + '.tmp')
[IO.File]::WriteAllBytes($keysetTemp, $keysetBytes)
Move-Item -LiteralPath $keysetTemp -Destination $keysetTarget -Force
& icacls.exe $keysetTarget /setowner '*S-1-5-32-544' /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not set the owner of $keysetTarget (icacls exit $LASTEXITCODE)." }
& icacls.exe $keysetTarget /reset /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not apply the trust folder's permissions to $keysetTarget (icacls exit $LASTEXITCODE)." }
Write-Output 'Entitlement keyset installed in the protected trust folder.'
# Voice Call listening (1.3.2): the same pinned Vosk speech model as the Linux images, in the protected data root
# (%ProgramData%\AnyAiCam\models\stt), where the VMS looks by default. Kept when it verifies; replaced when missing,
# altered or incomplete. Any failure stops the setup (a bundled model that does not verify is a damaged setup).
. (Join-Path $PSScriptRoot 'install-stt-model.ps1')
Install-AnyAiCamSttModel -DataRoot $DataRoot -Archive $SttModelArchive -Manifest (Join-Path $PSScriptRoot 'vosk-model-small-en-us-0.15.sha256')
Assert-AnyAiCamDataRootHasNoLinks $DataRoot
# Camera capacity fallback (Linux installer step 15) is deliberately NOT migrated on Windows. Windows 1.3.x kept its
# unsigned capacity in C:\opt\anyaicam\data\config\vms_capacity.json, a folder any signed-in user may modify by
# default, so that number cannot become a trusted limit. Cameras start after the cloud's first signed capacity
# (vms_capacity accepted file); a fallback file, if one ever exists here, is trusted only under the
# SYSTEM/Administrators-only rule (app/platform_paths.py).
$legacyCapacity = Join-Path $env:SystemDrive 'opt\anyaicam\data\config\vms_capacity.json'
if (Test-Path -LiteralPath $legacyCapacity) {
    Write-Output 'Camera capacity: the earlier release''s unsigned capacity is not carried over; cameras start after the cloud''s first signed capacity (usually within minutes of linking).'
} else {
    Write-Output 'Camera capacity: cameras start after the cloud''s first signed capacity, which this computer receives once it is linked to an AnyAiCam account (needed once, online). After that, Local cameras keep working without internet.'
}
$values = [ordered]@{}
$hadEnvironmentFile = Test-Path -LiteralPath $environmentFile
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
# On-device analytics whose code default is off (the Linux installer's 06-deploy-vms.sh defaults, 1.3.2): People
# Counting and the customer rule analytics (line crossing, intrusion, loitering) still run only for a camera the cloud
# licensed and a customer configured. Add-if-missing: an explicit true or false is never changed. 1.3.4: LPR joins
# them as on Linux, now that the Tesseract engine is bundled; it still runs only for a licensed, enabled camera, and
# app/analytics_platform.py reports it unavailable while the plate model is not installed. Facial recognition stays
# an explicit opt-in, as on Linux.
foreach ($analyticsFlag in @('ANYAICAM_LPR_ENABLED', 'PEOPLE_COUNTING_ENABLED', 'CUSTOMER_ANALYTICS_RULES_ENABLED')) {
    if (-not $values.Contains($analyticsFlag)) { $values[$analyticsFlag] = 'true' }
}
$values['ANYAICAM_RUNTIME_ROLE'] = 'edge'
$values['ANYAICAM_ENV'] = $(if ($DirectTest) { 'local' } else { 'production' })
$values['ANYAICAM_FORCE_HTTPS'] = 'false'
$values['ANYAICAM_SECURE_COOKIES'] = 'false'
$values['ANYAICAM_PUBLIC_URL'] = $(if ($DirectTest) { 'http://127.0.0.1:18767' } else { 'http://127.0.0.1:8000' })
$values['ANYAICAM_VMS_COMMIT'] = $SourceCommit
$values['ANYAICAM_BUILD_ID'] = $SourceCommit
# Local / Hybrid (Requirement 9): the same rules as Linux installer/03-product-mode.sh. A new installation records
# the chosen mode as the bootstrap mode (used until the cloud reports one); Setup never switches a saved mode; an
# earlier installation that never recorded one is left unset, so its runtime defaults are kept exactly.
$savedMode = ''
foreach ($key in @('ANYAICAM_PRODUCT_MODE', 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP')) {
    if (-not $savedMode -and $values.Contains($key)) {
        $savedMode = [string]$values[$key]
        if ($savedMode -cnotin @('local', 'hybrid')) { throw "$key in vms.env must be exactly local or hybrid; fix it before installing." }
    }
}
if ($ProductMode) {
    if ($ProductMode -cnotin @('local', 'hybrid')) { throw 'The product mode must be exactly local or hybrid.' }
    if ($savedMode -and $savedMode -ne $ProductMode) { throw "Setup cannot switch the existing product mode ($savedMode); change it from your AnyAiCam account instead." }
    if (-not $savedMode) { $values['ANYAICAM_PRODUCT_MODE_BOOTSTRAP'] = $ProductMode }
} elseif (-not $hadEnvironmentFile) {
    throw 'A new installation needs a product mode: choose Local or Hybrid (silent install: /ProductMode=local or /ProductMode=hybrid).'
}
# What /version reports (and the VMS pages show): the customer-facing release, exactly as the Linux installer writes it.
$values['ANYAICAM_VERSION'] = $ReleaseVersion
# The Windows setup's own version, which install-version-guard.ps1 reads first (it survives an uninstall with the data).
# 1.3.0 / 1.3.1 had no such line; their ANYAICAM_VERSION was the setup version, the guard's fallback.
$values['ANYAICAM_WINDOWS_SETUP_VERSION'] = $AppVersion
if ($DirectTest) {
    if ($PortalUrl) { throw 'DIRECT_TEST_PORTAL_URL_FORBIDDEN' }
    if (-not $TestApplianceIdentity -or -not $TestAcceptedCapacity -or -not $TestFeatureEntitlements) { throw 'DIRECT_TEST_SYNTHETIC_LICENSE_FIXTURES_REQUIRED' }
    $values['ANYAICAM_PRODUCT_MODE'] = 'local'
    $values['ANYAICAM_PRODUCT_MODE_BOOTSTRAP'] = 'local'
    $values['ANYAICAM_AGENT_MODE'] = 'disabled'
    $values['ANYAICAM_PORTAL_URL'] = ''
    $values['ANYAICAM_ANALYTICS_SYNC_ENABLED'] = 'false'
    $values['ANYAICAM_ENV'] = 'local'
    $values['ANYAICAM_BIND_HOST'] = '127.0.0.1'
    $values['ANYAICAM_HTTP_PORT'] = '18767'
    if ($TestApplianceIdentity) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $TestApplianceIdentity).Hash.ToLowerInvariant() -cne $TestApplianceIdentitySha256) { throw 'DIRECT_TEST_IDENTITY_HASH_MISMATCH' }
        $identity = Get-Content -LiteralPath $TestApplianceIdentity -Raw | ConvertFrom-Json
        if ($identity.appliance_id -notmatch '^TEST-[A-Za-z0-9-]{1,80}$' -or $identity.cloud_id -notmatch '^TEST-[A-Za-z0-9-]{1,80}$' -or
            $identity.customer_id -notmatch '^TEST-[A-Za-z0-9-]{1,80}$' -or $identity.site_id -notmatch '^TEST-[A-Za-z0-9-]{1,80}$' -or
            $identity.partner_id -notmatch '^TEST-[A-Za-z0-9-]{1,80}$' -or $identity.credential -notmatch '^test-[A-Za-z0-9._-]{8,160}$') { throw 'DIRECT_TEST_IDENTITY_NOT_SYNTHETIC' }
        Copy-Item -LiteralPath $TestApplianceIdentity -Destination (Join-Path $DataRoot 'config\appliance_identity.json') -Force
    }
    if ($TestFeatureEntitlements) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $TestFeatureEntitlements).Hash.ToLowerInvariant() -cne $TestFeatureEntitlementsSha256) { throw 'DIRECT_TEST_ENTITLEMENT_HASH_MISMATCH' }
        $grant = Get-Content -LiteralPath $TestFeatureEntitlements -Raw | ConvertFrom-Json
        $snapshot = if ($grant.snapshot) { $grant.snapshot } else { $grant }
        $registry = Get-Content -LiteralPath $EntitlementTrustRegistry -Raw | ConvertFrom-Json
        $signatureKey = [string]$snapshot.signature.key_id
        if ($snapshot.type -cne 'anyaicam.feature_entitlements' -or $snapshot.appliance_id -notmatch '^TEST-' -or
            $snapshot.cloud_id -notmatch '^TEST-' -or $snapshot.customer_id -notmatch '^TEST-' -or
            $registry.environment -cne 'synthetic' -or -not $registry.keys.$signatureKey -or $registry.keys.$signatureKey.state -cne 'active' -or
            $registry.keys.$signatureKey.environment -cne 'synthetic') { throw 'DIRECT_TEST_ENTITLEMENT_TRUST_REFUSED' }
        Copy-Item -LiteralPath $TestFeatureEntitlements -Destination (Join-Path $DataRoot 'config\feature_entitlements.json') -Force
    }
    if ($TestAcceptedCapacity) {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $TestAcceptedCapacity).Hash.ToLowerInvariant() -cne $TestAcceptedCapacitySha256) { throw 'DIRECT_TEST_CAPACITY_HASH_MISMATCH' }
        $accepted = Get-Content -LiteralPath $TestAcceptedCapacity -Raw | ConvertFrom-Json
        $snapshot = $accepted.snapshot
        $keyId = [string]$snapshot.signature.key_id
        $key = $registry.keys.$keyId
        if ($accepted.schema -ne 2 -or $accepted.trust_generation -lt 1 -or $accepted.identity.appliance_id -cne $identity.appliance_id -or
            $accepted.identity.cloud_id -cne $identity.cloud_id -or $accepted.identity.customer_id -cne $identity.customer_id -or
            $snapshot.type -cne 'anyaicam.feature_entitlements' -or $snapshot.appliance_id -cne $identity.appliance_id -or
            -not $snapshot.camera_capacity -or [int]$snapshot.camera_capacity.camera_slot_quantity -lt 1 -or
            -not $key -or $key.environment -cne 'synthetic' -or $key.state -cne 'active' -or
            $accepted.signer.key_id -cne $keyId -or $accepted.accepted_key -cne $key.public_key) { throw 'DIRECT_TEST_CAPACITY_TRUST_REFUSED' }
        if ($accepted.high_water.digest -notmatch '^[0-9a-f]{64}$' -or -not $accepted.high_water.issued_at -or -not $accepted.accepted_at) { throw 'DIRECT_TEST_CAPACITY_STATE_INCOMPLETE' }
        $acceptedCapacityTarget = Join-Path $updateRoot 'vms_capacity_accepted.json'
        Copy-Item -LiteralPath $TestAcceptedCapacity -Destination $acceptedCapacityTarget -Force
        Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $acceptedCapacityTarget -Force)
        Assert-AnyAiCamTrustFolderHasNoHardLinks $updateRoot
    }
    & $python (Join-Path $InstallRoot 'installer\verify-direct-test-licensing.py') `
        (Join-Path $DataRoot 'config\appliance_identity.json') `
        (Join-Path $DataRoot 'config\feature_entitlements.json') `
        (Join-Path $updateRoot 'vms_capacity_accepted.json') `
        $EntitlementTrustRegistry (Join-Path $InstallRoot 'app') (Join-Path $InstallRoot 'agent\system')
    if ($LASTEXITCODE -ne 0) { throw 'DIRECT_TEST_LICENSE_CRYPTOGRAPHIC_VERIFICATION_FAILED' }
}
$content = @('# Managed by the AnyAiCam Windows installer. Persistent across repair and uninstall.')
$content += $values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
[IO.File]::WriteAllLines($environmentFile, $content, [Text.UTF8Encoding]::new($false))

if (-not $DirectTest) {
    # Production account linking and inbound firewall behavior remain the existing production path.
    & (Join-Path $InstallRoot 'installer\cloud-link.ps1') -DataRoot $DataRoot -AppVersion $AppVersion -ReleaseVersion $ReleaseVersion -SourceCommit $SourceCommit -PortalUrl $PortalUrl
    & (Join-Path $InstallRoot 'installer\firewall.ps1') -Apply -InstallRoot $InstallRoot
} else {
    # Explicit per-image outbound block is installed before the service exists.
    # Only this candidate's executables and exact run-scoped rule group are touched.
    & (Join-Path $InstallRoot 'installer\firewall-direct-test.ps1') -Apply -InstallRoot $InstallRoot -RunId $TestRunId
    if (-not $?) { throw 'DIRECT_TEST_EGRESS_POLICY_FAILED' }
}
