# Clean-machine validation of the AnyAiCam VMS Windows setup (2026-10-07).
#
# Runs inside Windows Sandbox (a disposable VM: nothing here touches the host).
# Started by AnyAiCam-Validation.wsb's LogonCommand with:
#   C:\Validation\setup   the setup .exe (read-only from the host)
#   C:\Validation\results report.json, report.txt, setup logs (writable)
# Every check records pass/fail; the script never stops at the first failure.
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
function Http([string]$Path) {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 -Uri ("http://127.0.0.1:8000" + $Path)
    return $response
}
function WaitHealthy([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try { if ((Http '/health').StatusCode -eq 200) { return $true } } catch { }
        Start-Sleep -Seconds 5
    }
    return $false
}

Set-Content -Path (Join-Path $results 'report.txt') -Value ("AnyAiCam Windows validation " + (Get-Date -Format o) + " on " + [Environment]::OSVersion.VersionString)
$setup = Get-ChildItem 'C:\Validation\setup' -Filter 'AnyAiCam-VMS-Setup-*.exe' | Select-Object -First 1
Check 'setup file present' { if (-not $setup) { throw 'no AnyAiCam-VMS-Setup-*.exe' }; "$($setup.Name) $($setup.Length) bytes sha256=$((Get-FileHash $setup.FullName -Algorithm SHA256).Hash.ToLower())" }
$expectedVersion = if ($setup -and $setup.Name -match 'Setup-(\d+\.\d+\.\d+)-') { $Matches[1] } else { '' }

# ---------------------------------------------------------------- test cloud (2026-10-07)
# Cloud linking is validated against a disposable cloud INSIDE this Sandbox
# (C:\Validation\scripts\test-cloud.py: the installed app's real claim and
# appliance routes on a fresh database, HTTPS with a throwaway CA). The
# production cloud is never contacted: app.anyaicam.com is pointed at this VM
# too, so a setup that fell back to the default portal could not reach it.
$TestCloudHost = 'cloud.anyaicam.test'
$TestPortal = "https://${TestCloudHost}:8443"
Add-Content -Path "$env:SystemRoot\System32\drivers\etc\hosts" -Value "`r`n127.0.0.1 $TestCloudHost`r`n127.0.0.1 app.anyaicam.com"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
function Cloud([string]$Method, [string]$Path, $Body = $null, [switch]$Owner) {
    $arguments = @{ Method = $Method; Uri = "$TestPortal$Path"; TimeoutSec = 20; UseBasicParsing = $true; ContentType = 'application/json' }
    if ($Owner) { $arguments.Headers = @{ 'X-Test-Owner' = 'yes' } }
    if ($null -ne $Body) { $arguments.Body = ($Body | ConvertTo-Json -Compress) }
    Invoke-RestMethod @arguments
}
function WaitFor([int]$Seconds, [scriptblock]$Condition) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try { $value = & $Condition; if ($value) { return $value } } catch { }
        Start-Sleep -Seconds 3
    }
    return $null
}
function BundledPython([string]$Pattern) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like "$env:ProgramFiles\AnyAiCam\*" -and $_.CommandLine -match $Pattern } | Select-Object -First 1
}

