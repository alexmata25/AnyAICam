# Shared VMS editions: preserve saved choices on repair/upgrade.
function Set-GatewayEdition {
    param($Values, [string]$Edition = '')
    $valid = @('full_vms', 'local_gateway', 'cloud_gateway')
    $saved = [string]$Values['ANYAICAM_GATEWAY_EDITION']
    if ($saved -and $saved -cnotin $valid) { throw 'Invalid saved Gateway edition; fix it before installing.' }
    if ($Edition -and $Edition -cnotin $valid) { throw 'The edition must be full_vms, local_gateway or cloud_gateway.' }
    if ($saved -and $Edition -and $saved -cne $Edition) { throw 'Setup cannot switch the saved Gateway edition.' }
    if ($Edition) { $Values['ANYAICAM_GATEWAY_EDITION'] = $Edition }
    # No selection on an existing install means keep its original defaults.
    # Do not write upload flags: changing edition must never enable transport.
}

function Resolve-GatewayProductMode {
    param([string]$Edition, [string]$ProductMode, [bool]$ExistingInstall)
    if ($ExistingInstall -or $Edition -notin @('local_gateway', 'cloud_gateway')) { return $ProductMode }
    $expected = if ($Edition -eq 'local_gateway') { 'local' } else { 'hybrid' }
    if ($ProductMode -and $ProductMode -cne $expected) { throw 'The requested product mode conflicts with the Gateway edition.' }
    return $expected
}

function Assert-GatewayRuntimeConfiguration([string]$Edition, [string]$RuntimeRole) {
    $choice = $Edition.Trim().ToLowerInvariant()
    if (-not $choice) { $choice = 'full_vms' }
    if ($choice -notin @('full_vms','local_gateway','cloud_gateway')) { throw 'Invalid Gateway edition in service configuration.' }
    if ($choice -ne 'full_vms' -and $RuntimeRole.Trim().ToLowerInvariant() -ne 'edge') {
        throw 'The Windows Gateway service requires ANYAICAM_RUNTIME_ROLE=edge.'
    }
}
