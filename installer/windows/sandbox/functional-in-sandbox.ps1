# FUNCTIONAL Windows Sandbox scenario F-L / F-H for AnyAiCam VMS 1.2.7 (Windows setup 1.3.2). Blue, 2026-10-21.
#
# SYNTHETIC-KEYSET / SIMULATED CAMERA. This is NOT production-keyset acceptance and NOT physical-camera acceptance:
#   * the setup under test is the TEST-ONLY synthetic-keyset build (make-wsb.ps1 -Functional refuses anything else),
#     because only it accepts the camera capacity the disposable test cloud signs -- without capacity no camera runs;
#   * the "camera" is a simulated ONVIF device (onvif-camera-sim.py) and an FFmpeg test pattern published by a
#     separate copy of the bundled MediaMTX, all on loopback address 127.0.0.5 inside this Sandbox (no network);
#   * the cloud is the disposable in-Sandbox test cloud (test-cloud.py --functional), with a synthetic owner account.
# Exact differences from a real PC and a real camera are in README.md ("Functional scenarios F-L / F-H").
#
# Started by AnyAiCam-Validation.wsb's LogonCommand (make-wsb.ps1 -Functional). Writes results\report.txt,
# report.json, the logs and the media probes; results\DONE at the end. Every check records PASS/FAIL and the script
# never stops at the first failure; INFO lines record observed behaviour that has no pass/fail expectation.
$ErrorActionPreference = 'Continue'
$results = 'C:\Validation\results'
New-Item -ItemType Directory -Force -Path $results | Out-Null
$checks = New-Object System.Collections.Generic.List[object]
function Check([string]$Name, [scriptblock]$Test) {
    try { $detail = & $Test; $ok = $true } catch { $detail = $_.Exception.Message; $ok = $false }
    if ($detail -is [bool]) { $ok = $detail; $detail = '' }
    $checks.Add([pscustomobject]@{ name = $Name; ok = [bool]$ok; detail = "$detail" })
    Add-Content -Path (Join-Path $results 'report.txt') -Value ("{0}  {1}  {2}" -f ($(if ($ok) { 'PASS' } else { 'FAIL' })), $Name, $detail)
}
function Info([string]$Name, [scriptblock]$Observe) {
    try { $detail = & $Observe } catch { $detail = "error: $($_.Exception.Message)" }
    Add-Content -Path (Join-Path $results 'report.txt') -Value ("INFO  {0}  {1}" -f $Name, $detail)
}
function Http([string]$Path) { Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 -Uri ("http://127.0.0.1:8000" + $Path) }
function WaitHealthy([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) { try { if ((Http '/health').StatusCode -eq 200) { return $true } } catch { }; Start-Sleep -Seconds 5 }
    return $false
}
function WaitFor([int]$Seconds, [scriptblock]$Condition) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) { try { $value = & $Condition; if ($value) { return $value } } catch { }; Start-Sleep -Seconds 3 }
    return $null
}

$settings = Get-Content -Raw 'C:\Validation\scripts\acceptance.json' | ConvertFrom-Json
$mode = $settings.product_mode
$other = if ($mode -eq 'local') { 'hybrid' } else { 'local' }
$run = if ($mode -eq 'local') { 'F-L' } else { 'F-H' }
Set-Content -Path (Join-Path $results 'report.txt') -Value @(
    "AnyAiCam Windows FUNCTIONAL scenario $run " + (Get-Date -Format o) + " on " + [Environment]::OSVersion.VersionString,
    "LABEL: SYNTHETIC-KEYSET / SIMULATED CAMERA -- not production-keyset acceptance, not physical-camera acceptance.")
$setup = Get-ChildItem 'C:\Validation\setup' -Filter 'AnyAiCam-VMS-Setup-*.exe' | Select-Object -First 1
Check 'setup file present (SYNTHETIC-KEYSET build)' { if (-not $setup) { throw 'no setup' }; "$($setup.Name) $($setup.Length) bytes sha256=$((Get-FileHash $setup.FullName -Algorithm SHA256).Hash.ToLower())" }
Check 'run settings: synthetic-test keyset (functional runs never use the production build)' {
    if ($settings.keyset_kind -ne 'synthetic-test' -or -not $settings.functional) { throw "keyset_kind $($settings.keyset_kind) functional $($settings.functional)" }
    "keyset synthetic-test; mode $mode"
}

# ---------------------------------------------------------------- isolation (same rules as validate-in-sandbox.ps1)
$TestCloudHost = 'cloud.anyaicam.test'
$TestPortal = "https://${TestCloudHost}:8443"
Add-Content -Path "$env:SystemRoot\System32\drivers\etc\hosts" -Value "`r`n127.0.0.1 $TestCloudHost`r`n127.0.0.1 app.anyaicam.com`r`n127.0.0.1 portal.anyaicam.com`r`n127.0.0.1 portal-staging.anyaicam.com`r`n127.0.0.1 anyaicam.com"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Check 'isolation: no default route (Sandbox networking disabled)' {
    $routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue); if ($routes.Count) { throw "default route via $($routes.NextHop -join ',')" }; 'no default route'
}
Info 'network adapters in this Sandbox' { (@(Get-NetIPAddress -ErrorAction SilentlyContinue | ForEach-Object { "$($_.InterfaceAlias)=$($_.IPAddress)" }) -join '; ') }

