# Windows Firewall rules for AnyAiCam VMS (2026-10-07).
#
# Windows blocks unsolicited inbound connections by default, so phones and
# computers on the customer's network could not open the VMS or live view
# without these. Same reach as the Linux appliance (anyaicam-webrtc-firewall):
#   * TCP 8000 -- the VMS web interface (python.exe of this install only)
#   * UDP 8189 -- WebRTC live-view media (mediamtx.exe of this install only)
# accepted only from the local subnet and Tailscale (100.64.0.0/10), and only
# on Private/Domain networks -- never on a Public (coffee shop) network, and
# never needing router port forwarding.
#
#   firewall.ps1 -Apply  -InstallRoot <dir>   (idempotent: replaces our rules)
#   firewall.ps1 -Remove                      (uninstall)
param(
    [switch]$Apply,
    [switch]$Remove,
    [string]$InstallRoot,
    [int]$WebPort = 8000,
    [int]$WebRtcPort = 8189
)
$ErrorActionPreference = 'Stop'
$group = 'AnyAiCam VMS'
$remote = @('LocalSubnet', '100.64.0.0/10')
$profiles = @('Private', 'Domain')

function Remove-AnyAiCamRules {
    Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}

if ($Apply -eq $Remove) { throw 'Use exactly one of -Apply or -Remove.' }
if ($Remove) { Remove-AnyAiCamRules; exit 0 }

if (-not $InstallRoot) { throw '-InstallRoot is required with -Apply.' }
foreach ($port in @($WebPort, $WebRtcPort)) { if ($port -lt 1 -or $port -gt 65535) { throw "Invalid port: $port" } }
$python = Join-Path $InstallRoot 'runtime\python\python.exe'
$mediamtx = Join-Path $InstallRoot 'runtime\tools\mediamtx\mediamtx.exe'
foreach ($program in @($python, $mediamtx)) { if (-not (Test-Path -LiteralPath $program)) { throw "Program not found: $program" } }

Remove-AnyAiCamRules
New-NetFirewallRule -Name 'AnyAiCam-VMS-Web' -DisplayName 'AnyAiCam VMS (web, local network)' -Group $group `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $WebPort -Program $python `
    -RemoteAddress $remote -Profile $profiles | Out-Null
New-NetFirewallRule -Name 'AnyAiCam-VMS-WebRTC' -DisplayName 'AnyAiCam VMS (live view, local network)' -Group $group `
    -Direction Inbound -Action Allow -Protocol UDP -LocalPort $WebRtcPort -Program $mediamtx `
    -RemoteAddress $remote -Profile $profiles | Out-Null