# ---------------------------------------------------------------- install
$install = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/PortalUrl=$TestPortal", ('/LOG="' + (Join-Path $results 'setup-install.log') + '"') -Wait -PassThru
Check 'silent install exit code 0' { if ($install.ExitCode -ne 0) { throw "exit $($install.ExitCode)" }; 'exit 0' }
# 2026-10-07: the first Sandbox run found torch's DLLs need the Visual C++
# runtime, which a clean Windows lacks; setup now installs it and preflights.
$installLog = Get-Content (Join-Path $results 'setup-install.log') -ErrorAction SilentlyContinue
Check 'Visual C++ runtime installer succeeded (setup log)' { $line = $installLog | Where-Object { $_ -match 'Visual C\+\+ runtime installer exit code: (\d+)' } | Select-Object -Last 1; if (-not $line) { throw 'no exit code in setup log' }; $code = [int]([regex]::Match($line, 'exit code: (\d+)').Groups[1].Value); if ($code -notin 0, 1638, 3010) { throw "exit $code" }; "exit $code" }
Check 'Visual C++ x64 runtime registered' { $r = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction Stop; if ($r.Installed -ne 1) { throw 'Installed is not 1' }; "version $($r.Version)" }
Check 'setup ran the runtime preflight and it passed (setup log)' { $line = $installLog | Where-Object { $_ -match 'AnyAiCam runtime preflight passed' } | Select-Object -First 1; if (-not $line) { throw 'no preflight pass line in setup log' }; $line.Substring($line.IndexOf('AnyAiCam runtime')) }
Check 'bundled Python loads torch, cv2, ultralytics (preflight)' {
    $python = Join-Path $env:ProgramFiles 'AnyAiCam\runtime\python\python.exe'
    $out = & $python (Join-Path $env:ProgramFiles 'AnyAiCam\installer\runtime-preflight.py') 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "exit $LASTEXITCODE`: $out" }
    $out.Trim()
}
$service = Get-Service -Name AnyAiCamVMS -ErrorAction SilentlyContinue
Check 'service installed, automatic start' { if (-not $service) { throw 'no AnyAiCamVMS service' }; if ($service.StartType -ne 'Automatic') { throw "StartType $($service.StartType)" }; "StartType $($service.StartType)" }
Check 'service running' { (Get-Service AnyAiCamVMS).Status -eq 'Running' }
Check 'VMS answers /health within 3 minutes' { WaitHealthy 180 }
Check '/version reports the setup version' { $v = (Http '/version').Content | ConvertFrom-Json; if ($expectedVersion -and $v.version -ne $expectedVersion) { throw "version $($v.version), expected $expectedVersion" }; "version $($v.version) build $($v.build_id)" }
Check '/login page served' { (Http '/login').StatusCode -eq 200 }
Check 'static assets served' { (Http '/static/brand-icon.png').StatusCode -eq 200 }
Check 'listening on the local network (0.0.0.0:8000)' { $l = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop; if (-not ($l.LocalAddress -contains '0.0.0.0')) { throw "listening on $($l.LocalAddress -join ',')" }; '0.0.0.0:8000' }

# ---------------------------------------------------------------- files, data, permissions
$app = Join-Path $env:ProgramFiles 'AnyAiCam'
$data = Join-Path $env:ProgramData 'AnyAiCam'
Check 'MediaMTX installed' { Test-Path (Join-Path $app 'runtime\tools\mediamtx\mediamtx.exe') }
Check 'FFmpeg installed' { Test-Path (Join-Path $app 'runtime\tools\ffmpeg\ffmpeg.exe') }
Check 'data written under ProgramData, not C:\app' { if (Test-Path 'C:\app') { throw 'C:\app exists' }; if (-not (Test-Path (Join-Path $data 'database\partner_portal.db'))) { throw 'no database under ProgramData' }; 'database under ProgramData' }
Check 'vms.env has secrets and version' { $e = Get-Content (Join-Path $data 'config\vms.env'); foreach ($k in 'ANYAICAM_APP_SECRETS=', 'ANYAICAM_CAMERA_CREDENTIAL_KEY=', "ANYAICAM_VERSION=$expectedVersion") { if (-not ($e | Where-Object { $_.StartsWith($k) })) { throw "missing $k" } }; 'ok' }
Check 'data folder: SYSTEM and Administrators only' {
    $acl = Get-Acl $data
    if (-not $acl.AreAccessRulesProtected) { throw 'inheritance not removed' }
    $who = $acl.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique
    $extra = $who | Where-Object { $_ -notin 'S-1-5-18', 'S-1-5-32-544' }
    if ($extra) { throw "extra principals: $($extra -join ',')" }
    $who -join ','
}

