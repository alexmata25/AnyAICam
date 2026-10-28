# Prepares a Windows Sandbox validation folder for one setup .exe (2026-10-07).
#   make-wsb.ps1 -Setup <AnyAiCam-VMS-Setup-x.y.z-abcdef1.exe> -Folder <new empty folder> [-Previous <earlier setup .exe>]
# -Previous (2026-10-08): that setup is installed first and -Setup is validated
# as an upgrade over it (C:\Validation\previous, read-only).
# Writes <Folder>\setup\<exe>, <Folder>\results\, <Folder>\validate-in-sandbox.ps1
# and <Folder>\AnyAiCam-Validation.wsb (setup read-only, results writable).
# 2026-10-18 (Windows 1.2.7, Codex 21:07): networking is DISABLED -- the test cloud runs inside the Sandbox on
# 127.0.0.1, so the VM has no route to any production network or service. Each run is either
#   -KeysetKind production      the release candidate, built with the production public keyset (-ExpectedKeysetSha256
#                               must be its pin): verifies the exact keyset and that foreign signatures are refused;
#   -KeysetKind synthetic-test  the TEST-ONLY build of the same source with the synthetic Sandbox keyset
#                               (make-synthetic-keyset.py): signed capacity limits, expiry, tamper rejection and the
#                               cloud-authorized mode, signed by the synthetic key only (-SyntheticSigningKey).
# -Functional (2026-10-21, Blue): the FUNCTIONAL scenario F-L / F-H (functional-in-sandbox.ps1: sign-in, simulated
# camera, live view, recording, playback, motion, analytics, offline, Local<->Hybrid, link expiry/revocation).
# SYNTHETIC-KEYSET / SIMULATED CAMERA only: refused for a production-keyset run and with -Previous/-SimulatePrevious.
# Without -Functional the folder is exactly what it was before (validate-in-sandbox.ps1).
# -ProductMode local|hybrid is the /ProductMode the run installs with. -Previous must be an OLDER version than -Setup
# (a downgrade, e.g. from Windows 1.3.x, is refused here).
param(
    [Parameter(Mandatory=$true)][string]$Setup,
    [Parameter(Mandatory=$true)][string]$Folder,
    [Parameter(Mandatory=$true)][ValidateSet('local', 'hybrid')][string]$ProductMode,
    [Parameter(Mandatory=$true)][ValidateSet('production', 'synthetic-test')][string]$KeysetKind,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedKeysetSha256,
    [string]$SyntheticSigningKey = '',
    [string]$Previous = '',
    # 2026-10-08: plant Windows 1.3.0's leftovers instead of running its
    # (unsigned) setup, for Smart App Control machines.
    [switch]$SimulatePrevious,
    [switch]$Functional
)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $Folder) { throw "Use a new folder: $Folder exists." }
$setupFile = Get-Item -LiteralPath $Setup
if ($setupFile.Name -notmatch '^AnyAiCam-VMS-Setup-\d+\.\d+\.\d+(?:\.\d+)?-[0-9a-f]{7}\.exe$') { throw "Not an AnyAiCam setup name: $($setupFile.Name)" }
if ($KeysetKind -eq 'synthetic-test') {
    if (-not $SyntheticSigningKey) { throw 'A synthetic-test run needs -SyntheticSigningKey (make-synthetic-keyset.py).' }
    $syntheticKey = Get-Content -Raw -LiteralPath $SyntheticSigningKey | ConvertFrom-Json
    if ($syntheticKey.key_id -notlike 'sandbox-test-*') { throw 'Not a synthetic sandbox-test signing key.' }
} elseif ($SyntheticSigningKey) { throw 'A production run never receives a signing key.' }
if ($Functional -and $KeysetKind -ne 'synthetic-test') { throw 'A functional run uses the synthetic-test build only (SYNTHETIC-KEYSET / SIMULATED CAMERA).' }
# 1.3.4.1 acceptance: allow four-part setup versions and require the accepted, pinned, signed 1.3.4 setup as the upgrade baseline.
if ($Functional -and $SimulatePrevious) { throw 'A functional run never plants simulated 1.3.0 leftovers: use -Previous with a real earlier setup.' }
$root = New-Item -ItemType Directory -Path $Folder
foreach ($sub in 'setup', 'results', 'scripts', 'previous') { New-Item -ItemType Directory -Path (Join-Path $root.FullName $sub) | Out-Null }
Copy-Item -LiteralPath $setupFile.FullName -Destination (Join-Path $root.FullName 'setup')
if ($Previous) {
    $previousFile = Get-Item -LiteralPath $Previous
    if ($previousFile.Name -notmatch '^AnyAiCam-VMS-Setup-\d+\.\d+\.\d+(?:\.\d+)?-[0-9a-f]{7}\.exe$') { throw "Not an AnyAiCam setup name: $($previousFile.Name)" }
    $newVersionText = $setupFile.Name -replace '^AnyAiCam-VMS-Setup-(\d+\.\d+\.\d+(?:\.\d+)?)-.*$', '$1'
    $oldVersionText = $previousFile.Name -replace '^AnyAiCam-VMS-Setup-(\d+\.\d+\.\d+(?:\.\d+)?)-.*$', '$1'
    $newVersion = [version]$newVersionText
    $oldVersion = [version]$oldVersionText
    if ($oldVersion -ge $newVersion) { throw "-Previous $oldVersion is not older than $($newVersion) -- that is a downgrade, not a supported upgrade." }
    if ($oldVersionText -match '^1\.3\.4(?:\.0)?$') {
        $approvedPreviousSha256 = 'CAFC1D1BE9F457561094FBF756255D64266A249AE75C994C78A14A3E725DF034'
        $actualPreviousSha256 = (Get-FileHash -LiteralPath $previousFile.FullName -Algorithm SHA256).Hash
        if ($actualPreviousSha256 -cne $approvedPreviousSha256) { throw "Accepted 1.3.4 baseline SHA-256 mismatch: $actualPreviousSha256" }
        $publisherPin = Join-Path $PSScriptRoot '..\publisher-pin.ps1'
        if (-not (Test-Path -LiteralPath $publisherPin)) { throw 'Publisher identity verifier is missing; cannot accept the 1.3.4 upgrade baseline.' }
        . $publisherPin
        $signature = Get-AuthenticodeSignature -LiteralPath $previousFile.FullName
        $publisherProblem = Get-AnyAiCamPublisherProblem $signature $previousFile.FullName
        if ($publisherProblem) { throw "Accepted 1.3.4 baseline signature/publisher check failed: $publisherProblem" }
    }
    Copy-Item -LiteralPath $previousFile.FullName -Destination (Join-Path $root.FullName 'previous')
}
if ($SimulatePrevious) { Set-Content -LiteralPath (Join-Path $root.FullName 'previous\SIMULATE-1.3.0') -Value 'plant Windows 1.3.0 leftovers' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'validate-in-sandbox.ps1') -Destination (Join-Path $root.FullName 'scripts')
# The disposable test cloud the cloud-linking checks claim against (never production).
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'test-cloud.py') -Destination (Join-Path $root.FullName 'scripts')
# Windows 1.2.7 acceptance (2026-10-18): the checks, the synthetic snapshot signer and this run's settings.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'acceptance-127.ps1') -Destination (Join-Path $root.FullName 'scripts')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'synthetic-snapshots.py') -Destination (Join-Path $root.FullName 'scripts')
if ($KeysetKind -eq 'synthetic-test') { Copy-Item -LiteralPath $SyntheticSigningKey -Destination (Join-Path $root.FullName 'scripts\signing-key.json') }
# Functional scenario (2026-10-21): its entry script, the edge HTTP helper and the simulated ONVIF camera.
if ($Functional) { foreach ($f in 'functional-in-sandbox.ps1', 'edge-http.py', 'onvif-camera-sim.py', 'sandbox-proxy.py') { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $f) -Destination (Join-Path $root.FullName 'scripts') } }
$runSettings = [ordered]@{ product_mode = $ProductMode; keyset_kind = $KeysetKind; expected_keyset_sha256 = $ExpectedKeysetSha256 }
if ($Functional) { $runSettings.functional = $true; if ($Previous) { $runSettings.functional_upgrade = $true } }
$runSettings |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root.FullName 'scripts\acceptance.json') -Encoding ASCII
$escape = { param($s) [Security.SecurityElement]::Escape($s) }
$wsb = @"
<Configuration>
  <Networking>Disable</Networking>
  <MappedFolders>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'setup'))</HostFolder><SandboxFolder>C:\Validation\setup</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'scripts'))</HostFolder><SandboxFolder>C:\Validation\scripts</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'previous'))</HostFolder><SandboxFolder>C:\Validation\previous</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'results'))</HostFolder><SandboxFolder>C:\Validation\results</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Validation\scripts\$(if ($Functional) { 'functional-in-sandbox.ps1' } else { 'validate-in-sandbox.ps1' })</Command>
  </LogonCommand>
</Configuration>
"@
Set-Content -LiteralPath (Join-Path $root.FullName 'AnyAiCam-Validation.wsb') -Value $wsb -Encoding UTF8
"Sandbox validation folder ready: $($root.FullName)"
