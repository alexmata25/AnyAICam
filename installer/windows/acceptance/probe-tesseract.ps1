# Probe run BY Test-TesseractLocalSystem.ps1 as the account under test (LocalSystem on the R430 VM). Writes JSON only.
param([Parameter(Mandatory=$true)][string]$ResultPath, [Parameter(Mandatory=$true)][string]$InstallRoot,
      [string]$Image = '', [string]$Expected = '')
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'acceptance-common.ps1')
$out = [ordered]@{ identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name; sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
try {
    # Exactly what service-launcher.ps1 sets for the VMS process.
    $environment = Get-AnyAiCamTesseractEnvironment $InstallRoot
    $env:PATH = $environment.Path + ';' + $env:PATH
    $env:TESSDATA_PREFIX = $environment.TessdataPrefix
    $out.tessdata_prefix = $env:TESSDATA_PREFIX
    # Resolved through PATH, as pytesseract does ("tesseract").
    $command = Get-Command tesseract -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $out.resolved = if ($command) { $command.Source } else { $null }
    if ($command) {
        $version = & $command.Source --version 2>&1 | Out-String
        $out.version_exit = $LASTEXITCODE; $out.version = $version.Trim()
        $langs = & $command.Source --list-langs 2>&1 | Out-String
        $out.langs_exit = $LASTEXITCODE; $out.langs_raw = $langs.Trim()
        $out.languages = @($langs -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^[a-z_]{3,}$' })
        if ($Image) {
            $text = & $command.Source $Image stdout --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 2>&1 | Out-String
            $out.ocr_exit = $LASTEXITCODE; $out.ocr_text = ($text -replace '\s', ''); $out.ocr_expected = $Expected
        }
        $combined = "$($out.version) $($out.langs_raw) $($out.ocr_text)"
        $out.blocked_by_app_control = [bool]($combined -match '4551|Application Control policy')
    }
} catch { $out.error = $_.Exception.Message }
$out | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 -LiteralPath $ResultPath
