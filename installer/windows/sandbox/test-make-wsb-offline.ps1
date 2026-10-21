# Offline test of make-wsb.ps1 (Red 2026-10-18): prepares folders only -- never starts a Sandbox, never installs.
#   powershell -File test-make-wsb-offline.ps1 -Work <empty scratch folder> -SyntheticKey <signing-key.json>
param([Parameter(Mandatory=$true)][string]$Work, [Parameter(Mandatory=$true)][string]$SyntheticKey)
$ErrorActionPreference = 'Stop'
$make = Join-Path $PSScriptRoot 'make-wsb.ps1'
New-Item -ItemType Directory -Force $Work | Out-Null
$setup = Join-Path $Work 'AnyAiCam-VMS-Setup-1.2.7-04f19de.exe'; Set-Content $setup 'dummy' -Encoding ASCII
$older = Join-Path $Work 'AnyAiCam-VMS-Setup-1.2.6-66a0012.exe'; Set-Content $older 'dummy' -Encoding ASCII
$newer = Join-Path $Work 'AnyAiCam-VMS-Setup-1.3.0-aedecb6.exe'; Set-Content $newer 'dummy' -Encoding ASCII
$prod = '2389a655fb3e5ed91e8a4a9216f9c45b97feb3847571b0536384b8305979af7c'
$results = @()
function T([string]$Name, [scriptblock]$Body, [string]$ExpectError = '') {
    try { & $Body | Out-Null; $ok = -not $ExpectError; $why = if ($ok) { 'ok' } else { 'no error' } }
    catch { $ok = $ExpectError -and $_.Exception.Message -like "*$ExpectError*"; $why = $_.Exception.Message }
    $script:results += [pscustomobject]@{ ok = $ok; name = $Name; detail = $why }
    "{0}  {1}  ({2})" -f ($(if ($ok) { 'PASS' } else { 'FAIL' })), $Name, $why
}
T 'production run: folder prepared' { & $make -Setup $setup -Folder (Join-Path $Work 'p') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod }
T 'production run: networking DISABLED in the .wsb' { if (-not (Select-String -Path (Join-Path $Work 'p\AnyAiCam-Validation.wsb') -SimpleMatch '<Networking>Disable</Networking>')) { throw 'networking not disabled' } }
T 'production run: acceptance.json has the mode, kind and pin' { $a = Get-Content -Raw (Join-Path $Work 'p\scripts\acceptance.json') | ConvertFrom-Json; if ($a.product_mode -ne 'local' -or $a.keyset_kind -ne 'production' -or $a.expected_keyset_sha256 -ne $prod) { throw ($a | ConvertTo-Json -Compress) } }
T 'production run: NO signing key is copied' { if (Test-Path (Join-Path $Work 'p\scripts\signing-key.json')) { throw 'signing key present' } }
T 'production run: acceptance-127.ps1 and synthetic-snapshots.py copied' { foreach ($f in 'acceptance-127.ps1', 'synthetic-snapshots.py', 'validate-in-sandbox.ps1', 'test-cloud.py') { if (-not (Test-Path (Join-Path $Work "p\scripts\$f"))) { throw "missing $f" } } }
T 'synthetic-test run: folder prepared with the synthetic key' { & $make -Setup $setup -Folder (Join-Path $Work 's') -ProductMode hybrid -KeysetKind synthetic-test -ExpectedKeysetSha256 ('0' * 64) -SyntheticSigningKey $SyntheticKey; if (-not (Test-Path (Join-Path $Work 's\scripts\signing-key.json'))) { throw 'key not copied' } }
T 'production run with a signing key is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x1') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -SyntheticSigningKey $SyntheticKey } 'never receives a signing key'
T 'synthetic-test run without a key is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x2') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 $prod } 'needs -SyntheticSigningKey'
$fake = Join-Path $Work 'fake-key.json'; Set-Content $fake '{"key_id":"anyaicam-identity-49cfda63a27f","private_key_b64":"x"}' -Encoding ASCII
T 'a non-synthetic (production-looking) key is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x3') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 $prod -SyntheticSigningKey $fake } 'Not a synthetic'
T 'a 1.3.0 -Previous (downgrade) is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x4') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -Previous $newer } 'downgrade'
T 'an older 1.2.6 -Previous is accepted as an upgrade source' { & $make -Setup $setup -Folder (Join-Path $Work 'u') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -Previous $older }
T 'an invalid product mode is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x5') -ProductMode Local2 -KeysetKind production -ExpectedKeysetSha256 $prod } 'ProductMode'
T 'a malformed keyset pin is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x6') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 'ABC' } 'ExpectedKeysetSha256'
$failed = @($results | Where-Object { -not $_.ok }).Count
"MAKE-WSB OFFLINE TEST $($results.Count - $failed)/$($results.Count) passed"
if ($failed) { exit 1 }
