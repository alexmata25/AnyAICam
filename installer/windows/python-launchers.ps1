# pip creates distlib .exe wrappers in runtime\python\Scripts, but the VMS
# invokes modules with python.exe -m and invokes FFmpeg/Tesseract from their
# separately bundled tool directories. These wrappers are not part of the
# product runtime and cannot be in the build-time catalog because pip creates
# them on the target machine. Remove only top-level .exe launchers after pip.
function Remove-AnyAiCamGeneratedPythonLaunchers([Parameter(Mandatory=$true)][string]$PythonRoot) {
    if (-not (Test-Path -LiteralPath $PythonRoot -PathType Container)) { throw "Private Python root not found: $PythonRoot" }
    $scripts = Join-Path (Get-Item -LiteralPath $PythonRoot).FullName 'Scripts'
    if (-not (Test-Path -LiteralPath $scripts)) { return 0 }
    $scriptsItem = Get-Item -LiteralPath $scripts -Force
    if ($scriptsItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to process a reparse-point Python Scripts directory: $scripts" }
    $launchers = @(Get-ChildItem -LiteralPath $scripts -Filter '*.exe' -File -Force)
    foreach ($launcher in $launchers) {
        if ($launcher.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to remove a reparse-point Python launcher: $($launcher.FullName)" }
        Remove-Item -LiteralPath $launcher.FullName -Force -ErrorAction Stop
    }
    $remaining = @(Get-ChildItem -LiteralPath $scripts -Filter '*.exe' -File -Force)
    if ($remaining.Count) { throw "Python script launchers remain after cleanup: $(($remaining | ForEach-Object Name) -join ', ')" }
    return $launchers.Count
}