# ---------------------------------------------------------------- install
$installError = ''
try { $install = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/PortalUrl=$TestPortal", "/ProductMode=$mode", ('/LOG="' + (Join-Path $results 'setup-install.log') + '"') -Wait -PassThru -ErrorAction Stop }
catch { $install = $null; $installError = $_.Exception.Message }
Check 'install: silent setup exit code 0' { if (-not $install) { throw "setup did not start: $installError" }; if ($install.ExitCode -ne 0) { throw "exit $($install.ExitCode)" }; 'exit 0' }
Check 'install: VMS answers /health within 3 minutes' { WaitHealthy 180 }
Check 'install: /version is 1.2.7 with the full build commit' { $v = (Http '/version').Content | ConvertFrom-Json; if ($v.version -ne '1.2.7' -or "$($v.build_id)" -notmatch '^[0-9a-f]{40}$') { throw "version $($v.version) build $($v.build_id)" }; "version $($v.version) build $($v.build_id)" }
$app = Join-Path $env:ProgramFiles 'AnyAiCam'
$data = Join-Path $env:ProgramData 'AnyAiCam'
$agentState = Join-Path $data 'agent'

# ---------------------------------------------------------------- harness runtime OUTSIDE the install folder
# (as validate-in-sandbox.ps1 since 2026-10-21: Setup/agent restarts must never touch harness processes)
$cloudData = 'C:\TestCloud'
$cloudRuntime = Join-Path $cloudData 'runtime'
New-Item -ItemType Directory -Force $cloudRuntime | Out-Null
foreach ($part in 'python', 'app') {
    $source = if ($part -eq 'python') { Join-Path $app 'runtime\python' } else { Join-Path $app 'app' }
    & robocopy.exe $source (Join-Path $cloudRuntime $part) /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
}
$hp = Join-Path $cloudRuntime 'python\python.exe'           # harness Python (a copy of the bundled one)
$cloudApp = Join-Path $cloudRuntime 'app'
$sim = 'C:\Sim'
New-Item -ItemType Directory -Force $sim | Out-Null
& robocopy.exe (Join-Path $app 'runtime\tools\ffmpeg') (Join-Path $sim 'ffmpeg') /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
& robocopy.exe (Join-Path $app 'runtime\tools\mediamtx') (Join-Path $sim 'mediamtx') /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
$ffmpeg = Join-Path $sim 'ffmpeg\ffmpeg.exe'
$ffprobe = Get-ChildItem (Join-Path $sim 'ffmpeg') -Recurse -Filter 'ffprobe.exe' | Select-Object -First 1 -ExpandProperty FullName
Check 'harness tools copied outside the install folder (Python, FFmpeg, ffprobe, MediaMTX)' {
    foreach ($f in $hp, $ffmpeg, $ffprobe, (Join-Path $sim 'mediamtx\mediamtx.exe')) { if (-not $f -or -not (Test-Path -LiteralPath $f)) { throw "missing $f" } }; 'present'
}

function AppPython([string]$Code) {
    # The installed app on sys.path with the service's own data folders (service-launcher.ps1) and mode keys (vms.env).
    $file = Join-Path $env:TEMP ('fn-' + [guid]::NewGuid().ToString('N') + '.py')
    $prelude = @"
import os, sys
sys.path[:0] = [r'$app\app', r'$app\agent\system', r'$app\agent']
for line in open(r'$data\config\vms.env', encoding='utf-8-sig'):
    k, _, v = line.strip().partition('=')
    if k in ('ANYAICAM_PRODUCT_MODE', 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP'): os.environ[k] = v
os.environ['ANYAICAM_STATIC_FOLDER'] = r'$app\app\static'
os.environ['ANYAICAM_RECORDINGS_FOLDER'] = r'$data\recordings'
os.environ['ANYAICAM_HLS_FOLDER'] = r'$data\hls'
os.environ['ANYAICAM_PARTNER_DB'] = r'$data\database\partner_portal.db'
os.environ['ANYAICAM_STATE_DIR'] = r'$data\agent'
os.chdir(r'$app\app')
"@
    Set-Content -LiteralPath $file -Value ($prelude + "`n" + $Code) -Encoding ASCII
    try { $out = & (Join-Path $app 'runtime\python\python.exe') $file 2>&1; $rc = $LASTEXITCODE } finally { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    if ($rc -ne 0) { throw "python exit $rc`: $(($out | Out-String).Trim())" }
    ($out | Out-String).Trim()
}
function EdgeSql([string]$Sql) {
    # One read-only (or test-setup) statement on the edge database; rows as JSON. The SQL travels base64-encoded.
    $q = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Sql))
    $json = AppPython ("import sqlite3, json, base64; c = sqlite3.connect(r'$data\database\partner_portal.db'); c.row_factory = sqlite3.Row; " +
               "r = [dict(x) for x in c.execute(base64.b64decode('$q').decode()).fetchall()]; c.commit(); print(json.dumps(r))")
    ($json | ConvertFrom-Json) | ForEach-Object { $_ }   # one pipeline object per row (none for an UPDATE)
}
function CurrentMode { AppPython 'import product_mode; print(product_mode.current_mode())' }
function CloudFlags { AppPython 'import product_mode, json; print(json.dumps({k: product_mode.resolve_cloud_flag(k) for k in product_mode.FLAG_REGISTRY}))' | ConvertFrom-Json }
function Capacity { AppPython 'import vms_capacity, json; c = vms_capacity.load_capacity(); print(json.dumps(None if c is None else int(c["camera_slot_quantity"])))' }
function Licensed([int]$Number) { AppPython "import vms_capacity, json; print(json.dumps(vms_capacity.licensed_camera_numbers([$Number])))" }

# Edge HTTP through edge-http.py (cookie jar, no redirects, JSON body from a file).
$edgeHttp = 'C:\Validation\scripts\edge-http.py'
$ownerJar = 'C:\Sim\owner.cookies'
function Edge([string]$Method, [string]$Path, $Body = $null, [string]$Jar = $ownerJar, [string]$Out = '') {
    $arguments = @($edgeHttp, $Method, $Path)
    if ($Jar) { $arguments += @('--jar', $Jar) }
    if ($null -ne $Body) {
        $bodyFile = Join-Path $env:TEMP ('body-' + [guid]::NewGuid().ToString('N') + '.json')
        [IO.File]::WriteAllText($bodyFile, ($Body | ConvertTo-Json -Compress -Depth 6), [Text.Encoding]::ASCII)
        $arguments += @('--json-file', $bodyFile)
    }
    if ($Out) { $arguments += @('--out', $Out) }
    try { $line = & $hp @arguments 2>&1 | Select-Object -Last 1 } finally { if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue } }
    "$line" | ConvertFrom-Json
}
function Cloud([string]$Method, [string]$Path, $Body = $null, [switch]$Owner) {
    $arguments = @{ Method = $Method; Uri = "$TestPortal$Path"; TimeoutSec = 30; UseBasicParsing = $true; ContentType = 'application/json' }
    if ($Owner) { $arguments.Headers = @{ 'X-Test-Owner' = 'yes' } }
    if ($null -ne $Body) { $arguments.Body = ($Body | ConvertTo-Json -Compress) }
    Invoke-RestMethod @arguments
}
function CloudFails([string]$Method, [string]$Path, $Body) {
    try { Cloud $Method $Path $Body -Owner | Out-Null; return $false } catch { return $true }
}
function Probe([string]$File) {
    $transport = if ($File.StartsWith('rtsp://')) { @('-rtsp_transport', 'tcp') } else { @() }
    $out = & $ffprobe -v error @transport -show_entries stream=codec_type,codec_name,width,height -show_entries format=format_name,duration -of json $File 2>&1 | Out-String
    $out | ConvertFrom-Json
}
function Mkv([int]$Number) { @(Get-ChildItem (Join-Path $data "recordings\camera$Number") -Filter '*.mkv' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime) }

