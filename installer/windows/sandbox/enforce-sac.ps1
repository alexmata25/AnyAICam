# Windows 1.3.4.1 acceptance (Blue, 2026-10-29): turn Smart App Control ON (enforced) inside THIS disposable Sandbox
# before the run. Since 2026-10-23 Windows Sandbox on the Dell starts in evaluation mode (VerifiedAndReputablePolicyState=2),
# where nothing is blocked, so a pass proved nothing about enforcement. The host is never touched. Proven by the probe
# sandbox-sac-enforce-probe-20261029: after this, the unsigned 1.3.4.1 setup is refused (Code Integrity 3077).
# make-wsb.ps1 -EnforceSmartAppControl runs it first; the run's own diagnostic then reports state=1.
$results = 'C:\Validation\results'
New-Item -ItemType Directory -Force -Path $results | Out-Null
$log = Join-Path $results 'sac-enforcement.txt'
function Log($m) { Add-Content $log ("{0:o}  {1}" -f (Get-Date), $m) }
$key = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy'
Log ("before: VerifiedAndReputablePolicyState=" + (Get-ItemProperty $key -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState)
try {
    Set-ItemProperty $key -Name VerifiedAndReputablePolicyState -Value 1 -Type DWord -ErrorAction Stop
    $out = & (Join-Path $env:SystemRoot 'System32\CiTool.exe') --refresh --json 2>&1 | Out-String
    Log ("CiTool --refresh: " + ($out -replace '\s+', ' '))
} catch { Log ("enforcement could not be set: " + $_.Exception.Message) }
$state = (Get-ItemProperty $key -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
Log ("after: VerifiedAndReputablePolicyState=$state")
Set-Content (Join-Path $results 'SAC-STATE') "$state"
