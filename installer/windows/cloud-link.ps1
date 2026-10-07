# Cloud linking provisioning for the Windows install (2026-10-07).
#
# The Windows counterpart of installer/09-identity.sh (identity_provision,
# claim_label_provision, cloud_portal_provision, stamp_release), run by
# install-runtime.ps1 on every install, repair and upgrade:
#   config\appliance_identity.json  UUIDv4 appliance_id; created once, kept.
#   label\claim-label.txt           the claim code (12-char Crockford base32,
#                                   60 bits) in plaintext; created once, kept,
#                                   so a code already shown or printed stays
#                                   valid. The data folder is SYSTEM/
#                                   Administrators-only.
#   config\label_claim.json         only the verifier the agent sends,
#                                   sha256("anyaicam-label-claim-v1:"+code),
#                                   rewritten from the label file every run.
#   config\agent.env                ANYAICAM_PORTAL_URL (-PortalUrl, else the
#                                   saved one, else https://app.anyaicam.com)
#                                   and ANYAICAM_AGENT_MODE=production.
#   config\vms_release.json         the installed release the agent reports.
# The agent service (headless_claim.py) claims the PC with the verifier once
# the owner enters the code at <portal>/claim. Never logged: the code or its
# verifier.
param(
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$AppVersion,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [string]$PortalUrl = ''
)
$ErrorActionPreference = 'Stop'
$DefaultPortalUrl = 'https://app.anyaicam.com'
$Alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'  # Crockford base32: no I, L, O, U
$utf8 = [Text.UTF8Encoding]::new($false)

function Test-PortalUrl([string]$Url) {
    $uri = $null
    if ($Url -match '\s' -or -not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri)) { return $false }
    if ($uri.Scheme -ne 'https' -or -not $uri.Host) { return $false }
    $hostName = $uri.Host.ToLowerInvariant().Trim('[', ']')
    return -not ($hostName -eq 'localhost' -or $hostName -eq '0.0.0.0' -or $hostName -eq '::1' -or $hostName.StartsWith('127.'))
}

function New-LabelCode {
    # 12 bytes, each mod 32: 256 is a multiple of 32, so every character is uniform (60 bits).
    $bytes = New-Object byte[] 12
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return -join ($bytes | ForEach-Object { $Alphabet[$_ % 32] })
}

function Test-LabelCode([string]$Code) { return $Code -cmatch '^[0-9ABCDEFGHJKMNPQRSTVWXYZ]{12}$' }

function Format-LabelCode([string]$Code) { return '{0}-{1}-{2}' -f $Code.Substring(0, 4), $Code.Substring(4, 4), $Code.Substring(8, 4) }

function Get-LabelVerifier([string]$Code) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($utf8.GetBytes('anyaicam-label-claim-v1:' + $Code)) } finally { $sha.Dispose() }
    return -join ($hash | ForEach-Object { $_.ToString('x2') })
}

function Write-TextFile([string]$Path, [string[]]$Lines) {
    $temporary = "$Path.tmp"
    [IO.File]::WriteAllLines($temporary, $Lines, $utf8)
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Read-EnvFile([string]$Path) {
    $values = [ordered]@{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if (-not $line -or $line.TrimStart().StartsWith('#')) { continue }
            $parts = $line.Split('=', 2)
            if ($parts.Count -eq 2) { $values[$parts[0]] = $parts[1] }
        }
    }
    return $values
}

if ($PortalUrl -and -not (Test-PortalUrl $PortalUrl)) {
    throw "PortalUrl must be the AnyAiCam cloud's https:// address, for example $DefaultPortalUrl."
}
$config = Join-Path $DataRoot 'config'
$labelDir = Join-Path $DataRoot 'label'
New-Item -ItemType Directory -Force -Path $config, $labelDir | Out-Null

# ------------------------------------------------------------ identity
$identityFile = Join-Path $config 'appliance_identity.json'
$applianceId = ''
if (Test-Path -LiteralPath $identityFile) {
    try { $applianceId = [string](Get-Content -Raw -LiteralPath $identityFile | ConvertFrom-Json).appliance_id } catch { $applianceId = '' }
}
if ($applianceId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
    if (Test-Path -LiteralPath $identityFile) { throw "$identityFile exists but has no valid appliance_id; it is kept as it is (contact AnyAiCam support)." }
    $applianceId = [guid]::NewGuid().ToString().ToLowerInvariant()  # random (version 4)
    $identity = [ordered]@{ appliance_id = $applianceId; installer_version = $AppVersion; installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
    Write-TextFile $identityFile @(($identity | ConvertTo-Json))
}

# ------------------------------------------------------------ cloud portal
$agentEnvFile = Join-Path $config 'agent.env'
$agentEnv = Read-EnvFile $agentEnvFile
if ($PortalUrl) { $agentEnv['ANYAICAM_PORTAL_URL'] = $PortalUrl.TrimEnd('/') }
elseif (-not (Test-PortalUrl ([string]$agentEnv['ANYAICAM_PORTAL_URL']))) { $agentEnv['ANYAICAM_PORTAL_URL'] = $DefaultPortalUrl }
$agentEnv['ANYAICAM_AGENT_MODE'] = 'production'
$portal = $agentEnv['ANYAICAM_PORTAL_URL']
Write-TextFile $agentEnvFile (@('# Managed by the AnyAiCam Windows installer.') + @($agentEnv.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }))

# ------------------------------------------------------------ claim code
$labelFile = Join-Path $labelDir 'claim-label.txt'
$code = ''
if (Test-Path -LiteralPath $labelFile) {
    $saved = Get-Content -LiteralPath $labelFile | Where-Object { $_ -like 'Claim code:*' } | Select-Object -First 1
    if ($saved) { $code = ($saved -replace '^Claim code:\s*', '' -replace '[\s-]', '').ToUpperInvariant() }
}
if (-not (Test-LabelCode $code)) {
    $code = New-LabelCode
    Write-TextFile $labelFile @(
        'AnyAiCam claim code for this computer. Keep private: whoever has it can link this computer to their account.',
        "Claim code: $(Format-LabelCode $code)",
        "QR code: $($portal.TrimEnd('/'))/claim#label=$(Format-LabelCode $code)",
        "Appliance ID: $applianceId",
        "Created: $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    )
}
Write-TextFile (Join-Path $config 'label_claim.json') @("{`"version`": 1, `"verifier`": `"$(Get-LabelVerifier $code)`"}")

# ------------------------------------------------------------ installed release
$release = [ordered]@{ release_version = $AppVersion; installer_version = $AppVersion; vms_release_commit = $SourceCommit; platform = 'windows'; installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
Write-TextFile (Join-Path $config 'vms_release.json') @(($release | ConvertTo-Json))
'Cloud linking provisioned.'