# ---------------------------------------------------------------- firewall
$rules = Get-NetFirewallRule -Group 'AnyAiCam VMS' -ErrorAction SilentlyContinue
Check 'two firewall rules' { if (@($rules).Count -ne 2) { throw "found $(@($rules).Count)" }; ($rules.Name -join ',') }
foreach ($rule in $rules) {
    Check "rule $($rule.Name): private/domain only, local subnet + Tailscale, program-scoped" {
        $profile = "$($rule.Profile)"; if ($profile -match 'Public' -or $profile -eq 'Any') { throw "profile $profile" }
        $address = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
        $remote = (($address | Sort-Object) -join ',')
        # Windows may report the Tailscale range as a mask instead of a prefix.
        if ($remote -notin @('100.64.0.0/10,LocalSubnet', '100.64.0.0/255.192.0.0,LocalSubnet')) { throw "remote $remote" }
        $program = ($rule | Get-NetFirewallApplicationFilter).Program
        if (-not $program.StartsWith($app)) { throw "program $program" }
        "$profile; $($address -join ','); $program"
    }
}

# ---------------------------------------------------------------- cloud linking (2026-10-07)
$agentState = Join-Path $data 'agent'
$labelFile = Join-Path $data 'label\claim-label.txt'
$claimCode = ''
$applianceId = ''
Check 'cloud agent service installed, automatic, running' {
    $agent = Get-Service AnyAiCamAgent -ErrorAction Stop
    if ($agent.StartType -ne 'Automatic') { throw "StartType $($agent.StartType)" }
    if (-not (WaitFor 60 { (Get-Service AnyAiCamAgent).Status -eq 'Running' })) { throw "status $((Get-Service AnyAiCamAgent).Status)" }
    'Automatic, Running'
}
Check 'setup provisioned identity, claim code, verifier, portal and release' {
    $script:applianceId = [string](Get-Content -Raw (Join-Path $data 'config\appliance_identity.json') | ConvertFrom-Json).appliance_id
    if ($script:applianceId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') { throw 'no UUIDv4 appliance_id' }
    $line = Get-Content $labelFile | Where-Object { $_ -like 'Claim code:*' } | Select-Object -First 1
    $script:claimCode = ($line -replace '^Claim code:\s*', '').Trim()
    if ($script:claimCode -notmatch '^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$') { throw 'claim code has the wrong format' }
    $sha = [Security.Cryptography.SHA256]::Create()
    $expected = -join ($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes('anyaicam-label-claim-v1:' + ($script:claimCode -replace '-', ''))) | ForEach-Object { $_.ToString('x2') })
    if ((Get-Content -Raw (Join-Path $data 'config\label_claim.json') | ConvertFrom-Json).verifier -ne $expected) { throw 'label_claim.json verifier does not match the claim code' }
    if (-not (Get-Content (Join-Path $data 'config\agent.env') | Where-Object { $_ -eq "ANYAICAM_PORTAL_URL=$TestPortal" })) { throw 'agent.env does not name the portal given to setup' }
    $release = Get-Content -Raw (Join-Path $data 'config\vms_release.json') | ConvertFrom-Json
    if ($release.release_version -ne $expectedVersion) { throw "release $($release.release_version)" }
    'UUIDv4 identity; code + matching verifier; portal; release'
}
$python = Join-Path $app 'runtime\python\python.exe'
$cloudData = 'C:\TestCloud'
& $python 'C:\Validation\scripts\test-cloud.py' --app (Join-Path $app 'app') --data $cloudData --host $TestCloudHost --certificates-only
Import-Certificate -FilePath (Join-Path $cloudData 'tls\ca.cer') -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
$testCloud = Start-Process -FilePath $python -ArgumentList '"C:\Validation\scripts\test-cloud.py"', '--app', ('"' + (Join-Path $app 'app') + '"'), '--data', $cloudData, '--host', $TestCloudHost, '--port', '8443' `
    -WorkingDirectory $cloudData -WindowStyle Hidden -RedirectStandardOutput (Join-Path $results 'test-cloud.out.log') -RedirectStandardError (Join-Path $results 'test-cloud.err.log') -PassThru
Check 'test cloud up over HTTPS (real claim and appliance routes)' { if (-not (WaitFor 120 { Cloud GET '/test/state' })) { throw 'test cloud did not answer' }; 'https://cloud.anyaicam.test:8443' }
# The agent backed off while the cloud was not up yet; start it over.
Restart-Service AnyAiCamAgent -Force
Check 'agent opens a claim for this PC with its claim code verifier' {
    $claim = WaitFor 180 { @((Cloud GET '/test/state').claims | Where-Object { $_.status -eq 'pending' -and $_.device_id -eq $script:applianceId }) }
    if (-not $claim) { throw 'no pending claim for this appliance' }
    'pending claim for this appliance'
}
Check 'owner confirms with the claim code' {
    $result = Cloud POST '/api/portal/claims/confirm' @{ label_code = $script:claimCode; site_id = 'test-site' } -Owner
    if ($result.status -ne 'claimed') { throw "status $($result.status)" }
    'claimed'
}
Check 'agent completes the claim: credential and VMS cloud identity saved' {
    if (-not (WaitFor 180 { Test-Path (Join-Path $agentState 'credential.json') })) { throw 'no credential.json' }
    $identity = WaitFor 60 { Get-Content -Raw (Join-Path $data 'recordings\appliance_identity.json') -ErrorAction Stop | ConvertFrom-Json }
    if (-not $identity -or $identity.cloud_id -ne $script:applianceId.ToUpper() -or $identity.site_id -ne 'test-site') { throw 'VMS identity missing or wrong' }
    if (Test-Path (Join-Path $agentState 'claim_state.json')) { if (-not (WaitFor 60 { -not (Test-Path (Join-Path $agentState 'claim_state.json')) })) { throw 'claim state not cleared' } }
    "cloud_id $($identity.cloud_id), site test-site"
}
Check 'VMS points at the cloud (ANYAICAM_CLOUD_URL) and is healthy after its restart' {
    if (-not (WaitFor 60 { Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_ -eq "ANYAICAM_CLOUD_URL=$TestPortal" } })) { throw 'vms.env has no ANYAICAM_CLOUD_URL' }
    if (-not (WaitHealthy 180)) { throw 'VMS not healthy' }
    'linked and healthy'
}
Check 'cloud sees this PC online with its version (heartbeat)' {
    $appliance = WaitFor 180 { @((Cloud GET '/test/state').appliances | Where-Object { $_.last_check_in -and $_.cloud_id -eq $script:applianceId.ToUpper() })[0] }
    if (-not $appliance) { throw 'no heartbeat' }
    if ($appliance.online_status -ne 'online' -or -not "$($appliance.software_version)".StartsWith("$expectedVersion+")) { throw "status $($appliance.online_status) version $($appliance.software_version)" }
    "online, version $($appliance.software_version)"
}
function RunCommand([string]$Command) {
    $id = (Cloud POST '/test/command' @{ command = $Command } -Owner).id
    WaitFor 180 { @((Cloud GET '/test/state').commands | Where-Object { $_.id -eq $id -and $_.status -in 'completed', 'failed' })[0] }
}
Check 'remote reboot is refused on Windows' { $c = RunCommand 'reboot_appliance'; if (-not $c -or $c.status -ne 'failed' -or $c.error -notmatch 'not supported on Windows') { throw "result $($c.status) $($c.error)" }; $c.error }
Check 'remote software update is refused on Windows' { $c = RunCommand 'install_update'; if (-not $c -or $c.status -ne 'failed' -or $c.error -notmatch 'setup') { throw "result $($c.status) $($c.error)" }; $c.error }
Check 'remote restart_vms restarts the VMS' {
    $before = (BundledPython 'uvicorn').ProcessId
    $c = RunCommand 'restart_vms'
    if (-not $c -or $c.status -ne 'completed') { throw "result $($c.status) $($c.error)" }
    if (-not (WaitHealthy 180)) { throw 'VMS not healthy after restart' }
    $after = (BundledPython 'uvicorn').ProcessId
    if (-not $after -or $after -eq $before) { throw 'VMS process did not change' }
    'completed; new VMS process; healthy'
}
Check 'remote restart_service restarts the agent' {
    $before = (BundledPython 'agent-main').ProcessId
    $id = (Cloud POST '/test/command' @{ command = 'restart_service' } -Owner).id
    $after = WaitFor 180 { $p = BundledPython 'agent-main'; if ($p -and $p.ProcessId -ne $before -and (Get-Service AnyAiCamAgent).Status -eq 'Running') { $p.ProcessId } }
    if (-not $after) { throw 'agent did not restart' }
    'new agent process; service Running'
}
$credentialHash = (Get-FileHash (Join-Path $agentState 'credential.json')).Hash

# ---------------------------------------------------------------- resilience
Check 'service recovers after a restart' { Restart-Service AnyAiCamVMS -Force; WaitHealthy 180 }
Check 'service restarts itself after a crash' {
    $python = BundledPython 'uvicorn'
    if (-not $python) { throw 'VMS python.exe not found' }
    Stop-Process -Id $python.ProcessId -Force
    Start-Sleep -Seconds 15
    WaitHealthy 180
}

# ---------------------------------------------------------------- upgrade over itself, then uninstall
$secretsBefore = (Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_.StartsWith('ANYAICAM_APP_SECRETS=') })
# 2026-10-07: the second Sandbox run found a reinstall left every existing data
# file with an empty permission list (icacls /T), so setup could not read
# vms.env and stopped -- yet exited 0. Every data file must stay readable by
# Administrators and carry only the inherited SYSTEM/Administrators entries.
$dataFiles = @('config\vms.env', 'database\partner_portal.db', 'logs\AnyAiCamVMS.err.log', 'agent\credential.json', 'label\claim-label.txt') | ForEach-Object { Join-Path $data $_ }
function DataFilesHealthy {
    foreach ($file in $dataFiles) {
        try { [IO.File]::Open($file, 'Open', 'Read', 'ReadWrite').Close() } catch { throw "cannot read $file`: $($_.Exception.Message)" }
        $acl = Get-Acl -LiteralPath $file
        if (-not $acl.Access) { throw "$file has no permission entries" }
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if (-not $rule.IsInherited) { throw "$file has an explicit entry for $sid" }
            if ($sid -notin 'S-1-5-18', 'S-1-5-32-544') { throw "$file grants $sid" }
        }
    }
    'vms.env, database, service log, agent credential, claim code readable; inherited SYSTEM/Administrators only'
}
function StillLinked {
    if (-not (WaitFor 60 { (Get-Service AnyAiCamAgent -ErrorAction Stop).Status -eq 'Running' })) { throw 'agent service not running' }
    if ((Get-FileHash (Join-Path $agentState 'credential.json')).Hash -ne $credentialHash) { throw 'agent credential changed' }
    $line = Get-Content $labelFile | Where-Object { $_ -like 'Claim code:*' } | Select-Object -First 1
    if (($line -replace '^Claim code:\s*', '').Trim() -ne $claimCode) { throw 'claim code changed' }
    $since = (Get-Date).ToUniversalTime()
    if (-not (WaitFor 180 { @((Cloud GET '/test/state').appliances)[0].last_check_in -and ([datetime]@((Cloud GET '/test/state').appliances)[0].last_check_in) -gt (Get-Date).AddSeconds(-170) })) { throw 'no heartbeat after reinstall' }
    'agent running, same credential and claim code, heartbeat'
}
function Reinstall([string]$Log) {
    Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results $Log) + '"') -Wait -PassThru
}
function SecretsKept {
    if (-not (WaitHealthy 180)) { throw 'not healthy after reinstall' }
    $after = (Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_.StartsWith('ANYAICAM_APP_SECRETS=') })
    if ($after -ne $secretsBefore) { throw 'ANYAICAM_APP_SECRETS changed' }
    'healthy, secrets unchanged'
}

