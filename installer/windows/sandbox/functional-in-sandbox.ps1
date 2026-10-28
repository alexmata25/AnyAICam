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
# 1.3.3 acceptance (2026-10-21): with the accepted 1.3.2 setup in C:\Validation\previous this is the functional UPGRADE
# scenario U132-L / U132-H: 1.3.2 is installed, linked and records the simulated camera first, then the setup under
# test runs as an upgrade over it and the rest of the scenario runs on the new version; U132-L ends with a rollback
# to 1.3.2 (uninstall, then the 1.3.2 setup again).
$previousSetup = Get-ChildItem 'C:\Validation\previous' -Filter 'AnyAiCam-VMS-Setup-*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
$upgrade = [bool]$previousSetup
if ($upgrade) { $run = if ($mode -eq 'local') { 'U132-L' } else { 'U132-H' } }
Set-Content -Path (Join-Path $results 'report.txt') -Value @(
    "AnyAiCam Windows FUNCTIONAL scenario $run " + (Get-Date -Format o) + " on " + [Environment]::OSVersion.VersionString,
    "LABEL: SYNTHETIC-KEYSET / SIMULATED CAMERA -- not production-keyset acceptance, not physical-camera acceptance.")
$setup = Get-ChildItem 'C:\Validation\setup' -Filter 'AnyAiCam-VMS-Setup-*.exe' | Select-Object -First 1
Check 'setup file present (SYNTHETIC-KEYSET build)' { if (-not $setup) { throw 'no setup' }; "$($setup.Name) $($setup.Length) bytes sha256=$((Get-FileHash $setup.FullName -Algorithm SHA256).Hash.ToLower())" }
Check 'setup size is at most 750 MB (750000000 bytes)' {
    if (-not $setup -or $setup.Length -le 0 -or $setup.Length -gt 750000000) { throw 'setup missing, empty or over 750000000 bytes' }
    "$($setup.Length) bytes"
}
Check 'run settings: synthetic-test keyset (functional runs never use the production build)' {
    if ($settings.keyset_kind -ne 'synthetic-test' -or -not $settings.functional) { throw "keyset_kind $($settings.keyset_kind) functional $($settings.functional)" }
    "keyset synthetic-test; mode $mode"
}
if ($upgrade) {
    Check 'previous setup present (the accepted 1.3.2, SYNTHETIC-KEYSET): installed first, then upgraded' {
        "$($previousSetup.Name) $($previousSetup.Length) bytes sha256=$((Get-FileHash $previousSetup.FullName -Algorithm SHA256).Hash.ToLower())"
    }
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
$firstSetup = if ($upgrade) { $previousSetup } else { $setup }
try { $install = Start-Process -FilePath $firstSetup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/PortalUrl=$TestPortal", "/ProductMode=$mode", ('/LOG="' + (Join-Path $results 'setup-install.log') + '"') -Wait -PassThru -ErrorAction Stop }
catch { $install = $null; $installError = $_.Exception.Message }
Check "install$(if ($upgrade) { ' (previous 1.3.2)' }): silent setup exit code 0" { if (-not $install) { throw "setup did not start: $installError" }; if ($install.ExitCode -ne 0) { throw "exit $($install.ExitCode)" }; 'exit 0' }
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
function VmsProcess {
    # The running VMS: <app>\runtime\python\python.exe -m uvicorn main:app (the agent uses the same python, other args).
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like "$app\*" -and $_.CommandLine -match 'uvicorn\s+main:app' })
}
function VmsStartedAfterModeSaved {
    # 1.3.3 D3: product_mode.json alone gave a false PASS -- the VMS restarted (restart_vms) BEFORE the mode was saved
    # and kept the old mode's flags. The running VMS must have started after the mode file was last written.
    $saved = (Get-Item (Join-Path $data 'config\product_mode.json')).LastWriteTime
    $p = @(VmsProcess)
    if ($p.Count -ne 1) { return $null }
    $started = (Get-Process -Id $p[0].ProcessId -ErrorAction SilentlyContinue).StartTime
    if ($started -and $started -gt $saved) { return "VMS pid $($p[0].ProcessId) started $($started.ToString('HH:mm:ss')) after the mode was saved $($saved.ToString('HH:mm:ss'))" }
    return $null
}
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
    # F-H run 1: an ffprobe of a dead RTSP source blocked the whole script. Hard 30 s limit, then killed.
    $transport = if ($File.StartsWith('rtsp://')) { @('-rtsp_transport', 'tcp', '-timeout', '10000000') } else { @() }
    $outFile = Join-Path $env:TEMP ('probe-' + [guid]::NewGuid().ToString('N') + '.json')
    $argList = @('-v', 'error') + $transport + @('-show_entries', 'stream=codec_type,codec_name,width,height', '-show_entries', 'format=format_name,duration', '-of', 'json', ('"' + $File + '"'))
    $p = Start-Process -FilePath $ffprobe -ArgumentList $argList -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError ($outFile + '.err')
    if (-not $p.WaitForExit(30000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 500 }
    try { (Get-Content -Raw $outFile -ErrorAction Stop) | ConvertFrom-Json } catch { $null } finally { Remove-Item $outFile, ($outFile + '.err') -Force -ErrorAction SilentlyContinue }
}
function MediaMtxListeners {
    # Which ports every MediaMTX on this PC listens on (the VMS's own one in Hybrid, and the simulated camera's).
    @(Get-CimInstance Win32_Process -Filter "Name='mediamtx.exe'" | ForEach-Object {
        $id = $_.ProcessId; $who = if ($_.ExecutablePath -like "$app\*") { 'VMS' } else { 'camera-sim' }
        $tcp = @(Get-NetTCPConnection -State Listen -OwningProcess $id -ErrorAction SilentlyContinue | ForEach-Object { "tcp $($_.LocalAddress):$($_.LocalPort)" })
        $udp = @(Get-NetUDPEndpoint -OwningProcess $id -ErrorAction SilentlyContinue | ForEach-Object { "udp $($_.LocalAddress):$($_.LocalPort)" })
        "$who pid $id [" + (($tcp + $udp | Sort-Object -Unique) -join ', ') + ']'
    }) -join ' | '
}
function Mkv([int]$Number) { @(Get-ChildItem (Join-Path $data "recordings\camera$Number") -Filter '*.mkv' -ErrorAction SilentlyContinue | Sort-Object CreationTime) }
# F-L run 1 (2026-10-21 16:28, 4973d5ab): a directory listing's Length/LastWriteTime of a file FFmpeg still has open is
# stale on NTFS, so "no recording"/"recording stopped" were read from stale metadata while recording worked. The true
# size comes from an open handle; "recording" = the newest MKV's true size grows over 15 s.
function TrueLength([string]$File) {
    try { $s = [IO.File]::Open($File, 'Open', 'Read', 'ReadWrite, Delete'); try { $s.Length } finally { $s.Close() } } catch { -1 }
}
function Growing([int]$Number) {
    $m = Mkv $Number; if (-not $m.Count) { return $null }
    $f = $m[-1].FullName; $a = TrueLength $f; Start-Sleep -Seconds 15; $b = TrueLength $f
    $m2 = Mkv $Number
    if ($m2.Count -gt $m.Count) { return [pscustomobject]@{ name = $m2[-1].Name; from = 0; to = (TrueLength $m2[-1].FullName); note = 'new segment' } }
    if ($b -gt $a -and $a -ge 0) { return [pscustomobject]@{ name = $m[-1].Name; from = $a; to = $b; note = 'growing' } }
    return $null
}
function Diag([string]$Label) {
    # A snapshot for diagnosis: FFmpeg processes (live/recording tasks), HLS and recording files (true sizes), events.
    $out = New-Object System.Collections.Generic.List[string]
    $out.Add("== $Label $(Get-Date -Format o)")
    Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" | Where-Object { $_.ExecutablePath -like "$app\*" } |
        ForEach-Object { $out.Add("ffmpeg $($_.ProcessId): $($_.CommandLine)") }
    Get-ChildItem (Join-Path $data 'hls') -ErrorAction SilentlyContinue | Select-Object -Last 8 | ForEach-Object { $out.Add("hls $($_.Name) $(TrueLength $_.FullName)") }
    # F-L run 2: offline the customer playlist came back empty (local_playlist returns empty when os.stat() says the
    # playlist is older than 20 s). Record exactly what the app sees: os.stat age and the playlist's own lines.
    $pl = Join-Path $data "hls\camera$camNo.m3u8"
    $age = & $hp -c "import os, sys, time; p = sys.argv[1]; print(round(time.time() - os.stat(p).st_mtime, 1) if os.path.exists(p) else 'missing')" $pl 2>&1
    $out.Add("playlist camera$camNo.m3u8 os.stat age $age s")
    if (Test-Path $pl) { try { [IO.File]::ReadAllLines($pl) | Select-Object -Last 8 | ForEach-Object { $out.Add("  m3u8: $_") } } catch { $out.Add("  m3u8 unreadable: $($_.Exception.Message)") } }
    Mkv $camNo | ForEach-Object { $out.Add("mkv $($_.Name) created $($_.CreationTime.ToString('s')) true $(TrueLength $_.FullName)") }
    foreach ($f in 'motion_events.jsonl', 'analytics_events.json') { $p = Join-Path $data "recordings\$f"; $out.Add("$f exists $(Test-Path $p) bytes $(if (Test-Path $p) { TrueLength $p } else { 0 })") }
    $s = Edge GET '/api/cameras/status'; $out.Add("GET /api/cameras/status -> $($s.status) $((ConvertTo-Json $s.body -Compress -Depth 6) -replace '(.{1500}).*', '$1...')")
    $out | Set-Content (Join-Path $results "diag-$Label.txt")
}

