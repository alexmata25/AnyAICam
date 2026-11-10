<#
Windows 1.3.4.1 acceptance A6 (lifecycle): compares two Get-AnyAiCamInstallState.ps1 snapshots against the rules of
one lifecycle step. See LIFECYCLE-ACCEPTANCE-CHECKLIST.md for the full procedure.

  .\Compare-AnyAiCamInstallState.ps1 -Before 01.json -After 02.json -Step Upgrade -ExpectedVersion 1.3.4.1
  Steps: Upgrade (from the accepted signed 1.3.4), Repair (same setup re-run), Uninstall.
Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED (a snapshot could not read the data folder: run the snapshots elevated).
#>
param([Parameter(Mandatory=$true)][string]$Before, [Parameter(Mandatory=$true)][string]$After,
      [Parameter(Mandatory=$true)][ValidateSet('Upgrade', 'Repair', 'Uninstall')][string]$Step,
      [string]$ExpectedVersion = '', [string]$OutputPath = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'acceptance-common.ps1')
$b = Get-Content -Raw -LiteralPath $Before | ConvertFrom-Json
$a = Get-Content -Raw -LiteralPath $After | ConvertFrom-Json
$result = New-AcceptanceResult "A6-lifecycle-$($Step.ToLowerInvariant())" $Step
if (-not $OutputPath) { $OutputPath = [IO.Path]::ChangeExtension($After, ".compare-$($Step.ToLowerInvariant()).json") }
foreach ($field in 'computer', 'install_root', 'data_root') {
    if ([string]::IsNullOrWhiteSpace([string]$b.$field) -or [string]::IsNullOrWhiteSpace([string]$a.$field)) {
        $final = Complete-AcceptanceResult $result $OutputPath 'snapshot target identity is incomplete; take new snapshots with the current collector'
        "A6 BLOCKED: $($final.blocked_reason)"; exit 2
    }
    if ([string]$b.$field -ine [string]$a.$field) {
        $final = Complete-AcceptanceResult $result $OutputPath "snapshots describe different targets ($field)"
        "A6 BLOCKED: $($final.blocked_reason)"; exit 2
    }
}
if (-not $b.data.readable -or -not $a.data.readable) {
    $final = Complete-AcceptanceResult $result $OutputPath 'a snapshot could not read the data folder (take snapshots elevated)'
    "A6 BLOCKED: $($final.blocked_reason)"; exit 2
}
function Props($o) { if ($null -eq $o) { return @{} }; $h = @{}; foreach ($p in $o.PSObject.Properties) { $h[$p.Name] = $p.Value }; return $h }
function Check([string]$Name, [bool]$Passed, [string]$Detail = '') { Add-AcceptanceCheck $result $Name $Passed $Detail }

# The collector uses this sentinel for existing files it cannot hash. Never
# mistake matching sentinels, or a truthy sentinel, for installed-file evidence.
$unreadable = $false
if ($Step -eq 'Repair') {
    $beforeScripts = Props $b.scripts; $afterScripts = Props $a.scripts
    $unreadable = @($beforeScripts.Values + $afterScripts.Values | Where-Object { $_ -eq 'unreadable' }).Count -gt 0
    $unreadable = $unreadable -or $b.tesseract_manifest -eq 'unreadable'
}
if ($Step -in @('Upgrade', 'Repair')) {
    $unreadable = $unreadable -or $a.tesseract_manifest -eq 'unreadable' -or $a.runtime_catalog -eq 'unreadable'
}
if ($unreadable) {
    $final = Complete-AcceptanceResult $result $OutputPath 'a required installed file could not be hashed (take new snapshots with sufficient access)'
    "A6 BLOCKED: $($final.blocked_reason)"; exit 2
}

# Customer data must survive every step, uninstall included (the .iss marks ProgramData folders uninsneveruninstall).
$bi = Props $b.data.identity_sha256; $ai = Props $a.data.identity_sha256
if (@($bi.Values + $ai.Values | Where-Object { $_ -eq 'unreadable' }).Count -gt 0) {
    $final = Complete-AcceptanceResult $result $OutputPath 'an identity file could not be hashed (take snapshots elevated and resolve file access)'
    "A6 BLOCKED: $($final.blocked_reason)"; exit 2
}
foreach ($k in $bi.Keys) { Check "identity file unchanged: $k" ($ai[$k] -eq $bi[$k]) "" }
if ($null -eq $b.data.recordings.bytes -or $null -eq $a.data.recordings.bytes) {
    $final = Complete-AcceptanceResult $result $OutputPath 'recording byte totals are missing; take new snapshots with the current collector'
    "A6 BLOCKED: $($final.blocked_reason)"; exit 2
}
Check 'recordings kept (count not lower)' ($a.data.recordings.count -ge $b.data.recordings.count) "$($b.data.recordings.count) -> $($a.data.recordings.count)"
Check 'recordings kept (bytes not lower)' ($a.data.recordings.bytes -ge $b.data.recordings.bytes) "$($b.data.recordings.bytes) -> $($a.data.recordings.bytes)"
Check 'database kept' ($null -eq $b.data.database -or $null -ne $a.data.database) ''
$missingKeys = @($b.data.vms_env_keys | Where-Object { @($a.data.vms_env_keys) -notcontains $_ })
Check 'vms.env keeps every key' ($null -eq $b.data.vms_env_keys -or $missingKeys.Count -eq 0) "missing: $($missingKeys -join ', ')"
$bp = Props $b.data.vms_env_public; $ap = Props $a.data.vms_env_public
$changed = @($bp.Keys | Where-Object { $_ -notin 'ANYAICAM_BUILD_ID', 'ANYAICAM_VMS_COMMIT' -and $ap.ContainsKey($_) -and $ap[$_] -ne $bp[$_] })
Check 'saved settings unchanged (mode, analytics switches, limits)' ($changed.Count -eq 0) "changed: $($changed -join ', ')"