$repair = Reinstall 'setup-reinstall.log'
Check 'reinstall over itself exit code 0' { if ($repair.ExitCode -ne 0) { throw "exit $($repair.ExitCode)" }; 'exit 0' }
Check 'reinstall keeps data and secrets' { SecretsKept }
Check 'reinstall keeps data files readable with inherited permissions' { DataFilesHealthy }
Check 'reinstall keeps this PC linked to the cloud' { StillLinked }

# An install damaged by an earlier 1.3.0 candidate: vms.env (Administrators-
# owned) and the database (SYSTEM-owned) with empty permission lists.
foreach ($file in $dataFiles[0..1]) { & icacls.exe $file /inheritance:r /Q | Out-Null }
Check 'simulated damage: vms.env unreadable before the repair reinstall' { try { [void][IO.File]::ReadAllBytes($dataFiles[0]); throw 'still readable' } catch [UnauthorizedAccessException] { 'access denied, as an old candidate left it' } }
$repair2 = Reinstall 'setup-repair-reinstall.log'
Check 'repair reinstall exit code 0' { if ($repair2.ExitCode -ne 0) { throw "exit $($repair2.ExitCode)" }; 'exit 0' }
Check 'repair reinstall keeps data and secrets' { SecretsKept }
Check 'repair reinstall restores readable, inherited permissions' { DataFilesHealthy }
Check 'repair reinstall keeps this PC linked to the cloud' { StillLinked }