# ---------------------------------------------------------------- 1.3.3 acceptance checks (2026-10-21)
function PlaylistFresh([string]$Label) {
    # D2: on Windows a reader of the live playlist froze it (os.stat age grew to minutes); fixed builds keep it fresh.
    Check "live playlist fresh ($Label): camera<N>.m3u8 rewritten within 10 s (no freeze)" {
        $pl = Join-Path $data "hls\camera$camNo.m3u8"
        $age = & $hp -c "import os, sys, time; p = sys.argv[1]; print(round(time.time() - os.stat(p).st_mtime, 1) if os.path.exists(p) else -1)" $pl 2>&1
        $a = [double]"$age"; if ($a -lt 0) { throw 'no playlist' }; if ($a -ge 10) { throw "age $a s" }; "age $a s"
    }
}
function MotionReaderRtsp([string]$Label) {
    Check "motion reader ($Label): reads the camera stream over RTSP, never camera<N>.m3u8" {
        $readers = @(Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" | Where-Object { $_.ExecutablePath -like "$app\*" -and $_.CommandLine -match 'scale=160:90' })
        if (-not $readers) { throw 'no motion reader running' }
        $bad = @($readers | Where-Object { $_.CommandLine -match '\.m3u8' -or $_.CommandLine -notmatch '-i\s+rtsp://' })
        if ($bad) { throw "reader not on RTSP: $($bad[0].CommandLine -replace 'rtsp://[^@\s]*@', 'rtsp://***@')" }
        "$($readers.Count) reader(s) on rtsp://"
    }
}
function MoqCheck([string]$Label) {
    # O3: MediaMTX 1.21's default MoQ server (8892 TCP/UDP, 8893 UDP) must not run next to the VMS; WebRTC must.
    Check "MoQ ($Label): the VMS MediaMTX runs, no MoQ listener (8892 TCP/UDP, 8893 UDP), WebRTC 8889 TCP + 8189 UDP present" {
        $vms = WaitFor 90 { @(Get-CimInstance Win32_Process -Filter "Name='mediamtx.exe'" | Where-Object { $_.ExecutablePath -like "$app\*" })[0] }
        if (-not $vms) { throw 'no VMS MediaMTX process' }
        Start-Sleep -Seconds 5
        $tcp = @(Get-NetTCPConnection -State Listen -OwningProcess $vms.ProcessId -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.LocalPort } | Sort-Object -Unique)
        $udp = @(Get-NetUDPEndpoint -OwningProcess $vms.ProcessId -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.LocalPort } | Sort-Object -Unique)
        if ($tcp -contains 8892 -or $udp -contains 8892 -or $udp -contains 8893) { throw "MoQ listening: tcp $($tcp -join ',') udp $($udp -join ',')" }
        if ($tcp -notcontains 8889 -or $udp -notcontains 8189) { throw "WebRTC ports missing: tcp $($tcp -join ',') udp $($udp -join ',')" }
        "tcp $($tcp -join ','); udp $($udp -join ',')"
    }
}
$edgeExe = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
$script:proxy = $null
function StartProxy {
    # sandbox-proxy.py: Edge renders the REAL pages with the owner's session, and plays WHEP without CORS.
    if ($script:proxy -and -not $script:proxy.HasExited) { return }
    $script:proxy = Start-Process -FilePath $hp -ArgumentList @('"C:\Validation\scripts\sandbox-proxy.py"', '--jar', $ownerJar, '--results', $results, '--port', '18080') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $results 'sandbox-proxy.out.log') -RedirectStandardError (Join-Path $results 'sandbox-proxy.err.log')
    [void](WaitFor 20 { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri 'http://127.0.0.1:18080/__whep.html').StatusCode -eq 200 } catch { $false } })
}
function StopEdge {
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -like '*C:\Sim\edge-*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function WebRtcCheck([string]$Label) {
    Check "WebRTC ($Label): headless Edge plays the camera's VMS MediaMTX path over WHEP (local loopback ICE)" {
        if (-not (Test-Path -LiteralPath $edgeExe)) { throw "Edge not found at $edgeExe" }
        StartProxy
        $result = Join-Path $results 'webrtc-result.json'; Remove-Item $result -Force -ErrorAction SilentlyContinue
        # F-H on 126863e3: the first WHEP offer came before the camera's path had its source (404 "no stream is available").
        # The camera path is on demand (ready only once a reader connects), so wait for it to be CONFIGURED in the VMS MediaMTX
        # (8f7a24b0 F-H: 'ready' stayed false although WHEP then played).
        $ready = WaitFor 120 { $pth = Invoke-RestMethod -Uri "http://127.0.0.1:9997/v3/config/paths/get/$camId" -TimeoutSec 5; if ($pth.name -eq $camId) { $pth } }
        $mtx = @(Get-CimInstance Win32_Process -Filter "Name='mediamtx.exe'" | Where-Object { $_.ExecutablePath -like "$app\*" }).Count
        if ($mtx -ne 1) { throw "$mtx VMS MediaMTX processes (expected exactly 1)" }
        $pathNote = if ($ready) { "path configured (source $($ready.source))" } else { 'path not configured after 120 s' }
        # Sandbox limit (test-only shim, like discovery_networks): the Sandbox has no network interface, and MediaMTX never
        # offers a loopback ICE candidate on its own (advertised_hosts() accepts LAN/Tailscale addresses only), so the
        # harness adds 127.0.0.1 through the VMS MediaMTX's loopback-only API for this check and removes it afterwards.
        try {
            Invoke-RestMethod -Method Patch -Uri 'http://127.0.0.1:9997/v3/config/global/patch' -ContentType 'application/json' -TimeoutSec 10 -Body '{"webrtcAdditionalHosts":["127.0.0.1"]}' | Out-Null
            $hosts = (Invoke-RestMethod -Uri 'http://127.0.0.1:9997/v3/config/global/get' -TimeoutSec 10).webrtcAdditionalHosts
            $shim = "loopback ICE host added by the harness (MediaMTX webrtcAdditionalHosts: $($hosts -join ','))"
        } catch { $shim = "loopback candidate not added: $($_.Exception.Message)" }
        [void](Start-Process -FilePath $edgeExe -PassThru -ArgumentList @('--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
            '--user-data-dir=C:\Sim\edge-webrtc', '--allow-loopback-in-peer-connection', '--autoplay-policy=no-user-gesture-required',
            '--disable-features=WebRtcHideLocalIpsWithMdns',
            "http://127.0.0.1:18080/__whep.html?path=$camId"))
        $got = WaitFor 90 { if (Test-Path $result) { Get-Content -Raw $result | ConvertFrom-Json } }
        StopEdge
        try { Invoke-RestMethod -Method Patch -Uri 'http://127.0.0.1:9997/v3/config/global/patch' -ContentType 'application/json' -TimeoutSec 10 -Body '{"webrtcAdditionalHosts":[]}' | Out-Null } catch { }
        if (-not $got) { throw 'the WHEP page reported nothing within 90 s' }
        Copy-Item $result (Join-Path $results "webrtc-result-$Label.json") -Force
        $frame = Join-Path $results 'webrtc-frame.png'
        if (Test-Path $frame) { Move-Item $frame (Join-Path $results "webrtc-frame-$Label.png") -Force }
        if (-not $got.ok) { throw "not playing ($pathNote; $shim): $($got | ConvertTo-Json -Compress)" }
        "playing $($got.width)x$($got.height), t=$([math]::Round([double]$got.currentTime, 1)) s, ICE $($got.ice); $pathNote; 1 VMS MediaMTX; $shim; frame webrtc-frame-$Label.png"
    }
}
function SubscriptionCheck([string]$Label) {
    # Codex round 5 APPLIANCE-PRICING-DRIFT: on an appliance every add-on is "Coming soon" with no amount.
    Check "My subscription ($Label): every add-on row 'Coming soon' with no amount; no unapproved price on the page" {
        $file = Join-Path $results "subscription-portal-$Label.html"
        $r = Edge GET '/subscription-portal' -Out $file
        if ($r.status -ne 200) { throw "status $($r.status)" }
        $html = [IO.File]::ReadAllText($file)
        $start = $html.IndexOf('Optional analytics</h3>'); $end = $html.IndexOf('Advanced Line Crossing is not sold separately')
        if ($start -lt 0 -or $end -le $start) { throw 'no add-on section' }
        $section = $html.Substring($start, $end - $start)
        $rows = @([regex]::Matches($section, '<div class="health-row">(.*?)</div>', 'Singleline') | ForEach-Object { $_.Groups[1].Value })
        if (-not $rows) { throw 'no add-on rows' }
        $priced = @($rows | Where-Object { $_.Contains('$') }); if ($priced) { throw "add-on row with an amount: $($priced[0])" }
        if ($section -notmatch 'Coming soon') { throw 'no Coming soon' }
        if ($section -match 'addon-buy-button') { throw 'a buy button is shown' }
        foreach ($a in '$7.99', '$4.99', '$39.99', '$49.99', '$69.99') { if ($html.Contains($a)) { throw "$a on the page" } }
        StartProxy
        $png = Join-Path $results "subscription-portal-$Label.png"
        $shot = Start-Process -FilePath $edgeExe -PassThru -ArgumentList @('--headless=new', '--disable-gpu', '--no-first-run', '--user-data-dir=C:\Sim\edge-shot',
            '--window-size=1280,2400', "--screenshot=$png", 'http://127.0.0.1:18080/subscription-portal')
        if (-not $shot.WaitForExit(60000)) { Stop-Process -Id $shot.Id -Force -ErrorAction SilentlyContinue }
        StopEdge
        "$($rows.Count) add-on rows, all Coming soon without an amount; screenshot $(if (Test-Path $png) { 'saved' } else { 'NOT saved' })"
    }
}
function UpgradeState {
    [ordered]@{
        appliance_id = [string](Get-Content -Raw (Join-Path $data 'config\appliance_identity.json') | ConvertFrom-Json).appliance_id
        credential   = (Get-FileHash (Join-Path $agentState 'credential.json')).Hash
        secrets      = [string](Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_.StartsWith('ANYAICAM_APP_SECRETS=') })
        mode         = [string](Get-Content -Raw (Join-Path $data 'config\product_mode.json') | ConvertFrom-Json).mode
        capacity     = [string](Capacity)
        camera       = [string](@(EdgeSql "SELECT id, camera_number, onvif_endpoint FROM cameras WHERE id='$camId'")[0] | ConvertTo-Json -Compress)
        enrolled     = [string]@(EdgeSql "SELECT user_id FROM local_customer_credentials WHERE revoked_at IS NULL").Count
    }
}
function ClipIds {
    [void](Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true })
    $r = WaitFor 120 { $x = Edge GET "/api/customer/recordings/$camId"; if ($x.status -eq 200) { $x } }
    @($r.body.clips | ForEach-Object { [string]$_.id })
}
$uninstallKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}_is1'
function SetupVersionOf($file) { if ($file.Name -match 'Setup-(\d+\.\d+\.\d+)-') { $Matches[1] } else { '?' } }

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
if (-not $upgrade) { SubscriptionCheck $mode }