# ---------------------------------------------------------------- test cloud (functional routes; synthetic key; Sandbox only)
& $hp 'C:\Validation\scripts\test-cloud.py' --app $cloudApp --data $cloudData --host $TestCloudHost --certificates-only
Import-Certificate -FilePath (Join-Path $cloudData 'tls\ca.cer') -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
$script:cloudStarts = 0
function StartCloud {
    $script:cloudStarts++
    $arguments = @('"C:\Validation\scripts\test-cloud.py"', '--app', ('"' + $cloudApp + '"'), '--data', $cloudData, '--host', $TestCloudHost,
                   '--port', '8443', '--signing-key', '"C:\Validation\scripts\signing-key.json"', '--functional')
    $script:testCloud = Start-Process -FilePath $hp -ArgumentList $arguments -WorkingDirectory $cloudData -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $results "test-cloud-$script:cloudStarts.out.log") -RedirectStandardError (Join-Path $results "test-cloud-$script:cloudStarts.err.log")
    [bool](WaitFor 120 { Cloud GET '/test/state' })
}
function StopCloud { if ($script:testCloud -and -not $script:testCloud.HasExited) { Stop-Process -Id $script:testCloud.Id -Force }; Start-Sleep -Seconds 3 }
Check 'test cloud up (real claim, appliance and customer camera routes; --functional, synthetic key)' { if (-not (StartCloud)) { throw 'no answer' }; $TestPortal }
Restart-Service AnyAiCamAgent -Force

# ---------------------------------------------------------------- sign-in on a never-linked PC (before linking)
$ownerEmail = 'owner@example.test'
$cloudPassword = 'Synthetic-Cloud-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$appliancePassword = 'Synthetic-Appliance-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
[void](Cloud POST '/test/owner' @{ password = $cloudPassword } -Owner)
Check 'sign-in: a never-linked PC refuses customer sign-in (403, no session)' {
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true } -Jar 'C:\Sim\unlinked.cookies'
    if ($r.status -ne 403) { throw "status $($r.status) $($r.body | ConvertTo-Json -Compress)" }
    if ((Get-Content 'C:\Sim\unlinked.cookies' -ErrorAction SilentlyContinue) -match 'anyaicam_partner_session') { throw 'a session cookie was set' }
    "403 $($r.body.detail)"
}

