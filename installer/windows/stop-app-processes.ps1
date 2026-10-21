<#
Windows 1.3.2 (Blue, Sandbox acceptance P-H 2026-10-21): stop every process still running from THIS AnyAiCam install
folder once both services are stopped and removed -- before Setup replaces files, and before the uninstaller deletes
them.

Why: the VMS starts MediaMTX (WebRTC live view, runtime\tools\mediamtx\mediamtx.exe) and FFmpeg as child processes.
Stopping the WinSW service does not end them on Windows, so on a Hybrid PC a reinstall/upgrade failed with
"The process cannot access the file ...\mediamtx.exe" (setup exit 20). Local passed only because MediaMTX was not running.

Only processes whose executable is inside -InstallRoot are stopped (case-insensitive prefix with a trailing separator,
so "AnyAiCam2" never matches "AnyAiCam"); the calling process, its parent and the uninstaller (unins*.exe) are never
touched. Waits up to 20 s for them to exit. Prints what it stopped; never fails the caller.
#>
param([Parameter(Mandatory=$true)][string]$InstallRoot)
$root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
$self = $PID
$parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$self" -ErrorAction SilentlyContinue).ParentProcessId
# Both sides normalised to the full long form: a process started through a short (8.3) path reports it that way.
function Get-NormalPath([string]$Path) { try { return [IO.Path]::GetFullPath($Path) } catch { return $Path } }
$targets = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and $_.ProcessId -ne $self -and $_.ProcessId -ne $parent -and
    $_.Name -notlike 'unins*' -and
    (Get-NormalPath $_.ExecutablePath).StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
})
foreach ($p in $targets) {
    Write-Output "Stopping $($p.Name) (pid $($p.ProcessId)) still running from the AnyAiCam folder."
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    $left = @($targets | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    if (-not $left.Count) { break }
    Start-Sleep -Milliseconds 500
}
$left = @($targets | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
if ($left.Count) { Write-Output "Still running after 20 s: $(($left | ForEach-Object { $_.Name }) -join ', ')" }
Write-Output "AnyAiCam folder processes stopped: $($targets.Count - $left.Count) of $($targets.Count)."
exit 0