# ---------------------------------------------------------------- simulated camera on 127.0.0.5 (loopback only)
$camHost = '127.0.0.5'
$rtsp = "rtsp://${camHost}:8554/cam1"
# F-H run 1 (6ba2265a): MediaMTX 1.21 also starts a MoQ server on :8892/:8893 by default; in Hybrid the VMS's own
# MediaMTX already held :8892, so this camera server exited at start. Every listener but RTSP is off here.
@('logLevel: info', 'api: no', 'metrics: no', 'pprof: no', 'playback: no', 'rtsp: yes', "rtspAddress: ${camHost}:8554", 'rtspTransports: [tcp]',
  'rtmp: no', 'hls: no', 'webrtc: no', 'srt: no', 'moq: no', 'paths:', '  cam1:') |
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
Info "MediaMTX listeners ($mode)" { MediaMtxListeners }

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
    $p = Cloud POST '/api/customer/cameras/provision' @{ appliance_id = $cloudAppliance.id; device_key = $script:deviceKey; name = 'Simulated camera'; recording_mode = 'event' } -Owner
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
Check 'event-only: edge configuration is event mode and the segment recorder uses the private buffer' {
    $row = @(EdgeSql "SELECT local_recording_mode FROM cameras WHERE camera_number=$camNo")[0]
    if ($row.local_recording_mode -ne 'event') { throw "stored mode $($row.local_recording_mode)" }
    $writers = WaitFor 120 {
        $all = @(Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" | Where-Object {
            $_.CommandLine -match "camera$camNo[\\/]" -and $_.CommandLine -match '-f\s+segment'
        })
        if ($all.Count) { $all }
    }
    if (-not $writers) { throw 'no camera segment writer found' }
    foreach ($writer in $writers) {
        if ($writer.CommandLine -notmatch "camera$camNo[\\/]_event_buffer[\\/]") { throw 'continuous customer-facing segment writer detected' }
    }
    'stored event mode; segment writers use _event_buffer; idle retention/event playback still require their separate checks'
}

