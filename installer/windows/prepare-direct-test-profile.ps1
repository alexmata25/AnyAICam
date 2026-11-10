param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')][string]$RunId
)
$ErrorActionPreference = 'Stop'
$programData = [Environment]::GetFolderPath('CommonApplicationData')
if (-not $programData -or -not [IO.Path]::IsPathRooted($programData)) { throw 'DIRECT_TEST_PROGRAMDATA_UNAVAILABLE' }
$install = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$expectedInstall = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'AnyAiCam VMS Direct Test')).TrimEnd('\')
if ($install -ine $expectedInstall) { throw 'DIRECT_TEST_INSTALL_PATH_REFUSED' }
if (-not (Test-Path -LiteralPath $install -PathType Container)) { throw 'DIRECT_TEST_INSTALL_ROOT_MISSING' }
if ((Get-Item -LiteralPath $install -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'DIRECT_TEST_INSTALL_ROOT_REPARSE' }
$service = Get-Service -Name 'AnyAiCamVmsDirectTest' -ErrorAction SilentlyContinue
if ($service) { throw 'DIRECT_TEST_SERVICE_ALREADY_EXISTS' }
$parent = Join-Path $programData 'AnyAiCam-Test'
$root = Join-Path $parent $RunId
$guardPath = Join-Path $PSScriptRoot 'data-root-guard.ps1'
. $guardPath
if (Test-Path -LiteralPath $parent) {
    Assert-AnyAiCamDataRootHasNoLinks $parent
    if (-not (Test-AnyAiCamDataRootTrusted $parent)) { throw 'DIRECT_TEST_PARENT_ACL_UNTRUSTED' }
} else {
    New-Item -ItemType Directory -Path $parent | Out-Null
    Assert-AnyAiCamDataRootHasNoLinks $parent
    & icacls.exe $parent /setowner '*S-1-5-32-544' /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'DIRECT_TEST_PARENT_OWNER_FAILED' }
    & icacls.exe $parent /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'DIRECT_TEST_PARENT_ACL_FAILED' }
    if (-not (Test-AnyAiCamDataRootTrusted $parent)) { throw 'DIRECT_TEST_PARENT_ACL_UNTRUSTED' }
}
if (Test-Path -LiteralPath $root) { throw 'DIRECT_TEST_RUN_ROOT_ALREADY_EXISTS' }
New-Item -ItemType Directory -Path $root | Out-Null
Assert-AnyAiCamDataRootHasNoLinks $root
foreach ($name in @('config','database','recordings','hls','logs','mediamtx','agent','update','models')) {
    New-Item -ItemType Directory -Path (Join-Path $root $name) | Out-Null
}
$profile = [ordered]@{ schema = 1; run_id = $RunId; data_root = $root; service_name = 'AnyAiCamVmsDirectTest'; http_port = 18767 }
$profilePath = Join-Path $install 'direct-test-profile.json'
if (Test-Path -LiteralPath $profilePath) { throw 'DIRECT_TEST_PROFILE_ALREADY_EXISTS' }
[IO.File]::WriteAllText($profilePath, ($profile | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
Write-Output 'DIRECT_TEST_PROFILE_CREATED'
