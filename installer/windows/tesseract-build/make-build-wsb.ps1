# Writes the Windows Sandbox configuration for the minimal Tesseract build (build-tesseract-in-sandbox.ps1).
# Inputs are mapped READ-ONLY; only the output folder is writable. vGPU, clipboard, printers and audio are off;
# networking stays on (the Build Tools are downloaded inside the Sandbox).
param([Parameter(Mandatory=$true)][string]$Inputs, [Parameter(Mandatory=$true)][string]$Output, [Parameter(Mandatory=$true)][string]$WsbPath,
      [int]$MemoryMB = 6144)
$ErrorActionPreference = 'Stop'
$inputsFull = (Resolve-Path -LiteralPath $Inputs).Path
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$outputFull = (Resolve-Path -LiteralPath $Output).Path
if (Get-ChildItem -LiteralPath $outputFull -Force) { throw "Output folder must be empty: $outputFull" }
$xml = @"
<Configuration>
  <VGpu>Disable</VGpu>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <ProtectedClient>Enable</ProtectedClient>
  <MemoryInMB>$MemoryMB</MemoryInMB>
  <MappedFolders>
    <MappedFolder><HostFolder>$([Security.SecurityElement]::Escape($inputsFull))</HostFolder><SandboxFolder>C:\Inputs</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$([Security.SecurityElement]::Escape($outputFull))</HostFolder><SandboxFolder>C:\Output</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Inputs\build-tesseract-in-sandbox.ps1</Command></LogonCommand>
</Configuration>
"@
Set-Content -Encoding utf8 -LiteralPath $WsbPath -Value $xml
"Sandbox configuration: $WsbPath"
