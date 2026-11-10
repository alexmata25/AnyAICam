[CmdletBinding(DefaultParameterSetName='Apply')]
param(
    [Parameter(Mandatory=$true, ParameterSetName='Apply')][switch]$Apply,
    [Parameter(Mandatory=$true, ParameterSetName='Remove')][switch]$Remove,
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')][string]$RunId
)
$ErrorActionPreference = 'Stop'
$expected = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'AnyAiCam VMS Direct Test')).TrimEnd('\')
$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($root -ine $expected) { throw 'DIRECT_TEST_FIREWALL_INSTALL_ROOT_REFUSED' }
if (-not (Get-Command Get-NetFirewallRule -ErrorAction SilentlyContinue) -or -not (Get-Command New-NetFirewallRule -ErrorAction SilentlyContinue) -or -not (Get-Command Remove-NetFirewallRule -ErrorAction SilentlyContinue)) {
    throw 'DIRECT_TEST_FIREWALL_CMDLETS_UNAVAILABLE'
}
$group = "AnyAiCam VMS Direct Test $RunId"
$profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
if (-not $profiles.Count -or @($profiles | Where-Object { -not $_.Enabled }).Count) { throw 'DIRECT_TEST_FIREWALL_PROFILE_NOT_ENABLED' }

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
function Get-RuleName([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $nameHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Path.ToLowerInvariant())))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    return 'AAC-DirectTest-Out-' + $RunId.Replace('-', '') + '-' + $nameHash.Substring(0, 16)
}
function Assert-BlockedRule($Rule, [string]$ExpectedPath) {
    $app = $Rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $address = $Rule | Get-NetFirewallAddressFilter -ErrorAction Stop
    if ($Rule.Name -cne (Get-RuleName $ExpectedPath) -or $Rule.Group -cne $group -or
        $Rule.Enabled -ne 'True' -or $Rule.Direction -ne 'Outbound' -or $Rule.Action -ne 'Block' -or
        [IO.Path]::GetFullPath([string]$app.Program) -ine $ExpectedPath -or $address.RemoteAddress -notcontains 'Any') {
        throw 'DIRECT_TEST_FIREWALL_RULE_VERIFICATION_FAILED'
    }
}
function Test-BlockedRule($Rule, [string]$ExpectedPath) {
    try { Assert-BlockedRule $Rule $ExpectedPath; return $true } catch { return $false }
}

