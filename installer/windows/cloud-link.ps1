# Cloud linking provisioning for the Windows install (2026-10-07).
#
# The Windows counterpart of installer/09-identity.sh (identity_provision,
# claim_label_provision, cloud_portal_provision, stamp_release), run by
# install-runtime.ps1 on every install, repair and upgrade:
#   config\appliance_identity.json  UUIDv4 appliance_id; created once, kept.
#   label\claim-label.txt,          RETIRED 2026-10-08 and removed: Windows
#   config\label_claim.json         1.3.0's permanent claim code and its
#                                   verifier (see "claim code" below).
#   config\agent.env                ANYAICAM_PORTAL_URL (-PortalUrl, else the
#                                   saved one, else https://app.anyaicam.com)
#                                   and ANYAICAM_AGENT_MODE=production.
#   config\vms_release.json         the installed release the agent reports.
# The agent service links the PC through a fresh, one-time link an
# administrator asks for (open-claim-link.ps1 -> windows_link.py).
param(
    [Parameter(Mandatory=$true)][string]$DataRoot,
    [Parameter(Mandatory=$true)][string]$AppVersion,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [string]$PortalUrl = ''
)
$ErrorActionPreference = 'Stop'
$DefaultPortalUrl = 'https://app.anyaicam.com'
$utf8 = [Text.UTF8Encoding]::new($false)

function Test-PortalUrl([string]$Url) {
    $uri = $null
    if ($Url -match '\s' -or -not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri)) { return $false }
    if ($uri.Scheme -ne 'https' -or -not $uri.Host) { return $false }
    $hostName = $uri.Host.ToLowerInvariant().Trim('[', ']')
    return -not ($hostName -eq 'localhost' -or $hostName -eq '0.0.0.0' -or $hostName -eq '::1' -or $hostName.StartsWith('127.'))
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
New-Item -ItemType Directory -Force -Path $config | Out-Null

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

# ------------------------------------------------------------ claim code (retired)
# Windows 1.3.0 kept a permanent claim code in label\claim-label.txt and its
# verifier in config\label_claim.json: valid until the PC was linked, so
# whoever learned it (finish page, Start menu, a backup) could claim an
# unlinked PC. A Windows PC is now linked only through a fresh, one-time link
# an administrator asks for, and the agent sends no label (on-demand claims),
# so an upgrade removes both. A label claim 1.3.0 left open on the cloud
# expires within 15 minutes and is not reopened.
foreach ($retired in @((Join-Path $labelDir 'claim-label.txt'), (Join-Path $config 'label_claim.json'))) {
    if (Test-Path -LiteralPath $retired) { Remove-Item -LiteralPath $retired -Force }
}
if ((Test-Path -LiteralPath $labelDir) -and -not (Get-ChildItem -LiteralPath $labelDir -Force)) { Remove-Item -LiteralPath $labelDir -Force }

# ------------------------------------------------------------ installed release
$release = [ordered]@{ release_version = $AppVersion; installer_version = $AppVersion; vms_release_commit = $SourceCommit; platform = 'windows'; installed_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
Write-TextFile (Join-Path $config 'vms_release.json') @(($release | ConvertTo-Json))
'Cloud linking provisioned.'
