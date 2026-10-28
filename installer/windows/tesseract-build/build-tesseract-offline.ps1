# Guest-only offline preparation/build. No tool installation, networking or release packaging.
# Default is preparation only; -Build compiles in a NEW output directory.
param(
    [Parameter(Mandatory=$true)][string]$Inputs,
    [Parameter(Mandatory=$true)][string]$Output,
    [string]$Python = 'C:\AAC-Test\Python312\python.exe',
    [string]$Supplement = 'C:\AAC-Test\Prerequisites\VS2022-20261028\OCR-Licensing-Supplement',
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'WIN-UVT6JIPSLLK') { throw 'Run only inside the approved Windows test guest' }
$inputRoot = [IO.Path]::GetFullPath($Inputs).TrimEnd('\') + '\'
$outputRoot = [IO.Path]::GetFullPath($Output).TrimEnd('\') + '\'
if ($outputRoot.StartsWith($inputRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Output must not modify the sealed input tree'
}
if (Test-Path -LiteralPath $Output) { throw 'Output exists; preserve previous evidence and select a new directory' }
$toolchain = Get-Content -Raw 'C:\AAC-Test\Evidence\Toolchain-20261028\toolchain-verification.json' | ConvertFrom-Json
if (-not $toolchain.Passed -or $toolchain.InstallerRestartRequested) { throw 'Toolchain verification/restart gate is not satisfied' }
New-Item -ItemType Directory -Path $Output -ErrorAction Stop | Out-Null
Start-Transcript -Path (Join-Path $Output 'preparation-build.log') | Out-Null
function Invoke-Logged([string]$File, [string[]]$Arguments, [string]$Name) {
    $quoted = @($Arguments | ForEach-Object {
        if ($_ -match '"') { throw 'Embedded quotes are not supported in build arguments' }
        '"' + $_ + '"'
    })
    # Wait for the native command, not persistent compiler telemetry descendants.
    $p = Start-Process -FilePath $File -ArgumentList $quoted -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Output "$Name.stdout.log") `
        -RedirectStandardError (Join-Path $Output "$Name.stderr.log")
    # Retain the process handle before exit: Windows PowerShell 5 otherwise may
    # expose a null ExitCode even after WaitForExit reports completion.
    $nativeHandle = $p.Handle
    $p.WaitForExit()
    $p.Refresh()
    $exitCode = $p.ExitCode
    [ordered]@{ File = $File; Arguments = $Arguments; ProcessId = $p.Id;
        RootProcessWait = $true; ExitCode = $exitCode } | ConvertTo-Json -Depth 4 |
        Set-Content (Join-Path $Output "$Name.process.json") -Encoding UTF8
    if ($null -eq $exitCode -or $exitCode -ne 0) { throw "$Name failed with exit code $exitCode; logs retained" }
}
try {
    $manifest = Get-Content -Raw (Join-Path $Inputs 'inputs-manifest.json') | ConvertFrom-Json
    foreach ($entry in $manifest.PSObject.Properties) {
        $file = Join-Path $Inputs ($entry.Name -replace '/', '\')
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
            throw "Input mismatch: $($entry.Name)"
        }
    }
    $lock = Get-Content -Raw (Join-Path $Inputs 'tesseract-minimal.lock.json') | ConvertFrom-Json
    $vsInstances = Get-Content -Raw 'C:\AAC-Test\Evidence\Toolchain-20261028\vswhere.json' | ConvertFrom-Json
    $vs = $vsInstances | Where-Object {
        [IO.Path]::GetFullPath($_.installationPath) -eq 'C:\AAC-Test\BuildTools2022'
    } | Select-Object -First 1
    if (-not $vs -or $vs.installationVersion -notmatch '^17\.') { throw 'Expected VS 2022 instance missing' }
    $cmake = Join-Path $vs.installationPath 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
    $work = Join-Path $Output 'work'
    New-Item -ItemType Directory -Path $work | Out-Null
    Copy-Item -LiteralPath (Join-Path $Inputs 'src') -Destination (Join-Path $work 'src') -Recurse
    Copy-Item -LiteralPath (Join-Path $Inputs 'licenses') -Destination (Join-Path $Output 'input-licenses') -Recurse
    Copy-Item -LiteralPath $Supplement -Destination (Join-Path $Output 'licensing-supplement') -Recurse
    $extraNotices = Join-Path $PSScriptRoot 'notices'
    $noticePins = Get-Content -Raw (Join-Path $extraNotices 'provenance-pinned.json') | ConvertFrom-Json
    foreach ($notice in $noticePins) {
        if ((Get-FileHash -LiteralPath (Join-Path $extraNotices $notice.File) -Algorithm SHA256).Hash -ne $notice.SHA256) {
            throw "Supplemental notice mismatch: $($notice.File)"
        }
    }
    Copy-Item -LiteralPath $extraNotices -Destination (Join-Path $Output 'additional-notices') -Recurse
    $prefix = Join-Path $Inputs 'prefix\Library'
    $buildDir = Join-Path $work 'build'
    $configure = @('-S', (Join-Path $work 'src'), '-B', $buildDir,
        '-G', 'Visual Studio 17 2022', '-A', 'x64', '-T', 'v143',
        '-DCMAKE_SYSTEM_VERSION=10.0.22621.0', "-DCMAKE_PREFIX_PATH=$prefix",
        "-DLeptonica_DIR=$prefix\lib\cmake\leptonica") + @($lock.cmake_options) + @(
        "-DCMAKE_C_FLAGS=$($lock.deterministic_flags.CMAKE_C_FLAGS)",
        "-DCMAKE_CXX_FLAGS=$($lock.deterministic_flags.CMAKE_CXX_FLAGS)",
        "-DCMAKE_EXE_LINKER_FLAGS=$($lock.deterministic_flags.CMAKE_EXE_LINKER_FLAGS)")
    $plan = [ordered]@{
        PreparedUTC = [DateTime]::UtcNow.ToString('o'); InputsVerified = @($manifest.PSObject.Properties).Count
        RunnerSHA256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash
        InputManifestSHA256 = (Get-FileHash -LiteralPath (Join-Path $Inputs 'inputs-manifest.json') -Algorithm SHA256).Hash
        LockSHA256 = (Get-FileHash -LiteralPath (Join-Path $Inputs 'tesseract-minimal.lock.json') -Algorithm SHA256).Hash
        Toolchain = $toolchain; CMake = $cmake; ConfigureArguments = $configure
        BuildArguments = @('--build', $buildDir, '--config', 'Release', '--target', 'tesseract', '--parallel', '1')
        Compiled = $false; LicensingCleared = $false; ReleaseArchiveCreated = $false
    }
    $plan | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $Output 'build-plan.json') -Encoding UTF8
    if ($Build) {
        Invoke-Logged $cmake $configure 'configure'
        Invoke-Logged $cmake $plan.BuildArguments 'compile'
        $engines = @(Get-ChildItem -LiteralPath $buildDir -Recurse -Filter 'tesseract.exe')
        if ($engines.Count -ne 1) { throw 'Expected exactly one compiled tesseract.exe' }
        Invoke-Logged $Python @((Join-Path $PSScriptRoot 'audit-minimal-engine.py'),
            '--exe', $engines[0].FullName, '--inputs', $Inputs, '--output', (Join-Path $Output 'engine')) 'dependency-audit'
        Invoke-Logged $Python @((Join-Path $PSScriptRoot 'prepare-ocr-notices.py'),
            '--inputs', $Inputs, '--supplement', $Supplement, '--extra-notices', $extraNotices,
            '--audit', (Join-Path $Output 'dependency-audit.json'),
            '--output', (Join-Path $Output 'distribution-notices')) 'notice-audit'
        $dumpbin = Get-ChildItem -Path (Join-Path $vs.installationPath 'VC\Tools\MSVC\*\bin\Hostx64\x64\dumpbin.exe') |
            Sort-Object FullName -Descending | Select-Object -First 1
        if (-not $dumpbin) { throw 'dumpbin missing' }
        foreach ($binary in Get-ChildItem -LiteralPath (Join-Path $Output 'engine') -File |
            Where-Object Extension -in '.exe', '.dll') {
            Invoke-Logged $dumpbin.FullName @('/nologo', '/dependents', $binary.FullName) ("dumpbin-" + $binary.Name)
        }
        Invoke-Logged (Join-Path $Output 'engine\tesseract.exe') @('--version') 'engine-version'
        $engineVersion = (Get-Content -Raw (Join-Path $Output 'engine-version.stdout.log')) +
            (Get-Content -Raw (Join-Path $Output 'engine-version.stderr.log'))
        if ($engineVersion -notmatch '(?m)^tesseract 5\.5\.2(?:\s|$)') { throw 'Unexpected OCR engine version' }
        $plan.Compiled = $true
        $plan | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $Output 'build-plan.json') -Encoding UTF8
        # Component audits do not imply OCR acceptance or release authorization.
        Set-Content (Join-Path $Output 'DEPENDENCY-AND-NOTICE-AUDITS-COMPLETE-OCR-PENDING') 'Not a release artifact'
    } else {
        Set-Content (Join-Path $Output 'PREPARED-ONLY') 'No compilation performed; use a new output directory for -Build'
    }
} catch {
    $_ | Out-String | Set-Content (Join-Path $Output 'FAILED.txt')
    throw
} finally { Stop-Transcript | Out-Null }