if ($Apply) {
    if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue).Count) { throw 'DIRECT_TEST_FIREWALL_RULE_GROUP_ALREADY_EXISTS' }
    $created = New-Object System.Collections.Generic.List[object]
    $attempted = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($exe in Get-DirectTestExecutables) {
            $path = [IO.Path]::GetFullPath($exe.FullName)
            $name = Get-RuleName $path
            if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Name $name -ErrorAction SilentlyContinue).Count -or
                @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $name -ErrorAction SilentlyContinue).Count) {
                throw 'DIRECT_TEST_FIREWALL_RULE_NAME_COLLISION'
            }
            $record = [pscustomobject]@{ Name = $name; Path = $path }
            $attempted.Add($record)
            New-NetFirewallRule -Name $name -DisplayName "AnyAiCam isolated test outbound block ($($exe.Name))" -Group $group `
                -Direction Outbound -Action Block -Program $path -Protocol Any -RemoteAddress Any -Profile Any -Enabled True -ErrorAction Stop | Out-Null
            $created.Add($record)
        }
        $rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $group -ErrorAction Stop)
        $expectedExecutables = @(Get-DirectTestExecutables)
        if ($rules.Count -ne $expectedExecutables.Count) { throw 'DIRECT_TEST_FIREWALL_RULE_COUNT_MISMATCH' }
        $persistentNames = @((Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction Stop) | ForEach-Object Name | Sort-Object)
        $activeNames = @($rules | ForEach-Object Name | Sort-Object)
        if (($persistentNames -join "`n") -cne ($activeNames -join "`n")) { throw 'DIRECT_TEST_FIREWALL_RULE_NOT_EFFECTIVE' }
        foreach ($rule in $rules) {
            $matching = @($expectedExecutables | Where-Object { (Get-RuleName ([IO.Path]::GetFullPath($_.FullName))) -ceq $rule.Name })
            if ($matching.Count -ne 1) { throw 'DIRECT_TEST_FIREWALL_RULE_PATH_UNMATCHED' }
            Assert-BlockedRule $rule ([IO.Path]::GetFullPath($matching[0].FullName))
        }
    } catch {
        $primaryFailure = $_
        $cleanupUnconfirmed = $false
        foreach ($record in $created) {
            $persistent = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $record.Name -ErrorAction SilentlyContinue)
            $active = @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $record.Name -ErrorAction SilentlyContinue)
            if ($persistent.Count -eq 1 -and $active.Count -eq 1 -and
                (Test-BlockedRule $persistent[0] $record.Path) -and (Test-BlockedRule $active[0] $record.Path)) {
                try { Remove-NetFirewallRule -PolicyStore PersistentStore -Name $record.Name -ErrorAction Stop }
                catch { $cleanupUnconfirmed = $true }
            } else { $cleanupUnconfirmed = $true }
        }
        foreach ($record in $attempted | Where-Object { $_.Name -notin @($created | ForEach-Object Name) }) {
            if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Name $record.Name -ErrorAction SilentlyContinue).Count -or
                @(Get-NetFirewallRule -PolicyStore ActiveStore -Name $record.Name -ErrorAction SilentlyContinue).Count) {
                # New-NetFirewallRule failed after its attempt. Without a successful return, ownership cannot be
                # established; preserve the entry and surface the cleanup blocker instead of deleting by name.
                $cleanupUnconfirmed = $true
            }
        }
        $remainingPersistent = @(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue)
        $remainingActive = @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $group -ErrorAction SilentlyContinue)
        if ($cleanupUnconfirmed -or $remainingPersistent.Count -or $remainingActive.Count) {
            throw "DIRECT_TEST_FIREWALL_PARTIAL_CLEANUP_UNCONFIRMED; primary=$($primaryFailure.Exception.GetType().Name); persistent=$($remainingPersistent.Count); active=$($remainingActive.Count)"
        }
        throw $primaryFailure
    }
    Write-Output "Installed and verified $($created.Count) run-scoped outbound block rules; no allow rules were added."
} else {
    $rules = @(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue)
    $expectedNames = @(Get-DirectTestExecutables | ForEach-Object { Get-RuleName ([IO.Path]::GetFullPath($_.FullName)) })
    foreach ($rule in $rules) {
        if ($rule.Name -notin $expectedNames) { throw 'DIRECT_TEST_FIREWALL_REMOVE_NAME_REFUSED' }
        $matching = @(Get-DirectTestExecutables | Where-Object { (Get-RuleName ([IO.Path]::GetFullPath($_.FullName))) -ceq $rule.Name })
        if ($matching.Count -ne 1) { throw 'DIRECT_TEST_FIREWALL_REMOVE_PATH_REFUSED' }
        Assert-BlockedRule $rule ([IO.Path]::GetFullPath($matching[0].FullName))
    }
    foreach ($rule in $rules) {
        Remove-NetFirewallRule -Name $rule.Name -ErrorAction Stop
    }
    if (@(Get-NetFirewallRule -PolicyStore PersistentStore -Group $group -ErrorAction SilentlyContinue).Count -or
        @(Get-NetFirewallRule -PolicyStore ActiveStore -Group $group -ErrorAction SilentlyContinue).Count) { throw 'DIRECT_TEST_FIREWALL_REMOVE_UNCONFIRMED' }
    Write-Output "Removed only $($rules.Count) outbound rules for run $RunId."
}