# ---------------------------------------------------------------- linking: invalid, expired, used link codes
. (Join-Path $app 'installer\open-claim-link.ps1')
$ErrorActionPreference = 'Continue'
function LinkCodeOf([string]$Link) { if ($Link -cmatch '#code=([0-9A-Z]{8})&link=windows$') { return $Matches[1] }; return '' }
$linkCodes = New-Object System.Collections.Generic.List[string]
$applianceId = [string](Get-Content -Raw (Join-Path $data 'config\appliance_identity.json') | ConvertFrom-Json).appliance_id
Check 'linking: an invalid link code finds no computer' { if (-not (CloudFails POST '/api/portal/claims/lookup' @{ claim_code = 'ZZZZ2222' })) { throw 'lookup succeeded' }; 'refused' }
$expiredCode = ''
Check 'linking: an expired link code finds nothing and cannot be confirmed' {
    $a = Request-AccountLink $data 90; if ($a.status -ne 'ok') { throw "link request status $($a.status)" }
    $script:expiredCode = LinkCodeOf $a.link; $script:linkCodes.Add($script:expiredCode)
    $n = (Cloud POST '/test/expire-pending-claims' -Owner).expired; if ($n -lt 1) { throw 'nothing to expire' }
    if (-not (CloudFails POST '/api/portal/claims/lookup' @{ claim_code = $script:expiredCode })) { throw 'expired code still found' }
    if (-not (CloudFails POST '/api/portal/claims/confirm' @{ claim_code = $script:expiredCode; site_id = 'test-site' })) { throw 'expired code confirmed' }
    "expired $n; lookup and confirm refused"
}
$code = ''
Check 'linking: after expiry the computer gives a fresh, different one-time link' {
    $b = WaitFor 120 { $x = Request-AccountLink $data 60; if ($x.status -eq 'ok' -and (LinkCodeOf $x.link) -ne $script:expiredCode) { $x } }
    if (-not $b) { throw 'no fresh link' }
    $script:code = LinkCodeOf $b.link; $script:linkCodes.Add($script:code)
    if ((Cloud POST '/api/portal/claims/lookup' @{ claim_code = $script:code } -Owner).device_id -ne $applianceId) { throw 'new code finds another device' }
    'new code finds this PC'
}
Check 'linking: the owner confirms the fresh link and the agent completes the claim' {
    if ((Cloud POST '/api/portal/claims/confirm' @{ claim_code = $script:code; site_id = 'test-site' } -Owner).status -ne 'claimed') { throw 'not claimed' }
    if (-not (WaitFor 180 { Test-Path (Join-Path $agentState 'credential.json') })) { throw 'no credential.json' }
    $identity = WaitFor 90 { Get-Content -Raw (Join-Path $data 'recordings\appliance_identity.json') -ErrorAction Stop | ConvertFrom-Json }
    if (-not $identity -or $identity.site_id -ne 'test-site') { throw 'VMS identity missing' }
    if (-not (WaitHealthy 180)) { throw 'VMS not healthy after linking' }
    "claimed; cloud_id $($identity.cloud_id)"
}
Check 'linking: a used link code cannot be confirmed again' { if (-not (CloudFails POST '/api/portal/claims/confirm' @{ claim_code = $script:code; site_id = 'test-site' })) { throw 'confirmed twice' }; 'refused' }

# ---------------------------------------------------------------- signed camera capacity (synthetic key) and mode
[void](Cloud POST '/test/entitlement' @{ product = 'vms_license'; quantity = 2 } -Owner)
[void](Cloud POST '/test/entitlement' @{ product = "camera_slots_$mode"; quantity = 2 } -Owner)
Check 'capacity: the cloud-signed (SYNTHETIC key) capacity 2 is accepted' { $got = WaitFor 300 { $c = Capacity; if ($c -eq '2') { $c } }; if (-not $got) { throw "capacity $(Capacity)" }; 'capacity 2' }
Check "mode: runtime is $mode and product_mode.json says $mode" {
    $m = CurrentMode; if ($m -ne $mode) { throw "runtime $m" }
    $p = WaitFor 240 { (Get-Content -Raw (Join-Path $data 'config\product_mode.json') -ErrorAction Stop | ConvertFrom-Json).mode }; if ($p -ne $mode) { throw "product_mode.json $p" }
    "runtime $m; persisted $p"
}

# ---------------------------------------------------------------- customer sign-in online, session, appliance password
Check 'sign-in: the owner signs in online with the cloud password (303 + session cookie)' {
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true }
    if ($r.status -ne 303) { throw "status $($r.status) $($r.body | ConvertTo-Json -Compress)" }
    if (-not ((Get-Content $ownerJar) -match 'anyaicam_partner_session')) { throw 'no session cookie' }
    "303 -> $($r.location)"
}
Check 'sign-in: a wrong cloud password is refused (no session)' {
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = 'Wrong-Password-000'; customer_only = $true } -Jar 'C:\Sim\wrong.cookies'
    if ($r.status -lt 400) { throw "status $($r.status)" }; "status $($r.status)"
}
Check 'session: a protected customer API answers with the session and refuses without it' {
    $with = Edge GET '/api/customer/appliance-sign-in'; $without = Edge GET '/api/customer/appliance-sign-in' -Jar ''
    if ($with.status -ne 200) { throw "with session $($with.status)" }; if ($without.status -notin 401, 403) { throw "without session $($without.status)" }
    "200 with session; $($without.status) without"
}
Check 'appliance password: the owner enrols an offline appliance password' {
    $r = Edge POST '/api/customer/appliance-sign-in' @{ cloud_password = $cloudPassword; new_password = $appliancePassword }
    if ($r.status -ne 200 -or $r.body.status -ne 'enrolled') { throw "status $($r.status) $($r.body | ConvertTo-Json -Compress)" }
    $s = (Edge GET '/api/customer/appliance-sign-in').body; if (-not $s.enrolled -or -not $s.offline_ready) { throw "enrolled $($s.enrolled) offline_ready $($s.offline_ready)" }
    'enrolled; offline_ready'
}

# ---------------------------------------------------------------- simulated camera on 127.0.0.5 (loopback only)
$camHost = '127.0.0.5'
$rtsp = "rtsp://${camHost}:8554/cam1"
@('logLevel: info', 'api: no', 'rtsp: yes', "rtspAddress: ${camHost}:8554", 'rtspTransports: [tcp]', 'rtmp: no', 'hls: no', 'webrtc: no', 'srt: no', 'paths:', '  cam1:') |
    Set-Content -LiteralPath (Join-Path $sim 'camera-mediamtx.yml') -Encoding ASCII
