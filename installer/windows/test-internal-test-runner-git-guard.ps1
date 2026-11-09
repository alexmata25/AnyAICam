# Static regression for the internal-test runner's last-moment Git checks.
# This test parses source only; it never invokes the runner, build, MakeCat,
# SignTool, Azure signing, Inno Setup, VM, or Sandbox.
$ErrorActionPreference = 'Stop'
$runnerPath = Join-Path $PSScriptRoot 'Run-Windows-1.3.4.1-InternalTest.ps1'
$tokens = $null; $parseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Internal-test runner has syntax errors.' }

$runnerText = Get-Content -LiteralPath $runnerPath -Raw
$commitParameterIsRequired = $runnerText.Contains('[Parameter(Mandatory = $true)]') -and $runnerText.Contains("[ValidatePattern('^[0-9a-fA-F]{40}$')]") -and $runnerText.Contains('$ExpectedCommit = $ExpectedCommit.ToLowerInvariant()')
$headCaptureAt = $runnerText.IndexOf('$finalHeadLines = @(& git -C $Repo rev-parse HEAD', [StringComparison]::Ordinal)
$headExitAt = $runnerText.IndexOf('$finalHeadExitCode = $LASTEXITCODE', [StringComparison]::Ordinal)
$statusCaptureAt = $runnerText.IndexOf('$finalStatusLines = @(& git -C $Repo status --porcelain=v1 --untracked-files=all', [StringComparison]::Ordinal)
$statusExitAt = $runnerText.IndexOf('$finalStatusExitCode = $LASTEXITCODE', [StringComparison]::Ordinal)
$cleanCheckAt = $runnerText.IndexOf('$finalGitClean =', [StringComparison]::Ordinal)
$finalCheckAt = $runnerText.IndexOf("Add-Check 'Final source/owned-lock/output recheck'", [StringComparison]::Ordinal)

$captureOrderIsSafe = $headCaptureAt -ge 0 -and $headExitAt -gt $headCaptureAt -and $statusCaptureAt -gt $headExitAt -and $statusExitAt -gt $statusCaptureAt
$failClosedExpression = $cleanCheckAt -ge 0 -and $finalCheckAt -gt $cleanCheckAt -and $runnerText.Substring($cleanCheckAt, $finalCheckAt - $cleanCheckAt).Contains('$finalHeadExitCode -eq 0 -and $finalStatusExitCode -eq 0')
if (-not $commitParameterIsRequired) { throw 'The runner must require a full expected commit SHA at invocation.' }
if (-not $captureOrderIsSafe) { throw 'Final Git command exit statuses are not captured immediately after their commands.' }
if (-not $failClosedExpression) { throw 'Final clean-check does not require both Git exit statuses to be zero.' }
Write-Output 'Runner Git guard source assertions: 3 passed, 0 failed (not executed).'
