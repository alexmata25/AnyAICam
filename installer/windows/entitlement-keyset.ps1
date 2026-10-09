# Entitlement-signing keyset for a native Windows install (2026-10-08).
#
# The Windows counterpart of installer/13-entitlement-signing-keys.sh. The
# VMS trusts paid-feature snapshots (Talk Down, AAC Voice Call) only when
# signed by a key in
#     %ProgramData%\AnyAiCam\trust\entitlement_signing_keys.json
# and only when that file and its two parent folders are Administrators- or
# SYSTEM-owned, no reparse point, and changeable by nobody else
# (app/windows_trust_anchor.py). This script installs it that way.
#
#   Build (build.ps1):            Test-KeysetPackaging -- digest and plain
#                                 checks before anything is packaged.
#   Setup (install-runtime.ps1):  Install-EntitlementKeyset -- digest again,
#                                 the agent's own validate_entitlement_keyset
#                                 (canonical public keys only, the same rules
#                                 as Linux), Administrators-owned protected
#                                 folder and file, then the VMS's own check.
# A release without a keyset leaves an installed one unchanged (paid features
# then depend on it), like Linux. Nothing here is a secret: public keys only;
# key containers and private key material are refused.

$KeysetName = 'entitlement_signing_keys.json'
$TrustedOwnerSid = 'S-1-5-32-544'   # Administrators
$SystemSid = 'S-1-5-18'

function Get-Sha256Hex([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

# Build-time checks (no Python needed): digest, no key containers or private
# key material, and the {"keys": {...}} shape. The setup repeats the full,
# canonical validation with the agent's own code.
function Test-KeysetPackaging([string]$Path, [string]$Sha256) {
    if ($Sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'The entitlement keyset digest must be 64 lowercase hex characters (obtained on the cloud host, docs/entitlement-signing-keys.md).' }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Entitlement keyset not found: $Path" }
    if ((Get-Sha256Hex $Path) -cne $Sha256) { throw 'The entitlement keyset does not match the given digest; refusing to package it.' }
    $text = [IO.File]::ReadAllText($Path)
    if ($text -match '(?i)PRIVATE|BEGIN|OPENSSH') { throw 'The entitlement keyset contains a key container or private key material; refusing to package it.' }
    $document = $text | ConvertFrom-Json
    $names = @($document.PSObject.Properties.Name)
    if ($names.Count -ne 1 -or $names[0] -ne 'keys' -or -not @($document.keys.PSObject.Properties).Count) { throw 'The entitlement keyset must be exactly {"keys": {key_id: public key}} with at least one key.' }
}

function Test-ReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

# Administrators-owned, protected access list: SYSTEM and Administrators only.
function Set-TrustAcl([string]$Path, [switch]$Folder) {
    & icacls.exe $Path /setowner "*$TrustedOwnerSid" /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not make Administrators the owner of $Path." }
    $grants = if ($Folder) { @("*${SystemSid}:(OI)(CI)F", "*${TrustedOwnerSid}:(OI)(CI)F") } else { @("*${SystemSid}:F", "*${TrustedOwnerSid}:F") }
    & icacls.exe $Path /inheritance:r /grant:r @grants /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restrict the access list of $Path." }
}

# Installs the keyset. $Python is the bundled interpreter; $AgentRoot holds
# anyaicam_agent (validate_entitlement_keyset); $AppRoot holds the VMS
# (windows_trust_anchor). Returns a line for the setup log.
function Install-EntitlementKeyset([string]$DataRoot, [string]$Source, [string]$Sha256, [string]$Python, [string]$AgentRoot, [string]$AppRoot) {
    $trust = Join-Path $DataRoot 'trust'
    $target = Join-Path $trust $KeysetName
    if (-not $Sha256) {
        if (Test-Path -LiteralPath $target) { return 'This release provisions no entitlement-signing keyset; the installed one is left unchanged.' }
        return '[WARN] No entitlement-signing keyset is provisioned: Talk Down and AAC Voice Call stay unavailable on this computer (the VMS is unaffected).'
    }
    if ($Sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'The setup names a malformed entitlement keyset digest.' }
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw 'The setup names an entitlement-signing keyset but does not contain it.' }
    if ((Get-Sha256Hex $Source) -cne $Sha256) { throw 'The packaged entitlement-signing keyset does not match this release; refusing to trust it.' }
    & $Python -c "import sys; sys.path.insert(0, sys.argv[1]); from anyaicam_agent.updater import release_checks; release_checks.validate_entitlement_keyset(open(sys.argv[2], 'rb').read())" $AgentRoot $Source
    if ($LASTEXITCODE -ne 0) { throw 'The packaged entitlement-signing keyset is not a canonical public keyset; refusing to install it.' }

    # The data folder itself: Administrators-owned (a folder a user created
    # before the setup would otherwise stay theirs) and never a reparse point.
    if (Test-ReparsePoint $DataRoot) { throw "$DataRoot is a junction or symbolic link; refusing to install the entitlement keyset." }
    & icacls.exe $DataRoot /setowner "*$TrustedOwnerSid" /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not make Administrators the owner of $DataRoot." }
    # A trust folder that is a junction/link (planted to redirect the keyset)
    # is removed -- the link only, not what it points to -- and recreated.
    if (Test-ReparsePoint $trust) { [IO.Directory]::Delete($trust, $false) }
    if (-not (Test-Path -LiteralPath $trust -PathType Container)) { New-Item -ItemType Directory -Path $trust | Out-Null }
    Set-TrustAcl $trust -Folder
    if (Test-ReparsePoint $target) { [IO.File]::Delete($target) }
    $temporary = Join-Path $trust "$KeysetName.tmp"
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    [IO.File]::WriteAllBytes($temporary, [IO.File]::ReadAllBytes($Source))
    Move-Item -LiteralPath $temporary -Destination $target -Force
    Set-TrustAcl $target
    if ((Get-Sha256Hex $target) -cne $Sha256) { throw 'The installed entitlement-signing keyset does not match this release.' }

    # The VMS's own check must accept exactly what was installed.
    $problem = & $Python -c "import sys; sys.path.insert(0, sys.argv[1]); import windows_trust_anchor as w; print(w.anchor_problem(w.anchor_file()) or 'OK')" $AppRoot
    if ($LASTEXITCODE -ne 0 -or ($problem | Select-Object -Last 1) -ne 'OK') { throw "The VMS does not accept the installed entitlement keyset: $problem" }
    return "Provisioned the entitlement-signing public keyset at $target (sha256 $($Sha256.Substring(0, 12))...)."
}
