# Stand-in for service-launcher.ps1 used by Test-VmsJobObject.ps1 -Mode Simulate. It applies the REAL vms-job-limits.ps1
# to itself, starts a child (the VMS Python stand-in) that starts a grandchild (the FFmpeg stand-in), then reports
# what Windows says about the job: limits, member processes, and whether a child can break away.
param([Parameter(Mandatory=$true)][string]$ResultPath, [Parameter(Mandatory=$true)][string]$JobLimitsScript,
      [string]$CpuPercent = '80', [string]$MemoryMB = '0')
$ErrorActionPreference = 'Stop'
$out = [ordered]@{ launcher_pid = $PID }
try {
    . $JobLimitsScript
    $out.launcher_log = Set-AnyAiCamVmsJobLimits @{ ANYAICAM_VMS_CPU_LIMIT_PERCENT = $CpuPercent; ANYAICAM_VMS_MEMORY_LIMIT_MB = $MemoryMB }
    Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Collections.Generic;
public static class JobInspect {
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr j, int c, IntPtr info, int len, IntPtr ret);
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct STARTUPINFO { public int cb; public string r, d, t; public int x, y, xs, ys, xc, yc, fa, fl; public short sw, r2; public IntPtr r3, i, o, e; }
  [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION { public IntPtr hp, ht; public int pid, tid; }
  [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern bool CreateProcessW(string app, string cmd, IntPtr pa, IntPtr ta, bool inh, uint flags, IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
  [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr h, uint code);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  // JobObjectBasicProcessIdList (3): the processes Windows counts as members of this job.
  public static int[] Members(IntPtr job) {
    int size = 8 + 8 * 256; IntPtr buffer = Marshal.AllocHGlobal(size);
    try { if (!QueryInformationJobObject(job, 3, buffer, size, IntPtr.Zero)) throw new System.ComponentModel.Win32Exception();
      int n = Marshal.ReadInt32(buffer, 4); var ids = new int[n];
      for (int i = 0; i < n; i++) ids[i] = (int)Marshal.ReadIntPtr(buffer, 8 + i * IntPtr.Size).ToInt64(); return ids; }
    finally { Marshal.FreeHGlobal(buffer); }
  }
  // CREATE_BREAKAWAY_FROM_JOB: must FAIL (access denied) in a job that does not allow breakaway.
  public static int TryBreakaway(string exe) {
    var si = new STARTUPINFO(); si.cb = Marshal.SizeOf(si); PROCESS_INFORMATION pi;
    if (CreateProcessW(exe, "\"" + exe + "\" -n 1 127.0.0.1", IntPtr.Zero, IntPtr.Zero, false, 0x01000000 | 0x08000000, IntPtr.Zero, null, ref si, out pi)) {
      TerminateProcess(pi.hp, 0); CloseHandle(pi.hp); CloseHandle(pi.ht); return 0; }
    return Marshal.GetLastWin32Error();
  }
}
'@
    $ping = Join-Path $env:SystemRoot 'System32\PING.EXE'
    $grandchildCommand = "Start-Process -FilePath '$ping' -ArgumentList '-n','8','127.0.0.1' -WindowStyle Hidden -PassThru | ForEach-Object { `$_.Id } | Set-Content -LiteralPath '$ResultPath.grandchild'; Start-Sleep -Seconds 8"
    $child = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList '-NoProfile', '-NonInteractive', '-Command', $grandchildCommand -WindowStyle Hidden -PassThru
    $out.child_pid = $child.Id
    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-Path -LiteralPath "$ResultPath.grandchild") -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    Start-Sleep -Milliseconds 500
    $out.grandchild_pid = [int](Get-Content -LiteralPath "$ResultPath.grandchild" -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($script:AnyAiCamVmsJob) {
        $out.job_limits = [AnyAiCam.JobLimits]::Describe($script:AnyAiCamVmsJob)
        $out.job_members = @([JobInspect]::Members($script:AnyAiCamVmsJob))
    }
    $out.breakaway_error = [JobInspect]::TryBreakaway($ping)
    $child.WaitForExit(20000) | Out-Null
} catch { $out.error = $_.Exception.Message }
Remove-Item -LiteralPath "$ResultPath.grandchild" -ErrorAction SilentlyContinue
$out | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 -LiteralPath $ResultPath
