# Build tools only. No installation, PATH changes or tool execution here.
function Read-BuildWheelLock([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Wheel lock is missing.' }
    $entries = @(Get-Content -LiteralPath $Path)
    if ($entries.Count -eq 0) { throw 'Wheel lock is empty.' }
    $seen = @{}
    $parsed = @()
    foreach ($line in $entries) {
        if ($line -cnotmatch '^([0-9a-f]{64})  ([A-Za-z0-9_+.-]+\.whl)$') { throw 'Wheel lock contains a malformed entry.' }
        $digest = $Matches[1]; $name = $Matches[2]
        if ($seen.ContainsKey($name)) { throw "Wheel lock contains a duplicate name: $name" }
        $seen[$name] = $true
        $parsed += [pscustomobject]@{ Name = $name; Sha256 = $digest }
    }
    return $parsed
}

function Resolve-BuildGit([string]$GitExe = '') {
    if ($GitExe) {
        if (-not (Test-Path -LiteralPath $GitExe -PathType Leaf)) { throw 'GitExe does not identify an existing executable.' }
        return $GitExe
    }
    $command = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $installed = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
    if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }
    throw 'Git is unavailable; supply -GitExe pointing to the approved Git executable.'
}

# Blue's sealed 1.3.4 baseline. Inspect PE metadata without running the compiler;
# metadata does not replace approved package/hash or publisher verification.
function Get-BuildInnoVersionProblem($VersionInfo) {
    if ($null -eq $VersionInfo) { return 'Inno Setup compiler version metadata is unavailable.' }
    foreach ($field in @('FileMajorPart', 'FileMinorPart', 'FileBuildPart', 'FilePrivatePart')) {
        if ($null -eq $VersionInfo.PSObject.Properties[$field]) {
            return 'Inno Setup compiler version metadata is incomplete.'
        }
    }
    $actual = '{0}.{1}.{2}.{3}' -f $VersionInfo.FileMajorPart, $VersionInfo.FileMinorPart, $VersionInfo.FileBuildPart, $VersionInfo.FilePrivatePart
    if ($actual -ne '7.1.0.0') {
        return "Inno Setup 7.1.0 is required by the sealed baseline (compiler metadata: $actual). A replacement requires owner approval."
    }
    return ''
}

function Assert-BuildInnoCompiler([string]$Compiler) {
    if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) {
        throw "Inno Setup compiler not found: $Compiler"
    }
    $problem = Get-BuildInnoVersionProblem (Get-Item -LiteralPath $Compiler).VersionInfo
    if ($problem) { throw $problem }
}
