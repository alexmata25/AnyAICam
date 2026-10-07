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

# ---------------------------------------------------------------- install
$install = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-install.log') + '"') -Wait -PassThru
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

# ---------------------------------------------------------------- resilience
Check 'service recovers after a restart' { Restart-Service AnyAiCamVMS -Force; WaitHealthy 180 }
Check 'service restarts itself after a crash' {
    $python = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like "$app*" } | Select-Object -First 1
    if (-not $python) { throw 'VMS python.exe not found' }
    Stop-Process -Id $python.ProcessId -Force
    Start-Sleep -Seconds 15
    WaitHealthy 180
}

# ---------------------------------------------------------------- upgrade over itself, then uninstall
$secretsBefore = (Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_.StartsWith('ANYAICAM_APP_SECRETS=') })
$repair = Start-Process -FilePath $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-reinstall.log') + '"') -Wait -PassThru
Check 'reinstall over itself exit code 0' { $repair.ExitCode -eq 0 }
Check 'reinstall keeps data and secrets' { if (-not (WaitHealthy 180)) { throw 'not healthy after reinstall' }; $after = (Get-Content (Join-Path $data 'config\vms.env') | Where-Object { $_.StartsWith('ANYAICAM_APP_SECRETS=') }); if ($after -ne $secretsBefore) { throw 'ANYAICAM_APP_SECRETS changed' }; 'secrets unchanged' }
$uninstaller = Get-ChildItem $app -Filter 'unins*.exe' | Select-Object -First 1
$uninstall = Start-Process -FilePath $uninstaller.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', ('/LOG="' + (Join-Path $results 'setup-uninstall.log') + '"') -Wait -PassThru
Check 'uninstall exit code 0' { $uninstall.ExitCode -eq 0 }
Check 'uninstall removes the service' { -not (Get-Service AnyAiCamVMS -ErrorAction SilentlyContinue) }
Check 'uninstall removes the firewall rules' { @(Get-NetFirewallRule -Group 'AnyAiCam VMS' -ErrorAction SilentlyContinue).Count -eq 0 }
Check 'uninstall keeps customer data' { Test-Path (Join-Path $data 'database\partner_portal.db') }

# The service logs (kept after uninstall) for diagnosis. The data folder is
# SYSTEM/Administrators-only, so copy in backup mode.
& robocopy.exe (Join-Path $data 'logs') (Join-Path $results 'service-logs') *.log /B /R:0 /W:0 /NP /NJH /NJS | Out-Null

$failed = @($checks | Where-Object { -not $_.ok }).Count
$checks | ConvertTo-Json -Depth 3 | Set-Content (Join-Path $results 'report.json')
Add-Content (Join-Path $results 'report.txt') ("RESULT: {0} checks, {1} failed" -f $checks.Count, $failed)
Set-Content (Join-Path $results 'DONE') ("{0} failed" -f $failed)
