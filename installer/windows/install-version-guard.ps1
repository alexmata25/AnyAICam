<#
Windows 1.3.2 (Blue, Codex FAIL on ef93b503): decides whether this setup may replace what is already on this computer.
Setup (AnyAiCam-VMS.iss InitializeSetup) runs it before anything is changed and continues ONLY when BOTH the exit code
is 0 AND the output contains the line "install-version-guard: exit 0: ..." -- a script that fails to parse or crashes
can end with exit code 0 without that line. Anything else stops the setup (fail closed).

Evidence of an existing installation, from the most to the least authoritative:
  1. this AppId's uninstall entry (64-bit and 32-bit registry views): DisplayVersion, InstallLocation;
  2. %ProgramData%\AnyAiCam\config\vms.env: ANYAICAM_VERSION (written by every Windows setup since 1.3.0; it survives
     an uninstall together with the data);
  3. program files in the install folder (the VMS service wrapper) with neither of the above.
Rules:
  * nothing at all                                     -> clean install                  (exit 0)
  * any recorded version newer than this setup         -> downgrade refused              (exit 10)
  * an uninstall entry without a valid DisplayVersion  -> unknown version, refused       (exit 11)
  * no uninstall entry and vms.env present without a valid ANYAICAM_VERSION (or unreadable) -> refused (exit 11)
  * program files present but no version anywhere      -> refused                        (exit 11)
  * this setup's own version malformed                 -> refused                        (exit 12)
  * otherwise (same version = repair, older = upgrade) -> allowed                        (exit 0)
With a valid uninstall entry, a vms.env that is unreadable or lacks a valid version (the damage a repair reinstall
exists to fix) is logged and does not block; a VALID newer version in vms.env still refuses.
Versions: three or four dot-separated decimal parts (1.3.2, 1.3.2.1); a missing fourth part counts as 0, so 1.3.2 equals
1.3.2.0 and 1.3.2.1 is newer than 1.3.2. Anything else (suffixes, signs, spaces, empty) is malformed.
There is no override switch.
#>
param(
    [string]$SetupVersion = '',
    [string]$DefaultAppDir = '',
    [string]$ProgramData = '',
    # Test seam: provider paths that stand in for the two HKLM uninstall-entry views. Setup never passes it.
    [string[]]$UninstallKeyPaths = @()
)

$AppIdKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}_is1'

function ConvertTo-AnyAiCamVersion([string]$Text) {
    # [long[4]] for a well-formed version, $null otherwise.
    if ($null -eq $Text -or $Text -notmatch '^(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2,3}$') { return $null }
    $parts = @($Text.Split('.') | ForEach-Object { [long]$_ })
    while ($parts.Count -lt 4) { $parts += 0 }
    return ,[long[]]$parts
}

function Compare-AnyAiCamVersion([long[]]$A, [long[]]$B) {
    for ($i = 0; $i -lt 4; $i++) {
        if ($A[$i] -gt $B[$i]) { return 1 }
        if ($A[$i] -lt $B[$i]) { return -1 }
    }
    return 0
}

function Get-UninstallEntries {
    $entries = @()
    if ($UninstallKeyPaths.Count) {
        foreach ($path in $UninstallKeyPaths) {
            if (Test-Path -LiteralPath $path) {
                $item = Get-ItemProperty -LiteralPath $path -ErrorAction SilentlyContinue
                $entries += [pscustomobject]@{ View = $path; DisplayVersion = $item.DisplayVersion; InstallLocation = $item.InstallLocation }
            }
        }
        return $entries
    }
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64, [Microsoft.Win32.RegistryView]::Registry32)) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, $view)
        try {
            $key = $base.OpenSubKey($AppIdKey)
            if ($key) {
                try { $entries += [pscustomobject]@{ View = "HKLM $view"; DisplayVersion = $key.GetValue('DisplayVersion'); InstallLocation = $key.GetValue('InstallLocation') } }
                finally { $key.Close() }
            }
        } finally { $base.Close() }
    }
    return $entries
}

