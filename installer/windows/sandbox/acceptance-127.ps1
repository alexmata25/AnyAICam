# Windows 1.2.7 acceptance checks (Red 2026-10-18, Codex 21:07 BLOCKED items). Dot-sourced by validate-in-sandbox.ps1
# after the cloud link, reinstall and repair checks, so these exist: Check, Cloud, WaitFor, WaitHealthy, $app, $data,
# $setup, $results, $TestPortal. Settings come from C:\Validation\scripts\acceptance.json (written by make-wsb.ps1):
#   product_mode              local | hybrid   (the /ProductMode this run installed with)
#   keyset_kind               production | synthetic-test
#   expected_keyset_sha256    the public keyset pin the setup under test was built with
# Everything runs inside the disposable Sandbox against the in-Sandbox test cloud; only synthetic data exists here.

$acceptance = Get-Content -Raw 'C:\Validation\scripts\acceptance.json' | ConvertFrom-Json
$py = Join-Path $app 'runtime\python\python.exe'
$synthKey = 'C:\Validation\scripts\signing-key.json'
$signer = 'C:\Validation\scripts\synthetic-snapshots.py'
$staging = Join-Path $data 'config\vms_capacity_signed.json'
$identityFile = Join-Path $data 'recordings\appliance_identity.json'
$SY = 'S-1-5-18'; $BA = 'S-1-5-32-544'
# 2026-10-20 (Blue, Sandbox acceptance): a production run never receives the synthetic key (make-wsb.ps1 refuses to
# copy one), but its checks need SOME non-production key to sign the snapshot the production build must refuse. Make
# a throwaway sandbox-test-* Ed25519 key inside this disposable Sandbox (it never leaves it, and nothing trusts it).
if ($acceptance.keyset_kind -eq 'production' -and -not (Test-Path -LiteralPath $synthKey)) {
    $synthKey = Join-Path $env:TEMP 'throwaway-sandbox-test-key.json'
    # Python single quotes only: Windows PowerShell 5.1 strips embedded double quotes from native arguments.
    $generate = 'import base64, json, secrets, sys; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; ' +
        'k = Ed25519PrivateKey.generate(); b = lambda x: base64.b64encode(x).decode(); ' +
        'open(sys.argv[1], ''w'', encoding=''ascii'').write(json.dumps({''key_id'': ''sandbox-test-throwaway-'' + secrets.token_hex(6), ' +
        '''private_key_b64'': b(k.private_bytes_raw()), ''public_key_b64'': b(k.public_key().public_bytes_raw())}))'
    & $py -c $generate $synthKey
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $synthKey)) { throw 'could not create the throwaway non-production key' }
}

function AppPython([string]$Code) {
    # Runs $Code with the installed app + agent on sys.path; product-mode keys (only) from vms.env in the environment.
    $file = Join-Path $env:TEMP ('acc-' + [guid]::NewGuid().ToString('N') + '.py')
    $prelude = @"
import os, sys
sys.path[:0] = [r'$app\app', r'$app\agent\system', r'$app\agent']
for line in open(r'$data\config\vms.env', encoding='utf-8-sig'):
    k, _, v = line.strip().partition('=')
    if k in ('ANYAICAM_PRODUCT_MODE', 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP'): os.environ[k] = v
os.chdir(r'$app\app')
"@
    Set-Content -LiteralPath $file -Value ($prelude + "`n" + $Code) -Encoding UTF8
    try { $out = & $py $file 2>&1; $rc = $LASTEXITCODE } finally { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    if ($rc -ne 0) { throw "python exit $rc`: $(($out | Out-String).Trim())" }
    ($out | Out-String).Trim()
}
function Capacity { AppPython 'import vms_capacity, json; c = vms_capacity.load_capacity(); print(json.dumps(None if c is None else int(c["camera_slot_quantity"])))' }
function Licensed { AppPython 'import vms_capacity, json; print(json.dumps(vms_capacity.licensed_camera_numbers([1,2,3,4,5,6])))' }
function AcceptStaged {
    AppPython @'
import capacity_accept, capacity_accept_windows
try:
    print("OUTCOME " + capacity_accept_windows.accept_pending())
except (capacity_accept.Reject, capacity_accept_windows.Reject) as e:
    # The Windows acceptor re-raises the policy's refusal as its own Reject (same code): both are refusals.
    print("REJECT " + str(e))
'@
}
function Sign([string]$Case, [int]$Capacity = 2, [int]$Offset = 0) {
    $out = & $py $signer --app "$app\app" --key $synthKey --identity $identityFile --case $Case --capacity $Capacity --issued-offset-seconds $Offset --out $staging 2>&1
    if ($LASTEXITCODE -ne 0) { throw "signer: $(($out | Out-String).Trim())" }
}
function SidOf($ref) { try { $ref.Translate([Security.Principal.SecurityIdentifier]).Value } catch { "$ref" } }

# ---------------------------------------------------------------- isolation (the Sandbox has no network at all)
Check '1.2.7 isolation: no default route (Sandbox networking disabled)' {
    $routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
    if ($routes.Count) { throw "default route present: $($routes.NextHop -join ',')" }; 'no default route'
}
Check '1.2.7 isolation: production names resolve only to this VM' {
    foreach ($h in 'app.anyaicam.com', 'portal.anyaicam.com', 'portal-staging.anyaicam.com', 'anyaicam.com') {
        $ips = @([Net.Dns]::GetHostAddresses($h) | ForEach-Object { $_.IPAddressToString })
        if ($ips | Where-Object { $_ -ne '127.0.0.1' }) { throw "$h resolves to $($ips -join ',')" }
    }
    'app/portal/portal-staging/anyaicam.com -> 127.0.0.1 only'
}
Check '1.2.7 isolation: an outbound internet connection fails' {
    $c = New-Object Net.Sockets.TcpClient
    try { $t = $c.ConnectAsync('1.1.1.1', 443); if ($t.Wait(5000) -and $c.Connected) { throw 'reached 1.1.1.1:443' } } catch [AggregateException] { } finally { $c.Close() }
    'no route to the internet'
}

# ---------------------------------------------------------------- exact public entitlement keyset
$keyset = Join-Path $data 'update\entitlement_signing_keys.json'
Check "1.2.7 keyset: installed public keyset is exactly the pinned $($acceptance.keyset_kind) keyset" {
    $h = (Get-FileHash -LiteralPath $keyset -Algorithm SHA256).Hash.ToLower()
    if ($h -ne $acceptance.expected_keyset_sha256) { throw "sha256 $h, expected $($acceptance.expected_keyset_sha256)" }
    $raw = [IO.File]::ReadAllText($keyset)
    if ($raw -match 'PRIVATE|BEGIN|OPENSSH|private_key') { throw 'private key material in the installed keyset' }
    "sha256 $h"
}
Check '1.2.7 keyset: owner Administrators, only SYSTEM/Administrators, not a reparse point' {
    $item = Get-Item -LiteralPath $keyset -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse point' }
    $acl = Get-Acl -LiteralPath $keyset
    if ((SidOf $acl.GetOwner([Security.Principal.SecurityIdentifier])) -notin $SY, $BA) { throw "owner $($acl.Owner)" }
    $others = $acl.Access | Where-Object { (SidOf $_.IdentityReference) -notin $SY, $BA }
    if ($others) { throw "extra principals: $(($others | ForEach-Object { SidOf $_.IdentityReference }) -join ',')" }
    'owner SY/BA; SY/BA only'
}
Check '1.2.7 keyset: the synthetic Sandbox test key signs and verifies (key pair self-check)' {
    $o = & $py $signer --app "$app\app" --key $synthKey --case selfcheck 2>&1; if ($LASTEXITCODE -ne 0) { throw ($o | Out-String) }; ($o | Out-String).Trim()
}

# ---------------------------------------------------------------- signed capacity (limits, expiry, tamper)
Check '1.2.7 capacity: before any entitlement, 0 cameras are licensed' {
    $l = Licensed; if ($l -ne '[]') { throw "licensed $l" }; 'licensed [] (fail closed)'
}
if ($acceptance.keyset_kind -eq 'production') {
    # The production build trusts ONLY the production key: a snapshot signed by any other key is refused.
    Stop-Service AnyAiCamVMS -Force
    Check '1.2.7 capacity (production keyset): a snapshot signed by a non-production key is refused (bad_signature)' {
        Sign 'valid' 2; $r = AcceptStaged; if ($r -ne 'REJECT bad_signature') { throw $r }; $r
    }
    Check '1.2.7 capacity (production keyset): still 0 cameras' { $l = Licensed; if ($l -ne '[]') { throw "licensed $l" }; 'licensed []' }
    Remove-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue
    Start-Service AnyAiCamVMS; [void](WaitHealthy 180)
} else {
    # End to end through the REAL configuration route: the test cloud signs with the synthetic key (seeded first).
    [void](Cloud POST '/test/entitlement' @{ product = 'vms_license'; quantity = 2 } -Owner)
    [void](Cloud POST '/test/entitlement' @{ product = "camera_slots_$($acceptance.product_mode)"; quantity = 2 } -Owner)
    $expected = [int](Cloud GET '/test/capacity').camera_slot_quantity
    Check "1.2.7 capacity: the cloud-signed snapshot is accepted end to end (capacity $expected)" {
        $got = WaitFor 240 { $c = Capacity; if ($c -eq "$expected") { $c } }
        if (-not $got) { throw "capacity $(Capacity), expected $expected" }; "accepted capacity $got"
    }
    Check "1.2.7 capacity: exactly $expected cameras licensed, camera $($expected + 1) refused" {
        $l = Licensed; $want = '[' + ((1..$expected) -join ', ') + ']'
        if ($l -ne $want) { throw "licensed $l, expected $want" }; "licensed $l"
    }
    Stop-Service AnyAiCamVMS -Force     # no cloud sync while snapshots are planted
    $cases = @(
        @{ name = 'tampered (capacity edited after signing)'; case = 'tampered'; code = 'bad_signature' },
        @{ name = 'signed by an unknown key';                 case = 'unknown-key'; code = 'bad_signature' },
        @{ name = 'bound to another appliance';               case = 'wrong-binding'; code = 'identity_mismatch' },
        @{ name = 'expired';                                  case = 'expired'; code = 'expired' },
        @{ name = 'issued in the future';                     case = 'future'; code = 'issued_in_future' },
        @{ name = 'older than the accepted one (replay)';     case = 'replay'; code = 'replay' }
    )
    foreach ($c in $cases) {
        Check "1.2.7 capacity: a snapshot $($c.name) is refused ($($c.code)) and the accepted capacity stays $expected" {
            Sign $c.case 9; $r = AcceptStaged
            if ($r -ne "REJECT $($c.code)") { throw "$r" }
            $now = Capacity; if ($now -ne "$expected") { throw "capacity changed to $now" }
            "$r; capacity $now"
        }
    }
    Check '1.2.7 capacity: a newer valid synthetic snapshot raises the limit to 3' {
        Sign 'valid' 3 5; $r = AcceptStaged; if ($r -ne 'OUTCOME accepted') { throw $r }
        $l = Licensed; if ($l -ne '[1, 2, 3]') { throw "licensed $l" }; "$r; licensed $l"
    }
    Remove-Item -LiteralPath $staging -Force -ErrorAction SilentlyContinue
    Start-Service AnyAiCamVMS; [void](WaitHealthy 180)
    Check '1.2.7 capacity: the accepted limit persists across a VMS restart and is refreshed by the cloud' {
        $got = WaitFor 240 { $c = Capacity; if ($c -eq "$expected") { $c } }
        if (-not $got) { throw "capacity $(Capacity)" }; "capacity $got after restart (cloud snapshot is newest)"
    }
}

# ---------------------------------------------------------------- Local / Hybrid authorization and persistence
$mode = $acceptance.product_mode
$other = if ($mode -eq 'local') { 'hybrid' } else { 'local' }
Check "1.2.7 mode: installed as $mode (vms.env bootstrap) and the runtime reports $mode" {
    $line = Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_ -like 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP=*' }
    if ($line -ne "ANYAICAM_PRODUCT_MODE_BOOTSTRAP=$mode") { throw "vms.env: $line" }
    $m = AppPython 'import product_mode; print(product_mode.current_mode())'; if ($m -ne $mode) { throw "runtime $m" }
    "bootstrap $mode; runtime $m"
}
Check "1.2.7 mode: cloud flags follow $mode" {
    $flags = AppPython 'import product_mode, json; print(json.dumps({k: product_mode.resolve_cloud_flag(k) for k in product_mode.FLAG_REGISTRY}))' | ConvertFrom-Json
    $wrong = $flags.PSObject.Properties | Where-Object { $_.Value -ne ($mode -eq 'hybrid') }
    if ($wrong) { throw "flags: $(($wrong | ForEach-Object { $_.Name + '=' + $_.Value }) -join ', ')" }
    "$($flags.PSObject.Properties.Count) cloud flags all $($mode -eq 'hybrid')"
}
Check "1.2.7 mode: Setup refuses to switch $mode -> $other (exit != 0) and nothing changes" {
    $before = Get-FileHash (Join-Path $data 'config\vms.env')
    $p = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/ProductMode=$other", ('/LOG="' + (Join-Path $results 'setup-mode-switch-refused.log') + '"') -Wait -PassThru
    if ($p.ExitCode -eq 0) { throw 'setup accepted a mode switch' }
    if ((Get-FileHash (Join-Path $data 'config\vms.env')).Hash -ne $before.Hash) { throw 'vms.env changed' }
    if ((Get-Service AnyAiCamVMS).Status -ne 'Running') { Start-Service AnyAiCamVMS }
    if (-not (WaitHealthy 180)) { throw 'VMS not healthy after the refused setup' }
    "exit $($p.ExitCode); vms.env unchanged; healthy"
}
if ($acceptance.keyset_kind -eq 'synthetic-test') {
    Check "1.2.7 mode: the cloud-authorized mode ($mode) is persisted in product_mode.json" {
        $state = Join-Path $data 'config\product_mode.json'
        $got = WaitFor 240 { (Get-Content -Raw $state -ErrorAction Stop | ConvertFrom-Json).mode }
        if ($got -ne $mode) { throw "product_mode.json mode $got" }; "product_mode.json mode $got"
    }
}

# ---------------------------------------------------------------- downgrade prevention (owner/Codex 2026-10-18)
$uninstallKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}_is1'
$realVersion = (Get-ItemProperty -LiteralPath $uninstallKey -Name DisplayVersion -ErrorAction SilentlyContinue).DisplayVersion
Check "1.2.7 downgrade: the uninstall entry reports the installed version ($realVersion)" {
    if ($realVersion -ne $expectedVersion) { throw "DisplayVersion '$realVersion', expected '$expectedVersion'" }; $realVersion
}
Set-ItemProperty -LiteralPath $uninstallKey -Name DisplayVersion -Value '9.9.9'      # pretend a NEWER release is installed
Check '1.2.7 downgrade: Setup refuses to replace a newer installed version (9.9.9) and changes nothing' {
    $before = Get-FileHash (Join-Path $data 'config\vms.env')
    $log = Join-Path $results 'setup-downgrade-refused.log'
    $p = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + $log + '"') -Wait -PassThru
    if ($p.ExitCode -eq 0) { throw 'setup ran over a newer version' }
    if (-not (Select-String -LiteralPath $log -SimpleMatch 'Downgrade refused: a newer AnyAiCam VMS is installed' -Quiet)) { throw 'no refusal in the setup log' }
    if (-not (Select-String -LiteralPath $log -Pattern 'install-version-guard: exit 10: .*9\.9\.9' -Quiet)) { throw 'the guard did not name the installed 9.9.9' }
    if ((Get-FileHash (Join-Path $data 'config\vms.env')).Hash -ne $before.Hash) { throw 'vms.env changed' }
    if (-not (WaitHealthy 60)) { throw 'VMS not healthy after the refused setup' }
    "exit $($p.ExitCode); refusal logged; vms.env unchanged; VMS still healthy"
}
foreach ($bad in @('1.3.2-rc1', '')) {                                                   # Codex FAIL ef93b503: fail closed
    Set-ItemProperty -LiteralPath $uninstallKey -Name DisplayVersion -Value $bad
    Check "1.3.2 downgrade: Setup refuses when the installed version is malformed or empty ('$bad') and changes nothing" {
        $before = Get-FileHash (Join-Path $data 'config\vms.env')
        $log = Join-Path $results ('setup-unknown-version-refused-' + ($bad -replace '[^0-9A-Za-z]', '_') + '.log')
        $p = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + $log + '"') -Wait -PassThru
        if ($p.ExitCode -eq 0) { throw 'setup ran over an installation of unknown version' }
        if (-not (Select-String -LiteralPath $log -SimpleMatch 'install-version-guard: exit 11:' -Quiet)) { throw 'no unknown-version refusal in the setup log' }
        if ((Get-FileHash (Join-Path $data 'config\vms.env')).Hash -ne $before.Hash) { throw 'vms.env changed' }
        if (-not (WaitHealthy 60)) { throw 'VMS not healthy after the refused setup' }
        "exit $($p.ExitCode); refusal logged; vms.env unchanged; VMS still healthy"
    }
}
Set-ItemProperty -LiteralPath $uninstallKey -Name DisplayVersion -Value $realVersion  # restore
Check '1.2.7 downgrade: with the real version restored, a repair of the same version still runs (exit 0)' {
    $p = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-same-version-repair.log') + '"') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "exit $($p.ExitCode)" }; if (-not (WaitHealthy 180)) { throw 'not healthy' }; 'exit 0; healthy'
}

# ---------------------------------------------------------------- entitlement files: only SYSTEM/Administrators may write
Check '1.2.7 entitlement security: trust anchor, accepted capacity and staging are writable only by SYSTEM/Administrators' {
    $files = @((Join-Path $data 'update\entitlement_signing_keys.json'), (Join-Path $data 'update\vms_capacity_accepted.json'),
               (Join-Path $data 'update'), (Join-Path $data 'config'))
    $writeMask = [Security.AccessControl.FileSystemRights]'WriteData, AppendData, Delete, ChangePermissions, TakeOwnership'
    $bad = foreach ($f in $files) {
        if (-not (Test-Path -LiteralPath $f)) { continue }
        $acl = Get-Acl -LiteralPath $f
        if ((SidOf ($acl.GetOwner([Security.Principal.SecurityIdentifier]))) -notin $SY, $BA) { "owner of $f" }
        foreach ($r in $acl.Access) { if ($r.AccessControlType -eq 'Allow' -and (SidOf $r.IdentityReference) -notin $SY, $BA -and ($r.FileSystemRights -band $writeMask)) { "$(SidOf $r.IdentityReference) may write $f" } }
    }
    if ($bad) { throw ($bad -join ' | ') }; "$($files.Count) entitlement paths: SY/BA only"
}

# ---------------------------------------------------------------- installed ACLs (recursive) and reparse points
function AclProblems([string]$Root) {
    $problems = New-Object System.Collections.Generic.List[string]
    $writeMask = [Security.AccessControl.FileSystemRights]'WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
    $stack = New-Object System.Collections.Generic.Stack[IO.FileSystemInfo]
    $stack.Push((New-Object IO.DirectoryInfo $Root))
    $count = 0
    while ($stack.Count) {
        $item = $stack.Pop(); $count++
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $problems.Add("reparse point: $($item.FullName)"); continue }
        try { $acl = Get-Acl -LiteralPath $item.FullName } catch { $problems.Add("unreadable ACL: $($item.FullName)"); continue }
        $owner = SidOf ($acl.GetOwner([Security.Principal.SecurityIdentifier]))
        if ($owner -notin $SY, $BA) { $problems.Add("owner $owner on $($item.FullName)") }
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne 'Allow') { continue }
            $sid = SidOf $rule.IdentityReference
            if ($sid -notin $SY, $BA -and ($rule.FileSystemRights -band $writeMask)) { $problems.Add("$sid may write $($item.FullName)") }
        }
        if ($item -is [IO.DirectoryInfo]) { foreach ($child in $item.EnumerateFileSystemInfos()) { $stack.Push($child) } }   # never follows: reparse points are not descended
    }
    [pscustomobject]@{ Problems = $problems; Count = $count }
}
Check '1.2.7 ACL: data root protected with exactly SYSTEM + Administrators full control' {
    $acl = Get-Acl -LiteralPath $data
    if (-not $acl.AreAccessRulesProtected) { throw 'inheritance not removed' }
    $rules = @($acl.Access | ForEach-Object { "$(SidOf $_.IdentityReference):$($_.FileSystemRights)" } | Sort-Object -Unique)
    if (($rules -join ';') -ne "S-1-5-18:FullControl;S-1-5-32-544:FullControl") { throw ($rules -join ';') }
    $rules -join ';'
}
Check '1.2.7 ACL: recursive inspection -- no reparse point, owners SY/BA, no write access for anyone else' {
    $r = AclProblems $data; if ($r.Problems.Count) { throw (($r.Problems | Select-Object -First 8) -join ' | ') }; "$($r.Count) entries clean"
}
$target = 'C:\JunctionTarget'
New-Item -ItemType Directory -Force $target | Out-Null
Set-Content (Join-Path $target 'marker.txt') 'untouched' -Encoding ASCII
$targetSddl = (Get-Acl $target).Sddl
$junction = Join-Path $data 'recordings\planted-junction'
cmd /c mklink /J "$junction" "$target" | Out-Null
Check '1.2.7 junction: the recursive inspection reports the planted junction' {
    $r = AclProblems $data; if (-not ($r.Problems | Where-Object { $_ -like "reparse point: *planted-junction" })) { throw 'not reported' }; 'reported'
}
Check '1.2.7 junction: Setup refuses to run over a planted junction and never touches its target' {
    $p = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-junction-refused.log') + '"') -Wait -PassThru
    if ($p.ExitCode -eq 0) { throw 'setup ran over a junction' }
    if ((Get-Acl $target).Sddl -ne $targetSddl) { throw 'junction target ACL changed' }
    if ((Get-Content (Join-Path $target 'marker.txt')) -ne 'untouched' -or @(Get-ChildItem $target).Count -ne 1) { throw 'junction target content changed' }
    "exit $($p.ExitCode); target ACL and content unchanged"
}
cmd /c rmdir "$junction" | Out-Null
Check '1.2.7 junction: after removing it, a repair succeeds and the tree is clean again' {
    $p = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-after-junction.log') + '"') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "exit $($p.ExitCode)" }
    if (-not (WaitHealthy 180)) { throw 'not healthy' }
    $r = AclProblems $data; if ($r.Problems.Count) { throw (($r.Problems | Select-Object -First 5) -join ' | ') }
    "exit 0; healthy; $($r.Count) entries clean"
}
