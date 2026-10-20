param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [Parameter(Mandatory=$true)][string]$PythonArchive,
    [Parameter(Mandatory=$true)][string]$GetPipScript,
    [Parameter(Mandatory=$true)][string]$WheelRoot,
    [Parameter(Mandatory=$true)][string]$FFmpegArchive,
    [Parameter(Mandatory=$true)][string]$MediaMtxArchive,
    # 1.3.2: the pinned Vosk speech model for Voice Call listening (install-stt-model.ps1).
    [Parameter(Mandatory=$true)][string]$SttModelArchive,
    [Parameter(Mandatory=$true)][string]$AppVersion,
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
    # Requirement 9 (1.2.7): Local or Hybrid, chosen in Setup (or /ProductMode= for a silent install). Empty on an
    # upgrade, which keeps the saved mode. Mirrors installer/03-product-mode.sh.
    [string]$ProductMode = ''
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime-catalog.ps1')
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
# The VMS imports cv2/ultralytics/torch at startup; their DLLs need the Visual
# C++ runtime the setup installs first. Stop here, before any service exists,
# if anything fails to load (the setup log has the preflight's output).
& $python (Join-Path $InstallRoot 'installer\runtime-preflight.py')
if ($LASTEXITCODE -ne 0) { throw 'AnyAiCam runtime preflight failed: the bundled Python cannot load the VMS packages.' }
# The cloud agent (2026-10-07) must load too, before its service exists.
& $python -c "import sys; sys.path.insert(0, sys.argv[1]); import anyaicam_agent.service, anyaicam_agent.windows; print('AnyAiCam agent preflight passed')" (Join-Path $InstallRoot 'agent')
if ($LASTEXITCODE -ne 0) { throw 'AnyAiCam agent preflight failed: the bundled Python cannot load the cloud agent.' }

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
Set-AnyAiCamEntryAcl (Get-Item -LiteralPath $updateRoot -Force)
if ($EntitlementKeysetSha256 -notmatch '^[0-9a-f]{64}$') { throw 'The pinned entitlement keyset SHA-256 is malformed.' }
$keysetBytes = [IO.File]::ReadAllBytes($EntitlementKeyset)
$keysetText = [Text.Encoding]::ASCII.GetString($keysetBytes).ToUpperInvariant()
if ($keysetText.Contains('PRIVATE') -or $keysetText.Contains('BEGIN') -or $keysetText.Contains('OPENSSH')) { throw 'The packaged entitlement keyset contains private key material; refusing to install it.' }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $EntitlementKeyset).Hash.ToLowerInvariant() -ne $EntitlementKeysetSha256) { throw 'The packaged entitlement keyset does not match its pinned SHA-256; refusing to trust it.' }
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
    Write-Output 'Camera capacity: cameras start after the cloud''s first signed capacity.'
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
# licensed and a customer configured. Add-if-missing: an explicit true or false is never changed. LPR is not
# defaulted on Windows (no Tesseract OCR engine is bundled; app/analytics_platform.py reports it unavailable), and
# facial recognition stays an explicit opt-in, as on Linux.
foreach ($analyticsFlag in @('PEOPLE_COUNTING_ENABLED', 'CUSTOMER_ANALYTICS_RULES_ENABLED')) {
    if (-not $values.Contains($analyticsFlag)) { $values[$analyticsFlag] = 'true' }
}
$values['ANYAICAM_RUNTIME_ROLE'] = 'edge'
$values['ANYAICAM_ENV'] = 'production'
$values['ANYAICAM_FORCE_HTTPS'] = 'false'
$values['ANYAICAM_SECURE_COOKIES'] = 'false'
$values['ANYAICAM_PUBLIC_URL'] = 'http://127.0.0.1:8000'
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
# What /version reports (the Linux installer writes the same key).
$values['ANYAICAM_VERSION'] = $AppVersion
$content = @('# Managed by the AnyAiCam Windows installer. Persistent across repair and uninstall.')
$content += $values.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
[IO.File]::WriteAllLines($environmentFile, $content, [Text.UTF8Encoding]::new($false))

# Cloud linking (2026-10-07): appliance identity, claim code, cloud portal
# and installed release for the agent service (see cloud-link.ps1).
& (Join-Path $InstallRoot 'installer\cloud-link.ps1') -DataRoot $DataRoot -AppVersion $AppVersion -SourceCommit $SourceCommit -PortalUrl $PortalUrl

# Local-network access to the VMS and live view (see firewall.ps1).
& (Join-Path $InstallRoot 'installer\firewall.ps1') -Apply -InstallRoot $InstallRoot
