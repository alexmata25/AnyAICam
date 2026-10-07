# Prepares a Windows Sandbox validation folder for one setup .exe (2026-10-07).
#   make-wsb.ps1 -Setup <AnyAiCam-VMS-Setup-x.y.z-abcdef1.exe> -Folder <new empty folder>
# Writes <Folder>\setup\<exe>, <Folder>\results\, <Folder>\validate-in-sandbox.ps1
# and <Folder>\AnyAiCam-Validation.wsb (setup read-only, results writable,
# networking on so the VMS and a LAN camera can be tested).
param(
    [Parameter(Mandatory=$true)][string]$Setup,
    [Parameter(Mandatory=$true)][string]$Folder
)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $Folder) { throw "Use a new folder: $Folder exists." }
$setupFile = Get-Item -LiteralPath $Setup
if ($setupFile.Name -notmatch '^AnyAiCam-VMS-Setup-\d+\.\d+\.\d+-[0-9a-f]{7}\.exe$') { throw "Not an AnyAiCam setup name: $($setupFile.Name)" }
$root = New-Item -ItemType Directory -Path $Folder
foreach ($sub in 'setup', 'results', 'scripts') { New-Item -ItemType Directory -Path (Join-Path $root.FullName $sub) | Out-Null }
Copy-Item -LiteralPath $setupFile.FullName -Destination (Join-Path $root.FullName 'setup')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'validate-in-sandbox.ps1') -Destination (Join-Path $root.FullName 'scripts')
# The disposable test cloud the cloud-linking checks claim against (never production).
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'test-cloud.py') -Destination (Join-Path $root.FullName 'scripts')
$escape = { param($s) [Security.SecurityElement]::Escape($s) }
$wsb = @"
<Configuration>
  <Networking>Enable</Networking>
  <MappedFolders>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'setup'))</HostFolder><SandboxFolder>C:\Validation\setup</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'scripts'))</HostFolder><SandboxFolder>C:\Validation\scripts</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape (Join-Path $root.FullName 'results'))</HostFolder><SandboxFolder>C:\Validation\results</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Validation\scripts\validate-in-sandbox.ps1</Command>
  </LogonCommand>
</Configuration>
"@
Set-Content -LiteralPath (Join-Path $root.FullName 'AnyAiCam-Validation.wsb') -Value $wsb -Encoding UTF8
"Sandbox validation folder ready: $($root.FullName)"
