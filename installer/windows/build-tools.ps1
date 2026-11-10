# Build tools only. No installation, PATH changes or tool execution here.
function Read-BuildWheelLock([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Wheel lock is missing.' }
    $entries = @(Get-Content -LiteralPath $Path)
    if ($entries.Count -eq 0) { throw 'Wheel lock is empty.' }
    $seen = @{}
    $parsed = @()
    foreach ($line in $entries) {
        if ($line -cnotmatch '^([0-9a-f]{64})  ([A-Za-z0-9_+.-]+\.whl)$') { throw 'Wheel lock contains a malformed entry.' }
        $digest = $Matches[1]; $name = $Matches[2]
        if ($seen.ContainsKey($name)) { throw "Wheel lock contains a duplicate name: $name" }
        $seen[$name] = $true
        $parsed += [pscustomobject]@{ Name = $name; Sha256 = $digest }
    }
    return $parsed
}

function Resolve-BuildGit([string]$GitExe = '') {
    if ($GitExe) {
        if (-not (Test-Path -LiteralPath $GitExe -PathType Leaf)) { throw 'GitExe does not identify an existing executable.' }
        return $GitExe
    }
    $command = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    $installed = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
    if (Test-Path -LiteralPath $installed -PathType Leaf) { return $installed }
    throw 'Git is unavailable; supply -GitExe pointing to the approved Git executable.'
}

# Blue's sealed 1.3.4 baseline: Inno Setup 7.1.0 (owner-approved 2026-11-04).
# Genuine 7.1.0 binaries carry PE version 0.0.0.0, so the compiler is identified
# by content instead: every file of the compiler folder (Examples excepted) must
# match inno-setup-7.1.0.sha256, which was taken from the hash-pinned installer
# tooling/innosetup-7.1.0-x64.exe (SHA-256 0362a383...df2f), and every
# executable must carry a valid Authenticode signature from the pinned publisher
# certificate chaining to the pinned root. Nothing here runs the compiler.
$BuildInnoPublisherThumbprint = 'E0AB19C8D38CBF9C44709925122A7A02F8C70CB7'  # CN=Pyrsys B.V.
$BuildInnoRootThumbprint = 'CCBBF9E1485AF63CE47ABF8E9E648C2504FC319D'       # Sectigo Public Code Signing Root R46
# Written by the Inno installer itself; never loaded by the compiler.
$BuildInnoUninstallerFiles = @('unins000.exe', 'unins000.dat', 'unins000.msg')

function Read-BuildInnoManifest([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Inno Setup compiler manifest is missing.' }
    $entries = @(Get-Content -LiteralPath $Path)
    if ($entries.Count -eq 0) { throw 'Inno Setup compiler manifest is empty.' }
    $parsed = @{}
    foreach ($line in $entries) {
        if ($line -cnotmatch '^([0-9a-f]{64})  ([A-Za-z0-9_+.() -]+(/[A-Za-z0-9_+.() -]+)*)$') { throw 'Inno Setup compiler manifest contains a malformed entry.' }
        $digest = $Matches[1]; $name = $Matches[2]
        if ($name -match '(^|/)\.\.?(/|$)') { throw 'Inno Setup compiler manifest contains an unsafe path.' }
        if ($parsed.ContainsKey($name)) { throw "Inno Setup compiler manifest contains a duplicate name: $name" }
        $parsed[$name] = $digest
    }
    if (-not $parsed.ContainsKey('ISCC.exe')) { throw 'Inno Setup compiler manifest does not list ISCC.exe.' }
    return $parsed
}

# "" when the file is validly signed by the pinned publisher under the pinned root; otherwise why not.
function Get-BuildInnoSignatureProblem($Signature) {
    if ($null -eq $Signature -or [string]$Signature.Status -ne 'Valid') {
        return "not validly signed ($(if ($Signature) { $Signature.Status } else { 'no signature' }))"
    }
    $certificate = $Signature.SignerCertificate
    if ($null -eq $certificate -or $certificate.Thumbprint -ne $BuildInnoPublisherThumbprint) { return 'signed by an unapproved publisher' }
    $chain = New-Object Security.Cryptography.X509Certificates.X509Chain
    # Authenticode (with its timestamp) already established validity; only the chain's shape is checked here.
    $chain.ChainPolicy.RevocationMode = [Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    $chain.ChainPolicy.VerificationFlags = [Security.Cryptography.X509Certificates.X509VerificationFlags]::IgnoreNotTimeValid
    [void]$chain.Build($certificate)
    if ($chain.ChainElements.Count -lt 2) { return 'the signing certificate has no issuing chain' }
    if ($chain.ChainElements[$chain.ChainElements.Count - 1].Certificate.Thumbprint -ne $BuildInnoRootThumbprint) { return 'the signing certificate does not chain to the pinned root' }
    return ''
}

# Every reason the compiler folder is not the pinned Inno Setup 7.1.0; empty when it is.
function Get-BuildInnoCompilerProblems([string]$Compiler, [string]$Manifest) {
    if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) { return @("Inno Setup compiler not found: $Compiler") }
    if ((Split-Path -Leaf $Compiler) -ne 'ISCC.exe') { return @('The Inno Setup compiler must be ISCC.exe.') }
    $expected = Read-BuildInnoManifest $Manifest
    # One DirectoryInfo gives every file the same path form (8.3 short or long) as the folder.
    $folder = New-Object IO.DirectoryInfo (Split-Path -Parent $Compiler)
    $problems = @()
    $present = @{}
    foreach ($file in @($folder.EnumerateFiles('*', [IO.SearchOption]::AllDirectories))) {
        $name = $file.FullName.Substring($folder.FullName.TrimEnd('\').Length + 1) -replace '\\', '/'
        if ($name -like 'Examples/*') { continue }
        $present[$name] = $true
        if (-not $expected.ContainsKey($name)) {
            if ($BuildInnoUninstallerFiles -notcontains $name) { $problems += "unexpected file in the compiler folder: $name" }
            continue
        }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant() -ne $expected[$name]) {
            $problems += "compiler file does not match the pinned Inno Setup 7.1.0: $name"
            continue
        }
        if ($file.Extension -in @('.exe', '.dll')) {
            $signature = Get-BuildInnoSignatureProblem (Get-AuthenticodeSignature -LiteralPath $file.FullName)
            if ($signature) { $problems += "${name}: $signature" }
        }
    }
    foreach ($name in $expected.Keys) {
        if (-not $present.ContainsKey($name)) { $problems += "pinned compiler file is missing: $name" }
    }
    return $problems
}

function Assert-BuildInnoCompiler([string]$Compiler, [string]$Manifest = (Join-Path $PSScriptRoot 'inno-setup-7.1.0.sha256')) {
    $problems = @(Get-BuildInnoCompilerProblems $Compiler $Manifest)
    if ($problems.Count) {
        throw ("The Inno Setup compiler is not the pinned, signed 7.1.0:`n  " + ($problems -join "`n  "))
    }
}
