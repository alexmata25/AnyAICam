# Link this computer to an AnyAiCam account (2026-10-08).
#
# Asks the AnyAiCamAgent service for a fresh, one-time account link and opens
# it in the browser -- nobody sees, copies or types a claim code:
#   <portal>/claim#code=XXXXXXXX&link=windows
# The code travels only in the URL fragment (never sent to a server; /claim
# moves it into the tab and out of the address bar). The owner signs in,
# chooses a site and confirms; the agent completes the link. Each request
# replaces the previous code, and the code dies with its 15-minute claim.
#
# Who may link: only an administrator of this PC. The agent answers requests
# only in C:\ProgramData\AnyAiCam\agent\link-requests, which is SYSTEM/
# Administrators-only (windows_link.py). There is no local web page or
# listening port on Windows.
#
# Three ways in:
#   (no arguments)   Start menu > AnyAiCam > Link this computer to AnyAiCam,
#                    as the signed-in user: starts itself elevated (UAC), waits,
#                    then opens the link as that same user -- never an
#                    elevated browser.
#   -Elevated -Handoff <nonce>
#                    that elevated copy: requests the link and hands it back
#                    in %TEMP%\anyaicam-link-<nonce>.txt (created new, read
#                    and deleted at once by the first copy).
#   -Prepare <file>  the setup (already elevated): requests the link and
#                    writes it to <file> in the setup's own temporary folder;
#                    the setup opens it as the person who ran it.
# Nothing is written to a log or the console.
param([switch]$Elevated, [string]$Handoff = '', [string]$Prepare = '')
$ErrorActionPreference = 'Stop'

$LinkPattern = '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/claim#code=[0-9A-Z]{8}&link=windows$'
$NoncePattern = '^[0-9a-f]{32}$'

function New-Nonce {
    $bytes = New-Object byte[] 16
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}

# 'linked' once the agent holds its cloud credential, else 'unlinked'.
function Get-LinkState([string]$DataRoot) {
    if (Test-Path -LiteralPath (Join-Path $DataRoot 'agent\credential.json') -PathType Leaf) { return 'linked' }
    return 'unlinked'
}

# Asks the agent (windows_link.py) for a link. Returns @{ status; link }:
# status 'ok' (link set), 'linked', 'unavailable' (no cloud / no claim now)
# or 'timeout' (the agent did not answer).
function Request-AccountLink([string]$DataRoot, [int]$TimeoutSeconds = 45) {
    $folder = Join-Path $DataRoot 'agent\link-requests'
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $nonce = New-Nonce
    $request = Join-Path $folder "$nonce.request"
    $response = Join-Path $folder "$nonce.response"
    [IO.File]::WriteAllText($request, '{}')
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    try {
        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $response -PathType Leaf) {
                try { $answer = Get-Content -Raw -LiteralPath $response | ConvertFrom-Json } finally { Remove-Item -LiteralPath $response -Force -ErrorAction SilentlyContinue }
                $status = [string]$answer.status
                if ($status -eq 'ok' -and ([string]$answer.link) -cmatch $LinkPattern) { return @{ status = 'ok'; link = [string]$answer.link } }
                if ($status -eq 'linked') { return @{ status = 'linked'; link = '' } }
                return @{ status = 'unavailable'; link = '' }
            }
            Start-Sleep -Milliseconds 250
        }
        return @{ status = 'timeout'; link = '' }
    } finally {
        Remove-Item -LiteralPath $request -Force -ErrorAction SilentlyContinue
    }
}

# "<status>`n<link>" into a file that must not exist yet (CreateNew: never
# overwrites or follows something already there).
function Write-Result([string]$Path, [hashtable]$Result) {
    $bytes = [Text.Encoding]::ASCII.GetBytes(('{0}{1}{2}' -f $Result.status, "`n", $Result.link))
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
}

function Read-Result([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @{ status = 'missing'; link = '' } }
    try { $lines = @([IO.File]::ReadAllText($Path) -split "`n") } finally { Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue }
    $status = $lines[0].Trim()
    $link = if ($lines.Count -gt 1) { $lines[1].Trim() } else { '' }
    if ($status -eq 'ok' -and $link -cnotmatch $LinkPattern) { return @{ status = 'unavailable'; link = '' } }
    return @{ status = $status; link = $link }
}

function Get-HandoffPath([string]$Nonce) { return Join-Path ([IO.Path]::GetTempPath()) "anyaicam-link-$Nonce.txt" }

function Get-AccountLinkResult([string]$DataRoot) {
    if ((Get-LinkState $DataRoot) -eq 'linked') { return @{ status = 'linked'; link = '' } }
    return Request-AccountLink $DataRoot
}

function Show-Message([string]$Text, [int]$Icon = 64) {
    try { [void](New-Object -ComObject WScript.Shell).Popup($Text, 0, 'AnyAiCam', $Icon) } catch { }
}

function Test-Administrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-OpenClaimLink {
    $dataRoot = Join-Path $env:ProgramData 'AnyAiCam'
    if ($Prepare) {
        # From the setup: elevated, writes into its own temporary folder.
        if (-not (Test-Administrator) -or (Split-Path -Leaf $Prepare) -ne 'anyaicam-account-link.txt') { exit 1 }
        Write-Result $Prepare (Get-AccountLinkResult $dataRoot)
        exit 0
    }
    if ($Elevated) {
        if ($Handoff -cnotmatch $NoncePattern) { exit 1 }
        $result = if (Test-Administrator) { Get-AccountLinkResult $dataRoot } else { @{ status = 'denied'; link = '' } }
        Write-Result (Get-HandoffPath $Handoff) $result
        exit 0
    }
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $nonce = New-Nonce
    try {
        $child = Start-Process -FilePath $powershell -Verb RunAs -WindowStyle Hidden -PassThru -ArgumentList @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', ('"' + $PSCommandPath + '"'), '-Elevated', '-Handoff', $nonce)
        $child.WaitForExit()
    } catch {
        Show-Message 'Linking this computer to AnyAiCam needs an administrator of this PC.' 48
        exit 1
    }
    $result = Read-Result (Get-HandoffPath $nonce)
    switch ($result.status) {
        'ok' { Start-Process -FilePath $result.link; exit 0 }  # as the signed-in user, never elevated
        'linked' { Show-Message 'This computer is already linked to an AnyAiCam account.'; exit 0 }
        'denied' { Show-Message 'Linking this computer to AnyAiCam needs an administrator of this PC.' 48; exit 1 }
        'unavailable' { Show-Message 'AnyAiCam could not reach the AnyAiCam cloud. Check that this PC is connected to the internet, then try again.' 48; exit 1 }
        'timeout' { Show-Message 'The AnyAiCam Cloud Agent service did not answer. Restart the computer, then try again.' 48; exit 1 }
        default { Show-Message 'The link was prepared for a different Windows account. Sign in to Windows as the administrator, then choose Start menu > AnyAiCam > Link this computer to AnyAiCam.' 48; exit 1 }
    }
}

# Dot-sourced by the tests (functions only); run from the Start menu or setup.
if ($MyInvocation.InvocationName -ne '.') { Invoke-OpenClaimLink }