$uninstaller = Get-ChildItem $app -Filter 'unins*.exe' | Select-Object -First 1
$uninstall = Start-Process -FilePath $uninstaller.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-uninstall.log') + '"') -Wait -PassThru
Check 'uninstall exit code 0' { $uninstall.ExitCode -eq 0 }
Check 'uninstall removes the service' { -not (Get-Service AnyAiCamVMS -ErrorAction SilentlyContinue) }
Check 'uninstall removes the cloud agent service' { -not (Get-Service AnyAiCamAgent -ErrorAction SilentlyContinue) }
Check 'uninstall keeps the cloud link (credential, claim code)' { (Test-Path (Join-Path $agentState 'credential.json')) -and (Test-Path $labelFile) }
Check 'uninstall removes the firewall rules' { @(Get-NetFirewallRule -Group 'AnyAiCam VMS' -ErrorAction SilentlyContinue).Count -eq 0 }
Check 'uninstall keeps customer data' { Test-Path (Join-Path $data 'database\partner_portal.db') }

# The service logs (kept after uninstall) for diagnosis. The data folder is
# SYSTEM/Administrators-only, so copy in backup mode.
& robocopy.exe (Join-Path $data 'logs') (Join-Path $results 'service-logs') *.log /B /R:0 /W:0 /NP /NJH /NJS | Out-Null

