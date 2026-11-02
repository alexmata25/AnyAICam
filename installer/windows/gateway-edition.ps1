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
