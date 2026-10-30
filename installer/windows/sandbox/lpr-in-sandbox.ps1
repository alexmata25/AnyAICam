# Windows 1.3.4.1 LPR acceptance in Windows Sandbox (Blue, 2026-10-29). Test tooling only; never packaged.
# Real end-to-end licence-plate reading with the INSTALLED product: its lpr.py, its bundled Tesseract (runtime\tools\
# tesseract), its torch/ultralytics wheels, under ENFORCED Smart App Control (enforce-sac.ps1 runs first).
# The plate model is NOT in the installer (LIC-1: customer redistribution blocked; internal lab testing approved by
# the owner 2026-10-28). It is placed into THIS disposable Sandbox only, from C:\Validation\assets.
# Expected plate (read by eye from the sample): ZPN720.
$ErrorActionPreference = 'Continue'
$results = 'C:\Validation\results'; $assets = 'C:\Validation\assets'
New-Item -ItemType Directory -Force -Path $results | Out-Null
$report = Join-Path $results 'report.txt'
$failed = 0
function Check([string]$Name, [scriptblock]$Test) {
    try { $d = & $Test; $ok = $true } catch { $d = $_.Exception.Message; $ok = $false }
    if (-not $ok) { $script:failed++ }
    Add-Content $report ("{0}  {1}  {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $Name, $d)
}
$app = Join-Path $env:ProgramFiles 'AnyAiCam'; $data = Join-Path $env:ProgramData 'AnyAiCam'
$py = Join-Path $app 'runtime\python\python.exe'
$sample = Join-Path $assets 'carpark-frame1-vehicle-crop.png'
Check 'Smart App Control ENFORCED' {
    $s = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState
    if ($s -ne 1) { throw "VerifiedAndReputablePolicyState=$s" }; 'state 1'
}
$setup = Get-ChildItem 'C:\Validation\setup\*.exe' | Select-Object -First 1
Check 'signed setup installs (exit 0)' {
    $sig = Get-AuthenticodeSignature $setup.FullName; if ($sig.Status -ne 'Valid') { throw "setup signature $($sig.Status)" }
    $p = Start-Process $setup.FullName -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/ProductMode=local', ('/LOG="' + (Join-Path $results 'setup-install.log') + '"') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "exit $($p.ExitCode)" }; "exit 0; $($sig.SignerCertificate.Subject)"
}
Check 'VMS answers /health' {
    $deadline = (Get-Date).AddSeconds(180)
    do { try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 'http://127.0.0.1:8000/health').StatusCode -eq 200) { return 'healthy' } } catch { }; Start-Sleep 3 } while ((Get-Date) -lt $deadline)
    throw 'not healthy in 180 s'
}
Check 'lab plate model placed (not from the installer)' {
    $dir = Join-Path $data 'models\lpr'; New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Copy-Item (Join-Path $assets 'license-plate-finetune-v1n.pt') $dir -Force
    (Get-FileHash (Join-Path $dir 'license-plate-finetune-v1n.pt')).Hash.ToLower()
}
$probe = Join-Path $results 'lpr_probe.py'
$probeSource = @'
import json, os, sys, time, hashlib
result = {"identity": os.environ.get("USERNAME")}
try:
    os.environ.setdefault("YOLO_OFFLINE", "true")
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    os.environ["ANYAICAM_LPR_ENABLED"] = "true"
    import cv2, analytics_platform, lpr
    result["unavailable_reason"] = analytics_platform.unavailable_reason("lpr", windows=True)
    result["capability"] = lpr.capability()
    image = cv2.imread(sys.argv[1])
    result["image_sha256"] = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()
    # The product entry point main.py calls; camera_number=None skips only the per-camera on/off switch.
    start = time.perf_counter()
    plate = lpr.recognize_plate(image, camera_number=None)
    result["recognize_seconds"] = round(time.perf_counter() - start, 3)
    result["recognized"] = plate
    if plate is None:
        detection = lpr.detect_plate(image)
        result["detection"] = detection
        if detection:
            x, y, w, h = detection["region"]
            crop = image[y:y + h, x:x + w]
            result["glyph_read"] = lpr.read_plate_glyphs(crop)
            result["text_read"] = lpr.read_plate_text(crop)
except Exception:
    import traceback
    result["error"] = traceback.format_exc()
