# Shared helpers for the Windows 1.3.4.1 installer acceptance tools (Blue, 2026-10-28).
#
# Test tools only: nothing here is installed by the setup. Dot-source it:  . (Join-Path $PSScriptRoot 'acceptance-common.ps1')
#
#   Invoke-AcceptanceProbe   runs a probe script as LocalSystem (a one-shot SYSTEM scheduled task, removed afterwards)
#                            or as the current user, and returns the JSON the probe wrote.
#   Test-AcceptanceAdmin     true when the session is elevated (LocalSystem mode needs it).
#   New-AcceptanceResult / Add-AcceptanceCheck / Complete-AcceptanceResult   uniform PASS/FAIL/BLOCKED JSON reports.

function Test-AcceptanceAdmin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-AcceptanceResult([string]$Name, [string]$Mode) {
    return [ordered]@{ test = $Name; mode = $Mode; computer = $env:COMPUTERNAME; started = (Get-Date).ToUniversalTime().ToString('o'); checks = @(); result = 'PENDING' }
}

function Add-AcceptanceCheck($Result, [string]$Name, [bool]$Passed, [string]$Detail = '') {
    $Result.checks += [ordered]@{ name = $Name; passed = $Passed; detail = $Detail }
}

# PASS only when there is at least one check and none failed; BLOCKED when the run could not happen (no evidence).
function Complete-AcceptanceResult($Result, [string]$OutputPath, [string]$Blocked = '') {
    if ($Blocked) { $Result.result = 'BLOCKED'; $Result.blocked_reason = $Blocked }
    elseif (-not @($Result.checks).Count) { $Result.result = 'BLOCKED'; $Result.blocked_reason = 'no checks ran' }
    elseif (@($Result.checks | Where-Object { -not $_.passed }).Count) { $Result.result = 'FAIL' }
    else { $Result.result = 'PASS' }
    $Result.finished = (Get-Date).ToUniversalTime().ToString('o')
    if ($OutputPath) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null
        $Result | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 -LiteralPath $OutputPath
    }
    return $Result
}

# Runs $ProbeScript (a .ps1 that writes JSON to the -ResultPath it is given) and returns the parsed JSON.
#   -RunAs LocalSystem : a one-shot scheduled task with principal SYSTEM (what WinSW services run as). Needs elevation.
#   -RunAs CurrentUser : the same probe in a child powershell.exe of this user (local/mocked runs).
function Invoke-AcceptanceProbe {
    param(
        [Parameter(Mandatory=$true)][string]$ProbeScript,
        [hashtable]$Arguments = @{},
        [ValidateSet('LocalSystem', 'CurrentUser')][string]$RunAs = 'LocalSystem',
        [int]$TimeoutSeconds = 180,
        [string]$WorkDir = $env:TEMP
    )
    $id = 'AnyAiCamAcceptance-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $folder = Join-Path $WorkDir $id
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $resultPath = Join-Path $folder 'probe-result.json'
    $argumentText = (@("-ResultPath `"$resultPath`"") + @($Arguments.GetEnumerator() | Sort-Object Name | ForEach-Object {
        if ($_.Value -is [bool] -or $_.Value -is [switch]) { if ($_.Value) { "-$($_.Name)" } } else { "-$($_.Name) `"$($_.Value)`"" } })) -join ' '
    $commandLine = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$ProbeScript`" $argumentText"
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ($RunAs -eq 'CurrentUser') {
        $process = Start-Process -FilePath $powershell -ArgumentList $commandLine -PassThru -WindowStyle Hidden
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue; throw "Probe timed out after $TimeoutSeconds s." }
    } else {
        if (-not (Test-AcceptanceAdmin)) { throw 'LocalSystem mode needs an elevated (Administrator) PowerShell.' }
        # SYSTEM must be able to write the result: the folder is under the caller's TEMP, so grant it explicitly.
        & icacls.exe $folder /grant '*S-1-5-18:(OI)(CI)F' /Q | Out-Null
        $action = New-ScheduledTaskAction -Execute $powershell -Argument $commandLine
        $principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds $TimeoutSeconds) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $id -TaskPath '\AnyAiCamAcceptance\' -Action $action -Principal $principal -Settings $settings | Out-Null
        try {
            Start-ScheduledTask -TaskName $id -TaskPath '\AnyAiCamAcceptance\'
            $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
            do { Start-Sleep -Milliseconds 500; $state = (Get-ScheduledTask -TaskName $id -TaskPath '\AnyAiCamAcceptance\').State }
            while ($state -eq 'Running' -and (Get-Date) -lt $deadline)
            if ($state -eq 'Running') { throw "Probe still running as LocalSystem after $TimeoutSeconds s." }
        } finally { Unregister-ScheduledTask -TaskName $id -TaskPath '\AnyAiCamAcceptance\' -Confirm:$false -ErrorAction SilentlyContinue }
    }
    if (-not (Test-Path -LiteralPath $resultPath)) { throw "The probe wrote no result ($resultPath)." }
    $json = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json
    Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue
    return $json
}

# The exact process environment service-launcher.ps1 gives the VMS for Tesseract (kept in sync by a static test).
function Get-AnyAiCamTesseractEnvironment([string]$InstallRoot) {
    return @{
        Path = (Join-Path $InstallRoot 'runtime\tools\tesseract')
        TessdataPrefix = (Join-Path $InstallRoot 'runtime\tools\tesseract\tessdata')
    }
}
