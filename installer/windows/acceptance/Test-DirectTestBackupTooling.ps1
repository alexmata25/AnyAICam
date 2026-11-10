$ErrorActionPreference = 'Stop'
$fixture = Join-Path $PSScriptRoot ('.backup-fixture-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $fixture 'backup'
$restore = Join-Path $fixture 'restore'
if ([IO.DriveInfo]::new([IO.Path]::GetPathRoot($PSScriptRoot)).DriveFormat -ne 'NTFS') { throw 'FIXTURE_VOLUME_NOT_NTFS' }
try {
    New-Item -ItemType Directory -Path (Join-Path $fixture 'source\protected') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $fixture 'source\protected\license-like.txt'), 'synthetic fixture only')
    [IO.File]::WriteAllText((Join-Path $fixture 'source\empty.txt'), '')
    $expected = @(Get-ChildItem -LiteralPath (Join-Path $fixture 'source') -Recurse -File | ForEach-Object {
        [pscustomobject]@{ relative = $_.FullName.Substring((Join-Path $fixture 'source').Length + 1); sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() }
    })
    $privileges = (& whoami.exe /priv 2>&1 | Out-String)
    $hasBackup = $privileges -match 'SeBackupPrivilege\s+.*Enabled'
    $hasRestore = $privileges -match 'SeRestorePrivilege\s+.*Enabled'
    $backupLog = @(& robocopy.exe (Join-Path $fixture 'source') $backup /E /COPYALL /DCOPY:DAT /B /R:0 /W:0 /NP 2>&1)
    $backupCode = $LASTEXITCODE
    if ($backupCode -lt 8) {
        foreach ($item in $expected) {
            $copy = Join-Path $backup $item.relative
            if (-not (Test-Path -LiteralPath $copy -PathType Leaf) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $copy).Hash.ToLowerInvariant() -ne $item.sha256) { throw "BACKUP_HASH_MISMATCH:$($item.relative)" }
        }
    } elseif (($hasBackup -and $hasRestore) -and (($backupLog | Out-String) -notmatch '(1314|privilege|privileges)')) {
        throw "BACKUP_MODE_FAILED_FOR_UNEXPECTED_REASON:exit=$backupCode"
    }
    $normalLog = @(& robocopy.exe (Join-Path $fixture 'source') $backup /E /COPY:DAT /DCOPY:DAT /R:0 /W:0 /NP 2>&1)
    $normalCode = $LASTEXITCODE
    if ($normalCode -ge 8) { throw "FIXTURE_COPY_FAILED:$normalCode" }
    & robocopy.exe $backup $restore /E /COPY:DAT /DCOPY:DAT /R:0 /W:0 /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "FIXTURE_RESTORE_FAILED:$LASTEXITCODE" }
    foreach ($item in $expected) {
        foreach ($root in @($backup, $restore)) {
            $copy = Join-Path $root $item.relative
            if (-not (Test-Path -LiteralPath $copy -PathType Leaf) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $copy).Hash.ToLowerInvariant() -ne $item.sha256) { throw "RESTORE_HASH_MISMATCH:$($item.relative)" }
        }
    }
    [pscustomobject]@{
        fixture_volume = 'NTFS'; fixture_files = $expected.Count; fixture_hashes = 'PASS'
        ordinary_copy = 'PASS'; restore_copy = 'PASS'
        robocopy_backup_mode = if ($backupCode -lt 8) { 'PASS' } else { "BLOCKED_EXPECTED_EXIT_$backupCode" }
        SeBackupPrivilege_enabled = $hasBackup; SeRestorePrivilege_enabled = $hasRestore
        fixture_only = $true; production_data_read = $false
    } | ConvertTo-Json -Compress
} finally {
    if (Test-Path -LiteralPath $fixture) {
        $resolved = [IO.Path]::GetFullPath($fixture)
        $expectedParent = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
        if (-not $resolved.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase)) { throw 'FIXTURE_CLEANUP_PATH_ESCAPE' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
