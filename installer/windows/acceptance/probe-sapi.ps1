# Probe run BY Test-SapiLocalSystem.ps1 as the account under test. Calls the PRODUCT's greeting synthesis
# (app/aac_voice_call_greeting.synthesize_speech_sapi) in the installed Python runtime and measures the audio.
param([Parameter(Mandatory=$true)][string]$ResultPath, [Parameter(Mandatory=$true)][string]$PythonExe,
      [Parameter(Mandatory=$true)][string]$AppDir, [string]$Text = 'Hello, thank you for visiting. How can I help you?')
$ErrorActionPreference = 'Continue'
$out = [ordered]@{ identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name; sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
try {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try { $out.voices = @($synth.GetInstalledVoices() | ForEach-Object { "$($_.VoiceInfo.Name) [$($_.VoiceInfo.Culture)] enabled=$($_.Enabled)" }) } finally { $synth.Dispose() }
} catch { $out.voices_error = $_.Exception.Message }
$code = @'
import array, json, math, sys
sys.path.insert(0, sys.argv[1])
import aac_voice_call_greeting as g
pcm, rate = g.synthesize_speech_sapi(sys.argv[2])
samples = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
speech = [v for v in samples if abs(v) > 300]
level = g.level_greeting_pcm(pcm, "medium")
lv = [v for v in array.array("h", level[: len(level) - len(level) % 2]) if abs(v) > 300]
def dbfs(values):
    return round(20 * math.log10(math.sqrt(sum(v * v for v in values) / len(values)) / 32768), 1) if values else None
print(json.dumps({"rate": rate, "bytes": len(pcm), "seconds": round(len(pcm) / 2 / rate, 2),
                  "speech_seconds": round(len(speech) / rate, 2), "raw_dbfs": dbfs(speech), "levelled_dbfs": dbfs(lv),
                  "engine": g._tts_engine()}))
'@
try {
    $script = Join-Path ([IO.Path]::GetDirectoryName($ResultPath)) 'sapi-probe.py'
    Set-Content -Encoding utf8 -LiteralPath $script -Value $code
    Push-Location $AppDir
    try { $raw = & $PythonExe $script $AppDir $Text 2>&1 | Out-String; $out.python_exit = $LASTEXITCODE } finally { Pop-Location }
    $line = ($raw -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    if ($line) { $out.audio = $line | ConvertFrom-Json } else { $out.python_output = $raw.Trim() }
} catch { $out.error = $_.Exception.Message }
$out | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath $ResultPath
