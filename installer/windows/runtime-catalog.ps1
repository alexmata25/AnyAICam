# Signed catalog for the AnyAiCam private runtime (2026-10-08).
#
# Smart App Control (and any App Control for Business policy) refuses to load
# unsigned native code without reputation. The Azure-signed setup ran in a
# Smart App Control Sandbox, but the runtime it installs could not start:
# pydantic-core's and numpy's .pyd files and torch's DLLs (from the pinned
# PyPI wheels), WinSW, FFmpeg and MediaMTX are not signed by their authors
# ("An Application Control policy has blocked this file").
#
# Instead of re-signing hundreds of third-party files one by one, the build
# lists the SHA-256 Authenticode hash of every native file the setup ships in
# ONE catalog (makecat), signs that catalog with the AnyAiCam certificate,
# and the setup registers it in Windows' catalog database (CryptCATAdmin*,
# the same mechanism Windows and drivers use). Code integrity then treats
# exactly those files -- byte for byte -- as signed by AnyAiCam; anything
# else stays unsigned.
#
#   Build (build.ps1):   New-RuntimeCatalog
#   Setup (install-runtime.ps1):  Install-RuntimeCatalog (signature checked first)
#   Uninstall:           runtime-catalog.ps1 -Remove
param([switch]$Remove)

$CatalogBaseName = 'AnyAiCamRuntime.cat'
$NativeExtensions = @('.pyd', '.dll', '.exe')

# ------------------------------------------------------------ build time
# The native files inside the given zip archives (wheels are zip files) and
# loose files, copied into $Stage\<n>\<path in archive>. pip and
# Expand-Archive copy these bytes unchanged, so the installed files have the
# same hashes.
function Export-NativeFiles([string]$Stage, [string[]]$Archives, [string[]]$Files) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
    New-Item -ItemType Directory -Path $Stage | Out-Null
    $count = 0; $index = 0
    foreach ($archive in $Archives) {
        $index++
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            foreach ($entry in $zip.Entries) {
                if (-not $entry.Name -or $NativeExtensions -notcontains [IO.Path]::GetExtension($entry.Name).ToLowerInvariant()) { continue }
                $relative = ($entry.FullName -replace '/', '\') -replace '^[\\.]+', ''
                if ($relative -match '\.\.') { throw "Unsafe path in $archive`: $($entry.FullName)" }
                $target = Join-Path (Join-Path $Stage $index) $relative
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
                $count++
            }
        } finally { $zip.Dispose() }
    }
    $loose = Join-Path $Stage 'files'
    New-Item -ItemType Directory -Force -Path $loose | Out-Null
    foreach ($file in $Files) { Copy-Item -LiteralPath $file -Destination $loose; $count++ }
    return $count
}

# One catalog (SHA-256, Authenticode hashes of PE files) of every file under
# $Stage, written to $CatalogPath with makecat.exe (Windows SDK).
function New-RuntimeCatalog([string]$Stage, [string]$CatalogPath, [string]$MakeCat) {
    if (-not (Test-Path -LiteralPath $MakeCat)) { throw "makecat.exe not found: $MakeCat" }
    $files = @(Get-ChildItem -LiteralPath $Stage -Recurse -File)
    if (-not $files.Count) { throw 'No native files to catalog.' }
    $cdf = [IO.Path]::ChangeExtension($CatalogPath, '.cdf')
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('[CatalogHeader]'); $lines.Add("Name=$CatalogPath"); $lines.Add('ResultDir=')
    $lines.Add('PublicVersion=0x0000001'); $lines.Add('EncodingType=0x00010001'); $lines.Add('CatalogVersion=2'); $lines.Add('HashAlgorithms=SHA256')
    $lines.Add('[CatalogFiles]')
    $n = 0
    foreach ($file in $files) { $n++; $lines.Add("<HASH>F$n=$($file.FullName)") }
    [IO.File]::WriteAllLines($cdf, $lines, [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $CatalogPath) { Remove-Item -LiteralPath $CatalogPath -Force }
    $output = & $MakeCat -v $cdf 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $CatalogPath)) { throw "makecat failed: $output" }
    Remove-Item -LiteralPath $cdf -Force
    return $files.Count
}

