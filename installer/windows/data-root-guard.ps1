# %ProgramData%\AnyAiCam must be safe BEFORE the elevated installer acts on it (Codex 1.2.7 review, Windows-only blocker).
# ProgramData lets any local user create folders, so a standard user could pre-create AnyAiCam (and own it) or plant a
# junction / symbolic link inside it before the first install. Recursive takeown /R and icacls /T must never run over a
# tree that contains a link, and the elevated installer must never write through one.
#
# Get-AnyAiCamDataRootLink walks the tree itself with .NET, never descending into a reparse point (a junction, a
# directory symlink or a file symlink all carry FILE_ATTRIBUTE_REPARSE_POINT), and returns the first link it finds, or
# '' when there is none. It never follows a link, so a link pointing outside the tree is reported, not traversed.

function Get-AnyAiCamDataRootLink([string]$Root) {
    $rootInfo = New-Object IO.DirectoryInfo $Root
    if (-not $rootInfo.Exists) { return '' }
    if ($rootInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) { return $rootInfo.FullName }
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
