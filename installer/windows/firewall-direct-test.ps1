[CmdletBinding(DefaultParameterSetName='Apply')]
param(
    [Parameter(Mandatory=$true, ParameterSetName='Apply')][switch]$Apply,
    [Parameter(Mandatory=$true, ParameterSetName='Remove')][switch]$Remove,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')][string]$RunId,
    [Parameter(Mandatory=$true)][string]$CameraNetwork
)
$ErrorActionPreference = 'Stop'
$expected = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'AnyAiCam VMS Direct Test')).TrimEnd('\')
$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($root -ine $expected) { throw 'DIRECT_TEST_FIREWALL_INSTALL_ROOT_REFUSED' }
if (-not (Get-Command Get-NetFirewallRule -ErrorAction SilentlyContinue) -or -not (Get-Command New-NetFirewallRule -ErrorAction SilentlyContinue) -or -not (Get-Command Remove-NetFirewallRule -ErrorAction SilentlyContinue)) {
    throw 'DIRECT_TEST_FIREWALL_CMDLETS_UNAVAILABLE'
}
. (Join-Path $PSScriptRoot 'direct-test-firewall-policy.ps1')
$profilePath = Join-Path $root 'direct-test-profile.json'
if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf) -or
    ((Get-Item -LiteralPath $profilePath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'DIRECT_TEST_FIREWALL_PROFILE_INVALID' }
$profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
if ($profile.schema -ne 1 -or $profile.run_id -cne $RunId -or $profile.approved_camera_network -cne $CameraNetwork -or
    $profile.camera_interface_alias -isnot [string] -or -not $profile.camera_interface_alias -or
    $profile.camera_interface_address -notmatch '^(10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)') {
    throw 'DIRECT_TEST_FIREWALL_PROFILE_BINDING_REFUSED'
}
$cameraInterfaceAddress = [string]$profile.camera_interface_address
$cameraInterfaceAlias = [string]$profile.camera_interface_alias
$cameraRange = Get-DirectTestIPv4Range $CameraNetwork
$attachedRange = Get-DirectTestIPv4Range ([string]$profile.camera_interface_network)
$interfaceNumber = ConvertTo-DirectTestIPv4Number ([Net.IPAddress]::Parse($cameraInterfaceAddress))
if ($interfaceNumber -lt $attachedRange.Start -or $interfaceNumber -gt $attachedRange.End -or
    $cameraRange.Start -lt $attachedRange.Start -or $cameraRange.End -gt $attachedRange.End) {
    throw 'DIRECT_TEST_FIREWALL_PROFILE_INTERFACE_SUBNET_MISMATCH'
}
$cameraBytes = ([Net.IPAddress]::Parse(($CameraNetwork -split '/')[0])).GetAddressBytes()
$isRfc1918 = ($cameraBytes[0] -eq 10) -or ($cameraBytes[0] -eq 172 -and $cameraBytes[1] -ge 16 -and $cameraBytes[1] -le 31) -or ($cameraBytes[0] -eq 192 -and $cameraBytes[1] -eq 168)
if (-not $isRfc1918 -or [int](($CameraNetwork -split '/')[-1]) -lt 24) { throw 'DIRECT_TEST_FIREWALL_CAMERA_NETWORK_REFUSED' }
$allowedV4 = @($CameraNetwork, '127.0.0.0/8', '239.255.255.250/32') | Sort-Object
$blockedV4 = @(Get-DirectTestIPv4ComplementCidrs $allowedV4)
$otherLocalV4 = @(Get-DirectTestIPv4ComplementCidrs @("$cameraInterfaceAddress/32"))
$group = "AnyAiCam VMS Direct Test $RunId"
$profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
$defaultOutboundActions = @($profiles | ForEach-Object { [string]$_.DefaultOutboundAction } | Sort-Object -Unique)

function Get-DirectTestExecutables {
    $items = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -Filter '*.exe' -ErrorAction Stop)
    if (-not $items.Count) { throw 'DIRECT_TEST_FIREWALL_EXECUTABLE_INVENTORY_EMPTY' }
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'DIRECT_TEST_FIREWALL_REPARSE_EXECUTABLE_REFUSED' }
        $full = [IO.Path]::GetFullPath($item.FullName)
        if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'DIRECT_TEST_FIREWALL_EXECUTABLE_PATH_ESCAPE' }
    }
    return $items | Sort-Object FullName
}
function Get-RuleName([string]$Path, [string]$Scope) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $nameHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Path.ToLowerInvariant())))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return 'AAC-DirectTest-Out-' + $RunId.Replace('-', '') + '-' + $nameHash.Substring(0, 16) + '-' + $Scope
}
function Get-RuleSpec([string]$Path, [string]$Scope) {
    if ($Scope -ceq 'BlockIPv4OutsideAllowlist') { return [pscustomobject]@{ Name = (Get-RuleName $Path $Scope); Path = $Path; Scope = $Scope; Action = 'Block'; Addresses = $blockedV4; LocalAddresses = @('Any'); InterfaceAliases = @('Any') } }
    if ($Scope -ceq 'BlockAllIPv6') { return [pscustomobject]@{ Name = (Get-RuleName $Path $Scope); Path = $Path; Scope = $Scope; Action = 'Block'; Addresses = @('::/0'); LocalAddresses = @('Any'); InterfaceAliases = @('Any') } }
    if ($Scope -ceq 'BlockCameraFromOtherLocalIPv4') { return [pscustomobject]@{ Name = (Get-RuleName $Path $Scope); Path = $Path; Scope = $Scope; Action = 'Block'; Addresses = @($CameraNetwork); LocalAddresses = $otherLocalV4; InterfaceAliases = @('Any') } }
    if ($Scope -ceq 'AllowCameraOnPinnedInterface') { return [pscustomobject]@{ Name = (Get-RuleName $Path $Scope); Path = $Path; Scope = $Scope; Action = 'Allow'; Addresses = @($CameraNetwork); LocalAddresses = @($cameraInterfaceAddress); InterfaceAliases = @($cameraInterfaceAlias) } }
    if ($Scope -ceq 'AllowLoopback') { return [pscustomobject]@{ Name = (Get-RuleName $Path $Scope); Path = $Path; Scope = $Scope; Action = 'Allow'; Addresses = @('127.0.0.0/8'); LocalAddresses = @('127.0.0.0/8'); InterfaceAliases = @('Any') } }
    if ($Scope -ceq 'AllowOnvifMulticastOnPinnedInterface') { return [pscustomobject]@{ Name = (Get-RuleName $Path $Scope); Path = $Path; Scope = $Scope; Action = 'Allow'; Addresses = @('239.255.255.250/32'); LocalAddresses = @($cameraInterfaceAddress); InterfaceAliases = @($cameraInterfaceAlias) } }
    throw 'DIRECT_TEST_FIREWALL_SCOPE_UNKNOWN'
}
function Get-ExpectedSpecs {
    $specs = New-Object System.Collections.Generic.List[object]
    foreach ($exe in Get-DirectTestExecutables) {
        $path = [IO.Path]::GetFullPath($exe.FullName)
        foreach ($scope in @('BlockIPv4OutsideAllowlist','BlockAllIPv6','BlockCameraFromOtherLocalIPv4','AllowCameraOnPinnedInterface','AllowLoopback','AllowOnvifMulticastOnPinnedInterface')) { $specs.Add((Get-RuleSpec $path $scope)) }
    }
    return [object[]]$specs.ToArray()
}
function Assert-Rule($Rule, $Spec) {
    $app = $Rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $address = $Rule | Get-NetFirewallAddressFilter -ErrorAction Stop
    $ports = $Rule | Get-NetFirewallPortFilter -ErrorAction Stop
    $interface = $Rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop
    $actual = @($address.RemoteAddress | ForEach-Object { [string]$_ } | Sort-Object)
    $expectedAddresses = @($Spec.Addresses | ForEach-Object { [string]$_ } | Sort-Object)
    $actualLocal = @($address.LocalAddress | ForEach-Object { [string]$_ } | Sort-Object)
    $expectedLocal = @($Spec.LocalAddresses | ForEach-Object { [string]$_ } | Sort-Object)
    $actualInterfaces = @($interface.InterfaceAlias | ForEach-Object { [string]$_ } | Sort-Object)
    $expectedInterfaces = @($Spec.InterfaceAliases | ForEach-Object { [string]$_ } | Sort-Object)
    if ($Rule.Name -cne $Spec.Name -or $Rule.Group -cne $group -or $Rule.Enabled -ne 'True' -or
        $Rule.Direction -ne 'Outbound' -or $Rule.Action -cne $Spec.Action -or
        [IO.Path]::GetFullPath([string]$app.Program) -ine $Spec.Path -or
        [string]$ports.Protocol -cne 'Any' -or [string]$ports.LocalPort -cne 'Any' -or
        ($actualLocal -join [Environment]::NewLine) -cne ($expectedLocal -join [Environment]::NewLine) -or
        ($actualInterfaces -join [Environment]::NewLine) -cne ($expectedInterfaces -join [Environment]::NewLine) -or
        [string]$ports.RemotePort -cne 'Any' -or [string]$Rule.Profile -cne 'Any' -or
        ($actual -join "`n") -cne ($expectedAddresses -join "`n")) { throw 'DIRECT_TEST_FIREWALL_RULE_VERIFICATION_FAILED' }
}
function Test-Rule($Rule, $Spec) { try { Assert-Rule $Rule $Spec; return $true } catch { return $false } }

