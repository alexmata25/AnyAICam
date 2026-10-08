# Start menu > AnyAiCam > Link this computer to AnyAiCam (2026-10-08).
#
# Opens this computer's AnyAiCam link page in the browser, as the setup's
# finish page does, WITHOUT showing the claim code: the code travels only in
# the link's URL fragment (never sent to a server; /claim moves it into the
# tab and out of the address bar). The owner signs in, chooses a site and
# confirms; the AnyAiCamAgent service completes the link.
#
# Who may link: only an administrator of this PC. The code is in
# label\claim-label.txt under the SYSTEM/Administrators-only data folder, so
# this script elevates (UAC) before reading it; a standard user gets the UAC
# prompt and cannot continue without an administrator. There is no local web
# page or listening port on Windows (the Linux "AnyAiCam Setup" page is not
# used here: ANYAICAM_LINK_PORT=0 in agent-launcher.ps1).
#
# The elevated part opens the link through explorer.exe, which hands it to
# the signed-in user's own (non-elevated) shell: the browser never runs
# elevated. Nothing is written to a file, a log or the console; the code is
# only on explorer's command line for that moment.
param([switch]$Elevated)
$ErrorActionPreference = 'Stop'

$LinkPattern = '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/claim#label=[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$'

# The link the finish page opens: the label file's QR link (cloud-link.ps1)
# plus link=windows, which tells the claim page the owner came from this
# computer's own setup (installer link mode). '' when anything looks wrong.
function Get-ClaimLink([string]$LabelFile) {
    if (-not (Test-Path -LiteralPath $LabelFile -PathType Leaf)) { return '' }
    $line = Get-Content -LiteralPath $LabelFile | Where-Object { $_ -like 'QR code:*' } | Select-Object -First 1
    if (-not $line) { return '' }
    $link = ($line -replace '^QR code:\s*', '').Trim()
    if ($link -cnotmatch $LinkPattern) { return '' }
    return $link + '&link=windows'
}

# 'linked' once the agent holds its cloud credential, else 'unlinked'.
function Get-LinkState([string]$DataRoot) {
    if (Test-Path -LiteralPath (Join-Path $DataRoot 'agent\credential.json') -PathType Leaf) { return 'linked' }
    return 'unlinked'
}

function Show-Message([string]$Text, [int]$Icon = 64) {
    try { [void](New-Object -ComObject WScript.Shell).Popup($Text, 0, 'AnyAiCam', $Icon) } catch { }
}

function Test-Administrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-OpenClaimLink {
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Administrator)) {
        if ($Elevated) { Show-Message 'Linking this computer to AnyAiCam needs an administrator of this PC.' 48; exit 1 }
        try {
            Start-Process -FilePath $powershell -Verb RunAs -WindowStyle Hidden -ArgumentList @(
                '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                '-File', ('"' + $PSCommandPath + '"'), '-Elevated')
        } catch {
            Show-Message 'Linking this computer to AnyAiCam needs an administrator of this PC.' 48
            exit 1
        }
        exit 0
    }
    $dataRoot = Join-Path $env:ProgramData 'AnyAiCam'
    if ((Get-LinkState $dataRoot) -eq 'linked') { Show-Message 'This computer is already linked to an AnyAiCam account.'; exit 0 }
    $link = Get-ClaimLink (Join-Path $dataRoot 'label\claim-label.txt')
    if (-not $link) { Show-Message 'This computer has no AnyAiCam link. Run the AnyAiCam setup again (Repair), or contact AnyAiCam support.' 48; exit 1 }
    Start-Process -FilePath (Join-Path $env:SystemRoot 'explorer.exe') -ArgumentList $link
}

# Dot-sourced by the tests (functions only); run from the Start menu.
if ($MyInvocation.InvocationName -ne '.') { Invoke-OpenClaimLink }
