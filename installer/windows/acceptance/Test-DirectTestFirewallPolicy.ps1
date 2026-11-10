$ErrorActionPreference = 'Stop'
$policy = Join-Path (Split-Path -Parent $PSScriptRoot) 'direct-test-firewall-policy.ps1'
. $policy
$allowed = @('192.168.50.0/24', '127.0.0.0/8', '239.255.255.250/32')
$blocked = @(Get-DirectTestIPv4ComplementCidrs $allowed)
if (-not $blocked.Count) { throw 'FIREWALL_COMPLEMENT_EMPTY' }
$otherLocal = @(Get-DirectTestIPv4ComplementCidrs @('192.168.50.10/32'))
$allowedNumbers = @($allowed | ForEach-Object { Get-DirectTestIPv4Range $_ })
foreach ($cidr in $blocked) {
    $range = Get-DirectTestIPv4Range $cidr
    foreach ($allowRange in $allowedNumbers) {
        if ($range.Start -le $allowRange.End -and $allowRange.Start -le $range.End) { throw 'FIREWALL_COMPLEMENT_OVERLAPS_ALLOWLIST' }
    }
}
$blockedAddressCount = [long]0
foreach ($cidr in $blocked) { $blockedAddressCount += [long]1 -shl (32 - [int](($cidr -split '/')[-1])) }
$allowedAddressCount = [long]0
foreach ($cidr in $allowed) { $allowedAddressCount += [long]1 -shl (32 - [int](($cidr -split '/')[-1])) }
if (($blockedAddressCount + $allowedAddressCount) -ne 4294967296) { throw 'FIREWALL_COMPLEMENT_COVERAGE_MISMATCH' }
foreach ($address in @('192.168.50.10','127.0.0.1','239.255.255.250')) {
    $number = ConvertTo-DirectTestIPv4Number ([Net.IPAddress]::Parse($address))
    $matches = @($blocked | ForEach-Object { Get-DirectTestIPv4Range $_ } | Where-Object { $_.Start -le $number -and $number -le $_.End })
    if ($matches.Count) {
        throw 'FIREWALL_ALLOWED_TARGET_BLOCKED'
    }
}
foreach ($address in @('192.168.51.10','8.8.8.8','10.0.0.1','172.16.0.1','169.254.1.1')) {
    $number = ConvertTo-DirectTestIPv4Number ([Net.IPAddress]::Parse($address))
    $matches = @($blocked | ForEach-Object { Get-DirectTestIPv4Range $_ } | Where-Object { $_.Start -le $number -and $number -le $_.End })
    if (-not $matches.Count) {
        throw 'FIREWALL_UNAPPROVED_TARGET_NOT_BLOCKED'
    }
}
$pinnedAddress = ConvertTo-DirectTestIPv4Number ([Net.IPAddress]::Parse('192.168.50.10'))
$alternateAddress = ConvertTo-DirectTestIPv4Number ([Net.IPAddress]::Parse('192.168.50.11'))
$pinnedMatches = @($otherLocal | ForEach-Object { Get-DirectTestIPv4Range $_ } | Where-Object { $_.Start -le $pinnedAddress -and $pinnedAddress -le $_.End })
$alternateMatches = @($otherLocal | ForEach-Object { Get-DirectTestIPv4Range $_ } | Where-Object { $_.Start -le $alternateAddress -and $alternateAddress -le $_.End })
if ($pinnedMatches.Count -or -not $alternateMatches.Count) { throw 'FIREWALL_INTERFACE_LOCAL_ADDRESS_COMPLEMENT_INVALID' }
$overlapRejected = $false
try { Get-DirectTestIPv4ComplementCidrs @('192.168.50.0/24','192.168.50.0/25') | Out-Null } catch { $overlapRejected = $_.Exception.Message -match 'ALLOWLIST_OVERLAP' }
if (-not $overlapRejected) { throw 'FIREWALL_OVERLAPPING_ALLOWLIST_ACCEPTED' }
$canonicalRejected = $false
try { Get-DirectTestIPv4ComplementCidrs @('192.168.50.1/24') | Out-Null } catch { $canonicalRejected = $_.Exception.Message -match 'NOT_CANONICAL' }
if (-not $canonicalRejected) { throw 'FIREWALL_NONCANONICAL_NETWORK_ACCEPTED' }
Assert-DirectTestStrongHostSend -InterfaceAlias 'Ethernet' -InterfaceRows @(
    [pscustomobject]@{ InterfaceAlias='Ethernet'; WeakHostSend='Disabled' },
    [pscustomobject]@{ InterfaceAlias='Wi-Fi'; WeakHostSend='Disabled' }
) -ActiveInterfaceAliases @('Ethernet','Wi-Fi')
$weakHostRejected = $false
try { Assert-DirectTestStrongHostSend -InterfaceAlias 'Ethernet' -InterfaceRows @(
    [pscustomobject]@{ InterfaceAlias='Ethernet'; WeakHostSend='Disabled' },
    [pscustomobject]@{ InterfaceAlias='Wi-Fi'; WeakHostSend='Enabled' }
) -ActiveInterfaceAliases @('Ethernet','Wi-Fi') } catch { $weakHostRejected = $_.Exception.Message -match 'WEAK_HOST_SEND_MUST_BE_DISABLED' }
if (-not $weakHostRejected) { throw 'FIREWALL_ALTERNATE_WEAK_HOST_SEND_ACCEPTED' }
$wrongInterfaceRejected = $false
try { Assert-DirectTestStrongHostSend -InterfaceAlias 'Ethernet' -InterfaceRows @([pscustomobject]@{ InterfaceAlias='Wi-Fi'; WeakHostSend='Disabled' }) -ActiveInterfaceAliases @('Wi-Fi') } catch { $wrongInterfaceRejected = $_.Exception.Message -match 'WEAK_HOST_SEND_MUST_BE_DISABLED' }
if (-not $wrongInterfaceRejected) { throw 'FIREWALL_WRONG_INTERFACE_ACCEPTED' }
Write-Output "PASS: $($blocked.Count) destination CIDRs and the source-address complement enforce camera/interface, loopback, and ONVIF multicast policy; WeakHostSend is Disabled across all active IPv4 interfaces."