function Get-InstallDecision {
    # Returns @{ Code; Reason }. Pure apart from reading the registry entry, vms.env and the install folder.
    $setup = ConvertTo-AnyAiCamVersion $SetupVersion
    if ($null -eq $setup) { return @{ Code = 12; Reason = "this setup's version '$SetupVersion' is not a valid AnyAiCam version" } }

    $entries = @(Get-UninstallEntries)
    $root = if ($ProgramData) { $ProgramData } else { [Environment]::GetFolderPath('CommonApplicationData') }
    $envFile = Join-Path $root 'AnyAiCam\config\vms.env'
    $envExists = Test-Path -LiteralPath $envFile -PathType Leaf
    $envVersionText = $null; $envReadable = $false
    if ($envExists) {
        try {
            $lines = [IO.File]::ReadAllLines($envFile)
            $envReadable = $true
            $line = @($lines | Where-Object { $_ -like 'ANYAICAM_VERSION=*' }) | Select-Object -Last 1
            if ($line) { $envVersionText = $line.Substring('ANYAICAM_VERSION='.Length).Trim() }
        } catch { $envReadable = $false }
    }
    $appDirs = @($DefaultAppDir) + @($entries | ForEach-Object { $_.InstallLocation }) | Where-Object { $_ } | Select-Object -Unique
    $programFiles = @($appDirs | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'service\AnyAiCamVMS.exe') -PathType Leaf })

    if (-not $entries.Count -and -not $envExists -and -not $programFiles.Count) {
        return @{ Code = 0; Reason = 'no existing AnyAiCam VMS installation: clean install' }
    }

    $found = @()
    foreach ($entry in $entries) {
        $v = ConvertTo-AnyAiCamVersion ([string]$entry.DisplayVersion)
        if ($null -eq $v) { return @{ Code = 11; Reason = "the installed AnyAiCam VMS ($($entry.View)) has a missing or malformed version '$($entry.DisplayVersion)'" } }
        $found += ,@($v, "uninstall entry ($($entry.View)) $($entry.DisplayVersion)")
    }
    $envVersion = if ($envReadable) { ConvertTo-AnyAiCamVersion $envVersionText } else { $null }
    if ($envExists) {
        if ($null -ne $envVersion) { $found += ,@($envVersion, "data folder vms.env $envVersionText") }
        elseif (-not $entries.Count) {
            $why = if (-not $envReadable) { 'cannot be read' } elseif ($null -eq $envVersionText) { 'has no ANYAICAM_VERSION' } else { "has a malformed ANYAICAM_VERSION '$envVersionText'" }
            return @{ Code = 11; Reason = "existing AnyAiCam data was found but its vms.env $why" }
        }
        else { [Console]::Out.WriteLine('install-version-guard: vms.env unreadable or without a valid version; the uninstall entry decides (repair)') }
    }
    if (-not $found.Count) {
        return @{ Code = 11; Reason = "AnyAiCam program files were found in $($programFiles -join ', ') without any version information" }
    }
    foreach ($item in $found) {
        if ((Compare-AnyAiCamVersion $item[0] $setup) -gt 0) {
            return @{ Code = 10; Reason = "a newer AnyAiCam VMS is recorded ($($item[1])); this setup is $SetupVersion" }
        }
    }
    $highest = $found | ForEach-Object { $_[1] }
    return @{ Code = 0; Reason = "existing installation ($($highest -join '; ')) is not newer than ${SetupVersion}: repair or upgrade" }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $decision = Get-InstallDecision
    } catch {
        $decision = @{ Code = 13; Reason = "the version check failed: $($_.Exception.Message)" }
    }
    Write-Output ("install-version-guard: exit {0}: {1}" -f $decision.Code, $decision.Reason)
    exit $decision.Code
}
