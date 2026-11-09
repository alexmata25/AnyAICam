# Focused non-build regression for MakeCat's classic MAX_PATH behavior.
# This test calls only the pure path validator; it never runs MakeCat, SignTool,
# Azure signing, Inno Setup, or the installer build.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'runtime-catalog.ps1'
$tokens = $null; $parseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'runtime-catalog.ps1 has syntax errors.' }
. $scriptPath

$script:passed = 0
$script:failed = 0
function Check([string]$Name, [bool]$Condition) {
    if ($Condition) { $script:passed++; Write-Output ('PASS ' + $Name) }
    else { $script:failed++; Write-Output ('FAIL ' + $Name) }
}
function Is-Rejected([scriptblock]$Action) {
    try { & $Action; return $false } catch { return $true }
}

$longDllName = 'libopenblas64__v0.3.23-293-gc2f4bdbb-gcc_10_3_0-2bde3a66a51006b2b53eb373ff767a3f.dll'
$failedPath = 'C:\Users\Alejandro Mata\.codex\visualizations\2026\10\26\01a17902-1ce2-7cc2-8d16-ea9dcd16f9ef\windows1341-fdddbe7-internal-signing\installer\windows\output-internal-test\runtime-catalog\56\numpy.libs\' + $longDllName
$catalogPath = 'C:\build\output-internal-test\AnyAiCamRuntime.cat'
$cdfPath = 'C:\build\output-internal-test\AnyAiCamRuntime.cdf'
Check 'reproduces historical 284-character member path failure' ($failedPath.Length -eq 284 -and (Is-Rejected { Assert-MakeCatPathLengths $catalogPath $cdfPath @($failedPath) }))

# MAX_PATH includes the terminating NUL, so a 259-character DOS path is the
# largest accepted normal path and 260 is rejected before MakeCat is invoked.
$path259 = 'C:\' + ('a' * 100) + '\' + ('b' * 100) + '\' + ('c' * 54)
$path260 = 'C:\' + ('a' * 100) + '\' + ('b' * 100) + '\' + ('c' * 55)
Check '259-character member path remains permitted' ($path259.Length -eq 259 -and -not (Is-Rejected { Assert-MakeCatPathLengths $catalogPath $cdfPath @($path259) }))
Check '260-character member path is rejected' ($path260.Length -eq 260 -and (Is-Rejected { Assert-MakeCatPathLengths $catalogPath $cdfPath @($path260) }))

$shortStage = 'C:\Users\Alejandro Mata\AppData\Local\Temp\AnyAiCamCatalog-0123456789'
$shortMember = Join-Path (Join-Path $shortStage '56\numpy.libs') $longDllName
Check 'short temporary staging path accepts the same long-named DLL' ($shortMember.Length -lt 260 -and -not (Is-Rejected { Assert-MakeCatPathLengths $catalogPath $cdfPath @($shortMember) }))
Check 'long catalog or CDF path is also rejected' (Is-Rejected { Assert-MakeCatPathLengths ($path260) $cdfPath @() })

$buildText = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'build.ps1') -Raw
$exportAt = $buildText.IndexOf('Export-NativeFiles $catalogWorkStage', [StringComparison]::Ordinal)
$makeCatAt = $buildText.IndexOf('New-RuntimeCatalog $catalogWorkStage', [StringComparison]::Ordinal)
$moveAt = $buildText.IndexOf('Move-Item -LiteralPath $catalogWorkStage -Destination $catalogStage', [StringComparison]::Ordinal)
Check 'build catalogs from unique temp stage then moves it into package output' ($exportAt -ge 0 -and $makeCatAt -gt $exportAt -and $moveAt -gt $makeCatAt -and $buildText.Contains("Join-Path `$catalogTempRoot ('AnyAiCamCatalog-'"))
Check 'existing temp stages are rejected rather than removed' ((Get-Content -LiteralPath $scriptPath -Raw).Contains('Runtime catalog stage already exists; refusing to overwrite it'))

Write-Output ("Runtime catalog path tests: " + $script:passed + ' passed, ' + $script:failed + ' failed')
if ($script:failed -ne 0) { exit 1 }
