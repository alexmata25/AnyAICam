# Build tools only. No installation, PATH changes or tool execution here.
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
