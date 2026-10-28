<#
Windows 1.3.4.1 acceptance A5: EVERY native file the setup installs is trusted by Smart App Control / App Control,
not just a sample (build.ps1 spot-checks one staged file).

For each .pyd/.dll/.exe (runtime-catalog.ps1's $NativeExtensions) under the install folder the verifier computes the
catalog hash Windows uses (CryptCATAdminCalcHashFromFileHandle2, SHA256) and looks it up among the members of the
AnyAiCam runtime catalog (CryptCATEnumerateMember). Files not in the catalog must carry their own valid Authenticode
signature (e.g. the signed uninstaller, python.exe). Anything else is UNCOVERED.

  Installed (default; any user, read-only):  .\Test-RuntimeCatalogCoverage.ps1 -InstallRoot 'C:\Program Files\AnyAiCam'
      catalog = %SystemRoot%\System32\CatRoot\{F750E6C3-38EE-11D1-85E5-00C04FC295EE}\AnyAiCamRuntime.cat; its own
      signature must be Valid and pinned to the AnyAiCam publisher (publisher-pin.ps1).
  Build stage:  -InstallRoot <build output\runtime-catalog> -Catalog <output\AnyAiCamRuntime.cat> [-AllowUnsignedCatalog]
Category "generated-launcher": runtime\python\Scripts\*.exe that pip writes AT INSTALL TIME (launcher + script), so no
build-time catalog can contain them. They FAIL unless -AllowGeneratedLaunchers is given; the VMS itself never runs
them (it starts `python -m uvicorn`), but App Control would block them. Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED.
#>
param([string]$InstallRoot = (Join-Path $env:ProgramFiles 'AnyAiCam'),
      [string]$Catalog = (Join-Path $env:SystemRoot 'System32\CatRoot\{F750E6C3-38EE-11D1-85E5-00C04FC295EE}\AnyAiCamRuntime.cat'),
      [switch]$AllowUnsignedCatalog, [switch]$AllowGeneratedLaunchers,
      [string]$OutputDir = (Join-Path $env:TEMP 'AnyAiCamAcceptance'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'acceptance-common.ps1')
. (Join-Path $PSScriptRoot '..\runtime-catalog.ps1')
. (Join-Path $PSScriptRoot '..\publisher-pin.ps1')
$result = New-AcceptanceResult 'A5-runtime-catalog-coverage' $(if ($AllowUnsignedCatalog) { 'stage' } else { 'installed' })
$output = Join-Path $OutputDir 'A5-runtime-catalog-coverage.json'

if (-not ('AnyAiCamAcceptance.CatalogCoverage' -as [type])) {
    Add-Type -TypeDefinition @'
using System; using System.Collections.Generic; using System.Runtime.InteropServices; using Microsoft.Win32.SafeHandles;
namespace AnyAiCamAcceptance { public static class CatalogCoverage {
  [DllImport("wintrust.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern IntPtr CryptCATOpen(string f, uint flags, IntPtr prov, uint ver, uint enc);
  [DllImport("wintrust.dll", SetLastError=true)] static extern IntPtr CryptCATEnumerateMember(IntPtr h, IntPtr prev);
  [DllImport("wintrust.dll")] static extern bool CryptCATClose(IntPtr h);
  [DllImport("wintrust.dll", SetLastError=true, CharSet=CharSet.Unicode)] static extern bool CryptCATAdminAcquireContext2(out IntPtr h, IntPtr sub, string alg, IntPtr policy, uint flags);
  [DllImport("wintrust.dll", SetLastError=true)] static extern bool CryptCATAdminCalcHashFromFileHandle2(IntPtr h, SafeFileHandle f, ref uint size, byte[] hash, uint flags);
  [DllImport("wintrust.dll")] static extern bool CryptCATAdminReleaseContext(IntPtr h, uint flags);
  public static HashSet<string> Members(string catalog) {
    var tags = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    IntPtr h = CryptCATOpen(catalog, 0, IntPtr.Zero, 0, 0);
    if (h == IntPtr.Zero || h == new IntPtr(-1)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "CryptCATOpen " + catalog);
    try { IntPtr m = IntPtr.Zero; while ((m = CryptCATEnumerateMember(h, m)) != IntPtr.Zero) tags.Add(Marshal.PtrToStringUni(Marshal.ReadIntPtr(m, IntPtr.Size))); }
    finally { CryptCATClose(h); }
    return tags;
  }
  public static string Hash(string path) {
    IntPtr ctx; if (!CryptCATAdminAcquireContext2(out ctx, IntPtr.Zero, "SHA256", IntPtr.Zero, 0)) throw new System.ComponentModel.Win32Exception();
    try { using (var fs = new System.IO.FileStream(path, System.IO.FileMode.Open, System.IO.FileAccess.Read, System.IO.FileShare.ReadWrite | System.IO.FileShare.Delete)) {
        uint n = 0; CryptCATAdminCalcHashFromFileHandle2(ctx, fs.SafeFileHandle, ref n, null, 0); var b = new byte[n];
        if (!CryptCATAdminCalcHashFromFileHandle2(ctx, fs.SafeFileHandle, ref n, b, 0)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "hash " + path);
        return BitConverter.ToString(b).Replace("-", ""); } }
    finally { CryptCATAdminReleaseContext(ctx, 0); }
  }
} }
'@
}

try {
    if (-not (Test-Path -LiteralPath $InstallRoot)) { Complete-AcceptanceResult $result $output "install folder not found: $InstallRoot" | Out-Null; "A5 BLOCKED: $InstallRoot missing"; exit 2 }
    if (-not (Test-Path -LiteralPath $Catalog)) { Complete-AcceptanceResult $result $output "catalog not found: $Catalog" | Out-Null; "A5 BLOCKED: catalog missing"; exit 2 }
    $catalogSignature = Get-AuthenticodeSignature -LiteralPath $Catalog
    if (-not $AllowUnsignedCatalog) {
        Add-AcceptanceCheck $result 'catalog signature is valid' ($catalogSignature.Status -eq 'Valid') "$($catalogSignature.Status)"
        $pin = Get-AnyAiCamPublisherProblem $catalogSignature $Catalog
        Add-AcceptanceCheck $result 'catalog signed by the pinned AnyAiCam publisher' (-not $pin) $(if ($pin) { $pin } else { $catalogSignature.SignerCertificate.Subject })
    }
    $members = [AnyAiCamAcceptance.CatalogCoverage]::Members($Catalog)
    $files = @(Get-ChildItem -LiteralPath $InstallRoot -Recurse -File -Force | Where-Object { $NativeExtensions -contains $_.Extension.ToLowerInvariant() })
    $rootFull = (Get-Item -LiteralPath $InstallRoot).FullName.TrimEnd('\') + '\'
    $catalogued = @(); $embedded = @(); $generated = @(); $uncovered = @()
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($rootFull.Length)
        if ($members.Contains([AnyAiCamAcceptance.CatalogCoverage]::Hash($file.FullName))) { $catalogued += $relative; continue }
        $signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
        # Its own Authenticode signature, or a Windows catalog: either way App Control trusts it.
        if ($signature.Status -eq 'Valid') { $embedded += "$relative [$($signature.SignatureType): $($signature.SignerCertificate.Subject -replace ',.*','')]"; continue }
        if ($relative -match '^runtime\\python\\Scripts\\[^\\]+\.exe$') { $generated += $relative } else { $uncovered += $relative }
    }
    $result.counts = [ordered]@{ native_files = $files.Count; catalog_members = $members.Count; in_catalog = $catalogued.Count; embedded_signature = $embedded.Count; generated_launchers = $generated.Count; uncovered = $uncovered.Count }
    $result.embedded = $embedded; $result.generated_launchers = $generated; $result.uncovered = $uncovered
    Add-AcceptanceCheck $result 'native files found' ($files.Count -gt 0) "$($files.Count)"
    Add-AcceptanceCheck $result 'every native file is catalogued or validly signed' ($uncovered.Count -eq 0) $(if ($uncovered) { ($uncovered | Select-Object -First 20) -join '; ' } else { "$($catalogued.Count) catalogued, $($embedded.Count) embedded-signed" })
    Add-AcceptanceCheck $result 'no pip-generated launchers outside the catalog' ($AllowGeneratedLaunchers -or $generated.Count -eq 0) $(if ($generated) { "$($generated.Count) (e.g. $(($generated | Select-Object -First 3) -join ', '))$(if ($AllowGeneratedLaunchers) { '; allowed by -AllowGeneratedLaunchers' })" } else { '' })
} catch {
    Complete-AcceptanceResult $result $output $_.Exception.Message | Out-Null
    Write-Output "A5 BLOCKED: $($_.Exception.Message)"; exit 2
}
$final = Complete-AcceptanceResult $result $output
$final.checks | ForEach-Object { '{0} {1} {2}' -f ($(if ($_.passed) { 'PASS' } else { 'FAIL' })), $_.name, $_.detail }
"counts: " + (($final.counts.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ' ')
"A5 $($final.result) -> $output"
exit $(if ($final.result -eq 'PASS') { 0 } else { 1 })
