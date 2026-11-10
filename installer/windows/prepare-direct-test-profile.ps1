param(
    [Parameter(Mandatory=$true)][string]$InstallRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')][string]$RunId,
    [Parameter(Mandatory=$true)][string]$ApprovedCameraNetwork
)
$ErrorActionPreference = 'Stop'
function ConvertTo-IPv4Number([Net.IPAddress]$Address) {
    $bytes = $Address.GetAddressBytes()
    if ($bytes.Length -ne 4) { throw 'DIRECT_TEST_CAMERA_NETWORK_IPV4_REQUIRED' }
    return (([long]$bytes[0] -shl 24) -bor ([long]$bytes[1] -shl 16) -bor ([long]$bytes[2] -shl 8) -bor [long]$bytes[3])
}
function Get-IPv4Mask([int]$Prefix) {
    if ($Prefix -lt 1 -or $Prefix -gt 32) { throw 'DIRECT_TEST_CAMERA_PREFIX_REFUSED' }
    return ([long]4294967295 -band ([long]4294967295 -shl (32 - $Prefix)))
}
$cameraParts = $ApprovedCameraNetwork.Split('/')
if ($cameraParts.Count -ne 2 -or $cameraParts[1] -notmatch '^(2[4-9]|3[0-2])$') { throw 'DIRECT_TEST_CAMERA_NETWORK_BOUNDS_REFUSED' }
try { $cameraIp = [Net.IPAddress]::Parse($cameraParts[0]) } catch { throw 'DIRECT_TEST_CAMERA_NETWORK_ADDRESS_INVALID' }
$cameraBytes = $cameraIp.GetAddressBytes()
$cameraValue = ConvertTo-IPv4Number $cameraIp
$cameraPrefix = [int]$cameraParts[1]
$cameraMask = Get-IPv4Mask $cameraPrefix
$cameraNetworkValue = $cameraValue -band $cameraMask
$cameraNetworkBytes = [byte[]]@((($cameraNetworkValue -shr 24) -band 255), (($cameraNetworkValue -shr 16) -band 255), (($cameraNetworkValue -shr 8) -band 255), ($cameraNetworkValue -band 255))
$cameraNetworkCanonical = ([Net.IPAddress]::new($cameraNetworkBytes)).ToString() + '/' + $cameraPrefix
$isRfc1918 = ($cameraBytes[0] -eq 10) -or ($cameraBytes[0] -eq 172 -and $cameraBytes[1] -ge 16 -and $cameraBytes[1] -le 31) -or ($cameraBytes[0] -eq 192 -and $cameraBytes[1] -eq 168)
if (-not $isRfc1918 -or $cameraNetworkCanonical -cne $ApprovedCameraNetwork) { throw 'DIRECT_TEST_CAMERA_NETWORK_NOT_CANONICAL_RFC1918' }
$adapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object { $_.Status -eq 'Up' })
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.AddressState -eq 'Preferred' })
$matchedInterfaces = New-Object System.Collections.Generic.List[object]
foreach ($address in $addresses) {
    if (-not (@($adapters | Where-Object { $_.ifIndex -eq $address.InterfaceIndex }).Count)) { continue }
    try { $localIp = [Net.IPAddress]::Parse([string]$address.IPAddress) } catch { continue }
    $localBytes = $localIp.GetAddressBytes()
    if (-not (($localBytes[0] -eq 10) -or ($localBytes[0] -eq 172 -and $localBytes[1] -ge 16 -and $localBytes[1] -le 31) -or ($localBytes[0] -eq 192 -and $localBytes[1] -eq 168))) { continue }
    $localPrefix = [int]$address.PrefixLength
    if ($cameraPrefix -lt $localPrefix) { continue }
    $localMask = Get-IPv4Mask $localPrefix
    $localValue = ConvertTo-IPv4Number $localIp
    if (($cameraNetworkValue -band $localMask) -ne ($localValue -band $localMask)) { continue }
    $localNetworkValue = $localValue -band $localMask
    $localNetworkBytes = [byte[]]@((($localNetworkValue -shr 24) -band 255), (($localNetworkValue -shr 16) -band 255), (($localNetworkValue -shr 8) -band 255), ($localNetworkValue -band 255))
    $matchedInterfaces.Add([pscustomobject]@{
        address = $localIp.ToString()
        network = ([Net.IPAddress]::new($localNetworkBytes)).ToString() + '/' + $localPrefix
        alias = [string]$address.InterfaceAlias
    })
}
if ($matchedInterfaces.Count -eq 0) { throw 'DIRECT_TEST_CAMERA_NETWORK_NOT_ATTACHED_TO_ACTIVE_INTERFACE' }
if ($matchedInterfaces.Count -ne 1) { throw 'DIRECT_TEST_CAMERA_NETWORK_INTERFACE_AMBIGUOUS' }
$matchedInterface = $matchedInterfaces[0]
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
if (-not $matchedInterface.alias -or $matchedInterface.alias.Length -gt 128 -or $matchedInterface.alias -match '[\x00-\x1f]') { throw 'DIRECT_TEST_CAMERA_INTERFACE_ALIAS_INVALID' }
$profile = [ordered]@{ schema = 1; run_id = $RunId; data_root = $root; service_name = 'AnyAiCamVmsDirectTest'; http_port = 18767; approved_camera_network = $cameraNetworkCanonical; camera_interface_address = $matchedInterface.address; camera_interface_network = $matchedInterface.network; camera_interface_alias = $matchedInterface.alias }
$profilePath = Join-Path $install 'direct-test-profile.json'
if (Test-Path -LiteralPath $profilePath) { throw 'DIRECT_TEST_PROFILE_ALREADY_EXISTS' }
[IO.File]::WriteAllText($profilePath, ($profile | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
Write-Output 'DIRECT_TEST_PROFILE_CREATED'