if ($Apply) {
    if (-not $profiles.Count -or @($profiles | Where-Object { -not $_.Enabled }).Count) { throw 'DIRECT_TEST_FIREWALL_PROFILE_NOT_ENABLED' }
    if (-not $defaultOutboundActions.Count -or @($defaultOutboundActions | Where-Object { $_ -notin @('Allow','Block') }).Count) {
        throw 'DIRECT_TEST_FIREWALL_DEFAULT_OUTBOUND_ACTION_UNVERIFIED'
    }
    $interfaceAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
        $_.AddressState -eq 'Preferred' -and $_.IPAddress -eq $cameraInterfaceAddress -and $_.InterfaceAlias -eq $cameraInterfaceAlias
    })
    $activeAdapter = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {
        $_.InterfaceAlias -eq $cameraInterfaceAlias -and $_.Status -eq 'Up'
    })
    if ($interfaceAddresses.Count -ne 1 -or $activeAdapter.Count -ne 1) { throw 'DIRECT_TEST_FIREWALL_INTERFACE_NOT_ACTIVE' }
    $upAdapters = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object { $_.Status -eq 'Up' })
    $upInterfaceAliases = @($upAdapters | ForEach-Object { [string]$_.InterfaceAlias } | Sort-Object -Unique)
    $ipInterfaces = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $upInterfaceAliases -contains $_.InterfaceAlias })
    Assert-DirectTestStrongHostSend -InterfaceAlias $cameraInterfaceAlias -InterfaceRows $ipInterfaces -ActiveInterfaceAliases $upInterfaceAliases
    if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue).Count) { throw 'DIRECT_TEST_FIREWALL_RULE_GROUP_ALREADY_EXISTS' }
    $created = New-Object System.Collections.Generic.List[object]
    $attempted = New-Object System.Collections.Generic.List[object]
    try {
        $specs = @(Get-ExpectedSpecs)
        foreach ($spec in $specs) {
            if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Name $spec.Name -ErrorAction SilentlyContinue).Count -or
                @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $spec.Name -ErrorAction SilentlyContinue).Count) { throw 'DIRECT_TEST_FIREWALL_RULE_NAME_COLLISION' }
            $attempted.Add($spec)
            New-NetFirewallRule -Name $spec.Name -DisplayName "AnyAiCam isolated network $($spec.Scope)" -Group $group `
                -Direction Outbound -Action $spec.Action -Program $spec.Path -Protocol Any -RemoteAddress $spec.Addresses `
                -LocalAddress $spec.LocalAddresses -InterfaceAlias $spec.InterfaceAliases -Profile Any -Enabled True -ErrorAction Stop | Out-Null
            $created.Add($spec)
        }
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $group -ErrorAction Stop)
        if ($rules.Count -ne $specs.Count) { throw 'DIRECT_TEST_FIREWALL_RULE_COUNT_MISMATCH' }
        $persistentNames = @((Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction Stop) | ForEach-Object Name | Sort-Object)
        $activeNames = @($rules | ForEach-Object Name | Sort-Object)
        if (($persistentNames -join "`n") -cne ($activeNames -join "`n")) { throw 'DIRECT_TEST_FIREWALL_RULE_NOT_EFFECTIVE' }
        foreach ($rule in $rules) {
            $matching = @($specs | Where-Object { $_.Name -ceq $rule.Name })
            if ($matching.Count -ne 1) { throw 'DIRECT_TEST_FIREWALL_RULE_PATH_UNMATCHED' }
            Assert-Rule $rule $matching[0]
        }
    } catch {
        $primaryFailure = $_
        $cleanupUnconfirmed = $false
        foreach ($spec in $created) {
            $persistent = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $spec.Name -ErrorAction SilentlyContinue)
            $active = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $spec.Name -ErrorAction SilentlyContinue)
            if ($persistent.Count -eq 1 -and $active.Count -eq 1 -and (Test-Rule $persistent[0] $spec) -and (Test-Rule $active[0] $spec)) {
                try { Remove-NetFirewallRule -PolicyStore PersistentStore -Name $spec.Name -ErrorAction Stop } catch { $cleanupUnconfirmed = $true }
            } else { $cleanupUnconfirmed = $true }
        }
        foreach ($spec in $attempted | Where-Object { $_.Name -notin @($created | ForEach-Object Name) }) {
            if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Name $spec.Name -ErrorAction SilentlyContinue).Count -or
                @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $spec.Name -ErrorAction SilentlyContinue).Count) { $cleanupUnconfirmed = $true }
        }
        $remainingPersistent = @(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue)
        $remainingActive = @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $group -ErrorAction SilentlyContinue)
        if ($cleanupUnconfirmed -or $remainingPersistent.Count -or $remainingActive.Count) {
            throw "DIRECT_TEST_FIREWALL_PARTIAL_CLEANUP_UNCONFIRMED; primary=$($primaryFailure.Exception.GetType().Name); persistent=$($remainingPersistent.Count); active=$($remainingActive.Count)"
        }
        throw $primaryFailure
    }
    Write-Output "Installed and verified $($created.Count) run-scoped per-executable rules. Default outbound action: $($defaultOutboundActions -join ','). WeakHostSend is Disabled on all active IPv4 interfaces. Camera-subnet and ONVIF multicast egress require the recorded interface and local address; only loopback is allowed locally; all other IPv4 and all IPv6 destinations are blocked."
} else {
    $specs = @(Get-ExpectedSpecs)
    $rules = @(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue)
    if ($rules.Count -ne $specs.Count) { throw 'DIRECT_TEST_FIREWALL_REMOVE_INVENTORY_MISMATCH' }
    foreach ($rule in $rules) {
        $matching = @($specs | Where-Object { $_.Name -ceq $rule.Name })
        if ($matching.Count -ne 1) { throw 'DIRECT_TEST_FIREWALL_REMOVE_NAME_REFUSED' }
        Assert-Rule $rule $matching[0]
        $active = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $rule.Name -ErrorAction SilentlyContinue)
        if ($active.Count -ne 1) { throw 'DIRECT_TEST_FIREWALL_REMOVE_ACTIVE_RULE_UNCONFIRMED' }
        Assert-Rule $active[0] $matching[0]
    }
    foreach ($rule in $rules) { Remove-NetFirewallRule -PolicyStore PersistentStore -Name $rule.Name -ErrorAction Stop }
    if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue).Count -or
        @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $group -ErrorAction SilentlyContinue).Count) { throw 'DIRECT_TEST_FIREWALL_REMOVE_UNCONFIRMED' }
    Write-Output "Removed only $($rules.Count) run-scoped outbound rules for run $RunId."
}
