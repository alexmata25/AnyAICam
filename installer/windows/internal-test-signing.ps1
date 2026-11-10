param(
    [string]$SignToolPath,
    [string]$DlibPath,
    [string]$MetadataPath,
    [string]$TimestampUrl,
    [string]$UninstallerCacheDir,
    [string]$TargetPath
)

$ErrorActionPreference = 'Stop'

function Test-InternalUninstallerTarget {
    param([string]$Path, [string]$CacheDirectory)
    $fullPath = [IO.Path]::GetFullPath($Path)
    $fullCache = [IO.Path]::GetFullPath($CacheDirectory).TrimEnd('\')
    $parent = [IO.Path]::GetDirectoryName($fullPath).TrimEnd('\')
    if (-not [string]::Equals($parent, $fullCache, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ([IO.Path]::GetFileName($fullPath) -notmatch '^uninst\.[^.]+\.tmp$') {
        throw 'Inno Setup requested signing for an unexpected file inside the uninstaller cache directory.'
    }
    return $true
}

function Publish-VerifiedSignedUninstaller {
    param(
        [Parameter(Mandatory=$true)][string]$SourcePath,
        [Parameter(Mandatory=$true)][string]$CacheDirectory,
        [Parameter(Mandatory=$true)][scriptblock]$VerifySignature
    )
    $fullSource = [IO.Path]::GetFullPath($SourcePath)
    $fullCache = [IO.Path]::GetFullPath($CacheDirectory).TrimEnd('\')
    $source = Get-Item -LiteralPath $fullSource -Force -ErrorAction Stop
    $cache = Get-Item -LiteralPath $fullCache -Force -ErrorAction Stop
    if (($source.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Refusing to cache a reparse-point uninstaller.' }
    if (($cache.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Refusing to cache an uninstaller in a reparse-point directory.' }
    if (-not (Test-InternalUninstallerTarget $fullSource $fullCache)) { return $null }

    $finalPath = Join-Path $fullCache 'AnyAiCamSignedUninstaller.exe'
    $stagingPath = $finalPath + '.partial'
    if ((Test-Path -LiteralPath $finalPath) -or (Test-Path -LiteralPath $stagingPath)) {
        throw 'Signed uninstaller cache destination already exists; refusing to overwrite it.'
    }

    & $VerifySignature $fullSource
    $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $fullSource -ErrorAction Stop).Hash
    Copy-Item -LiteralPath $fullSource -Destination $stagingPath -ErrorAction Stop
    $stagedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $stagingPath -ErrorAction Stop).Hash
    if ($stagedHash -ne $sourceHash) { throw 'Copied signed uninstaller bytes do not match the compiler signing input.' }
    & $VerifySignature $stagingPath
    [IO.File]::Move($stagingPath, $finalPath)
    return Get-Item -LiteralPath $finalPath -ErrorAction Stop
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        foreach ($value in @($SignToolPath, $DlibPath, $MetadataPath, $TimestampUrl, $UninstallerCacheDir, $TargetPath)) {
            if ([string]::IsNullOrWhiteSpace($value)) { throw 'The internal-test signing callback is missing a required argument.' }
        }
        foreach ($path in @($SignToolPath, $DlibPath, $MetadataPath, $TargetPath)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Internal-test signing input is missing: $path" }
        }
        if ($TimestampUrl -notmatch '^https?://') { throw 'TimestampUrl must be an HTTP(S) URL.' }

        & $SignToolPath sign /fd SHA256 /tr $TimestampUrl /td SHA256 /dlib $DlibPath /dmdf $MetadataPath $TargetPath
        $signExitCode = $LASTEXITCODE
        if ($signExitCode -ne 0) { throw "Internal-test SignTool signing failed with exit code $signExitCode." }

        $verifySignature = {
            param([string]$Path)
            $signature = Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
            if ($signature.Status -ne 'Valid') { throw "Internal-test Authenticode verification failed: $Path ($($signature.Status))." }
            & $SignToolPath verify /pa /all $Path
            $verifyExitCode = $LASTEXITCODE
            if ($verifyExitCode -ne 0) { throw "Internal-test SignTool verification failed with exit code ${verifyExitCode}: $Path." }
        }.GetNewClosure()

        if (Test-InternalUninstallerTarget $TargetPath $UninstallerCacheDir) {
            $cached = Publish-VerifiedSignedUninstaller -SourcePath $TargetPath -CacheDirectory $UninstallerCacheDir -VerifySignature $verifySignature
            if (-not $cached) { throw 'The signed uninstaller was not retained.' }
            Write-Output ("Verified signed uninstaller cache artifact: " + $cached.Name)
        } else {
            & $verifySignature $TargetPath
        }
        exit 0
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