switch ($Step) {
    'Upgrade' {
        Check 'still registered' ([bool]$a.registered) ''
        if ($ExpectedVersion) { Check "version is $ExpectedVersion" ($a.version -eq $ExpectedVersion) "$($b.version) -> $($a.version)" }
        Check 'version changed' ($a.version -ne $b.version) "$($b.version) -> $($a.version)"
        Check 'build id changed (new code installed)' ($ap['ANYAICAM_BUILD_ID'] -and $ap['ANYAICAM_BUILD_ID'] -ne $bp['ANYAICAM_BUILD_ID']) "$($bp['ANYAICAM_BUILD_ID']) -> $($ap['ANYAICAM_BUILD_ID'])"
        Check 'Tesseract installed' ([bool]$a.tesseract_manifest) ''
        Check 'LPR switch present (add-if-missing)' ($ap.ContainsKey('ANYAICAM_LPR_ENABLED')) "$($ap['ANYAICAM_LPR_ENABLED'])"
        if ($bp.ContainsKey('ANYAICAM_LPR_ENABLED')) { Check 'an explicit LPR choice is kept' ($ap['ANYAICAM_LPR_ENABLED'] -eq $bp['ANYAICAM_LPR_ENABLED']) '' }
    }
    'Repair' {
        Check 'still registered' ([bool]$a.registered) ''
        Check 'version unchanged' ($a.version -eq $b.version) "$($b.version) -> $($a.version)"
        $lost = @($b.program_file_list | Where-Object { @($a.program_file_list) -notcontains $_ })
        Check 'no program file lost' ($lost.Count -eq 0) "lost: $(($lost | Select-Object -First 10) -join ', ')"
        $bs = Props $b.scripts; $as = Props $a.scripts
        $scriptChanges = @($bs.Keys | Where-Object { $as[$_] -ne $bs[$_] })
        Check 'service/installer scripts identical' ($scriptChanges.Count -eq 0) "changed: $($scriptChanges -join ', ')"
        Check 'Tesseract manifest identical' ($a.tesseract_manifest -eq $b.tesseract_manifest) ''
    }
    'Uninstall' {
        Check 'no longer registered' (-not $a.registered) ''
        Check 'program folder removed' (-not $a.program_folder_exists) ''
        foreach ($name in 'AnyAiCamVMS', 'AnyAiCamAgent') { Check "service removed: $name" ($null -eq $a.services.$name) '' }
        Check 'runtime catalog removed' ($null -eq $a.runtime_catalog) ''
        Check 'firewall rules removed' (@($a.firewall_rules).Count -eq 0) "$(@($a.firewall_rules) -join ', ')"
        Check 'data folders kept' (@($b.data.folders | Where-Object { @($a.data.folders) -notcontains $_ }).Count -eq 0) ''
    }
}
if ($Step -ne 'Uninstall') {
    foreach ($name in 'AnyAiCamVMS', 'AnyAiCamAgent') {
        $s = $a.services.$name
        Check "service $name installed, automatic, running as LocalSystem" ($s -and $s.start_mode -eq 'Auto' -and $s.state -eq 'Running' -and $s.account -eq 'LocalSystem') "$($s.state) $($s.start_mode) $($s.account)"
    }
    Check 'runtime catalog registered' ([bool]$a.runtime_catalog) ''
    Check 'firewall rules present' (@($a.firewall_rules).Count -ge [math]::Max(1, @($b.firewall_rules).Count)) "$(@($a.firewall_rules) -join ', ')"
}
$final = Complete-AcceptanceResult $result $OutputPath
$final.checks | ForEach-Object { '{0} {1} {2}' -f ($(if ($_.passed) { 'PASS' } else { 'FAIL' })), $_.name, $_.detail }
"A6 $Step $($final.result) -> $OutputPath"
exit $(if ($final.result -eq 'PASS') { 0 } else { 1 })
