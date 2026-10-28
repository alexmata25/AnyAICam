<#
Windows 1.3.4.1 acceptance A4: the VMS runs inside the CPU-capped Job Object of vms-job-limits.ps1, children included.

  -Mode Simulate  (any Windows PC, no service, no elevation): a launcher stand-in applies the REAL vms-job-limits.ps1,
                  starts a child that starts a grandchild. Windows' own job data must show the requested limits, both
                  descendants as job members, and a CREATE_BREAKAWAY_FROM_JOB spawn refused (ERROR_ACCESS_DENIED).
  -Mode Service   (R430 test VM, installed 1.3.4.1, elevated): the running AnyAiCamVMS service (WinSW) -- the launcher
                  logged "resource limits applied (cpu=<vms.env value>...)" in %ProgramData%\AnyAiCam\logs, and the
                  launcher, python.exe and every FFmpeg/MediaMTX/Tesseract descendant are inside a job.
                  Limitation (documented): from outside, Windows only tells whether a process is in SOME job; the
                  launcher's log line proves the AnyAiCam job and its limits, Simulate proves inheritance/breakaway.
Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED.
#>
param([ValidateSet('Simulate', 'Service')][string]$Mode = 'Service',
      [string]$InstallRoot = (Join-Path $env:ProgramFiles 'AnyAiCam'),
      [string]$JobLimitsScript = '',
      [string]$CpuPercent = '80', [string]$MemoryMB = '0',
      [string]$OutputDir = (Join-Path $env:TEMP 'AnyAiCamAcceptance'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'acceptance-common.ps1')
$result = New-AcceptanceResult 'A4-vms-job-object' $Mode
$output = Join-Path $OutputDir 'A4-vms-job-object.json'

function Test-InAnyJob([int]$ProcessId) {
    if (-not ('AnyAiCamAcceptance.JobQuery' -as [type])) {
        Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
namespace AnyAiCamAcceptance { public static class JobQuery {
  [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool IsProcessInJob(IntPtr p, IntPtr j, out bool r);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  public static int InJob(int pid) { IntPtr h = OpenProcess(0x1000, false, pid); if (h == IntPtr.Zero) return -1;
    try { bool r; if (!IsProcessInJob(h, IntPtr.Zero, out r)) return -1; return r ? 1 : 0; } finally { CloseHandle(h); } }
} }
'@
    }
    return [AnyAiCamAcceptance.JobQuery]::InJob($ProcessId)
}

try {
    if ($Mode -eq 'Simulate') {
        if (-not $JobLimitsScript) { $JobLimitsScript = Join-Path $PSScriptRoot '..\vms-job-limits.ps1' }
        $JobLimitsScript = (Resolve-Path -LiteralPath $JobLimitsScript).Path
        $probe = Invoke-AcceptanceProbe -ProbeScript (Join-Path $PSScriptRoot 'sim-launcher.ps1') -RunAs CurrentUser -TimeoutSeconds 90 `
            -Arguments @{ JobLimitsScript = $JobLimitsScript; CpuPercent = $CpuPercent; MemoryMB = $MemoryMB }
        $result.probe = $probe
        $expectedCpu = if ($CpuPercent -in @('0', '100')) { 0 } else { [int]$CpuPercent }
        Add-AcceptanceCheck $result 'launcher applied the job (fail-open line says applied)' ([string]$probe.launcher_log -match 'resource limits applied') "$($probe.launcher_log)"
        Add-AcceptanceCheck $result 'Windows reports the requested limits' ($probe.job_limits -eq "cpu=$expectedCpu;memoryMB=$MemoryMB") "$($probe.job_limits)"
        $members = @($probe.job_members | ForEach-Object { [int]$_ })
        Add-AcceptanceCheck $result 'launcher is a job member' ($members -contains [int]$probe.launcher_pid) "members: $($members -join ',')"
        Add-AcceptanceCheck $result 'child (Python stand-in) inherited the job' ($probe.child_pid -and $members -contains [int]$probe.child_pid) "child $($probe.child_pid)"
        Add-AcceptanceCheck $result 'grandchild (FFmpeg stand-in) inherited the job' ($probe.grandchild_pid -and $members -contains [int]$probe.grandchild_pid) "grandchild $($probe.grandchild_pid)"
        Add-AcceptanceCheck $result 'breakaway from the job is refused (ERROR_ACCESS_DENIED)' ($probe.breakaway_error -eq 5) "CreateProcess error $($probe.breakaway_error)"
        Add-AcceptanceCheck $result 'this test process is not in the VMS job' (-not ($members -contains $PID)) ''
        if ($probe.error) { Add-AcceptanceCheck $result 'probe completed without error' $false $probe.error }
    } else {
        if (-not (Test-AcceptanceAdmin)) { Complete-AcceptanceResult $result $output 'Service mode needs an elevated PowerShell (SYSTEM processes).' | Out-Null; 'A4 BLOCKED: not elevated'; exit 2 }
        $service = Get-CimInstance Win32_Service -Filter "Name='AnyAiCamVMS'"
        if (-not $service -or $service.State -ne 'Running') { Complete-AcceptanceResult $result $output 'AnyAiCamVMS is not running' | Out-Null; 'A4 BLOCKED: service not running'; exit 2 }
        $all = @(Get-CimInstance Win32_Process)
        $tree = New-Object System.Collections.Generic.List[object]
        $queue = New-Object System.Collections.Generic.Queue[int]; $queue.Enqueue([int]$service.ProcessId)
        while ($queue.Count) { $parent = $queue.Dequeue(); foreach ($p in $all | Where-Object { $_.ParentProcessId -eq $parent }) { $tree.Add($p); $queue.Enqueue([int]$p.ProcessId) } }
        $launcher = $tree | Where-Object { $_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'service-launcher\.ps1' } | Select-Object -First 1
        Add-AcceptanceCheck $result 'the launcher runs under WinSW' ([bool]$launcher) "WinSW pid $($service.ProcessId)"
        $python = $tree | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'uvicorn' } | Select-Object -First 1
        Add-AcceptanceCheck $result 'the VMS python.exe runs under the launcher' ([bool]$python) "$($python.ProcessId)"
        foreach ($p in $tree | Where-Object { $_.ProcessId -ne $service.ProcessId -and $_.Name -ne 'conhost.exe' }) {
            Add-AcceptanceCheck $result "in a job: $($p.Name) ($($p.ProcessId))" ((Test-InAnyJob ([int]$p.ProcessId)) -eq 1) ''
        }
        $configured = '80'
        $envFile = Join-Path $env:ProgramData 'AnyAiCam\config\vms.env'
        if (Test-Path -LiteralPath $envFile) {  # only this one key is read; vms.env holds secrets
            $line = Select-String -LiteralPath $envFile -Pattern '^ANYAICAM_VMS_CPU_LIMIT_PERCENT=(\d{1,3})\s*$' | Select-Object -First 1
            if ($line) { $configured = $line.Matches[0].Groups[1].Value }
        }
        $expected = if ($configured -in @('0', '100')) { 'resource limits off' } else { "resource limits applied (cpu=$configured;" }
        $logs = @(Get-ChildItem (Join-Path $env:ProgramData 'AnyAiCam\logs') -Filter '*VMS*out*' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
        $hit = $logs | Select-Object -First 3 | ForEach-Object { Select-String -LiteralPath $_.FullName -SimpleMatch 'AnyAiCam VMS launcher: resource limits' | Select-Object -Last 1 } | Select-Object -First 1
        Add-AcceptanceCheck $result "launcher log: $expected" ([bool]$hit -and $hit.Line.Contains($expected)) "$($hit.Line)"
    }
} catch {
    Complete-AcceptanceResult $result $output $_.Exception.Message | Out-Null
    Write-Output "A4 BLOCKED: $($_.Exception.Message)"; exit 2
}
$final = Complete-AcceptanceResult $result $output
$final.checks | ForEach-Object { '{0} {1} {2}' -f ($(if ($_.passed) { 'PASS' } else { 'FAIL' })), $_.name, $_.detail }
"A4 $($final.result) ($Mode) -> $output"
exit $(if ($final.result -eq 'PASS') { 0 } else { 1 })
