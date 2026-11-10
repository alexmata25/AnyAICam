param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')][string]$RunId
)
$ErrorActionPreference = 'Stop'
$programData = [Environment]::GetFolderPath('CommonApplicationData')
if (-not $programData) { throw 'DIRECT_TEST_PROGRAMDATA_UNAVAILABLE' }
$install = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$expectedInstall = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('ProgramFiles')) 'AnyAiCam VMS Direct Test')).TrimEnd('\')
if ($install -ine $expectedInstall) { throw 'DIRECT_TEST_INSTALL_PATH_REFUSED' }
$service = Get-Service -Name 'AnyAiCamVmsDirectTest' -ErrorAction SilentlyContinue
if ($service) { throw 'DIRECT_TEST_SERVICE_ALREADY_EXISTS' }
$base = Join-Path $programData 'AnyAiCam-Test'
$target = Join-Path $base $RunId
foreach ($path in @($base, $target)) {
    if (Test-Path -LiteralPath $path) {
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'DIRECT_TEST_DATA_PATH_REPARSE' }
    }
}
if (Test-Path -LiteralPath $target) { throw 'DIRECT_TEST_RUN_ROOT_ALREADY_EXISTS' }
$profile = Join-Path $install 'direct-test-profile.json'
if (Test-Path -LiteralPath $profile) { throw 'DIRECT_TEST_PROFILE_ALREADY_EXISTS' }
Write-Output 'DIRECT_TEST_GUARD_OK'
