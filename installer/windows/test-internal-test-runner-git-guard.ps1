# Static regression for the internal-test runner's last-moment Git checks.
# This test parses source only; it never invokes the runner, build, MakeCat,
# SignTool, Azure signing, Inno Setup, VM, or Sandbox.
$ErrorActionPreference = 'Stop'
$runnerPath = Join-Path $PSScriptRoot 'Run-Windows-1.3.4.1-InternalTest.ps1'
$buildPath = Join-Path $PSScriptRoot 'build.ps1'
$tokens = $null; $parseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Internal-test runner has syntax errors.' }

$runnerText = Get-Content -LiteralPath $runnerPath -Raw
$buildText = Get-Content -LiteralPath $buildPath -Raw
$missingCommitIsRejected = $runnerText.Contains('[Parameter(Mandatory = $true)]')
$malformedCommitIsRejected = $runnerText.Contains("[ValidatePattern('^[0-9a-fA-F]{40}$')]")
$commitIsNormalized = $runnerText.Contains('$ExpectedCommit = $ExpectedCommit.ToLowerInvariant()')
$commitPrefixAt = $runnerText.IndexOf('$commitPrefix = $ExpectedCommit.Substring(0, 7)', [StringComparison]::Ordinal)
$normalizeCommitAt = $runnerText.IndexOf('$ExpectedCommit = $ExpectedCommit.ToLowerInvariant()', [StringComparison]::Ordinal)
$evidenceNameAt = $runnerText.IndexOf('$EvidenceDir = Join-Path $EvidenceParent ("Windows-1.3.4.1-" + $commitPrefix + "-"', [StringComparison]::Ordinal)
$expectedInstallerAt = $runnerText.IndexOf("`$expectedSetupName = 'AnyAiCam-VMS-Setup-1.3.4.1-' + `$commitPrefix + '-INTERNAL-TEST-DO-NOT-SHIP.exe'", [StringComparison]::Ordinal)
$setupPathAt = $runnerText.IndexOf('$setupPath = Join-Path $OutputDir $expectedSetupName', [StringComparison]::Ordinal)
$setupRejectAt = $runnerText.IndexOf("if (-not (Test-Path -LiteralPath `$setupPath -PathType Leaf)) { throw 'Expected internal-test installer is missing.' }", [StringComparison]::Ordinal)
$buildInstallerConventionPresent = $buildText.Contains("'AnyAiCam-VMS-Setup-' + `$Version + '-' + `$sourceCommit.Substring(0, 7) + `$setupSuffix + '.exe'") -and $buildText.Contains("'-INTERNAL-TEST-DO-NOT-SHIP'")
$installerNameMatchesBuildConvention = $expectedInstallerAt -ge 0 -and $setupPathAt -gt $expectedInstallerAt -and $runnerText.Substring($expectedInstallerAt, $setupPathAt - $expectedInstallerAt).Contains('$commitPrefix') -and $buildInstallerConventionPresent
$incorrectInstallerNameIsRejected = $setupPathAt -gt $expectedInstallerAt -and $setupRejectAt -gt $setupPathAt -and $runnerText.Contains("throw 'Expected internal-test installer is missing.'")
$evidenceNameUsesValidatedCommit = $commitIsNormalized -and $normalizeCommitAt -ge 0 -and $commitPrefixAt -gt $normalizeCommitAt -and $evidenceNameAt -gt $commitPrefixAt
$noStaleBaselinePrefix = -not $runnerText.Contains('711ab21')
$headCaptureAt = $runnerText.IndexOf('$finalHeadLines = @(& git -C $Repo rev-parse HEAD', [StringComparison]::Ordinal)
$headExitAt = $runnerText.IndexOf('$finalHeadExitCode = $LASTEXITCODE', [StringComparison]::Ordinal)
$statusCaptureAt = $runnerText.IndexOf('$finalStatusLines = @(& git -C $Repo status --porcelain=v1 --untracked-files=all', [StringComparison]::Ordinal)
$statusExitAt = $runnerText.IndexOf('$finalStatusExitCode = $LASTEXITCODE', [StringComparison]::Ordinal)
$cleanCheckAt = $runnerText.IndexOf('$finalGitClean =', [StringComparison]::Ordinal)
$finalCheckAt = $runnerText.IndexOf("Add-Check 'Final source/owned-lock/output recheck'", [StringComparison]::Ordinal)
$initialBranchCheck = $runnerText.Contains("Add-Check 'Approved source branch' (`$branchCode -eq 0 -and `$branch -ceq `$ExpectedBranch)")
$initialHeadCheck = $runnerText.Contains("Add-Check 'Approved exact source commit' (`$headCode -eq 0 -and `$head -ceq `$ExpectedCommit)")
$initialStatusCheck = $runnerText.Contains("Add-Check 'Clean Git worktree' (`$statusCode -eq 0 -and `$statusLines.Count -eq 0)")

$captureOrderIsSafe = $headCaptureAt -ge 0 -and $headExitAt -gt $headCaptureAt -and $statusCaptureAt -gt $headExitAt -and $statusExitAt -gt $statusCaptureAt
$failClosedExpression = $cleanCheckAt -ge 0 -and $finalCheckAt -gt $cleanCheckAt -and $runnerText.Substring($cleanCheckAt, $finalCheckAt - $cleanCheckAt).Contains('$finalHeadExitCode -eq 0 -and $finalStatusExitCode -eq 0')
$assertions = [ordered]@{
    'missing expected commit is rejected' = $missingCommitIsRejected
    'malformed expected commit is rejected' = $malformedCommitIsRejected
    'installer name uses the build SHA-prefix convention' = $installerNameMatchesBuildConvention
    'incorrect installer name is rejected' = $incorrectInstallerNameIsRejected
    'evidence naming uses the validated commit prefix' = $evidenceNameUsesValidatedCommit
    'first Git checkpoint requires successful commands' = ($initialBranchCheck -and $initialHeadCheck -and $initialStatusCheck)
    'final Git checkpoint captures and requires both successful exits' = ($captureOrderIsSafe -and $failClosedExpression)
    'runner contains no stale baseline SHA in executable naming' = $noStaleBaselinePrefix
}
foreach ($assertion in $assertions.GetEnumerator()) {
    if (-not $assertion.Value) { throw ('FAIL ' + $assertion.Key) }
}
Write-Output ("Runner identity and fail-closed source assertions: " + $assertions.Count + ' passed, 0 failed.')