$camServer = Start-Process -FilePath (Join-Path $sim 'mediamtx\mediamtx.exe') -ArgumentList ('"' + (Join-Path $sim 'camera-mediamtx.yml') + '"') -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $results 'camera-mediamtx.out.log') -RedirectStandardError (Join-Path $results 'camera-mediamtx.err.log')
Start-Sleep -Seconds 3
# A black frame with a white box that moves for 6 s, then holds still for 6 s (basic motion needs motion that stops).
$encoder = if ((& $ffmpeg -hide_banner -encoders 2>&1 | Out-String) -match 'libx264') { @('-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency') } else { @('-c:v', 'mpeg4') }
$ffArgs = @('-hide_banner', '-loglevel', 'warning', '-re', '-f', 'lavfi', '-i', 'color=c=black:s=640x360:r=10', '-f', 'lavfi', '-i', 'color=c=white:s=160x120:r=10',
            '-filter_complex', "[0][1]overlay=x='if(lt(mod(t,12),6),40+60*mod(t,12),400)':y=120:shortest=0", '-pix_fmt', 'yuv420p', '-g', '20') + $encoder +
          @('-f', 'rtsp', '-rtsp_transport', 'tcp', $rtsp)
$camPublisher = Start-Process -FilePath $ffmpeg -ArgumentList $ffArgs -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $results 'camera-ffmpeg.out.log') -RedirectStandardError (Join-Path $results 'camera-ffmpeg.err.log')
$onvifSim = Start-Process -FilePath $hp -ArgumentList @('"C:\Validation\scripts\onvif-camera-sim.py"', '--bind', $camHost, '--http-port', '80', '--rtsp', $rtsp,
        '--endpoint', 'urn:uuid:5a7d0c1e-0000-4000-8000-00000000c001', '--manufacturer', 'AnyAiCamSim', '--model', 'SIM-1') -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $results 'onvif-sim.out.log') -RedirectStandardError (Join-Path $results 'onvif-sim.err.log')
Check 'simulated camera: synthetic RTSP stream published on 127.0.0.5:8554 (FFmpeg test pattern via MediaMTX)' {
    $ok = WaitFor 60 { $p = Probe $rtsp; if ($p.streams | Where-Object { $_.codec_type -eq 'video' }) { $p } }
    if (-not $ok) { throw "no stream; ffmpeg exited $($camPublisher.HasExited)" }
    $v = $ok.streams | Where-Object { $_.codec_type -eq 'video' } | Select-Object -First 1; "$($v.codec_name) $($v.width)x$($v.height); encoder $($encoder[1])"
}
Check 'simulated camera: ONVIF device service answers on 127.0.0.5:80' {
    $body = '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>'
    $r = WaitFor 30 { Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://${camHost}/onvif/device_service" -Body $body -ContentType 'application/soap+xml' -TimeoutSec 5 }
    if (-not $r -or $r.Content -notmatch 'AnyAiCamSim') { throw "no ONVIF answer (sim exited $($onvifSim.HasExited))" }; 'GetDeviceInformation: AnyAiCamSim SIM-1'
}

# ---------------------------------------------------------------- discovery and provisioning through the real routes
# LIMITATION (documented): the agent never scans loopback by default (windows.private_networks) and manual add-by-IP
# refuses loopback, so the harness gives the agent the explicit discovery range 127.0.0.4/30 (.5 and .6) in agent.json.
$agentJson = Join-Path $data 'config\agent.json'
$cfg = Get-Content -Raw $agentJson | ConvertFrom-Json
$cfg | Add-Member -NotePropertyName discovery_networks -NotePropertyValue @('127.0.0.4/30') -Force
[IO.File]::WriteAllText($agentJson, ($cfg | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding $false))
Restart-Service AnyAiCamAgent -Force
Check 'discovery: agent.json keeps the test discovery range after the agent restart' {
    Start-Sleep -Seconds 15; $n = (Get-Content -Raw $agentJson | ConvertFrom-Json).discovery_networks; if (@($n) -notcontains '127.0.0.4/30') { throw "discovery_networks $($n -join ',')" }; '127.0.0.4/30'
}
$cloudAppliance = (Cloud GET '/test/state').appliances | Select-Object -First 1
$deviceKey = ''
Check 'discovery: a cloud scan job finds the simulated camera (agent scan, real scan-job routes)' {
    $job = Cloud POST "/api/customer/appliances/$($cloudAppliance.id)/scan" @{} -Owner
    $done = WaitFor 420 { $j = Cloud GET "/api/customer/camera-scans/$($job.job_id)" -Owner; if ($j.status -in 'complete', 'completed', 'failed', 'timed_out') { $j } }
    if (-not $done) { throw 'scan did not finish' }
    $done | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $results 'scan-job.json')
    $found = @($done.results)
    if (-not $found.Count) { throw "status $($done.status); no camera found: $($done.message)" }
    $script:deviceKey = [string]$found[0].device_key
    "status $($done.status); $($found.Count) camera(s); device_key $($script:deviceKey.Substring(0, [Math]::Min(12, $script:deviceKey.Length)))..."
}
$cloudCamera = $null
Check 'provisioning: the owner adds the discovered camera; the agent verifies it (RTSP DESCRIBE) and the cloud provisions it' {
    $p = Cloud POST '/api/customer/cameras/provision' @{ appliance_id = $cloudAppliance.id; device_key = $script:deviceKey; name = 'Simulated camera'; recording_mode = 'continuous' } -Owner
    $done = WaitFor 420 { $j = Cloud GET "/api/customer/camera-provisioning/$($p.job_id)" -Owner; if ($j.status -in 'provisioned', 'failed', 'timed_out') { $j } }
    $done | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $results 'provision-job.json')
    if (-not $done -or $done.status -ne 'provisioned') { throw "status $($done.status) $($done.failure_code) $($done.message)" }
    $script:cloudCamera = WaitFor 240 { @((Cloud GET '/test/cameras' -Owner).cameras | Where-Object { $_.onvif_endpoint })[0] }
    if (-not $script:cloudCamera) { throw 'provisioned, but no stream address (ONVIF GetStreamUri) reached the cloud' }
    "provisioned; camera_number $($script:cloudCamera.camera_number); stream $($script:cloudCamera.onvif_endpoint)"
}
$camera = $null; $camNo = 0
Check 'edge sync: the camera reaches the VMS with its stream address and is licensed' {
    $script:camera = WaitFor 300 { @(EdgeSql "SELECT id, camera_number, onvif_endpoint FROM cameras WHERE onvif_endpoint LIKE 'rtsp://127.0.0.5%'")[0] }
    if (-not $script:camera) { throw 'no camera on the edge' }
    $script:camNo = [int]$script:camera.camera_number
    $l = Licensed $script:camNo; if ($l -ne "[$($script:camNo)]") { throw "licensed $l" }
    "camera $($script:camNo) $($script:camera.onvif_endpoint); licensed"
}
$camId = if ($camera) { [string]$camera.id } else { 'none' }

