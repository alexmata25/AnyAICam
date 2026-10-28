# AnyAiCam VMS resource limits on Windows (1.3.4 parity, Blue): a Job Object around the VMS service.
#
# Why: on a customer's own PC the VMS (recording FFmpeg, live view, and on-device analytics in the same Python
# process) shares the CPU with everything else the PC does. The app already limits analytics (one inference at a
# time, PyTorch threads capped, scan intervals); this caps the whole VMS so the PC stays usable however many cameras
# and analytics a customer turns on.
#
# service-launcher.ps1 calls Set-AnyAiCamVmsJobLimits before it starts Python. The launcher assigns ITSELF to a new
# job; every process it then starts (Python, and Python's FFmpeg, MediaMTX and Tesseract children) inherits the job,
# so the limits cover the VMS as a whole. Settings come from vms.env:
#   ANYAICAM_VMS_CPU_LIMIT_PERCENT  hard cap on the VMS's share of ALL cores, 10..100 (default 80; 0 or 100 = no cap)
#   ANYAICAM_VMS_MEMORY_LIMIT_MB    commit limit for the whole VMS in MB (default 0 = no limit; at least 1024 when
#                                   set). Off by default: a process over a job memory limit gets allocation failures,
#                                   which could stop recording, so it is only for an administrator who chooses it.
# Fail-open: if Windows refuses the job (an old Windows, or a service host whose own job forbids nesting), the VMS
# starts without it and the launcher logs why. Recording must never be lost to a resource safeguard.
# The defaults are provisional until the owner-approved Dell/physical benchmarks set them.

$script:AnyAiCamVmsJob = $null

function Get-AnyAiCamVmsJobSettings([hashtable]$Settings) {
    $cpu = 80
    $raw = [string]$Settings['ANYAICAM_VMS_CPU_LIMIT_PERCENT']
    if ($raw -match '^\d{1,3}$') { $cpu = [int]$raw }
    if ($cpu -ne 0 -and ($cpu -lt 10 -or $cpu -gt 100)) { $cpu = 80 }
    if ($cpu -eq 100) { $cpu = 0 }
    $memory = 0
    $raw = [string]$Settings['ANYAICAM_VMS_MEMORY_LIMIT_MB']
    if ($raw -match '^\d{1,7}$') { $memory = [int]$raw }
    if ($memory -ne 0 -and $memory -lt 1024) { $memory = 1024 }
    return @{ CpuPercent = $cpu; MemoryMB = $memory }
}

function Initialize-AnyAiCamJobApi {
    if ('AnyAiCam.JobLimits' -as [type]) { return }
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace AnyAiCam {
    public static class JobLimits {
        [StructLayout(LayoutKind.Sequential)] struct CpuRateInfo { public uint ControlFlags; public uint CpuRate; }
        [StructLayout(LayoutKind.Sequential)] struct Basic {
            public long PerProcessUserTimeLimit; public long PerJobUserTimeLimit; public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize; public UIntPtr MaximumWorkingSetSize; public uint ActiveProcessLimit;
            public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)] struct Io {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
        [StructLayout(LayoutKind.Sequential)] struct Extended {
            public Basic BasicLimitInformation; public Io IoInfo; public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit; public UIntPtr PeakProcessMemoryUsed; public UIntPtr PeakJobMemoryUsed; }
        const int ExtendedLimitInformation = 9, CpuRateControlInformation = 15;
        const uint CPU_RATE_ENABLE = 0x1, CPU_RATE_HARD_CAP = 0x4, LIMIT_JOB_MEMORY = 0x200;
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObjectW(IntPtr a, string n);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref CpuRateInfo i, int l);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref Extended i, int l);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool QueryInformationJobObject(IntPtr j, int c, ref CpuRateInfo i, int l, IntPtr r);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool QueryInformationJobObject(IntPtr j, int c, ref Extended i, int l, IntPtr r);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
        [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();

        // Creates the job, applies the limits and puts the calling process in it. Returns the job handle.
        public static IntPtr ApplyToCurrentProcess(int cpuPercent, long memoryMB) {
            IntPtr job = CreateJobObjectW(IntPtr.Zero, null);
            if (job == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject");
            if (cpuPercent > 0) {
                var cpu = new CpuRateInfo { ControlFlags = CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, CpuRate = (uint)(cpuPercent * 100) };
                if (!SetInformationJobObject(job, CpuRateControlInformation, ref cpu, Marshal.SizeOf(cpu)))
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "CPU rate limit");
            }
            if (memoryMB > 0) {
                var ext = new Extended();
                ext.BasicLimitInformation.LimitFlags = LIMIT_JOB_MEMORY;
                ext.JobMemoryLimit = new UIntPtr((ulong)memoryMB * 1024UL * 1024UL);
                if (!SetInformationJobObject(job, ExtendedLimitInformation, ref ext, Marshal.SizeOf(ext)))
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "memory limit");
            }
            if (!AssignProcessToJobObject(job, GetCurrentProcess()))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject");
            return job;
        }

        // The limits a job really carries: "cpu=<percent>;memoryMB=<MB>" (0 = none), read back from Windows.
        public static string Describe(IntPtr job) {
            var cpu = new CpuRateInfo(); var ext = new Extended();
            QueryInformationJobObject(job, CpuRateControlInformation, ref cpu, Marshal.SizeOf(cpu), IntPtr.Zero);
            QueryInformationJobObject(job, ExtendedLimitInformation, ref ext, Marshal.SizeOf(ext), IntPtr.Zero);
            uint percent = (cpu.ControlFlags & CPU_RATE_ENABLE) != 0 ? cpu.CpuRate / 100 : 0;
            ulong mb = (ext.BasicLimitInformation.LimitFlags & LIMIT_JOB_MEMORY) != 0 ? ext.JobMemoryLimit.ToUInt64() / (1024UL * 1024UL) : 0;
            return "cpu=" + percent + ";memoryMB=" + mb;
        }
    }
}
'@
}

# Returns one log line: what was applied, or why nothing was.
function Set-AnyAiCamVmsJobLimits([hashtable]$Settings) {
    $limits = Get-AnyAiCamVmsJobSettings $Settings
    if ($limits.CpuPercent -eq 0 -and $limits.MemoryMB -eq 0) { return 'AnyAiCam VMS launcher: resource limits off (vms.env).' }
    try {
        Initialize-AnyAiCamJobApi
        $script:AnyAiCamVmsJob = [AnyAiCam.JobLimits]::ApplyToCurrentProcess($limits.CpuPercent, $limits.MemoryMB)
        return "AnyAiCam VMS launcher: resource limits applied ($([AnyAiCam.JobLimits]::Describe($script:AnyAiCamVmsJob)))."
    } catch {
        return "AnyAiCam VMS launcher: resource limits NOT applied, starting without them: $($_.Exception.Message)"
    }
}
