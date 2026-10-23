<#
Windows 1.3.3 D4 (Blue, Sandbox U132-L rerun on 906f7819, 2026-10-23): run by service-launcher.ps1 BEFORE it starts the
VMS, so a service (re)start always begins from a clean slate.

Why: a service stop sends Ctrl+C to the launcher. When the launcher (PowerShell) exited at once (code 0), WinSW did not
look for its child processes, and the VMS python (uvicorn main:app) survived as an ORPHAN: it kept port 8000 and the
old product mode's flags. Every new VMS then failed to bind ([Errno 10048]) and WinSW restarted the service every ~45 s;
MediaMTX never started, the live playlist froze and offline live view was empty, until a reboot.

Stops, from THIS install folder only (case-insensitive prefix with a trailing separator):
  * every VMS python (runtime\python\python.exe ... uvicorn main:app) other than this launcher's own children, and their
    descendants that also run from the install folder (FFmpeg, MediaMTX);
  * FFmpeg / ffprobe / MediaMTX whose parent process no longer exists (orphans of an earlier VMS).
Never touched: the cloud agent (agent-main.py), anything whose parent is alive and is not a stale VMS, anything outside
the install folder, this process and its parent. Prints what it stopped; never fails the launcher.
#>
param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference = 'Continue'
function Get-NormalPath([string]$Path) { try { return [IO.Path]::GetFullPath($Path) } catch { return $Path } }
$root = (Get-NormalPath $InstallRoot).TrimEnd('\') + '\'
$self = $PID
$all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$parent = if ($byId.ContainsKey($self)) { [int]$byId[$self].ParentProcessId } else { -1 }
function Test-InRoot($p) { $p.ExecutablePath -and (Get-NormalPath $p.ExecutablePath).StartsWith($root, [StringComparison]::OrdinalIgnoreCase) }

$stale = @($all | Where-Object {
    $_.ProcessId -ne $self -and $_.ProcessId -ne $parent -and $_.Name -eq 'python.exe' -and (Test-InRoot $_) -and
    $_.CommandLine -match 'uvicorn\s+main:app' })
$targets = New-Object System.Collections.Generic.List[object]
$queue = New-Object System.Collections.Generic.Queue[int]
foreach ($p in $stale) { $targets.Add($p); $queue.Enqueue([int]$p.ProcessId) }
while ($queue.Count) {   # descendants of a stale VMS that run from the install folder
    $id = $queue.Dequeue()
    foreach ($c in $all | Where-Object { [int]$_.ParentProcessId -eq $id -and $_.ProcessId -ne $self -and (Test-InRoot $_) }) {
        if (-not ($targets | Where-Object { $_.ProcessId -eq $c.ProcessId })) { $targets.Add($c); $queue.Enqueue([int]$c.ProcessId) }
    }
}
foreach ($p in $all | Where-Object { $_.Name -in @('ffmpeg.exe', 'ffprobe.exe', 'mediamtx.exe') -and (Test-InRoot $_) -and
                                      -not $byId.ContainsKey([int]$_.ParentProcessId) }) {
    if (-not ($targets | Where-Object { $_.ProcessId -eq $p.ProcessId })) { $targets.Add($p) }
}
foreach ($p in $targets) {
    $kind = if ($stale | Where-Object { $_.ProcessId -eq $p.ProcessId }) { 'stale VMS' } else { 'left-over' }
    Write-Output "AnyAiCam VMS launcher: stopping $kind $($p.Name) (pid $($p.ProcessId)) from an earlier VMS."
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline -and @($targets | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }).Count) {
    Start-Sleep -Milliseconds 300
}
$left = @($targets | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
if ($left.Count) { Write-Output "AnyAiCam VMS launcher: still running after 15 s: $(($left | ForEach-Object { "$($_.Name) $($_.ProcessId)" }) -join ', ')" }
if ($targets.Count) { Write-Output "AnyAiCam VMS launcher: stopped $($targets.Count - $left.Count) of $($targets.Count) process(es) from an earlier VMS." }
exit 0