# ---------------------------------------------------------------- live view
$liveStarted = Get-Date
Check 'live view: the customer live playlist serves fresh segments of the simulated camera' {
    $pl = WaitFor 120 { $r = Edge GET "/api/customer/cameras/$camId/live/playlist.m3u8"; if ($r.status -eq 200 -and "$($r.body)" -match '#EXTINF') { $r } }
    if (-not $pl) { throw 'no live playlist with segments' }
    "$($pl.body)" | Set-Content (Join-Path $results 'live-playlist.m3u8')
    $segment = ("$($pl.body)" -split "`n" | Where-Object { $_ -and -not $_.StartsWith('#') } | Select-Object -Last 1).Trim()
    $file = Join-Path $results 'live-segment.ts'
    $s = Edge GET $segment -Out $file
    if ($s.status -ne 200) { throw "segment $($s.status)" }
    $p = Probe $file; $v = $p.streams | Where-Object { $_.codec_type -eq 'video' } | Select-Object -First 1
    if (-not $v) { throw 'segment has no video' }; "playlist + segment $($v.codec_name) $($v.width)x$($v.height)"
}
Check 'live view: the still image endpoint returns a JPEG' {
    $f = Join-Path $results 'live-still.jpg'; $r = Edge GET "/api/customer/cameras/$camId/live/still.jpg" -Out $f
    if ($r.status -ne 200 -or $r.content_type -notmatch 'jpeg') { throw "status $($r.status) $($r.content_type)" }; "$($r.length) bytes"
}
Check 'live view (operator pages): /static/hls/camera<N>.m3u8 -- used by /, /camera/<N> and /playback -- is served' {
    $files = @(Get-ChildItem (Join-Path $data 'hls') -Filter "camera$camNo*.m3u8" -ErrorAction SilentlyContinue)
    $r = Edge GET "/static/hls/camera$camNo.m3u8" -Jar ''
    if ($r.status -ne 200) { throw "HTTP $($r.status) although $($files.Count) playlist file(s) exist in $data\hls (static files are served from $app\app\static)" }
    'served'
}
Info 'live view: WebRTC' { 'not tested: WebRTC signalling runs through the cloud (live_view_p2p), which the test cloud does not serve' }

