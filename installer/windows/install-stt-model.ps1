<#
Windows 1.3.2 (Blue, Codex FAIL on ef93b503): the Vosk small English speech model for AAC Voice Call listening.

The same model the Linux images bake (Dockerfile: vosk-model-small-en-us-0.15.zip, SHA-256 30f26242..., Apache-2.0,
Alpha Cephei). Installed where app/aac_voice_call_listen.py looks by default on Windows (platform_paths.native_path):
%ProgramData%\AnyAiCam\models\stt\vosk-model-small-en-us-0.15. That folder inherits the data root's SYSTEM/Administrators-
only permissions (install-runtime.ps1 sets them before this runs), so a standard user cannot plant or alter it.

The manifest (vosk-model-small-en-us-0.15.sha256) pins the archive and every extracted file.
  * installed model matches the manifest exactly (same files, same hashes) -> kept (repair/upgrade does nothing);
  * missing, incomplete, altered or extra files -> replaced from the archive;
  * the archive does not match its pin, or the extracted tree does not match the manifest -> error; an existing model is
    left exactly as it was.
A link (junction / symbolic link) anywhere in models, models\stt or the model folder is refused. The swap uses a staging
folder beside the model, so a failure never leaves a half-written model in place.
#>

function Get-AnyAiCamSttManifest([string]$Manifest) {
    $archiveSha = $null; $files = [ordered]@{}
    foreach ($line in [IO.File]::ReadAllLines($Manifest)) {
        if (-not $line.Trim()) { continue }
        if ($line -match '^([0-9a-f]{64})  \*archive\* (\S+)$') { $archiveSha = $Matches[1]; continue }
        if ($line -notmatch '^([0-9a-f]{64})  ([A-Za-z0-9._/-]+)$') { throw "Malformed speech model manifest line: $line" }
        $relative = $Matches[2]
        if ($relative.Contains('..') -or $relative.StartsWith('/')) { throw "Unsafe path in the speech model manifest: $relative" }
        $files[$relative] = $Matches[1]
    }
    if (-not $archiveSha -or -not $files.Count) { throw 'The speech model manifest is incomplete.' }
    $names = @($files.Keys | ForEach-Object { $_.Split('/')[0] } | Select-Object -Unique)
    if ($names.Count -ne 1) { throw 'The speech model manifest must describe exactly one model folder.' }
    return @{ ArchiveSha = $archiveSha; Files = $files; Name = $names[0] }
}

function Assert-AnyAiCamSttNoLink([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing a link at $Path." }
    }
}

# $null when $Folder holds exactly the manifest's files with the manifest's hashes; otherwise the reason.
function Test-AnyAiCamSttModel([string]$Folder, $Spec) {
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) { return 'missing' }
    $root = (Get-Item -LiteralPath $Folder -Force).FullName
    $seen = @{}
    foreach ($item in Get-ChildItem -LiteralPath $root -Recurse -Force) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { return "link inside the model: $($item.FullName)" }
        if ($item.PSIsContainer) { continue }
        $relative = $Spec.Name + '/' + $item.FullName.Substring($root.Length + 1).Replace('\', '/')
        if (-not $Spec.Files.Contains($relative)) { return "unexpected file $relative" }
        if ((Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Spec.Files[$relative]) { return "altered file $relative" }
        $seen[$relative] = $true
    }
    foreach ($relative in $Spec.Files.Keys) { if (-not $seen.Contains($relative)) { return "missing file $relative" } }
    return $null
}

function Install-AnyAiCamSttModel {
    param(
        [Parameter(Mandatory=$true)][string]$DataRoot,
        [Parameter(Mandatory=$true)][string]$Archive,
        [Parameter(Mandatory=$true)][string]$Manifest
    )
    $spec = Get-AnyAiCamSttManifest $Manifest
    $models = Join-Path $DataRoot 'models'
    $stt = Join-Path $models 'stt'
    foreach ($folder in @($models, $stt)) {
        Assert-AnyAiCamSttNoLink $folder
        if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder | Out-Null }
        Assert-AnyAiCamSttNoLink $folder
    }
    $target = Join-Path $stt $spec.Name
    Assert-AnyAiCamSttNoLink $target
    # Left-overs of an interrupted earlier run (only this script's own names).
    Get-ChildItem -LiteralPath $stt -Force -Directory | Where-Object { $_.Name -like '.staging-*' -or $_.Name -like '.old-*' } | ForEach-Object {
        if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing a link at $($_.FullName)." }
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
    $problem = Test-AnyAiCamSttModel $target $spec
    if (-not $problem) { Write-Output "Speech model $($spec.Name): present and verified (kept)."; return }

    if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $spec.ArchiveSha) {
        throw "The bundled speech model archive does not match its pinned SHA-256; nothing was changed."
    }
    $staging = Join-Path $stt ('.staging-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        Expand-Archive -LiteralPath $Archive -DestinationPath $staging
        $staged = Join-Path $staging $spec.Name
        $stagedProblem = Test-AnyAiCamSttModel $staged $spec
        if ($stagedProblem) { throw "The extracted speech model does not match its manifest ($stagedProblem); nothing was changed." }
        if (@(Get-ChildItem -LiteralPath $staging -Force).Count -ne 1) { throw 'The speech model archive holds more than the model folder; nothing was changed.' }
        $old = $null
        if (Test-Path -LiteralPath $target) {
            $old = Join-Path $stt ('.old-' + [guid]::NewGuid().ToString('N'))
            Move-Item -LiteralPath $target -Destination $old
        }
        Move-Item -LiteralPath $staged -Destination $target
        if ($old) { Remove-Item -LiteralPath $old -Recurse -Force }
    } finally {
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    }
    $after = Test-AnyAiCamSttModel $target $spec
    if ($after) { throw "The installed speech model failed verification ($after)." }
    $action = if ($problem -eq 'missing') { 'installed' } else { "replaced ($problem)" }
    Write-Output "Speech model $($spec.Name): $action and verified."
}
