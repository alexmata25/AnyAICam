param(
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [Parameter(Mandatory=$true)][string]$SourceCommit,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [switch]$Compile,
    [string]$InnoCompiler
)
$ErrorActionPreference = 'Stop'
# Only caller-supplied offline inputs. No network or service operations.
$stage = Join-Path $OutputRoot 'payload'
& $PythonPath (Join-Path $PSScriptRoot 'stage_payload.py') --payload $Payload --output $stage --source-commit $SourceCommit
if ($LASTEXITCODE -ne 0) { throw 'Offline payload validation failed.' }
if (-not $Compile) {
    Write-Output "Validated local test payload staged at $stage. No EXE compiled."
    return
}
if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw 'An explicitly supplied local Inno Setup compiler is required.'
}
$manifest = Get-Content -LiteralPath (Join-Path $stage 'payload.json') -Raw | ConvertFrom-Json
$compilerArgs = @(
    "/DPayloadRoot=$stage",
    "/DCandidateVersion=$($manifest.version)",
    "/O$OutputRoot",
    (Join-Path $PSScriptRoot 'AnyAiCam-test.iss')
)
& $InnoCompiler @compilerArgs
if ($LASTEXITCODE -ne 0) { throw "Test EXE compilation failed ($LASTEXITCODE)." }
Write-Output 'Local unsigned AnyAiCam-test.exe built. Clean-machine installation and security validation still required.'