# ---------------------------------------------------------------- UPGRADE 1.3.2 -> setup under test (U132-L / U132-H)
if ($upgrade) {
    # A finished, listed recording made by 1.3.2: a VMS restart ends the open segment; listed once older than 90 s.
    $preGrowing = WaitFor 300 { Growing $camNo }
    Restart-Service AnyAiCamVMS -Force; [void](WaitHealthy 180)
    $script:preClips = @()
    Check 'upgrade (before): 1.3.2 recorded the simulated camera and lists a finished clip' {
        $clips = WaitFor 420 { $r = Edge GET "/api/customer/recordings/$camId"; if ($r.status -eq 200 -and @($r.body.clips).Count) { @($r.body.clips) } }
        if (-not $clips) { throw "no clip listed under 1.3.2 (recording was growing: $([bool]$preGrowing))" }
        $script:preClips = @($clips | ForEach-Object { [string]$_.id }); "$($script:preClips.Count) clip(s) before the upgrade"
    }
    $beforeState = UpgradeState
    $beforeBuild = ((Http '/version').Content | ConvertFrom-Json).build_id
    $beforeState | ConvertTo-Json | Set-Content (Join-Path $results 'upgrade-before.json')
    $up = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-upgrade.log') + '"') -Wait -PassThru
    Check 'upgrade: the setup under test runs over 1.3.2 (exit 0) and the VMS is healthy' {
        if ($up.ExitCode -ne 0) { throw "exit $($up.ExitCode)" }; if (-not (WaitHealthy 240)) { throw 'not healthy' }; 'exit 0; healthy'
    }
    Check 'upgrade: the uninstall entry and /version report the new setup' {
        $u = Get-ItemProperty $uninstallKey; $v = (Http '/version').Content | ConvertFrom-Json
        if ($u.DisplayVersion -ne (SetupVersionOf $setup)) { throw "DisplayVersion $($u.DisplayVersion), expected $(SetupVersionOf $setup)" }
        if ($v.build_id -eq $beforeBuild) { throw "build still $($v.build_id)" }
        "DisplayVersion $($u.DisplayVersion); build $($v.build_id) (was $beforeBuild)"
    }
    Check 'upgrade: identity, link credential, secrets, mode, capacity, camera and appliance password are kept' {
        $after = WaitFor 120 { $s = UpgradeState; if ($s.capacity -eq $beforeState.capacity) { $s } }
        if (-not $after) { $after = UpgradeState }
        $after | ConvertTo-Json | Set-Content (Join-Path $results 'upgrade-after.json')
        $changed = @(foreach ($k in $beforeState.Keys) { if ("$($after[$k])" -ne "$($beforeState[$k])") { $k } })
        if ($changed) { throw "changed: $($changed -join ', ') (see upgrade-before.json / upgrade-after.json)" }; "all $($beforeState.Keys.Count) kept"
    }
    Check 'upgrade: the clips recorded by 1.3.2 are still listed and play (MP4, H.264) on the new version' {
        $ids = ClipIds
        $missing = @($script:preClips | Where-Object { $ids -notcontains $_ }); if ($missing) { throw "$($missing.Count) clip(s) missing after the upgrade" }
        $file = Join-Path $results 'upgrade-old-clip.mp4'
        $m = Edge GET "/api/customer/recordings/$camId/$($script:preClips[0])/media" -Out $file
        if ($m.status -eq 302 -and $m.location) { $m = Edge GET $m.location -Out $file }
        if ($m.status -ne 200) { throw "media $($m.status)" }
        $p = Probe $file; if (-not ($p.streams | Where-Object { $_.codec_type -eq 'video' })) { throw 'no video in the old clip' }
        "$($script:preClips.Count) clip(s) kept; the oldest plays"
    }
    Check 'upgrade: running the 1.3.2 setup over the new version is refused (downgrade) and changes nothing' {
        $envBefore = (Get-FileHash (Join-Path $data 'config\vms.env')).Hash
        $d = Start-Process -FilePath $previousSetup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-downgrade-133-refused.log') + '"') -Wait -PassThru
        if ($d.ExitCode -eq 0) { throw '1.3.2 setup ran over the newer version' }
        if ((Get-FileHash (Join-Path $data 'config\vms.env')).Hash -ne $envBefore) { throw 'vms.env changed' }
        if ((Get-ItemProperty $uninstallKey).DisplayVersion -ne (SetupVersionOf $setup)) { throw 'version changed' }
        if (-not (WaitHealthy 120)) { throw 'not healthy after the refused setup' }
        "exit $($d.ExitCode); vms.env and version unchanged; healthy"
    }
    SubscriptionCheck "$mode-after-upgrade"
}

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
if ($mode -eq 'hybrid') { MoqCheck $mode; WebRtcCheck $mode }
else { Info 'live view: WebRTC' { 'Local mode runs no WebRTC publisher (P2P is a Hybrid feature): checked in the Hybrid runs and after each switch to Hybrid' } }
Info 'live view: WebRTC through the cloud' { 'not tested: cloud signalling / P2P / TURN run through the cloud (live_view_p2p), which the test cloud does not serve' }