print(json.dumps(result, default=str))
'@
Set-Content -Encoding UTF8 -Path $probe -Value $probeSource
function Plate($j) {
    if ($null -eq $j.recognized) { return $null }
    if ($j.recognized -is [string]) { return $j.recognized }
    $j.recognized.plate_number
}
$tessDir = Join-Path $app 'runtime\tools\tesseract'
Check 'LPR available on Windows once the model is present (the gate opens)' {
    $env:PATH = $tessDir + ';' + (Join-Path $app 'runtime\tools\ffmpeg') + ';' + $env:PATH
    $env:TESSDATA_PREFIX = Join-Path $tessDir 'tessdata'
    Push-Location (Join-Path $app 'app')
    try { & $py $probe $sample 2>&1 | Out-File -Encoding utf8 (Join-Path $results 'lpr-admin.json') } finally { Pop-Location }
    $j = Get-Content -Raw (Join-Path $results 'lpr-admin.json') | ConvertFrom-Json
    if ($j.error) { throw (($j.error -split "`n" | Select-Object -Last 3) -join ' | ') }
    if ($j.unavailable_reason) { throw "still unavailable: $($j.unavailable_reason)" }
    'unavailable_reason=None; capability ' + ($j.capability | ConvertTo-Json -Compress)
}
Check 'real OCR reads the sample plate ZPN720 (installed lpr.py + bundled Tesseract)' {
    $j = Get-Content -Raw (Join-Path $results 'lpr-admin.json') | ConvertFrom-Json
    $text = Plate $j
    if (-not $text) { throw "no plate recognized; detection=$($j.detection | ConvertTo-Json -Compress); glyph_read=$($j.glyph_read | ConvertTo-Json -Compress); text_read=$($j.text_read | ConvertTo-Json -Compress)" }
    if ("$text" -ne 'ZPN720') { throw "read '$text' (expected ZPN720): $($j.recognized | ConvertTo-Json -Compress)" }
    "read $text in $($j.recognize_seconds) s: $($j.recognized | ConvertTo-Json -Compress)"
}
Check 'the same read as LocalSystem (the service identity)' {
    $out = Join-Path $results 'lpr-system.json'
    $wrapper = Join-Path $results 'lpr-system.ps1'
    $lines = @(
        "`$env:PATH = '$tessDir;$app\runtime\tools\ffmpeg;' + `$env:PATH",
        "`$env:TESSDATA_PREFIX = '$tessDir\tessdata'",
        "Set-Location '$app\app'",
        "& '$py' '$probe' '$sample' 2>&1 | Out-File -Encoding utf8 '$out'"
    )
    Set-Content -Path $wrapper -Value $lines
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$wrapper`""
    Register-ScheduledTask -TaskName 'AnyAiCamLprProbe' -Action $action -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
    Start-ScheduledTask -TaskName 'AnyAiCamLprProbe'
    $deadline = (Get-Date).AddSeconds(240)
    while ((Get-Date) -lt $deadline -and -not ((Test-Path $out) -and ((Get-Content -Raw $out) -match '\}\s*$'))) { Start-Sleep 3 }
    Unregister-ScheduledTask -TaskName 'AnyAiCamLprProbe' -Confirm:$false
    $j = Get-Content -Raw $out | ConvertFrom-Json
    if ($j.error) { throw (($j.error -split "`n" | Select-Object -Last 3) -join ' | ') }
    $text = Plate $j
    if ("$text" -ne 'ZPN720') { throw "SYSTEM read '$text'" }
    "identity $($j.identity); read $text in $($j.recognize_seconds) s"
}
Check 'Code Integrity: nothing blocked under enforcement' {
    $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; Id = 3033, 3076, 3077, 3089, 3091, 3092 } -ErrorAction SilentlyContinue)
    $ev | ForEach-Object { "{0:o}  {1}  {2}" -f $_.TimeCreated, $_.Id, ($_.Message -replace '\s+', ' ') } | Set-Content (Join-Path $results 'codeintegrity-events.txt')
    $blocked = @($ev | Where-Object { $_.Id -in 3077, 3092 })
    if ($blocked) { throw "$($blocked.Count) block event(s); see codeintegrity-events.txt" }
    "$($ev.Count) CI event(s), 0 blocks"
}
Add-Content $report "RESULT: $failed failed"
Set-Content (Join-Path $results 'DONE') "$failed failed"
