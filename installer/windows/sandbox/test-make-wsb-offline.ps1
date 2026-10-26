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
T 'production runner: installer waits are bounded and return process handles' {
    $runner = Get-Content -Raw (Join-Path $Work 'p\scripts\validate-in-sandbox.ps1')
    if ($runner -notmatch '(?s)Start-Process\s+-FilePath\s+\$FilePath.*?-PassThru.*?WaitForExit\(\$TimeoutSeconds\s*\*\s*1000\)') { throw 'bounded process capture is missing' }
    if ($runner -notmatch 'Start-BoundedProcess\s+-FilePath\s+\$setup\.FullName') { throw 'primary installer does not use bounded process capture' }
}
T 'production runner: terminal errors write report JSON and DONE' {
    $runner = Get-Content -Raw (Join-Path $Work 'p\scripts\validate-in-sandbox.ps1')
    if ($runner -notmatch '(?s)function WriteAcceptanceSummary\s*\{.*?report\.json.*?\bDONE\b' -or $runner -notmatch '(?s)trap\s*\{.*?WriteAcceptanceSummary') { throw 'fatal-error completion call chain is missing' }
}
T 'production runner: failed installation marks dependent acceptance NOT TESTED' {
    $runner = Get-Content -Raw (Join-Path $Work 'p\scripts\validate-in-sandbox.ps1')
    if ($runner -notmatch '(?s)if\s*\(-not\s*\$install\s*-or\s*\$install\.ExitCode\s*-ne\s*0\).*?NotTested\s+\$name\s+\$reason.*?WriteAcceptanceSummary') { throw 'failed install does not stop dependent checks with NOT TESTED status' }
    if ($runner -notmatch "status\s*=\s*'NOT TESTED'") { throw 'NOT TESTED report status is missing' }
}
T 'production runner: test cloud checks the pinned cryptography dependency' {
    $runner = Get-Content -Raw (Join-Path $Work 'p\scripts\validate-in-sandbox.ps1')
    if ($runner -notmatch 'cryptography==44\.0\.0') { throw 'cryptography pin preflight is missing' }
}
T 'Windows runtime dependency: cryptography 44.0.0 is hash-pinned' {
    $requirements = Get-Content (Join-Path $PSScriptRoot '..\requirements-windows.txt')
    $lock = Get-Content (Join-Path $PSScriptRoot '..\wheels.lock.sha256')
    if ($requirements -notcontains 'cryptography==44.0.0') { throw 'requirements-windows.txt pin missing' }
    if (-not ($lock | Where-Object { $_ -match '^708ee5f1bafe76d041b53a4f95eb28cdeb8d18da17e597d46d7833ee59b97ede\s+cryptography-44\.0\.0-cp39-abi3-win_amd64\.whl$' })) { throw 'approved cryptography wheel hash missing' }
}
T 'synthetic-test run: folder prepared with the synthetic key' { & $make -Setup $setup -Folder (Join-Path $Work 's') -ProductMode hybrid -KeysetKind synthetic-test -ExpectedKeysetSha256 ('0' * 64) -SyntheticSigningKey $SyntheticKey; if (-not (Test-Path (Join-Path $Work 's\scripts\signing-key.json'))) { throw 'key not copied' } }
T 'production run with a signing key is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x1') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -SyntheticSigningKey $SyntheticKey } 'never receives a signing key'
T 'synthetic-test run without a key is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x2') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 $prod } 'needs -SyntheticSigningKey'
$fake = Join-Path $Work 'fake-key.json'; Set-Content $fake '{"key_id":"anyaicam-identity-49cfda63a27f","private_key_b64":"x"}' -Encoding ASCII
T 'a non-synthetic (production-looking) key is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x3') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 $prod -SyntheticSigningKey $fake } 'Not a synthetic'
T 'a 1.3.0 -Previous (downgrade) is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x4') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -Previous $newer } 'downgrade'
T 'an older 1.2.6 -Previous is accepted as an upgrade source' { & $make -Setup $setup -Folder (Join-Path $Work 'u') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -Previous $older }
T 'an invalid product mode is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x5') -ProductMode Local2 -KeysetKind production -ExpectedKeysetSha256 $prod } 'ProductMode'
T 'a malformed keyset pin is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x6') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 'ABC' } 'ExpectedKeysetSha256'
# Functional scenario F-L / F-H (Blue 2026-10-21): SYNTHETIC-KEYSET / SIMULATED CAMERA only, default folders unchanged.
T 'functional run: folder prepared, functional entry script + helpers copied, functional=true' {
    & $make -Setup $setup -Folder (Join-Path $Work 'f') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 ('0' * 64) -SyntheticSigningKey $SyntheticKey -Functional
    foreach ($f in 'functional-in-sandbox.ps1', 'edge-http.py', 'onvif-camera-sim.py', 'test-cloud.py', 'signing-key.json') { if (-not (Test-Path (Join-Path $Work "f\scripts\$f"))) { throw "missing $f" } }
    if (-not (Get-Content -Raw (Join-Path $Work 'f\scripts\acceptance.json') | ConvertFrom-Json).functional) { throw 'functional not set' }
    if (-not (Select-String -Path (Join-Path $Work 'f\AnyAiCam-Validation.wsb') -SimpleMatch 'scripts\functional-in-sandbox.ps1</Command>')) { throw 'logon command is not the functional script' }
    if (-not (Select-String -Path (Join-Path $Work 'f\AnyAiCam-Validation.wsb') -SimpleMatch '<Networking>Disable</Networking>')) { throw 'networking not disabled' }
}
T 'default runs are unchanged: validate-in-sandbox.ps1, no functional key, no functional files' {
    foreach ($d in 'p', 's') {
        if (-not (Select-String -Path (Join-Path $Work "$d\AnyAiCam-Validation.wsb") -SimpleMatch 'scripts\validate-in-sandbox.ps1</Command>')) { throw "$d logon command changed" }
        if ((Get-Content -Raw (Join-Path $Work "$d\scripts\acceptance.json") | ConvertFrom-Json).PSObject.Properties.Name -contains 'functional') { throw "$d has functional" }
        foreach ($f in 'functional-in-sandbox.ps1', 'edge-http.py', 'onvif-camera-sim.py') { if (Test-Path (Join-Path $Work "$d\scripts\$f")) { throw "$d has $f" } }
    }
}
T 'functional run with the production keyset is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x7') -ProductMode local -KeysetKind production -ExpectedKeysetSha256 $prod -Functional } 'synthetic-test build only'
# 1.3.3 (2026-10-21): a functional run may be an UPGRADE from a real earlier setup (U132-L / U132-H); never simulated.
T 'functional upgrade run: the earlier setup is copied, functional_upgrade=true, sandbox-proxy.py copied' {
    & $make -Setup $setup -Folder (Join-Path $Work 'fu') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 ('0' * 64) -SyntheticSigningKey $SyntheticKey -Previous $older -Functional
    if (-not (Test-Path (Join-Path $Work ('fu\previous\' + (Split-Path $older -Leaf))))) { throw 'previous setup not copied' }
    $a = Get-Content -Raw (Join-Path $Work 'fu\scripts\acceptance.json') | ConvertFrom-Json
    if (-not $a.functional -or -not $a.functional_upgrade) { throw ($a | ConvertTo-Json -Compress) }
    if (-not (Test-Path (Join-Path $Work 'fu\scripts\sandbox-proxy.py'))) { throw 'sandbox-proxy.py missing' }
}
T 'functional run with simulated 1.3.0 leftovers is refused' { & $make -Setup $setup -Folder (Join-Path $Work 'x8') -ProductMode local -KeysetKind synthetic-test -ExpectedKeysetSha256 $prod -SyntheticSigningKey $SyntheticKey -SimulatePrevious -Functional } 'never plants simulated'
$failed = @($results | Where-Object { -not $_.ok }).Count
"MAKE-WSB OFFLINE TEST $($results.Count - $failed)/$($results.Count) passed"
if ($failed) { exit 1 }