# ---------------------------------------------------------------- recording, motion, analytics
Check 'recording: the VMS records the simulated camera to MKV under ProgramData (true size growing)' {
    $g = WaitFor 300 { Growing $camNo }
    Diag 'recording'
    if (-not $g) { throw "no growing MKV in $data\recordings\camera$camNo ($((Mkv $camNo).Count) file(s)); see diag-recording.txt" }
    "$($g.name) $($g.from) -> $($g.to) bytes ($($g.note))"
}
PlaylistFresh 'recording'
Check 'motion (basic, default ON): the moving box produces motion events' {
    $file = Join-Path $data 'recordings\motion_events.jsonl'
    $events = WaitFor 300 { if (Test-Path $file) { @([IO.File]::ReadAllLines($file) | Where-Object { $_ -match "`"camera`"\s*:\s*$camNo\b" }) } }
    Diag 'motion'
    if (-not $events) { throw "no motion event for camera $camNo (motion_events.jsonl exists: $(Test-Path $file)); see diag-motion.txt" }
    "$(@($events).Count) motion event line(s)"
}
PlaylistFresh 'motion'
MotionReaderRtsp 'motion'
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
        $proc = WaitFor 180 { VmsStartedAfterModeSaved }
        if (-not $proc) { throw "the running VMS started before the new mode was saved (it still runs with the old mode's flags); VMS processes: $(@(VmsProcess).Count)" }
        if (-not (WaitHealthy 180)) { throw 'VMS not healthy after the mode restart' }
        $flags = CloudFlags; $wrong = $flags.PSObject.Properties | Where-Object { $_.Value -ne ($Target -eq 'hybrid') }
        if ($wrong) { throw "cloud flags: $(($wrong | ForEach-Object { $_.Name + '=' + $_.Value }) -join ', ')" }
        "persisted $p; restart_vms completed; runtime $m; $proc; $($flags.PSObject.Properties.Count) cloud flags $($Target -eq 'hybrid')"
    }
    Info "MediaMTX listeners after the switch to $Target" { Start-Sleep -Seconds 20; MediaMtxListeners }
    if ($Target -eq 'hybrid') { MoqCheck "after the switch to hybrid"; WebRtcCheck "after-switch-hybrid" }
    Check "mode: after the switch to $Target the camera is still licensed and recording resumes" {
        $l = Licensed $camNo; if ($l -ne "[$camNo]") { throw "licensed $l" }
        $g = WaitFor 300 { Growing $camNo }
        Diag "after-switch-$Target"
        if (-not $g) { throw "no growing recording after the switch; see diag-after-switch-$Target.txt" }; "licensed; $($g.name) $($g.note) ($($g.from) -> $($g.to) bytes)"
    }
    PlaylistFresh "after-switch-$Target"
    MotionReaderRtsp "after-switch-$Target"
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
    $script:lastLive = $null
    $pl = WaitFor 120 { $r = Edge GET "/api/customer/cameras/$camId/live/playlist.m3u8"; $script:lastLive = $r; if ($r.status -eq 200 -and "$($r.body)" -match '#EXTINF') { $r } }
    Diag 'offline-live'
    if (-not $pl) { throw "no live playlist offline: last answer $($script:lastLive.status) $((ConvertTo-Json $script:lastLive.body -Compress) -replace '(.{300}).*', '$1...')" }
    'playlist with segments'
}
PlaylistFresh 'offline'
Check 'offline: recording continues without the cloud' {
    # 906f7819 U132-L/U132-H: the simulated camera's ~1 KB/s stream is written in 256 KiB MKV steps, so within 180 s a
    # 15 s sample often saw neither growth nor a rollover although new segments kept appearing offline. Compare against
    # the newest segment when the check starts and allow longer than one 300 s segment: a stopped recorder still fails.
    $base = @(Mkv $camNo)
    $baseName = if ($base.Count) { $base[-1].Name } else { '' }
    $baseLen = if ($base.Count) { TrueLength $base[-1].FullName } else { -1 }
    $g = WaitFor 330 {
        $now = @(Mkv $camNo)
        if ($now.Count -and $now[-1].Name -ne $baseName) { [pscustomobject]@{ name = $now[-1].Name; from = 0; to = (TrueLength $now[-1].FullName); note = "new segment since the check began (after $baseName)" } }
        elseif ($now.Count -and (TrueLength $now[-1].FullName) -gt $baseLen) { [pscustomobject]@{ name = $now[-1].Name; from = $baseLen; to = (TrueLength $now[-1].FullName); note = 'growing' } }
    }
    Diag 'offline-recording'
    if (-not $g) { throw 'no growing recording offline; see diag-offline-recording.txt' }; "$($g.name) $($g.note) ($($g.from) -> $($g.to) bytes)"
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
# ---------------------------------------------------------------- FIX-FORWARD (U132-L): uninstall, 1.3.2 refused, new setup again
# Owner policy 2026-10-22: fix-forward is the default; version metadata is never reset or downgraded. 1.3.3 on 126863e3
# showed the 1.3.2 setup refuses over the newer data folder (install-version-guard exit 10). Checked here: that refusal
# changes nothing, and the setup under test installs again over the kept data with identity, link and recordings.
if ($upgrade -and $mode -eq 'local') {
    $rbState = UpgradeState; $rbMkv = (Mkv $camNo).Count; $rbClips = ClipIds
    $unins = Get-ChildItem $app -Filter 'unins*.exe' | Select-Object -First 1
    $rbU = Start-Process -FilePath $unins.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-rollback-uninstall.log') + '"') -Wait -PassThru
    Check 'rollback: uninstalling the new version (exit 0) keeps data, link credential and recordings' {
        if ($rbU.ExitCode -ne 0) { throw "exit $($rbU.ExitCode)" }
        if (-not (Test-Path (Join-Path $agentState 'credential.json'))) { throw 'credential gone' }
        if ((Mkv $camNo).Count -lt $rbMkv) { throw 'recordings gone' }
        "exit 0; credential and $rbMkv recording file(s) kept"
    }
    $rbI = Start-Process -FilePath $previousSetup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/PortalUrl=$TestPortal", "/ProductMode=$mode",
        ('/LOG="' + (Join-Path $results 'setup-rollback-install.log') + '"') -Wait -PassThru
    Check 'fix-forward: the 1.3.2 setup is refused over the newer data folder and changes nothing' {
        if ($rbI.ExitCode -eq 0) { throw '1.3.2 installed over the newer data folder' }
        $text = [IO.File]::ReadAllText((Join-Path $results 'setup-rollback-install.log'))
        if ($text -notmatch 'Downgrade refused') { throw "exit $($rbI.ExitCode) without the downgrade refusal" }
        if (Test-Path $uninstallKey) { throw 'an uninstall entry appeared' }
        if (-not (Test-Path (Join-Path $agentState 'credential.json'))) { throw 'credential gone' }
        if ((Mkv $camNo).Count -lt $rbMkv) { throw 'recordings gone' }
        "exit $($rbI.ExitCode) (downgrade refused); nothing installed; credential and $rbMkv recording file(s) kept"
    }
    $ffI = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/PortalUrl=$TestPortal", "/ProductMode=$mode",
        ('/LOG="' + (Join-Path $results 'setup-fixforward-install.log') + '"') -Wait -PassThru
    Check 'fix-forward: the setup under test installs again (exit 0), healthy; identity, link, secrets kept; recordings listed' {
        if ($ffI.ExitCode -ne 0) { throw "exit $($ffI.ExitCode)" }
        if (-not (WaitHealthy 240)) { throw 'not healthy' }
        if ((Get-ItemProperty $uninstallKey).DisplayVersion -ne (SetupVersionOf $setup)) { throw "DisplayVersion $((Get-ItemProperty $uninstallKey).DisplayVersion)" }
        $now = UpgradeState
        foreach ($k in 'appliance_id', 'credential', 'secrets') { if ("$($now[$k])" -ne "$($rbState[$k])") { throw "$k changed" } }
        $ids = ClipIds; $missing = @($rbClips | Where-Object { $ids -notcontains $_ }); if ($missing) { throw "$($missing.Count) clip(s) missing" }
        "$(SetupVersionOf $setup) again; identity, link, secrets kept; $($ids.Count) clip(s) listed"
    }
}
[void](Cloud POST '/test/revoke-appliance' -Owner)
Check 'revoked appliance: the cloud refuses it and online customer sign-in on this PC is refused' {
    # U132-L on 126863e3: the VMS was not running (failed rollback), Edge returned status 0, reported as 'accepted'.
    if (-not (WaitHealthy 180)) { throw 'inconclusive: the VMS is not healthy, so sign-in could not be tried' }
    $script:lastLogin = $null
    $r = WaitFor 120 { $x = Edge POST '/api/partner-login' @{ email = $ownerEmail; password = $cloudPassword; customer_only = $true } -Jar 'C:\Sim\revoked.cookies'; $script:lastLogin = $x; if ($x.status -ge 400) { $x } }
    if (-not $r) {
        if (-not $script:lastLogin -or [int]$script:lastLogin.status -eq 0) { throw 'inconclusive: no answer from the VMS (status 0)' }
        throw "sign-in still accepted after revocation (status $($script:lastLogin.status))"
    }
    "status $($r.status) $($r.body.detail)"
}
Info 'revoked appliance: what the agent and VMS do (observed, no expectation in the spec)' {
    Start-Sleep -Seconds 90
    $log = Get-ChildItem (Join-Path $data 'logs') -Filter '*agent*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    $tail = if ($log) { (Get-Content $log.FullName -Tail 400 | Where-Object { $_ -match '403|revoked|Invalid appliance credential' } | Select-Object -Last 3) -join ' | ' } else { 'no agent log' }
    "agent service $((Get-Service AnyAiCamAgent).Status); VMS healthy $(WaitHealthy 30); recording files $( (Mkv $camNo).Count ); agent log: $tail"
}

# ---------------------------------------------------------------- collect, secrets, done
StopCloud
# 1.3.3 F-L run 1: the still-running evidence proxy held sandbox-proxy.err.log open, so the secrets scan threw.
if ($script:proxy -and -not $script:proxy.HasExited) { Stop-Process -Id $script:proxy.Id -Force -ErrorAction SilentlyContinue }
StopEdge
Start-Sleep -Seconds 2
foreach ($p in $onvifSim, $camPublisher, $camServer) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
Diag 'end'
# F-L run 1: the app's own WinSW out/err logs were still open, so a copy while the services ran skipped them.
# The Sandbox is disposable: stop both services first, then collect every log and the small event/JSON stores.
Stop-Service AnyAiCamVMS, AnyAiCamAgent -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 5
& robocopy.exe (Join-Path $data 'logs') (Join-Path $results 'service-logs') *.log /B /R:1 /W:1 /NP /NJH /NJS | Out-Null
& robocopy.exe (Join-Path $data 'agent') (Join-Path $results 'agent-logs') *.log /B /R:1 /W:1 /NP /NJH /NJS | Out-Null
& robocopy.exe (Join-Path $data 'recordings') (Join-Path $results 'recordings-stores') *.jsonl analytics_events.json /B /R:1 /W:1 /NP /NJH /NJS | Out-Null
Info 'collected logs' { (@(Get-ChildItem $results -Recurse -File -Include *.log | ForEach-Object { "$($_.Directory.Name)\$($_.Name)" }) -join ', ') }
$credential = [string](Get-Content -Raw (Join-Path $agentState 'credential.json') -ErrorAction SilentlyContinue | ConvertFrom-Json).credential
Check 'no link code, credential or synthetic password in any collected log or report' {
    $secrets = @($linkCodes) + @($credential, $cloudPassword, $appliancePassword) | Where-Object { $_ }
    if (@($secrets).Count -lt 4) { throw 'secrets unknown; nothing to search for' }
    $leaks = Get-ChildItem $results -Recurse -File | Where-Object { $_.Extension -in '.log', '.txt', '.json', '.m3u8', '.html' } | Where-Object {
        $stream = [IO.File]::Open($_.FullName, 'Open', 'Read', 'ReadWrite, Delete')
        try { $text = (New-Object IO.StreamReader($stream)).ReadToEnd() } finally { $stream.Close() }
        @($secrets | Where-Object { $text.Contains($_) }).Count -gt 0 }
    if ($leaks) { throw "found in $($leaks.Name -join ', ')" }; "$(@(Get-ChildItem $results -Recurse -File).Count) files checked"
}
$failed = @($checks | Where-Object { -not $_.ok }).Count
$checks | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $results 'report.json')
Add-Content (Join-Path $results 'report.txt') ("RESULT: {0} checks, {1} failed (SYNTHETIC-KEYSET / SIMULATED CAMERA)" -f $checks.Count, $failed)
Set-Content (Join-Path $results 'DONE') ("{0} failed" -f $failed)
