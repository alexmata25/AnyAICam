# %ProgramData%\AnyAiCam must be safe BEFORE the elevated installer acts on it (Codex 1.2.7 review, Windows-only blocker).
# ProgramData lets any local user create folders, so a standard user could pre-create AnyAiCam (and own it) or plant a
# junction / symbolic link inside it before the first install. Recursive takeown /R and icacls /T must never run over a
# tree that contains a link, and the elevated installer must never write through one.
#
# Get-AnyAiCamDataRootLink walks the tree itself with .NET, never descending into a reparse point (a junction, a
# directory symlink or a file symlink all carry FILE_ATTRIBUTE_REPARSE_POINT), and returns the first link it finds, or
# '' when there is none. It never follows a link, so a link pointing outside the tree is reported, not traversed.

function Get-AnyAiCamDataRootLink([string]$Root) {
    # Get-Item detects an existing dangling reparse entry too; DirectoryInfo.Exists
    # reports false when a directory junction's target has been removed.
    $rootInfo = Get-Item -LiteralPath $Root -Force -ErrorAction SilentlyContinue
    if ($null -eq $rootInfo) { return '' }
    if ($rootInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $rootInfo.FullName }
    if ($rootInfo -isnot [IO.DirectoryInfo]) { return '' }
    $pending = New-Object 'System.Collections.Generic.Stack[IO.DirectoryInfo]'
    $pending.Push($rootInfo)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($entry in $directory.EnumerateFileSystemInfos()) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $entry.FullName }
            if ($entry -is [IO.DirectoryInfo]) { $pending.Push($entry) }
        }
    }
    return ''
}

# Throws (fail closed) when the data folder contains a link; the message names it so an administrator can remove it.
function Assert-AnyAiCamDataRootHasNoLinks([string]$Root) {
    $link = Get-AnyAiCamDataRootLink $Root
    if ($link) {
        throw "Refusing to install: $link is a link (junction or symbolic link) inside the AnyAiCam data folder. Remove it, then run the setup again."
    }
}

# Reset one entry without a recursive icacls/takeown operation. The caller has
# already restricted the parent directory, so a standard user cannot replace
# this entry while its ACL is repaired. Directories are reset before they are
# enumerated; this removes their explicit writable ACEs before the walk moves
# further down. Reparse points are rejected before any ACL tool sees the path.
function Set-AnyAiCamEntryAcl([System.IO.FileSystemInfo]$Entry) {
    if ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to repair permissions through link $($Entry.FullName)."
    }
    # Already clean (trusted owner, nobody else may modify): leave it -- this keeps a large genuine recordings tree fast.
    # Anything else (foreign owner, any untrusted write ACE, unreadable ACL) is re-owned and reset to inherit.
    $clean = $false
    try { $clean = Test-AnyAiCamAclClean (Get-Acl -LiteralPath $Entry.FullName -ErrorAction Stop) } catch { $clean = $false }
    if (-not $clean) {
        & icacls.exe $Entry.FullName /setowner '*S-1-5-32-544' /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not set the owner of $($Entry.FullName) (icacls exit $LASTEXITCODE)." }
        & icacls.exe $Entry.FullName /reset /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not reset the permissions of $($Entry.FullName) (icacls exit $LASTEXITCODE)." }
    }
    if ($Entry -is [IO.DirectoryInfo]) {
        foreach ($child in $Entry.EnumerateFileSystemInfos()) {
            Set-AnyAiCamEntryAcl $child
        }
    }
}

function Set-AnyAiCamDescendantAcl([string]$Root) {
    $rootInfo = Get-Item -LiteralPath $Root -Force -ErrorAction SilentlyContinue
    if ($null -eq $rootInfo) { return }
    if ($rootInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to repair permissions through link $($rootInfo.FullName)."
    }
    if ($rootInfo -isnot [IO.DirectoryInfo]) { return }
    foreach ($child in $rootInfo.EnumerateFileSystemInfos()) {
        Set-AnyAiCamEntryAcl $child
    }
}

# First-install hardening (Red, owner 2026-10-18): a standard user can pre-create %ProgramData%\AnyAiCam before the
# first install and leave user-writable entries ANYWHERE in it (recordings, hls, logs, mediamtx -- not only the secret
# folders), then later plant a junction there for the SYSTEM service to write through. The installer therefore decides,
# BEFORE it touches the root, whether the root was already a genuine AnyAiCam data root:
#   owned by SYSTEM or Administrators, inheritance disabled (protected), and no allow-ACE granting any modifying right to
#   anyone else.
# Only an administrator can produce that state, so an attacker cannot use it to skip the repair. When it does not hold
# (a first install, or a tree someone else prepared), the whole tree is walked and repaired with Set-AnyAiCamEntryAcl.
$AnyAiCamTrustedSids = @('S-1-5-18', 'S-1-5-32-544')
# WriteData/AddFile, AppendData/AddSubdirectory, WriteExtendedAttributes, DeleteSubdirectoriesAndFiles, WriteAttributes,
# Delete, ChangePermissions, TakeOwnership, GENERIC_ALL, GENERIC_WRITE.
$AnyAiCamWriteMask = 0x2 -bor 0x4 -bor 0x10 -bor 0x40 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000

# Pure policy over an ACL object (no file system access), so it is testable without administrator rights.
# Owner is SYSTEM/Administrators and no allow-ACE (explicit or inherited) gives anyone else a modifying right.
function Test-AnyAiCamAclClean([System.Security.AccessControl.FileSystemSecurity]$Acl) {
    if ($null -eq $Acl) { return $false }
    $owner = $Acl.GetOwner([Security.Principal.SecurityIdentifier])
    if ($null -eq $owner -or $AnyAiCamTrustedSids -notcontains $owner.Value) { return $false }
    foreach ($rule in $Acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if ($AnyAiCamTrustedSids -contains $rule.IdentityReference.Value) { continue }
        if ([int64]$rule.FileSystemRights -band $AnyAiCamWriteMask) { return $false }
    }
    return $true
}

# A genuine data ROOT is also protected (it must not inherit ProgramData's "users may create" entries).
function Test-AnyAiCamAclTrusted([System.Security.AccessControl.FileSystemSecurity]$Acl) {
    return ((Test-AnyAiCamAclClean $Acl) -and $Acl.AreAccessRulesProtected)
}

# $true only for an existing, non-link root that already satisfies Test-AnyAiCamAclTrusted. Any error -> $false
# (fail safe: the full repair walk runs).
function Test-AnyAiCamDataRootTrusted([string]$Root) {
    try {
        $item = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        if ($item -isnot [IO.DirectoryInfo]) { return $false }
        return (Test-AnyAiCamAclTrusted (Get-Acl -LiteralPath $Root -ErrorAction Stop))
    } catch {
        return $false
    }
}