# ------------------------------------------------------------ install time
# CryptCATAdmin* from wintrust.dll, declared in memory (Reflection.Emit): no
# compiled helper file is written, so nothing unsigned is loaded to do this.
function Get-CatalogApi {
    $existing = 'AnyAiCam.CatalogApi' -as [type]
    if ($existing) { return $existing }
    $assembly = [AppDomain]::CurrentDomain.DefineDynamicAssembly((New-Object Reflection.AssemblyName 'AnyAiCamCatalogApi'), [Reflection.Emit.AssemblyBuilderAccess]::Run)
    $module = $assembly.DefineDynamicModule('AnyAiCamCatalogApi')
    $type = $module.DefineType('AnyAiCam.CatalogApi', 'Public, Class, Sealed, Abstract')
    $setLastError = New-Object Reflection.Emit.CustomAttributeBuilder(
        [Runtime.InteropServices.DllImportAttribute].GetConstructor(@([string])), @('wintrust.dll'),
        [Reflection.FieldInfo[]]@([Runtime.InteropServices.DllImportAttribute].GetField('SetLastError'), [Runtime.InteropServices.DllImportAttribute].GetField('CharSet')),
        [object[]]@($true, [Runtime.InteropServices.CharSet]::Unicode))
    $declare = {
        param([string]$Name, [Type]$Return, [Type[]]$Parameters)
        $method = $type.DefinePInvokeMethod($Name, 'wintrust.dll', 'Public, Static, PinvokeImpl', 'Standard', $Return, $Parameters,
            [Runtime.InteropServices.CallingConvention]::Winapi, [Runtime.InteropServices.CharSet]::Unicode)
        $method.SetImplementationFlags('PreserveSig')
        $method.SetCustomAttribute($setLastError)
    }
    & $declare 'CryptCATAdminAcquireContext' ([bool]) @([IntPtr].MakeByRefType(), [IntPtr], [uint32])
    & $declare 'CryptCATAdminReleaseContext' ([bool]) @([IntPtr], [uint32])
    & $declare 'CryptCATAdminAddCatalog' ([IntPtr]) @([IntPtr], [string], [string], [uint32])
    & $declare 'CryptCATAdminReleaseCatalogContext' ([bool]) @([IntPtr], [IntPtr], [uint32])
    & $declare 'CryptCATAdminRemoveCatalog' ([bool]) @([IntPtr], [string], [uint32])
    return $type.CreateType()
}

# The catalog database code integrity reads: DRIVER_ACTION_VERIFY
# {F750E6C3-38EE-11D1-85E5-00C04FC295EE}, where Windows' own catalogs are.
# Without it (NULL subsystem) a catalog lands in {127D0A1D-...} and Smart
# App Control keeps blocking its files (Windows Sandbox, 2026-10-08).
$CodeIntegritySubsystem = [Guid]'F750E6C3-38EE-11D1-85E5-00C04FC295EE'

function Use-CatalogAdmin([scriptblock]$Action) {
    $api = Get-CatalogApi
    $admin = [IntPtr]::Zero
    $subsystem = [Runtime.InteropServices.Marshal]::AllocHGlobal(16)
    try {
        [Runtime.InteropServices.Marshal]::Copy($CodeIntegritySubsystem.ToByteArray(), 0, $subsystem, 16)
        if (-not $api::CryptCATAdminAcquireContext([ref]$admin, $subsystem, 0)) {
            throw "CryptCATAdminAcquireContext failed ($([Runtime.InteropServices.Marshal]::GetLastWin32Error()))."
        }
        try { & $Action $api $admin } finally { [void]$api::CryptCATAdminReleaseContext($admin, 0) }
    } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($subsystem) }
}

function Get-InstalledCatalogPath {
    return Join-Path $env:SystemRoot "System32\CatRoot\{$($CodeIntegritySubsystem.ToString().ToUpperInvariant())}\$CatalogBaseName"
}

# Registers the catalog (replacing an earlier AnyAiCam one) only if it carries
# a valid Authenticode signature; returns a one-line result for the setup log.
function Install-RuntimeCatalog([string]$CatalogPath) {
    if (-not (Test-Path -LiteralPath $CatalogPath)) { throw "Runtime catalog not found: $CatalogPath" }
    $signature = Get-AuthenticodeSignature -LiteralPath $CatalogPath
    if ($signature.Status -ne 'Valid') { return "AnyAiCam runtime catalog not registered: it is not validly signed ($($signature.Status)); Smart App Control will block the runtime." }
    Use-CatalogAdmin {
        param($api, $admin)
        $info = $api::CryptCATAdminAddCatalog($admin, $CatalogPath, $CatalogBaseName, 0)
        if ($info -eq [IntPtr]::Zero) { throw "CryptCATAdminAddCatalog failed ($([Runtime.InteropServices.Marshal]::GetLastWin32Error()))." }
        [void]$api::CryptCATAdminReleaseCatalogContext($admin, $info, 0)
    }
    if (-not (Test-Path -LiteralPath (Get-InstalledCatalogPath))) { throw 'The runtime catalog was not registered.' }
    return "AnyAiCam runtime catalog registered (signed by $($signature.SignerCertificate.Subject))."
}

function Remove-RuntimeCatalog {
    if (-not (Test-Path -LiteralPath (Get-InstalledCatalogPath))) { return 'AnyAiCam runtime catalog not registered; nothing to remove.' }
    Use-CatalogAdmin {
        param($api, $admin)
        if (-not $api::CryptCATAdminRemoveCatalog($admin, $CatalogBaseName, 0)) { throw "CryptCATAdminRemoveCatalog failed ($([Runtime.InteropServices.Marshal]::GetLastWin32Error()))." }
    }
    return 'AnyAiCam runtime catalog removed.'
}

# Run from the uninstaller: -Remove. Dot-sourced by build.ps1, install-runtime.ps1 and the tests.
if ($MyInvocation.InvocationName -ne '.' -and $Remove) { Remove-RuntimeCatalog }
