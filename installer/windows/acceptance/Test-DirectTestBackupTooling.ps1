$ErrorActionPreference = 'Stop'
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('AAC-Backup-' + [guid]::NewGuid().ToString('N'))
$backup = Join-Path $fixture 'backup'
$restore = Join-Path $fixture 'restore'
$fixtureVolume = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($fixture))
if ($fixtureVolume.DriveFormat -ne 'NTFS') { throw 'FIXTURE_VOLUME_NOT_NTFS' }
try {
    New-Item -ItemType Directory -Path (Join-Path $fixture 'source\protected') -Force | Out-Null
    $source = Join-Path $fixture 'source'
    $sample = Join-Path $source 'protected\synthetic-only.txt'
    [IO.File]::WriteAllText($sample, 'disposable fixture only')
    Set-Content -LiteralPath $sample -Stream AcceptanceStream -Value 'synthetic alternate stream' -NoNewline
    [IO.File]::WriteAllText((Join-Path $source 'empty.txt'), '')
    $fixedTime = [DateTime]::SpecifyKind([DateTime]::new(2020, 2, 3, 4, 5, 6), [DateTimeKind]::Utc)
    [IO.File]::SetLastWriteTimeUtc($sample, $fixedTime)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = Get-Acl -LiteralPath (Join-Path $source 'protected')
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath (Join-Path $source 'protected') -AclObject $acl
    $saclFixtureCreated = $false
    try {
        $fileSecurity = Get-Acl -LiteralPath $sample -Audit -ErrorAction Stop
        $auditRule = [Security.AccessControl.FileSystemAuditRule]::new(
            $identity, [Security.AccessControl.FileSystemRights]::ReadData, [Security.AccessControl.AuditFlags]::Success)
        $fileSecurity.AddAuditRule($auditRule)
        Set-Acl -LiteralPath $sample -AclObject $fileSecurity -ErrorAction Stop
        $saclFixtureCreated = $true
    } catch { $saclFixtureCreated = $false }

    function Get-Inventory([string]$Root, [switch]$IncludeAudit) {
        $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        $items = @(Get-Item -LiteralPath $base -Force)
        $items += @(Get-ChildItem -LiteralPath $base -Recurse -Force)
        foreach ($item in $items) {
            $relative = if ($item.FullName -ieq $base) { '.' } else { $item.FullName.Substring($base.Length + 1) }
            $aclArgs = @{ LiteralPath = $item.FullName }
            if ($IncludeAudit) { $aclArgs.Audit = $true }
            $security = Get-Acl @aclArgs -ErrorAction Stop
            $entry = [ordered]@{
                relative = $relative; directory = $item.PSIsContainer
                attributes = [int]$item.Attributes
                creation_utc_ticks = $item.CreationTimeUtc.Ticks
                last_access_utc_ticks = $item.LastAccessTimeUtc.Ticks
                last_write_utc_ticks = $item.LastWriteTimeUtc.Ticks
                sddl = $security.Sddl
            }
            if (-not $item.PSIsContainer) {
                $entry.sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $item.FullName).Hash.ToLowerInvariant()
                $entry.length = $item.Length
                $streams = @(Get-Item -LiteralPath $item.FullName -Stream * -ErrorAction Stop |
                    Where-Object { $_.Stream -ne ':$DATA' } | Sort-Object Stream)
                $entry.streams = @($streams | ForEach-Object {
                    [pscustomobject]@{ name = $_.Stream; length = $_.Length; value = (Get-Content -LiteralPath $item.FullName -Stream $_.Stream -Raw -ErrorAction Stop) }
                })
            }
            [pscustomobject]$entry
        }
    }
    function Assert-InventoryEqual([object[]]$Expected, [object[]]$Actual, [string]$Label) {
        if ($Expected.Count -ne $Actual.Count) { throw "${Label}_ENTRY_COUNT_MISMATCH" }
        $left = @($Expected | Sort-Object relative | ConvertTo-Json -Depth 8 -Compress)
        $right = @($Actual | Sort-Object relative | ConvertTo-Json -Depth 8 -Compress)
        if (($left -join '') -cne ($right -join '')) { throw "${Label}_METADATA_MISMATCH" }
    }
    $expectedContent = @(Get-Inventory $source)
    $privileges = (& whoami.exe /priv 2>&1 | Out-String)
    $hasBackup = $privileges -match 'SeBackupPrivilege\s+.*Enabled'
    $hasRestore = $privileges -match 'SeRestorePrivilege\s+.*Enabled'
    $hasSecurity = $privileges -match 'SeSecurityPrivilege\s+.*Enabled'
    $backupLog = @(& robocopy.exe (Join-Path $fixture 'source') $backup /E /COPYALL /DCOPY:DAT /B /R:0 /W:0 /NP 2>&1)
    $backupCode = $LASTEXITCODE
    $metadataResult = 'NOT_RUN'
    if ($backupCode -lt 8) {
        $expectedMetadata = @(Get-Inventory $source -IncludeAudit)
        Assert-InventoryEqual $expectedMetadata @(Get-Inventory $backup -IncludeAudit) 'BACKUP'
        $metadataResult = 'BACKUP_PASS'
        $restoreLog = @(& robocopy.exe $backup $restore /E /COPYALL /DCOPY:DAT /B /R:0 /W:0 /NP 2>&1)
        $restoreCode = $LASTEXITCODE
        if ($restoreCode -ge 8) { throw "RESTORE_METADATA_COPY_FAILED:$restoreCode" }
        Assert-InventoryEqual $expectedMetadata @(Get-Inventory $restore -IncludeAudit) 'RESTORE'
        $metadataResult = 'BACKUP_AND_RESTORE_PASS'
    } else {
        $backupText = $backupLog | Out-String
        if ($backupText -match '(?i)(You do not have the Manage Auditing user right|SeSecurityPrivilege)') {
            $metadataResult = 'BLOCKED_MANAGE_AUDITING_RIGHT'
        } elseif ($backupText -match '(?i)(1314|0x00000522|A required privilege is not held by the client)') {
            $metadataResult = 'BLOCKED_BACKUP_RESTORE_PRIVILEGE'
        } else {
            throw "BACKUP_MODE_FAILED_FOR_UNEXPECTED_REASON:exit=$backupCode"
        }
    }
    $normalLog = @(& robocopy.exe (Join-Path $fixture 'source') $backup /E /COPY:DAT /DCOPY:DAT /R:0 /W:0 /NP 2>&1)
    $normalCode = $LASTEXITCODE
    if ($normalCode -ge 8) { throw "FIXTURE_COPY_FAILED:$normalCode" }
    & robocopy.exe $backup $restore /E /COPY:DAT /DCOPY:DAT /R:0 /W:0 /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "FIXTURE_RESTORE_FAILED:$LASTEXITCODE" }
    foreach ($item in $expectedContent | Where-Object { -not $_.directory }) {
        foreach ($root in @($backup, $restore)) {
            $copy = Join-Path $root $item.relative
            if (-not (Test-Path -LiteralPath $copy -PathType Leaf) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $copy).Hash.ToLowerInvariant() -ne $item.sha256) { throw "RESTORE_HASH_MISMATCH:$($item.relative)" }
        }
    }
    $encryptionResult = 'NOT_CONFIRMED'
    if (Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue) {
        try {
            $volume = Get-BitLockerVolume -MountPoint ([IO.Path]::GetPathRoot($fixture)) -ErrorAction Stop
            if ($volume.VolumeStatus -eq 'FullyEncrypted' -and $volume.ProtectionStatus -eq 'On' -and $volume.EncryptionPercentage -eq 100) { $encryptionResult = 'CONFIRMED' }
        } catch { $encryptionResult = 'NOT_CONFIRMED' }
    }
    [pscustomobject]@{
        fixture_volume = 'NTFS'; fixture_files = @($expectedContent | Where-Object { -not $_.directory }).Count; fixture_hashes = 'PASS'
        ordinary_copy = 'PASS'; restore_copy = 'PASS'
        robocopy_backup_mode = if ($backupCode -lt 8) { 'PASS' } else { $metadataResult }
        metadata_backup_restore = $metadataResult
        alternate_data_stream = if ($metadataResult -eq 'BACKUP_AND_RESTORE_PASS') { 'PASS' } else { 'NOT_VERIFIED' }
        acl_owner_and_audit_metadata = if ($metadataResult -eq 'BACKUP_AND_RESTORE_PASS' -and $saclFixtureCreated) { 'PASS' } elseif ($metadataResult -eq 'BACKUP_AND_RESTORE_PASS') { 'SACL_NOT_EXERCISED' } else { 'NOT_VERIFIED' }
        SACL_fixture_created = $saclFixtureCreated
        encrypted_fixture_volume = $encryptionResult
        SeBackupPrivilege_enabled = $hasBackup; SeRestorePrivilege_enabled = $hasRestore; SeSecurityPrivilege_enabled = $hasSecurity
        fixture_only = $true; production_data_read = $false
    } | ConvertTo-Json -Compress
} finally {
    if (Test-Path -LiteralPath $fixture) {
        $resolved = [IO.Path]::GetFullPath($fixture)
        $expectedParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $resolved.StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase)) { throw 'FIXTURE_CLEANUP_PATH_ESCAPE' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
