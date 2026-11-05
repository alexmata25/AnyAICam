# Disposable Windows Gateway acceptance VM on the R430. Test tooling; never packaged.
#
# Builds a NEW Hyper-V VM whose disk is a differencing disk over the self-contained
# AAC-WinVMS-1341-Test pre-acceptance export (2026-10-28). The parent is only read; the
# original VM, its checkpoint and the export are never changed. Every write lands in
# the new VM's own folder, so discarding it is: Remove-VM, then delete that folder.
#
# Default: print the plan and the checks, change nothing.
#   -Create  create the VM and its "clean-base" checkpoint (not started). Requires
#            reviewed evidence files (and their SHA-256) that independently show the
#            parent disk holds no customer data, credentials or production database,
#            and that the switch reaches no other guest or production network. This
#            script cannot prove either; it only refuses to proceed without them.
#   -Start   also start it, only under the R430 host reserve: at least 9 GiB free
#            before start and at least 4 GiB left for existing workloads after it,
#            and no other running VM on the same switch.
param(
    [string]$ParentVhd = 'C:\Hyper-V\AAC-WinVMS-1341-Test\Recovery\Pre-Acceptance-20261028T165114Z\Export\AAC-WinVMS-1341-Test\Virtual Hard Disks\AAC-WinVMS-1341-Test.vhdx',
    [Parameter(Mandatory=$true)][ValidatePattern('^AAC-GW-Accept-[0-9a-f]{7}$')][string]$Name,
    [string]$Root = 'C:\Hyper-V',
    # Private: no host or internet path. Cloud/provider tests need a separately approved network.
    [string]$SwitchName = 'AAC-WinVMS-1341-Private',
    [ValidateRange(2, 8)][int]$MemoryGB = 4,
    [string]$CleanParentEvidence = '',
    [string]$CleanParentEvidenceSha256 = '',
    [string]$IsolationEvidence = '',
    [string]$IsolationEvidenceSha256 = '',
    [switch]$Create,
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
# Owner's R430 reserve for any new guest.
$MinimumFreeBeforeStartGB = 9
$MinimumRemainingAfterStartGB = 4
function Get-EvidenceProblem([string]$Path, [string]$Sha256, [string]$What) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "$What evidence file is required" }
    if ($Sha256 -notmatch '^[0-9a-f]{64}$') { return "$What evidence SHA-256 (lowercase) is required" }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() -ne $Sha256) { return "$What evidence does not match its reviewed SHA-256" }
    return ''
}
$folder = Join-Path $Root $Name
$disk = Join-Path $folder "$Name.avhdx"
$problems = @()
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) { $problems += "a VM named $Name already exists" }
if (Test-Path -LiteralPath $folder) { $problems += "$folder already exists" }
if (-not (Test-Path -LiteralPath $ParentVhd -PathType Leaf)) { $problems += "parent disk not found: $ParentVhd" }
else {
    $parent = Get-VHD -Path $ParentVhd
    if ($parent.VhdType -ne 'Dynamic' -or $parent.ParentPath) { $problems += 'parent disk is not a self-contained dynamic VHDX' }
    $users = @(Get-VM | Get-VMHardDiskDrive | Where-Object { $_.Path -eq $ParentVhd })
    if ($users.Count) { $problems += "parent disk is attached to $($users.VMName -join ', ')" }
}
$switch = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
if (-not $switch) { $problems += "switch not found: $SwitchName" }
elseif ($switch.SwitchType -ne 'Private') { $problems += "$SwitchName is $($switch.SwitchType), not Private" }
$freeGB = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 1)
$neededGB = [math]::Max($MinimumFreeBeforeStartGB, $MemoryGB + $MinimumRemainingAfterStartGB)
$switchPeers = @(Get-VM | Where-Object { $_.Name -ne $Name } | Get-VMNetworkAdapter | Where-Object { $_.SwitchName -eq $SwitchName } | ForEach-Object VMName | Sort-Object -Unique)

Write-Output "Plan for $Name"
Write-Output "  differencing disk : $disk"
Write-Output "  parent (read only): $ParentVhd"
Write-Output "  memory            : $MemoryGB GB static, 4 vCPU, Gen 2, Secure Boot MicrosoftWindows, no TPM"
Write-Output "  network           : $SwitchName (private)"
Write-Output "  checkpoint        : clean-base (Standard), automatic checkpoints off"
Write-Output "  host free memory  : $freeGB GB (start needs $neededGB GB: >= $MinimumFreeBeforeStartGB free, >= $MinimumRemainingAfterStartGB left after)"
Write-Output "  other VMs on switch: $(if ($switchPeers.Count) { $switchPeers -join ', ' } else { 'none' }) (none may be running at start)"
Write-Output "  required evidence : clean parent disk and network isolation, reviewed and hashed (not proven by this script)"
Write-Output "  note              : same computer name/SID as AAC-WinVMS-1341-Test"
if ($problems.Count) { throw ("Refused:`n  " + ($problems -join "`n  ")) }
if (-not $Create) { Write-Output 'Plan only: nothing was changed.'; return }
$evidenceProblems = @(@((Get-EvidenceProblem $CleanParentEvidence $CleanParentEvidenceSha256 'Clean parent disk'),
                        (Get-EvidenceProblem $IsolationEvidence $IsolationEvidenceSha256 'Network isolation')) | Where-Object { $_ })
if ($evidenceProblems.Count) { throw ("Not created:`n  " + ($evidenceProblems -join "`n  ")) }

New-Item -ItemType Directory -Path $folder | Out-Null
New-VHD -Path $disk -ParentPath $ParentVhd -Differencing | Out-Null
New-VM -Name $Name -Generation 2 -MemoryStartupBytes ($MemoryGB * 1GB) -VHDPath $disk -SwitchName $SwitchName -Path $Root | Out-Null
Set-VM -Name $Name -ProcessorCount 4 -StaticMemory -AutomaticCheckpointsEnabled $false -CheckpointType Standard `
    -Notes "Disposable Windows Gateway acceptance VM. Differencing disk over $ParentVhd. Discard: Remove-VM, then delete $folder."
Set-VMFirmware -VMName $Name -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
Checkpoint-VM -Name $Name -SnapshotName 'clean-base'
Write-Output "Created $Name (off) with checkpoint clean-base."
if (-not $Start) { return }
$freeGB = [math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB, 1)
if ($freeGB -lt $neededGB) { throw "Not started: $freeGB GB free, $neededGB GB needed. Stop nothing else; retry later." }
$runningPeers = @(Get-VM | Where-Object { $_.Name -ne $Name -and $_.State -ne 'Off' } | Get-VMNetworkAdapter | Where-Object { $_.SwitchName -eq $SwitchName } | ForEach-Object VMName | Sort-Object -Unique)
if ($runningPeers.Count) { throw "Not started: $($runningPeers -join ', ') is running on $SwitchName." }
Start-VM -Name $Name
Write-Output "Started $Name."
