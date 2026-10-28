# Installer size policy (Windows 1.3.4.1; owner budget 2026-10-23: at most 750 MB).
#
# 750 MB is counted in decimal bytes (750,000,000), the stricter reading. build.ps1 refuses a larger setup right after
# Inno Setup writes it, so an oversized installer is never signed, verified or handed on. Today's setup is about
# 618.4 MB; the Tesseract bundle adds about 25.5 MB (compressed). Changing the budget is an owner decision: edit this
# constant and its test together (installer/tests/test_windows_acceptance_tools_1341.py).
$AnyAiCamSetupMaxBytes = [long]750000000

# $null when the file is within the budget, else the reason. A missing file is a problem too (the gate must not pass
# silently when the build wrote its setup somewhere else).
function Get-AnyAiCamSetupSizeProblem([string]$Path, [long]$MaxBytes = $AnyAiCamSetupMaxBytes) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "setup not found: $Path" }
    $size = (Get-Item -LiteralPath $Path).Length
    if ($size -gt $MaxBytes) {
        return ('{0} is {1:N0} bytes, over the {2:N0}-byte (750 MB) installer budget by {3:N0} bytes' -f (Split-Path -Leaf $Path), $size, $MaxBytes, ($size - $MaxBytes))
    }
    return $null
}