# ---------------------------------------------------------------- recording, motion, analytics
Check 'recording: the VMS records the simulated camera to MKV under ProgramData (H.264)' {
    $f = WaitFor 180 { $m = Mkv $camNo; if ($m.Count -and $m[-1].Length -gt 0) { $m[-1] } }
    if (-not $f) { throw "no recording in $data\recordings\camera$camNo" }
    "$($f.Name) $($f.Length) bytes (growing segment)"
}
Check 'motion (basic, default ON): the moving box produces motion events' {
    $events = WaitFor 180 { $file = Join-Path $data 'recordings\motion_events.jsonl'; if (Test-Path $file) { @(Get-Content $file | Where-Object { $_ -match "`"camera`"\s*:\s*$camNo\b|camera$camNo" }) } }
    if (-not $events) { throw 'no motion event' }
    "$(@($events).Count) motion event line(s)"
}
Info 'motion: /api/events?camera=<N> with the owner session' { $r = Edge GET "/api/events?camera=$camNo"; "status $($r.status); events $(@($r.body.events).Count)" }
Check 'analytics: premium analytics are OFF by default on the new camera; Windows-unavailable ones say why' {
    $r = Edge GET "/api/customer/analytics-control/cameras/$camId"
    if ($r.status -ne 200) { throw "status $($r.status)" }
    $r.body | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $results 'analytics-control.json')
    $items = @($r.body.analytics)
    $on = @($items | Where-Object { $_.enabled }); if ($on) { throw "enabled: $($on.key -join ',')" }
    $lpr = $items | Where-Object { $_.key -eq 'lpr' }; $ppe = $items | Where-Object { $_.key -eq 'ppe' }; $face = $items | Where-Object { $_.key -eq 'facial_recognition' }
    if ($lpr.runtime_unavailable_reason -notmatch 'not available on Windows') { throw "lpr: $($lpr.runtime_unavailable_reason)" }
    if ($ppe.runtime_unavailable_reason -notmatch 'not available on Windows') { throw "ppe: $($ppe.runtime_unavailable_reason)" }
    if (-not $face.runtime_unavailable_reason) { throw 'facial recognition shows no reason' }
    "$($items.Count) analytics, none enabled; LPR/PPE unavailable on Windows; facial off (administrator)"
}
Check 'analytics: turning LPR on is refused on Windows and LPR stays off' {
    $r = Edge PUT "/api/customer/analytics-control/cameras/$camId/lpr" @{ enabled = $true }
    if ($r.status -lt 400) { throw "accepted ($($r.status))" }
    $after = @((Edge GET "/api/customer/analytics-control/cameras/$camId").body.analytics | Where-Object { $_.key -eq 'lpr' })[0]
    if ($after.enabled) { throw 'LPR shows enabled' }
    "$($r.status) $($r.body.detail); still off"
}

# ---------------------------------------------------------------- mode transitions through the cloud (restart_vms)
function RestartCount { @((Cloud GET '/test/state').commands | Where-Object { $_.command -eq 'restart_vms' -and $_.status -eq 'completed' }).Count }
function SwitchMode([string]$Target, [scriptblock]$Change) {
    $before = RestartCount; $at = Get-Date
    & $Change
    Check "mode: the cloud switches $((CurrentMode)) -> $Target; product_mode.json, restart_vms and the runtime follow" {
        $p = WaitFor 300 { $m = (Get-Content -Raw (Join-Path $data 'config\product_mode.json') -ErrorAction Stop | ConvertFrom-Json).mode; if ($m -eq $Target) { $m } }
        if (-not $p) { throw "product_mode.json did not become $Target" }
        if (-not (WaitFor 300 { (RestartCount) -gt $before })) { throw 'no completed restart_vms command' }
        if (-not (WaitHealthy 180)) { throw 'VMS not healthy after the restart' }
        $m = CurrentMode; if ($m -ne $Target) { throw "runtime $m" }
        $flags = CloudFlags; $wrong = $flags.PSObject.Properties | Where-Object { $_.Value -ne ($Target -eq 'hybrid') }
        if ($wrong) { throw "cloud flags: $(($wrong | ForEach-Object { $_.Name + '=' + $_.Value }) -join ', ')" }
        "persisted $p; restart_vms completed; runtime $m; $($flags.PSObject.Properties.Count) cloud flags $($Target -eq 'hybrid')"
    }
    Check "mode: after the switch to $Target the camera is still licensed and recording resumes" {
        $l = Licensed $camNo; if ($l -ne "[$camNo]") { throw "licensed $l" }
        $f = WaitFor 240 { $m = Mkv $camNo; if ($m.Count -and $m[-1].LastWriteTime -gt $at.AddSeconds(30)) { $m[-1] } }
        if (-not $f) { throw 'no recording written after the switch' }; "licensed; $($f.Name) written after the switch"
    }
}
if ($mode -eq 'local') {
    SwitchMode 'hybrid' { [void](Cloud POST '/test/entitlement' @{ product = 'camera_slots_hybrid'; quantity = 2 } -Owner) }
    SwitchMode 'local' { [void](Cloud POST '/test/entitlement-status' @{ product = 'camera_slots_hybrid'; status = 'cancelled' } -Owner) }
} else {
    [void](Cloud POST '/test/entitlement' @{ product = 'camera_slots_local'; quantity = 2 } -Owner)
    SwitchMode 'local' { [void](Cloud POST '/test/entitlement-status' @{ product = 'camera_slots_hybrid'; status = 'cancelled' } -Owner) }
    SwitchMode 'hybrid' { [void](Cloud POST '/test/entitlement-status' @{ product = 'camera_slots_hybrid'; status = 'active' } -Owner) }
}
Check 'capacity: still 2 after the mode switches' { $c = Capacity; if ($c -ne '2') { throw "capacity $c" }; 'capacity 2' }
# The online sign-in refreshes last_verified_at, and the mode restarts ended the earlier session.
[void](Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true })

# ---------------------------------------------------------------- OFFLINE: the cloud is gone
StopCloud
Check 'offline: the test cloud is stopped (no cloud reachable from this PC)' { if (CloudFails GET '/test/state' $null) { 'cloud unreachable' } else { throw 'cloud still answers' } }
Remove-Item $ownerJar -Force -ErrorAction SilentlyContinue
Check 'offline: the owner signs in with the appliance password (303 + session)' {
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $appliancePassword; customer_only = $true }
    if ($r.status -ne 303) { throw "status $($r.status) $($r.body | ConvertTo-Json -Compress)" }; "303 -> $($r.location)"
}
Check 'offline: a wrong appliance password is refused (503, no session)' {
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = 'Wrong-Appliance-000'; customer_only = $true } -Jar 'C:\Sim\wrong2.cookies'
    if ($r.status -ne 503) { throw "status $($r.status)" }; "503 $($r.body.detail)"
}
Check 'offline: the cloud password does not sign in while offline (503)' {
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true } -Jar 'C:\Sim\wrong3.cookies'
    if ($r.status -ne 503) { throw "status $($r.status)" }; '503'
}
Check 'offline: live view still works without the cloud' {
    $pl = WaitFor 90 { $r = Edge GET "/api/customer/cameras/$camId/live/playlist.m3u8"; if ($r.status -eq 200 -and "$($r.body)" -match '#EXTINF') { $r } }
    if (-not $pl) { throw 'no live playlist offline' }; 'playlist with segments'
}
$offlineAt = Get-Date
Check 'offline: recording continues without the cloud' {
    $f = WaitFor 120 { $m = Mkv $camNo; if ($m.Count -and $m[-1].LastWriteTime -gt $offlineAt) { $m[-1] } }
    if (-not $f) { throw 'recording stopped' }; "$($f.Name) still growing"
}

# ---------------------------------------------------------------- playback (offline): a finished 5-minute segment
Check 'playback (offline): the recording list shows a finished segment of the simulated camera' {
    $clips = WaitFor 720 { $r = Edge GET "/api/customer/recordings/$camId"; if ($r.status -eq 200 -and @($r.body.clips).Count) { @($r.body.clips) } }
    if (-not $clips) { throw 'no clip listed (segments are 5 minutes; listed once older than 90 s)' }
    $script:clip = $clips[0]; "$(@($clips).Count) clip(s)"
}
Check 'playback (offline): the clip plays as MP4 (H.264) from this PC' {
    $file = Join-Path $results 'playback-clip.mp4'
    $r = Edge GET "/api/customer/recordings/$camId/$($script:clip.id)/media" -Out $file
    if ($r.status -eq 302 -and $r.location) { $r = Edge GET $r.location -Out $file }
    if ($r.status -ne 200) { throw "status $($r.status)" }
    $p = Probe $file; $v = $p.streams | Where-Object { $_.codec_type -eq 'video' } | Select-Object -First 1
    if (-not $v) { throw 'no video in the clip' }; "$($r.content_type) $($r.length) bytes; $($v.codec_name) $($v.width)x$($v.height); $([math]::Round([double]$p.format.duration)) s"
}
Check 'offline: after the offline grace window (72 h) the appliance password is refused' {
    $old = (Get-Date).AddHours(-73).ToString('yyyy-MM-ddTHH:mm:ss')
    [void](EdgeSql "UPDATE local_customer_credentials SET last_verified_at = '$old'")
    $r = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $appliancePassword; customer_only = $true } -Jar 'C:\Sim\grace.cookies'
    if ($r.status -ne 503 -or "$($r.body.detail)" -notmatch 'too long') { throw "status $($r.status) $($r.body.detail)" }; "503 $($r.body.detail)"
}

# ---------------------------------------------------------------- back online, then the appliance is revoked
Check 'online again: the cloud returns and the owner signs in online again' {
    if (-not (StartCloud)) { throw 'cloud did not start' }
    $r = WaitFor 120 { $x = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true } -Jar 'C:\Sim\again.cookies'; if ($x.status -eq 303) { $x } }
    if (-not $r) { throw 'online sign-in did not recover' }; '303'
}
[void](Cloud POST '/test/revoke-appliance' -Owner)
Check 'revoked appliance: the cloud refuses it and online customer sign-in on this PC is refused' {
    $r = WaitFor 120 { $x = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true } -Jar 'C:\Sim\revoked.cookies'; if ($x.status -ge 400) { $x } }
    if (-not $r) { throw 'sign-in still accepted after revocation' }; "status $($r.status) $($r.body.detail)"
}
Info 'revoked appliance: what the agent and VMS do (observed, no expectation in the spec)' {
    Start-Sleep -Seconds 90
    $log = Get-ChildItem (Join-Path $data 'logs') -Filter '*agent*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    $tail = if ($log) { (Get-Content $log.FullName -Tail 400 | Where-Object { $_ -match '403|revoked|Invalid appliance credential' } | Select-Object -Last 3) -join ' | ' } else { 'no agent log' }
    "agent service $((Get-Service AnyAiCamAgent).Status); VMS healthy $(WaitHealthy 30); recording files $( (Mkv $camNo).Count ); agent log: $tail"
}

# ---------------------------------------------------------------- collect, secrets, done
StopCloud
foreach ($p in $onvifSim, $camPublisher, $camServer) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
& robocopy.exe (Join-Path $data 'logs') (Join-Path $results 'service-logs') *.log /B /R:0 /W:0 /NP /NJH /NJS | Out-Null
$credential = [string](Get-Content -Raw (Join-Path $agentState 'credential.json') -ErrorAction SilentlyContinue | ConvertFrom-Json).credential
Check 'no link code, credential or synthetic password in any collected log or report' {
    $secrets = @($linkCodes) + @($credential, $cloudPassword, $appliancePassword) | Where-Object { $_ }
    if (@($secrets).Count -lt 4) { throw 'secrets unknown; nothing to search for' }
    $leaks = Get-ChildItem $results -Recurse -File | Where-Object { $_.Extension -in '.log', '.txt', '.json', '.m3u8' } | Where-Object {
        $text = [IO.File]::ReadAllText($_.FullName); @($secrets | Where-Object { $text.Contains($_) }).Count -gt 0 }
    if ($leaks) { throw "found in $($leaks.Name -join ', ')" }; "$(@(Get-ChildItem $results -Recurse -File).Count) files checked"
}
$failed = @($checks | Where-Object { -not $_.ok }).Count
$checks | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $results 'report.json')
Add-Content (Join-Path $results 'report.txt') ("RESULT: {0} checks, {1} failed (SYNTHETIC-KEYSET / SIMULATED CAMERA)" -f $checks.Count, $failed)
Set-Content (Join-Path $results 'DONE') ("{0} failed" -f $failed)
