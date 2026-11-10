function ConvertTo-DirectTestIPv4Number([Net.IPAddress]$Address) {
    $bytes = $Address.GetAddressBytes()
    if ($bytes.Length -ne 4) { throw 'DIRECT_TEST_FIREWALL_IPV4_REQUIRED' }
    return [long]$bytes[0] * 16777216 + [long]$bytes[1] * 65536 + [long]$bytes[2] * 256 + [long]$bytes[3]
}

function ConvertFrom-DirectTestIPv4Number([long]$Value) {
    if ($Value -lt 0 -or $Value -gt 4294967295) { throw 'DIRECT_TEST_FIREWALL_IPV4_RANGE_INVALID' }
    $bytes = [byte[]]@((($Value -shr 24) -band 255), (($Value -shr 16) -band 255), (($Value -shr 8) -band 255), ($Value -band 255))
    return ([Net.IPAddress]::new($bytes)).ToString()
}

function Get-DirectTestIPv4Range([string]$Cidr) {
    $parts = $Cidr.Split('/')
    if ($parts.Count -ne 2 -or $parts[1] -notmatch '^(0|[1-9][0-9]?)$') { throw 'DIRECT_TEST_FIREWALL_CIDR_INVALID' }
    try { $ip = [Net.IPAddress]::Parse($parts[0]) } catch { throw 'DIRECT_TEST_FIREWALL_CIDR_INVALID' }
    $address = ConvertTo-DirectTestIPv4Number $ip
    $prefix = [int]$parts[1]
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $prefix -lt 0 -or $prefix -gt 32) { throw 'DIRECT_TEST_FIREWALL_CIDR_INVALID' }
    $mask = if ($prefix -eq 0) { [long]0 } else { [long]4294967295 -band ([long]4294967295 -shl (32 - $prefix)) }
    $network = $address -band $mask
    if ((ConvertFrom-DirectTestIPv4Number $network) -cne $parts[0]) { throw 'DIRECT_TEST_FIREWALL_CIDR_NOT_CANONICAL' }
    $size = [long]1 -shl (32 - $prefix)
    return [pscustomobject]@{ Start = $network; End = $network + $size - 1; Cidr = $Cidr }
}

function Get-DirectTestIPv4ComplementCidrs([string[]]$AllowedCidrs) {
    $allowed = @($AllowedCidrs | ForEach-Object { Get-DirectTestIPv4Range $_ } | Sort-Object Start)
    if (-not $allowed.Count) { throw 'DIRECT_TEST_FIREWALL_ALLOWLIST_EMPTY' }
    for ($i = 1; $i -lt $allowed.Count; $i++) {
        if ($allowed[$i].Start -le $allowed[$i - 1].End) { throw 'DIRECT_TEST_FIREWALL_ALLOWLIST_OVERLAP' }
    }
    $ranges = New-Object System.Collections.Generic.List[object]
    $cursor = [long]0
    foreach ($range in $allowed) {
        if ($cursor -lt $range.Start) { $ranges.Add([pscustomobject]@{ Start = $cursor; End = $range.Start - 1 }) }
        $cursor = $range.End + 1
    }
    if ($cursor -le 4294967295) { $ranges.Add([pscustomobject]@{ Start = $cursor; End = [long]4294967295 }) }
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($range in $ranges) {
        $current = [long]$range.Start
        while ($current -le $range.End) {
            $alignmentPower = 32
            if ($current -ne 0) {
                $probe = $current
                $alignmentPower = 0
                while (($probe -band 1) -eq 0 -and $alignmentPower -lt 32) { $alignmentPower++; $probe = $probe -shr 1 }
            }
            $remaining = [long]$range.End - $current + 1
            $maximumPower = 0
            $blockSize = [long]1
            while (($blockSize * 2) -le $remaining) { $blockSize *= 2; $maximumPower++ }
            $power = [Math]::Min($alignmentPower, $maximumPower)
            $size = [long]1 -shl $power
            $prefix = 32 - $power
            $result.Add("$(ConvertFrom-DirectTestIPv4Number $current)/$prefix")
            $current += $size
        }
    }
    return [string[]]$result.ToArray()
}

function Assert-DirectTestStrongHostSend([string]$InterfaceAlias, [object[]]$InterfaceRows,
                                         [string[]]$ActiveInterfaceAliases) {
    $pinned = @($InterfaceRows | Where-Object { $_.InterfaceAlias -ceq $InterfaceAlias })
    $activeAliases = @($ActiveInterfaceAliases | Sort-Object -Unique)
    $activeRows = @($InterfaceRows | Where-Object { $activeAliases -ccontains [string]$_.InterfaceAlias })
    $activeRowAliases = @($activeRows | ForEach-Object { [string]$_.InterfaceAlias } | Sort-Object -Unique)
    if ($pinned.Count -ne 1 -or $pinned[0].WeakHostSend -cne 'Disabled' -or
        $activeAliases -notcontains $InterfaceAlias -or -not $activeAliases.Count -or
        ($activeAliases -join "`n") -cne ($activeRowAliases -join "`n") -or
        @($activeRows | Where-Object { [string]$_.WeakHostSend -cne 'Disabled' }).Count) {
        throw 'DIRECT_TEST_WEAK_HOST_SEND_MUST_BE_DISABLED'
    }
}
