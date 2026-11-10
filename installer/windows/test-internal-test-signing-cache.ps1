# Focused offline regression for the internal-test uninstaller signing callback.
# Uses a mocked verifier and inert files; never runs SignTool, ISCC, Azure signing,
# MakeCat, the installer, a VM, or Sandbox.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'internal-test-signing.ps1'
$buildPath = Join-Path $PSScriptRoot 'build.ps1'
$buildText = Get-Content -LiteralPath $buildPath -Raw
. $scriptPath

$checks = 0
function Check([string]$Name, [bool]$Passed) {
    $script:checks++
    if (-not $Passed) { throw "FAIL: $Name" }
    Write-Output "PASS: $Name"
}
function Expect-Throw([scriptblock]$Action, [string]$Name) {
    $threw = $false
    try { & $Action } catch { $threw = $true }
    Check $Name $threw
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('AnyAiCamUninstallerCacheTest-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null
try {
    $cache = Join-Path $tempRoot 'signed-uninstallers'
    New-Item -ItemType Directory -Path $cache -ErrorAction Stop | Out-Null
    $compilerInput = Join-Path $cache 'uninst.e32.tmp'
    [IO.File]::WriteAllBytes($compilerInput, [Text.Encoding]::UTF8.GetBytes('mocked signed uninstaller bytes'))
    $verifyCalls = 0
    $mockVerify = { param([string]$Path) $script:verifyCalls++; if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Verifier target missing.' } }
    $published = Publish-VerifiedSignedUninstaller -SourcePath $compilerInput -CacheDirectory $cache -VerifySignature $mockVerify
    $expectedCache = Join-Path $cache 'AnyAiCamSignedUninstaller.exe'
    Check 'verified Inno temporary uninstaller is retained as an .exe cache artifact' ($published.FullName -eq $expectedCache -and (Test-Path -LiteralPath $expectedCache -PathType Leaf))
    Check 'source and cached uninstaller bytes match exactly' ((Get-FileHash -Algorithm SHA256 $compilerInput).Hash -eq (Get-FileHash -Algorithm SHA256 $expectedCache).Hash)
    Check 'source and copied cache each pass the verifier' ($verifyCalls -eq 2)

    $otherCache = Join-Path $tempRoot 'failed-cache'
    New-Item -ItemType Directory -Path $otherCache -ErrorAction Stop | Out-Null
    $failedInput = Join-Path $otherCache 'uninst.e32.tmp'
    [IO.File]::WriteAllText($failedInput, 'unsigned or unverifiable')
    $rejectVerify = { param([string]$Path) throw 'mock signature rejection' }
    Expect-Throw { Publish-VerifiedSignedUninstaller -SourcePath $failedInput -CacheDirectory $otherCache -VerifySignature $rejectVerify | Out-Null } 'signature-verification failure rejects cache promotion'
    Check 'failed verification leaves no accepted .exe cache artifact' (@(Get-ChildItem -LiteralPath $otherCache -Filter '*.exe' -File).Count -eq 0)

    $outside = Join-Path $tempRoot 'setup.exe'
    [IO.File]::WriteAllText($outside, 'setup')
    $outsideCache = Join-Path $tempRoot 'outside-cache'
    New-Item -ItemType Directory -Path $outsideCache -ErrorAction Stop | Out-Null
    $unusedVerify = { param([string]$Path) throw 'setup must not be cached as an uninstaller' }
    $notCached = Publish-VerifiedSignedUninstaller -SourcePath $outside -CacheDirectory $outsideCache -VerifySignature $unusedVerify
    Check 'setup signing input outside the uninstaller directory is not cached' ($null -eq $notCached -and @(Get-ChildItem -LiteralPath $outsideCache -Force).Count -eq 0)

    $unexpected = Join-Path $cache 'unexpected.tmp'
    [IO.File]::WriteAllText($unexpected, 'unexpected compiler target')
    Expect-Throw { Test-InternalUninstallerTarget $unexpected $cache | Out-Null } 'unexpected file in uninstaller cache directory is rejected'
    Expect-Throw { Publish-VerifiedSignedUninstaller -SourcePath $compilerInput -CacheDirectory $cache -VerifySignature $mockVerify | Out-Null } 'existing cache artifact is never overwritten'

    Check 'internal test uses the verified signing callback' ($buildText.Contains('internal-test-signing.ps1') -and $buildText.Contains(' -File $q') -and $buildText.Contains('-TargetPath $f'))
    Check 'post-build rejects extra cache entries and requires one expected cached uninstaller' ($buildText.Contains('$cacheEntries.Count -ne 1') -and $buildText.Contains('$signedUninstallers.Count -ne 1') -and $buildText.Contains('AnyAiCamSignedUninstaller.exe'))
    Check 'post-build signature checks remain fail-closed' ($buildText.Contains('Get-AuthenticodeSignature -LiteralPath $file.FullName') -and $buildText.Contains('& $signTool verify /pa /all $file.FullName') -and $buildText.Contains('if ($LASTEXITCODE -ne 0)'))
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output ("RESULT: $checks checks passed.")
