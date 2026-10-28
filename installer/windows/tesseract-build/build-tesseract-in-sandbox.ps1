# Builds the minimal Windows Tesseract (no libarchive/libcurl -> no libiconv/libxml2/ICU/OpenSSL) INSIDE Windows
# Sandbox, then runs the OCR smoke tests there. Never run on the host: it installs Visual Studio Build Tools.
#
#   C:\Inputs  (read-only)  staged by prepare-build-inputs.py: src\, prefix\Library\, tessdata\, ocr-tests\,
#                           optional conda-bundle\ (the current bundle, OCR-compared), inputs-manifest.json
#   C:\Output  (writable)   engine\tesseract.exe + DLLs, build-info.json, ocr-results.json, logs, DONE / FAILED
param([string]$Inputs = 'C:\Inputs', [string]$Output = 'C:\Output')
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Output | Out-Null
Start-Transcript -Path (Join-Path $Output 'build.log') -Force | Out-Null
$info = [ordered]@{ started = (Get-Date).ToUniversalTime().ToString('o') }
function Save-Info { $info | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 (Join-Path $Output 'build-info.json') }
try {
    # 1. Inputs are exactly what the host staged.
    $manifest = Get-Content -Raw (Join-Path $Inputs 'inputs-manifest.json') | ConvertFrom-Json
    foreach ($entry in $manifest.PSObject.Properties) {
        $path = Join-Path $Inputs ($entry.Name -replace '/', '\')
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $entry.Value) { throw "Input changed: $($entry.Name)" }
    }
    $lock = Get-Content -Raw (Join-Path $Inputs 'tesseract-minimal.lock.json') | ConvertFrom-Json
    $info.inputs_verified = @($manifest.PSObject.Properties).Count

    # 2. Visual Studio 2022 Build Tools, in this disposable Sandbox only. Microsoft-signed bootstrapper required.
    $bootstrapper = 'C:\bt\vs_buildtools.exe'
    New-Item -ItemType Directory -Force -Path 'C:\bt' | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri 'https://aka.ms/vs/17/release/vs_buildtools.exe' -OutFile $bootstrapper
    $signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '^CN=Microsoft Corporation,') { throw 'vs_buildtools.exe is not validly signed by Microsoft.' }
    $install = Start-Process -FilePath $bootstrapper -Wait -PassThru -ArgumentList @('--quiet', '--wait', '--norestart', '--nocache',
        '--add', 'Microsoft.VisualStudio.Workload.VCTools', '--add', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        '--add', 'Microsoft.VisualStudio.Component.Windows11SDK.22621', '--add', 'Microsoft.VisualStudio.Component.VC.CMake.Project')
    if ($install.ExitCode -notin 0, 3010) { throw "Build Tools installation failed: exit $($install.ExitCode)" }
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vs = & $vswhere -products * -latest -format json | ConvertFrom-Json | Select-Object -First 1
    $cmake = Join-Path $vs.installationPath 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
    $vsdev = Join-Path $vs.installationPath 'Common7\Tools\VsDevCmd.bat'
    $info.visual_studio = @{ version = $vs.installationVersion; name = $vs.displayName }
    $info.cmake = ((& $cmake --version) | Select-Object -First 1)
    $info.msvc = (cmd /c "`"$vsdev`" -arch=amd64 -no_logo >nul && cl 2>&1" | Select-Object -First 1)
    Save-Info

    # 3. Configure and build from a writable copy of the pinned source.
    $work = 'C:\work'
    Copy-Item -Recurse -Force (Join-Path $Inputs 'src') "$work\src"
    $prefix = Join-Path $Inputs 'prefix\Library'
    $configure = @('-S', "$work\src", '-B', "$work\build", '-G', 'Visual Studio 17 2022', '-A', 'x64',
        "-DCMAKE_PREFIX_PATH=$prefix", "-DLeptonica_DIR=$prefix\lib\cmake\leptonica", "-DCMAKE_INSTALL_PREFIX=$work\install") +
        @($lock.cmake_options) +
        @("-DCMAKE_C_FLAGS=$($lock.deterministic_flags.CMAKE_C_FLAGS)", "-DCMAKE_CXX_FLAGS=$($lock.deterministic_flags.CMAKE_CXX_FLAGS)",
          "-DCMAKE_EXE_LINKER_FLAGS=$($lock.deterministic_flags.CMAKE_EXE_LINKER_FLAGS)")
    & $cmake @configure 2>&1 | Tee-Object -FilePath (Join-Path $Output 'configure.log')
    if ($LASTEXITCODE -ne 0) { throw "CMake configure failed ($LASTEXITCODE)" }
    & $cmake --build "$work\build" --config Release --target tesseract --parallel 2>&1 | Tee-Object -FilePath (Join-Path $Output 'compile.log')
    if ($LASTEXITCODE -ne 0) { throw "Build failed ($LASTEXITCODE)" }
    $exe = Get-ChildItem -Recurse -Filter tesseract.exe "$work\build" | Select-Object -First 1
    if (-not $exe) { throw 'tesseract.exe was not produced.' }

    # 4. Engine folder: tesseract.exe + the prefix DLLs (the host keeps only the real import closure).
    $engine = Join-Path $Output 'engine'
    New-Item -ItemType Directory -Force -Path $engine | Out-Null
    Copy-Item $exe.FullName $engine
    Copy-Item (Join-Path $prefix 'bin\*.dll') $engine
    $dumpbin = cmd /c "`"$vsdev`" -arch=amd64 -no_logo >nul && dumpbin /nologo /dependents `"$engine\tesseract.exe`""
    $dumpbin | Set-Content -Encoding utf8 (Join-Path $Output 'dumpbin-dependents.txt')
    $info.tesseract_exe_sha256 = (Get-FileHash -Algorithm SHA256 "$engine\tesseract.exe").Hash.ToLowerInvariant()
    $info.forbidden_imports = @($lock.forbidden_dlls | Where-Object { ($dumpbin -join "`n") -match [regex]::Escape($_) })
    Save-Info

    # 5. OCR smoke tests, the way app/lpr.py calls Tesseract (single line, plate character whitelist).
    $results = @()
    $engines = @(@{ name = 'minimal'; dir = $engine })
    if (Test-Path (Join-Path $Inputs 'conda-bundle\tesseract\tesseract.exe')) { $engines += @{ name = 'conda-bundle'; dir = (Join-Path $Inputs 'conda-bundle\tesseract') } }
    foreach ($e in $engines) {
        $env:TESSDATA_PREFIX = Join-Path $Inputs 'tessdata'
        $version = & (Join-Path $e.dir 'tesseract.exe') --version 2>&1 | Out-String
        $launch = $LASTEXITCODE
        foreach ($image in Get-ChildItem (Join-Path $Inputs 'ocr-tests') -Filter *.png) {
            $expected = (Get-Content -Raw ($image.FullName -replace '\.png$', '.txt')).Trim()
            $text = & (Join-Path $e.dir 'tesseract.exe') $image.FullName stdout --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 2>&1 | Out-String
            $results += [ordered]@{ engine = $e.name; image = $image.Name; expected = $expected; got = ($text -replace '\s', ''); exit = $LASTEXITCODE;
                                    match = (($text -replace '\s', '') -eq $expected); blocked_by_app_control = ($text -match '4551|Application Control') }
        }
        $results += [ordered]@{ engine = $e.name; version_exit = $launch; version = $version.Trim() }
    }
    $results | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $Output 'ocr-results.json')
    $info.finished = (Get-Date).ToUniversalTime().ToString('o')
    Save-Info
    Set-Content (Join-Path $Output 'DONE') 'ok'
} catch {
    $info.error = $_.Exception.Message
    Save-Info
    Set-Content (Join-Path $Output 'FAILED') $_.Exception.Message
} finally { Stop-Transcript | Out-Null }