# A setup step that fails must fail the setup's exit code (not 0) and leave no
# service. Forced here by putting a folder where vms.env belongs; vms.env is
# restored afterwards.
$envFile = $dataFiles[0]
$envBackup = Join-Path $env:TEMP 'vms.env.validation-backup'
Copy-Item -LiteralPath $envFile -Destination $envBackup -Force
Remove-Item -LiteralPath $envFile -Force
New-Item -ItemType Directory -Path $envFile | Out-Null
$broken = Reinstall 'setup-forced-failure.log'
Check 'a failing setup step gives a nonzero exit code (20)' { if ($broken.ExitCode -ne 20) { throw "exit $($broken.ExitCode)" }; 'exit 20' }
Check 'a failing setup leaves no service behind' { if (Get-Service AnyAiCamVMS, AnyAiCamAgent -ErrorAction SilentlyContinue) { throw 'service installed' }; 'no VMS or agent service' }
Remove-Item -LiteralPath $envFile -Recurse -Force
Copy-Item -LiteralPath $envBackup -Destination $envFile -Force

# The claim code and the agent credential are secrets: never in any log or
# report this validation collected (setup logs, service logs incl. agent.log).
$credential = [string](Get-Content -Raw (Join-Path $agentState 'credential.json') | ConvertFrom-Json).credential
if ($testCloud -and -not $testCloud.HasExited) { Stop-Process -Id $testCloud.Id -Force }
Check 'no claim code or credential in any log' {
    $leaks = Get-ChildItem $results -Recurse -File | Where-Object { $_.Extension -in '.log', '.txt', '.json' } | Where-Object {
        $text = [IO.File]::ReadAllText($_.FullName)
        $text.Contains($claimCode) -or $text.Contains($claimCode -replace '-', '') -or ($credential -and $text.Contains($credential))
    }
    if ($leaks) { throw "found in $($leaks.Name -join ', ')" }
    "$(@(Get-ChildItem $results -Recurse -File).Count) files checked"
}

$failed = @($checks | Where-Object { -not $_.ok }).Count
$checks | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $results 'report.json')
Add-Content (Join-Path $results 'report.txt') ("RESULT: {0} checks, {1} failed" -f $checks.Count, $failed)
Set-Content (Join-Path $results 'DONE') ("{0} failed" -f $failed)
