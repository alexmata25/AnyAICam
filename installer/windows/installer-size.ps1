# Release acceptance uses the conservative decimal interpretation of 750 MB.
function Assert-InstallerSize {
    param([Parameter(Mandatory=$true)][string]$Path,
          [ValidateRange(1,750000000)][long]$MaxBytes = 750000000)
    $file = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($file.PSIsContainer -or $file.Length -le 0 -or $file.Length -gt $MaxBytes) {
        throw "Installer size refused: $Path ($($file.Length) bytes, allowed 1..$MaxBytes)."
    }
    return $file.Length
}
