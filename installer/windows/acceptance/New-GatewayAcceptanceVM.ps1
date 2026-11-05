# Disposable Windows Gateway acceptance VM on the R430. Test tooling; never packaged.
#
# Builds a NEW Hyper-V VM whose disk is a differencing disk over the self-contained
# AAC-WinVMS-1341-Test pre-acceptance export (2026-10-28). The parent is only read; the
# original VM, its checkpoint and the export are never changed. Every write lands in
# the new VM's own folder, so discarding it is: Remove-VM, then delete that folder.
#
# Default: print the plan and the checks, change nothing.
#   -Create  create the VM and its "clean-base" checkpoint (not started)
#   -Start   also start it, only if enough host memory is free
param(
    [string]$ParentVhd = 'C:\Hyper-V\AAC-WinVMS-1341-Test\Recovery\Pre-Acceptance-20261028T165114Z\Export\AAC-WinVMS-1341-Test\Virtual Hard Disks\AAC-WinVMS-1341-Test.vhdx',
    [Parameter(Mandatory=$true)][ValidatePattern('^AAC-GW-Accept-[0-9a-f]{7}$')][string]$Name,
    [string]$Root = 'C:\Hyper-V',
    # Private: no host or internet path. Cloud/provider tests need a separately approved network.
    [string]$SwitchName = 'AAC-WinVMS-1341-Private',
    [ValidateRange(2, 8)][int]$MemoryGB = 4,
    [switch]$Create,
    [switch]$Start
)
$ErrorActionPreference = 'Stop'
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

Write-Output "Plan for $Name"
Write-Output "  differencing disk : $disk"
Write-Output "  parent (read only): $ParentVhd"
Write-Output "  memory            : $MemoryGB GB static, 4 vCPU, Gen 2, Secure Boot MicrosoftWindows, no TPM"
Write-Output "  network           : $SwitchName (private)"
Write-Output "  checkpoint        : clean-base (Standard), automatic checkpoints off"
Write-Output "  host free memory  : $freeGB GB"
Write-Output "  note              : same computer name/SID as AAC-WinVMS-1341-Test; never run both on one switch"
if ($problems.Count) { throw ("Refused:`n  " + ($problems -join "`n  ")) }
if (-not $Create) { Write-Output 'Plan only: nothing was changed.'; return }

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
if ($freeGB -lt $MemoryGB + 1) { throw "Not started: $freeGB GB free, $($MemoryGB + 1) GB needed. Stop nothing else; retry later." }
Start-VM -Name $Name
Write-Output "Started $Name."
