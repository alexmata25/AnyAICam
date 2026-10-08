# Build integrity for the Windows setup (2026-10-08), dot-sourced by build.ps1.
#
# The setup is named after, and installs ANYAICAM_BUILD_ID for, one exact
# commit, so everything it packages must BE that commit. Until 2026-10-08
# build.ps1 checked only app/ and installer/windows/, while the .iss also
# packages appliance-agent\anyaicam_agent\* (the cloud agent that runs as
# LocalSystem): an edited, untracked or git-ignored agent file was shipped
# inside a setup that claimed a clean commit.
#
# The repository folders checked are read from the .iss's own [Files]
# Source: "..\..\<folder>\*" lines, so a folder packaged later is covered
# without editing this file. In each of them, and in installer/windows,
# every file must be tracked and unchanged; a git-ignored file fails too
# (the .iss copies whatever is on disk), except where the .iss itself
# excludes it (Excludes: "tests\*,__pycache__\*"). installer/windows is
# packaged file by file plus vendor/, which build.ps1 pins by SHA-256
# (Get-VendorProblems), so an ignored file there is allowed. Nothing is
# changed.

function Get-PackagedRepoFolders([string]$IssPath) {
    $folders = [ordered]@{}
    foreach ($line in Get-Content -LiteralPath $IssPath) {
        if ($line -notmatch '^\s*Source:\s*"\.\.\\\.\.\\([^"]+?)\\\*"') { continue }
        $folder = $Matches[1] -replace '\\', '/'
        $excludes = @()
        if ($line -match 'Excludes:\s*"([^"]*)"') {
            foreach ($pattern in ($Matches[1] -split ',')) {
                # Only "<folder name>\*" is understood; anything else must be
                # handled here before the .iss may use it.
                if ($pattern.Trim() -notmatch '^([A-Za-z0-9_.-]+)\\\*$') { throw "source-integrity.ps1 does not understand the Excludes pattern '$pattern' in $IssPath" }
                $excludes += $Matches[1]
            }
        }
        $folders[$folder] = $excludes
    }
    return $folders
}

function Get-PackagedSourceProblems([string]$Repo, [string]$IssPath) {
    $folders = Get-PackagedRepoFolders $IssPath
    if (-not $folders.Contains('app') -or -not $folders.Contains('appliance-agent/anyaicam_agent')) {
        throw "Expected app and appliance-agent/anyaicam_agent among the packaged folders of $IssPath."
    }
    $paths = @($folders.Keys) + @('installer/windows')
    $status = & git -C $Repo -c core.quotepath=off status --porcelain=v1 --ignored --untracked-files=all -- @paths
    if ($LASTEXITCODE -ne 0) { throw 'git status failed; cannot verify the packaged sources.' }
    $problems = @()
    foreach ($entry in @($status)) {
        if (-not $entry) { continue }
        $code = $entry.Substring(0, 2)
        $path = $entry.Substring(3).Trim('"')
        if ($code -eq '!!') {
            # installer/windows is packaged file by file (each one tracked,
            # so a change to it fails above) plus the pinned vendor/: an
            # ignored file there (output/, __pycache__/ from the tests) is
            # never packaged.
            if ($path -like 'installer/windows/*') { continue }
            $folder = @($folders.Keys | Where-Object { $path -like "$_/*" }) | Select-Object -First 1
            if ($folder) {
                # Inno's "<name>\*" excludes a folder of that name at any depth.
                $inside = $path.Substring($folder.Length + 1).Split('/')
                $parents = if ($inside.Count -gt 1) { $inside[0..($inside.Count - 2)] } else { @() }
                $excluded = @($folders[$folder] | Where-Object { $parents -contains $_ })
                if ($excluded.Count) { continue }
            }
            $problems += "ignored but packaged: $path"
        } elseif ($code -eq '??') {
            $problems += "untracked: $path"
        } else {
            $problems += "uncommitted change ($($code.Trim())): $path"
        }
    }
    return $problems
}

# vendor/ holds exactly the pinned inputs and the locked wheels: an extra file
# there would be packaged too ("vendor\wheels\*" is recursive).
function Get-VendorProblems([string]$Vendor, [string[]]$PinnedFiles, [string[]]$LockedWheels) {
    $problems = @()
    $expected = @($PinnedFiles) + @($LockedWheels | ForEach-Object { 'wheels/' + $_ })
    # Listed from the same path string it is cut from (a short 8.3 or a long
    # spelling of the folder would otherwise not line up).
    $folder = (Get-Item -LiteralPath $Vendor).FullName.TrimEnd('\')
    $root = $folder + '\'
    foreach ($file in Get-ChildItem -LiteralPath $folder -Recurse -File -Force) {
        $relative = $file.FullName.Substring($root.Length) -replace '\\', '/'
        if ($expected -notcontains $relative) { $problems += "not pinned: vendor/$relative" }
    }
    return $problems
}
