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
    & icacls.exe $Entry.FullName /setowner '*S-1-5-32-544' /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not set the owner of $($Entry.FullName) (icacls exit $LASTEXITCODE)." }
    & icacls.exe $Entry.FullName /reset /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not reset the permissions of $($Entry.FullName) (icacls exit $LASTEXITCODE)." }
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
