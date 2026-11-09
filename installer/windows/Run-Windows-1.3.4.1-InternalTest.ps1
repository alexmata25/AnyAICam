#Requires -Version 5.1
# One-shot, owner-authorized internal-test build only.
# This script does not change source/configuration, and it refuses any non-approved inputs.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ExpectedCommit
)

$ErrorActionPreference = 'Stop'

$Repo = 'C:\Users\Alejandro Mata\.codex\worktrees\windows-1341-codex1-candidate\windows1341-fdddbe7-internal-signing'
$ExpectedBranch = 'codex/windows-vms-1341-candidate-20261109'
$ExpectedCommit = $ExpectedCommit.ToLowerInvariant()
$BaseCommit = 'fdddbe7f2b4ee4b25c69dff05a9b576eb3b47824'
$InputDir = 'C:\Users\Alejandro Mata\anyaicam-release-windows\candidates\1.3.4.1-fdddbe7-release-1.2.7-UNSIGNED-SYNTHETIC-KEYSET-DO-NOT-SHIP\build-output'
$ExpectedKeysetHash = '629ac8e4545a9c324a21d6b0f08098ebc1c814bcc29e9a6f46fff992357c8b49'
$ExpectedRegistryHash = 'b8e64ab1831439c25b447c33b930b95d1497257171c01fc9ffa86a9249dad719'
$ClientRoot = 'C:\Users\Alejandro Mata\.codex\visualizations\2026\10\26\01a17902-1ce2-7cc2-8d16-ea9dcd16f9ef\artifact-signing-client-1.0.146-attempt06'
$ExpectedDlibHash = '123fa0c50341777d8e5b1414df52f39a1f2c7ab4a9f716abf6310069a6cb8a19'
$ExpectedNupkgHash = '7761e5d8f31e54b764da21d33cd8b0a2940e1b05526c7dfcb5e94b0431426f70'
$MetadataPath = 'C:\AnyAiCamSigning\metadata.json'
$SignToolPath = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
$PythonPath = 'C:\Users\Alejandro Mata\AppData\Local\Programs\Python\Python312-RC11-20261025-170614\python.exe'
$LockPath = 'C:\Users\Alejandro Mata\anyaicam-red\.docker-test.lock'
$OutputDir = Join-Path $Repo 'installer\windows\output-internal-test'
$BuildScript = Join-Path $Repo 'installer\windows\build.ps1'
$ExpectedPublisherSubject = 'CN=Any AI Cam, O=Any AI Cam, L=Porter, S=Texas, C=US'
$ExpectedPublisherEku = '1.3.6.1.4.1.311.97.227715007.494188131.681389833.725589707'
$ExpectedPublisherRoot = 'F40042E2E5F7E8EF8189FED15519AECE42C3BFA2'
$ExpectedMsSubjectPrefix = 'CN=Microsoft Corporation,'
$ExpectedInnoPublisherPrefix = 'CN=Pyrsys B.V.,'

$script:Checks = @()
$script:Artifacts = @()
$script:BuildStarted = $false
$script:RunStatus = 'PREFLIGHT_IN_PROGRESS'
$script:SafeFailure = $null
$script:LockLease = $null
$script:LockStatus = 'NOT_ACQUIRED'

$ExpectedLocalAppData = Join-Path $env:USERPROFILE 'AppData\Local'
$EvidenceParent = Join-Path $ExpectedLocalAppData 'AnyAiCam\InternalTestBuildEvidence'
if (-not (Test-Path -LiteralPath $EvidenceParent -PathType Container)) {
    New-Item -ItemType Directory -Path $EvidenceParent -Force | Out-Null
}
$RunStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
$RunId = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$commitPrefix = $ExpectedCommit.Substring(0, 7)
$EvidenceDir = Join-Path $EvidenceParent ("Windows-1.3.4.1-" + $commitPrefix + "-" + $RunStamp + "-" + $RunId)
if (Test-Path -LiteralPath $EvidenceDir) { throw 'Evidence directory collision; stop without retry.' }
New-Item -ItemType Directory -Path $EvidenceDir -ErrorAction Stop | Out-Null
$ReportPath = Join-Path $EvidenceDir 'run-report.json'
$SignatureLogPath = Join-Path $EvidenceDir 'signature-verification.log'

function Save-RunReport {
    $report = [ordered]@{
        schema = 1
        status = $script:RunStatus
        source_branch = $ExpectedBranch
        source_commit = $ExpectedCommit
        internal_test_only = $true
        signing_mode = 'Azure Artifact Signing via approved Dlib; no local certificate selection'
        checks = @($script:Checks)
        artifacts = @($script:Artifacts)
        safe_failure = $script:SafeFailure
        shared_test_lock = $script:LockStatus
        evidence_directory = $EvidenceDir
    }
    $json = $report | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText($ReportPath, $json)
}

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $script:Checks += [pscustomobject]@{
        name = $Name
        passed = $Passed
        detail = $Detail
        checked_utc = (Get-Date).ToUniversalTime().ToString('o')
    }
    if (-not $Passed) { throw ("Precondition failed: " + $Name) }
}

function Get-Sha256Lower {
    param([string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-AtomicDirectoryLock {
    param([Parameter(Mandatory=$true)][string]$Path)

    # Match Red's mkdir-based protocol. cmd.exe mkdir returns nonzero when the
    # directory already exists; unlike Directory.CreateDirectory(), this is an
    # atomic create attempt and never treats an existing lock as success.
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $env:ComSpec
    $startInfo.Arguments = '/d /c mkdir "' + $Path + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'Could not start atomic shared-lock acquisition.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $null = $stdoutTask.Result
        $null = $stderrTask.Result
        if ($process.ExitCode -ne 0) { throw 'Shared Dell test lock is held or could not be acquired; stop safely.' }
    }
    finally {
        $process.Dispose()
    }
}

function Acquire-SharedTestLock {
    param([Parameter(Mandatory=$true)][string]$Path)

    if (Test-Path -LiteralPath $Path) {
        throw 'Shared Dell test lock is already present; no build work was started.'
    }
    New-AtomicDirectoryLock -Path $Path

    # Successful atomic directory creation establishes ownership. The marker is
    # created exclusively so this runner can never overwrite another owner.
    $owner = 'codex windows-vms-1.3.4.1 internal-test ' + $ExpectedCommit + ' pid ' + $PID + ' utc ' + (Get-Date).ToUniversalTime().ToString('o') + ' id ' + [Guid]::NewGuid().ToString('N')
    $lease = [pscustomobject]@{ Path = $Path; Owner = $owner; OwnerPath = (Join-Path $Path 'owner') }
    $script:LockLease = $lease
    $script:LockStatus = 'ACQUIRED'

    $stream = $null
    try {
        $stream = [System.IO.File]::Open($lease.OwnerPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($owner + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    catch {
        # The directory was atomically created by this process. Release only if
        # it is still empty; otherwise preserve it for manual inspection.
        try {
            if ((Test-Path -LiteralPath $Path -PathType Container) -and @(Get-ChildItem -LiteralPath $Path -Force).Count -eq 0) {
                [System.IO.Directory]::Delete($Path, $false)
                $script:LockLease = $null
                $script:LockStatus = 'RELEASED_AFTER_MARKER_FAILURE'
            }
        } catch { }
        throw 'Could not create the shared-lock owner marker; no signing or build may proceed.'
    }
    finally {
        if ($stream) { $stream.Dispose() }
    }
    return $lease
}

function Release-SharedTestLock {
    param([Parameter(Mandatory=$true)]$Lease)

    if (-not $Lease -or -not $Lease.Path -or -not $Lease.Owner -or -not $Lease.OwnerPath) {
        throw 'Lock lease is incomplete; refusing cleanup.'
    }
    if (-not (Test-Path -LiteralPath $Lease.Path -PathType Container)) {
        throw 'Owned shared-lock directory is missing; refusing to claim successful cleanup.'
    }
    if (-not (Test-Path -LiteralPath $Lease.OwnerPath -PathType Leaf)) {
        throw 'Shared-lock owner marker is missing; ownership cannot be proven, so preserving the lock directory.'
    }
    $currentOwner = [System.IO.File]::ReadAllText($Lease.OwnerPath, [System.Text.Encoding]::UTF8).TrimEnd("`r", "`n")
    if ($currentOwner -cne $Lease.Owner) {
        throw 'Shared-lock owner marker changed; preserving the lock and all contents.'
    }

    $entries = @(Get-ChildItem -LiteralPath $Lease.Path -Force)
    foreach ($entry in $entries) {
        if ($entry.FullName -cne $Lease.OwnerPath) {
            throw 'Unexpected shared-lock content exists; preserving the lock and all contents.'
        }
    }
    if (Test-Path -LiteralPath $Lease.OwnerPath -PathType Leaf) {
        [System.IO.File]::Delete($Lease.OwnerPath)
    }
    [System.IO.Directory]::Delete($Lease.Path, $false)
    $script:LockLease = $null
    $script:LockStatus = 'RELEASED'
}

function Test-SharedTestLockOwned {
    param([Parameter(Mandatory=$true)]$Lease)
    if (-not $Lease -or -not (Test-Path -LiteralPath $Lease.Path -PathType Container) -or -not (Test-Path -LiteralPath $Lease.OwnerPath -PathType Leaf)) { return $false }
    $currentOwner = [System.IO.File]::ReadAllText($Lease.OwnerPath, [System.Text.Encoding]::UTF8).TrimEnd("`r", "`n")
    return $currentOwner -ceq $Lease.Owner
}

function Get-FileSignatureRecord {
    param([string]$Path, [string]$Label)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid') { throw ("Authenticode status is not Valid for " + $Label) }
    if (-not $signature.SignerCertificate) { throw ("No signer certificate for " + $Label) }
    $problem = Get-AnyAiCamPublisherProblem $signature $Path
    if ($problem) { throw ("Approved publisher verification failed for " + $Label) }
    if (-not $signature.TimeStamperCertificate) { throw ("No timestamp certificate for " + $Label) }

    $verifyOutput = @(& $SignToolPath verify /pa /all /v $Path 2>&1)
    $verifyExitCode = $LASTEXITCODE
    if ($verifyExitCode -ne 0) { throw ("SignTool verification failed for " + $Label) }
    Add-Content -LiteralPath $SignatureLogPath -Value ("[" + $Label + "] Authenticode=Valid; publisher=" + $signature.SignerCertificate.Subject + "; timestamp signer=" + $signature.TimeStamperCertificate.Subject + "; SignTool verify exit=" + $verifyExitCode)
    foreach ($line in $verifyOutput) {
        $text = [string]$line
        if ($text -match 'timestamp|verified|signer|certificate|success|SHA256|SHA-256') {
            Add-Content -LiteralPath $SignatureLogPath -Value $text
        }
    }
    return [ordered]@{
        path = $Path
        label = $Label
        sha256 = Get-Sha256Lower $Path
        size_bytes = (Get-Item -LiteralPath $Path).Length
        authenticode = [string]$signature.Status
        publisher = $signature.SignerCertificate.Subject
        timestamp_signer = $signature.TimeStamperCertificate.Subject
        signtool_verify_exit_code = $verifyExitCode
    }
}

try {
    $script:LockLease = Acquire-SharedTestLock -Path $LockPath
    Add-Check 'Shared Dell test lock acquired atomically' ((Test-SharedTestLockOwned $script:LockLease)) ('owned lock acquired: ' + $LockPath)
    Add-Check 'PowerShell 5.1' ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -eq 1) ([string]$PSVersionTable.PSVersion)
    $normalLocal = [System.IO.Path]::GetFullPath($ExpectedLocalAppData).TrimEnd('\')
    $actualLocal = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\')
    Add-Check 'Native user LocalAppData context' ($actualLocal -ieq $normalLocal -and $actualLocal -notmatch 'openai\.codex') $actualLocal

    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $availableRamGiB = [math]::Round(([double]$os.FreePhysicalMemory * 1KB) / 1GB, 2)
    $cDrive = Get-PSDrive -Name C -ErrorAction Stop
    $freeDiskGiB = [math]::Round(([double]$cDrive.Free) / 1GB, 2)
    Add-Check 'RAM minimum 12 GiB' ($availableRamGiB -ge 12) ("available GiB=" + $availableRamGiB)
    Add-Check 'C drive minimum 20 GiB' ($freeDiskGiB -ge 20) ("free GiB=" + $freeDiskGiB)

    $branch = (& git -C $Repo branch --show-current 2>$null | Out-String).Trim()
    $branchCode = $LASTEXITCODE
    $head = (& git -C $Repo rev-parse HEAD 2>$null | Out-String).Trim()
    $headCode = $LASTEXITCODE
    $statusLines = @(& git -C $Repo status --porcelain=v1 --untracked-files=all 2>$null)
    $statusCode = $LASTEXITCODE
    Add-Check 'Approved source branch' ($branchCode -eq 0 -and $branch -ceq $ExpectedBranch) $branch
    Add-Check 'Approved exact source commit' ($headCode -eq 0 -and $head -ceq $ExpectedCommit) $head
    Add-Check 'Clean Git worktree' ($statusCode -eq 0 -and $statusLines.Count -eq 0) ('porcelain entries=' + $statusLines.Count)
    $installerScript = Join-Path $Repo 'installer\windows\AnyAiCam-VMS.iss'
    . (Join-Path $Repo 'installer\windows\source-integrity.ps1')
    $sourceProblems = @(Get-PackagedSourceProblems $Repo $installerScript)
    Add-Check 'Packaged-source integrity including untracked/ignored paths' ($sourceProblems.Count -eq 0) ('source-integrity problems=' + $sourceProblems.Count)

    Add-Check 'Internal output directory fresh' (-not (Test-Path -LiteralPath $OutputDir)) $OutputDir
    Add-Check 'Build script present' (Test-Path -LiteralPath $BuildScript -PathType Leaf) $BuildScript
    $catalogScript = Join-Path $Repo 'installer\windows\runtime-catalog.ps1'
    $catalogText = Get-Content -LiteralPath $catalogScript -Raw
    $buildText = Get-Content -LiteralPath $BuildScript -Raw
    $pathLengthGuard = $catalogText.Contains('function Assert-MakeCatPathLengths') -and $catalogText.Contains('must be shorter than MAX_PATH (260)')
    $shortStageFlow = $buildText.Contains('AnyAiCamCatalog-') -and $buildText.Contains('Move-Item -LiteralPath $catalogWorkStage -Destination $catalogStage')
    Add-Check 'Approved MakeCat path-length correction' ($pathLengthGuard -and $shortStageFlow) 'short same-volume staging and MAX_PATH guard are present'

    $keysetPath = Join-Path $InputDir 'entitlement-signing-public-keys.json'
    $registryPath = Join-Path $InputDir 'entitlement-trust-registry.json'
    Add-Check 'Synthetic input directory is explicit DO-NOT-SHIP candidate' ($InputDir -match 'UNSIGNED-SYNTHETIC-KEYSET-DO-NOT-SHIP' -and (Test-Path -LiteralPath $InputDir -PathType Container)) $InputDir
    Add-Check 'Synthetic public keyset hash' ((Test-Path -LiteralPath $keysetPath -PathType Leaf) -and (Get-Sha256Lower $keysetPath) -ceq $ExpectedKeysetHash) $ExpectedKeysetHash
    Add-Check 'Synthetic trust registry hash' ((Test-Path -LiteralPath $registryPath -PathType Leaf) -and (Get-Sha256Lower $registryPath) -ceq $ExpectedRegistryHash) $ExpectedRegistryHash
    $keysetDoc = Get-Content -LiteralPath $keysetPath -Raw | ConvertFrom-Json
    $registryDoc = Get-Content -LiteralPath $registryPath -Raw | ConvertFrom-Json
    $keysetIds = @($keysetDoc.keys.PSObject.Properties.Name)
    $registryIds = @($registryDoc.keys.PSObject.Properties.Name)
    Add-Check 'Registry is synthetic and contains only approved test key' ($registryDoc.environment -ceq 'synthetic' -and $keysetIds.Count -eq 1 -and $registryIds.Count -eq 1 -and $keysetIds[0] -ceq 'sandbox-test-50296db437b2' -and $registryIds[0] -ceq $keysetIds[0]) ('environment=' + $registryDoc.environment + '; key-id=' + ($registryIds -join ','))

    $packageDir = Join-Path $ClientRoot 'package'
    $sumPath = Join-Path $ClientRoot 'SHA256SUMS.txt'
    Add-Check 'Artifact Signing package and checksum manifest present' ((Test-Path -LiteralPath $packageDir -PathType Container) -and (Test-Path -LiteralPath $sumPath -PathType Leaf)) $ClientRoot
    $manifestLines = @(Get-Content -LiteralPath $sumPath)
    $manifestEntries = @()
    $seen = @{}
    foreach ($line in $manifestLines) {
        if ($line -notmatch '^([0-9a-fA-F]{64})\s{2}(.+)$') { throw 'Signing-client checksum manifest has a malformed line.' }
        $expectedHash = $Matches[1].ToLowerInvariant()
        $relative = $Matches[2].Replace('/', '\')
if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|\\)\.\.(\\|$)' -or ($relative -ne 'source-package.nupkg' -and -not $relative.StartsWith('package\'))) { throw 'Signing-client manifest path is outside the approved package.' }
        if ($seen.ContainsKey($relative)) { throw 'Signing-client checksum manifest contains a duplicate path.' }
        $seen[$relative] = $true
        $filePath = Join-Path $ClientRoot $relative
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) { throw 'A signing-client manifest file is missing.' }
        $actualHash = Get-Sha256Lower $filePath
        if ($actualHash -cne $expectedHash) { throw 'A signing-client package hash does not match its manifest.' }
        $manifestEntries += [pscustomobject]@{ path = $relative; sha256 = $actualHash }
    }
    $packageFiles = @(Get-ChildItem -LiteralPath $packageDir -File -Recurse)
    $dlibPath = Join-Path $packageDir 'bin\x64\Azure.CodeSigning.Dlib.dll'
    $dlibHash = Get-Sha256Lower $dlibPath
    $nupkgHash = Get-Sha256Lower (Join-Path $ClientRoot 'source-package.nupkg')
    Add-Check 'Complete 41-file x64 client package verified' ($manifestEntries.Count -eq 42 -and @($manifestEntries | Where-Object {$_.path -like 'package\*'}).Count -eq 41 -and $packageFiles.Count -eq 41 -and $nupkgHash -ceq $ExpectedNupkgHash) ('manifest entries=' + $manifestEntries.Count + '; package files=' + $packageFiles.Count)
    Add-Check 'Pinned Dlib version, hash, and x64 architecture' ((Test-Path -LiteralPath $dlibPath -PathType Leaf) -and $dlibHash -ceq $ExpectedDlibHash -and [Diagnostics.FileVersionInfo]::GetVersionInfo($dlibPath).FileVersion -ceq '1.0.146.0') ('Dlib SHA-256=' + $dlibHash)
    $dlibSig = Get-AuthenticodeSignature -LiteralPath $dlibPath
    Add-Check 'Dlib Authenticode publisher' ($dlibSig.Status -eq 'Valid' -and $dlibSig.SignerCertificate.Subject.StartsWith($ExpectedMsSubjectPrefix)) ([string]$dlibSig.Status + '; Microsoft signer')
    $dlibBytes = [System.IO.File]::ReadAllBytes($dlibPath)
    $peOffset = [BitConverter]::ToInt32($dlibBytes, 0x3c)
    $peMachine = [BitConverter]::ToUInt16($dlibBytes, $peOffset + 4)
    Add-Check 'Dlib PE is x64' ($peMachine -eq 0x8664) ('PE machine=0x' + $peMachine.ToString('X4'))

    Add-Check 'Signing metadata exists' (Test-Path -LiteralPath $MetadataPath -PathType Leaf) $MetadataPath
    $metadata = Get-Content -LiteralPath $MetadataPath -Raw | ConvertFrom-Json
    $metadataOk = $metadata.CodeSigningAccountName -ceq 'anyaicamsigning' -and $metadata.CertificateProfileName -ceq 'AnyAICam' -and $metadata.Endpoint -ceq 'https://scus.codesigning.azure.net'
    Add-Check 'Approved Azure account/profile/endpoint metadata' $metadataOk 'metadata values matched; no credentials or secret fields logged'

    $signToolVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($SignToolPath)
    $signToolSig = Get-AuthenticodeSignature -LiteralPath $SignToolPath
    Add-Check 'Approved x64 SignTool' ((Test-Path -LiteralPath $SignToolPath -PathType Leaf) -and $signToolVersion.ProductVersion -ceq '10.0.26100.8249' -and $signToolSig.Status -eq 'Valid' -and $signToolSig.SignerCertificate.Subject.StartsWith($ExpectedMsSubjectPrefix)) ('product version=' + $signToolVersion.ProductVersion + '; signature=' + $signToolSig.Status)

    $innoRoot = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 7'
    $isccPath = Join-Path $innoRoot 'ISCC.exe'
    $innoCompanion = Join-Path $innoRoot 'unins000.exe'
    Add-Check 'Inno Setup compiler and signed companion exist' ((Test-Path -LiteralPath $isccPath -PathType Leaf) -and (Test-Path -LiteralPath $innoCompanion -PathType Leaf)) $innoRoot
    $isccSig = Get-AuthenticodeSignature -LiteralPath $isccPath
    $innoSig = Get-AuthenticodeSignature -LiteralPath $innoCompanion
    $innoVersion = ([Diagnostics.FileVersionInfo]::GetVersionInfo($innoCompanion).ProductVersion).Trim()
    $innoPublisherOk = $isccSig.Status -eq 'Valid' -and $innoSig.Status -eq 'Valid' -and $isccSig.SignerCertificate.Subject.StartsWith($ExpectedInnoPublisherPrefix) -and $innoSig.SignerCertificate.Subject.StartsWith($ExpectedInnoPublisherPrefix)
    Add-Check 'Inno Setup 7.1.0 signed identity' ($innoVersion -ceq '7.1.0' -and $innoPublisherOk) ('companion ProductVersion=' + $innoVersion + '; signatures valid')
    $pythonVersion = (& $PythonPath --version 2>&1 | Out-String).Trim()
    $pythonExit = $LASTEXITCODE
    Add-Check 'Pinned Python 3.12.10' ($pythonExit -eq 0 -and $pythonVersion -ceq 'Python 3.12.10' -and (Test-Path -LiteralPath $PythonPath -PathType Leaf)) $pythonVersion

    $publisherPinPath = Join-Path $Repo 'installer\windows\publisher-pin.ps1'
    $publisherPinText = Get-Content -LiteralPath $publisherPinPath -Raw
    $pinOk = $publisherPinText.Contains($ExpectedPublisherSubject) -and $publisherPinText.Contains($ExpectedPublisherEku) -and $publisherPinText.Contains($ExpectedPublisherRoot)
    Add-Check 'Approved AnyAiCam publisher pin present' $pinOk 'subject, identity EKU, and Microsoft root thumbprint matched'
    $pinDiffOutput = @(& git -C $Repo diff --quiet $BaseCommit $ExpectedCommit -- installer/windows/publisher-pin.ps1)
    $pinDiffExit = $LASTEXITCODE
    Add-Check 'Publisher pin unchanged from verified base' ($pinDiffExit -eq 0) 'publisher-pin.ps1 has no diff from fdddbe7'
    $buildText = Get-Content -LiteralPath $BuildScript -Raw
    $expectedGuard = '$registryEnvironment = if ($InternalTestAzureSign) { ''internal-test'' } elseif ($Sign -or $AzureSign) { ''production'' } else { ''any'' }'
    $productionGuard = $buildText.Contains($expectedGuard)
    $defaultOffGuard = $buildText.Contains('[switch]$InternalTestAzureSign') -and $buildText.Contains('if ($InternalTestAzureSign -and (Test-Path -LiteralPath $outputDir))')
    $isolatedGuard = $buildText.Contains("'output-internal-test'") -and $buildText.Contains('-INTERNAL-TEST-DO-NOT-SHIP')
    Add-Check 'Production signing guard and isolated default-off mode' ($productionGuard -and $defaultOffGuard -and $isolatedGuard) 'production Sign/AzureSign still require production registry; internal mode is explicit and isolated'

    $azCommand = Get-Command az -ErrorAction SilentlyContinue
    $azPath = $null
    if ($azCommand) { $azPath = $azCommand.Source; if (-not $azPath) { $azPath = $azCommand.Path } }
    Add-Check 'Azure CLI available' (-not [string]::IsNullOrWhiteSpace($azPath)) 'executable path resolved; path omitted from report'
    $expiryLines = @(& $azPath account get-access-token --resource 'https://codesigning.azure.net' --query expires_on --output tsv --only-show-errors 2>$null)
    $azExit = $LASTEXITCODE
    $expiryText = ($expiryLines | Select-Object -First 1 | Out-String).Trim()
    $expiryValid = $azExit -eq 0 -and $expiryText -match '^[0-9]{9,12}$'
    $expiryUtc = $null
    if ($expiryValid) {
        try { $expiryUtc = [DateTimeOffset]::FromUnixTimeSeconds([long]$expiryText).UtcDateTime } catch { $expiryValid = $false }
    }
    Add-Check 'Azure Code Signing token available without exposing it' ($expiryValid -and $expiryUtc -gt [DateTime]::UtcNow) ('token valid to UTC=' + $(if ($expiryUtc) { $expiryUtc.ToString('o') } else { 'unavailable' }))

    $script:Result = $null
    $finalHeadLines = @(& git -C $Repo rev-parse HEAD 2>$null)
    $finalHeadExitCode = $LASTEXITCODE
    $finalStatusLines = @(& git -C $Repo status --porcelain=v1 --untracked-files=all 2>$null)
    $finalStatusExitCode = $LASTEXITCODE
    $finalHead = ($finalHeadLines | Out-String).Trim()
    $finalGitClean = $finalHeadExitCode -eq 0 -and $finalStatusExitCode -eq 0 -and $finalHead -ceq $ExpectedCommit -and $finalStatusLines.Count -eq 0
    Add-Check 'Final source/owned-lock/output recheck' ($finalGitClean -and (Test-SharedTestLockOwned $script:LockLease) -and -not (Test-Path -LiteralPath $OutputDir)) 'both final Git commands succeeded, source identity is exact and clean, runner-owned lock is still held, and isolated output is absent immediately before build invocation'
    $script:RunStatus = 'PREFLIGHT_PASSED'
    Save-RunReport

    # Process-scoped signing overrides only. The build script owns catalog/setup/uninstaller signing.
    $env:ANYAICAM_AZURE_SIGNING_DLIB = $dlibPath
    $env:ANYAICAM_AZURE_SIGNING_METADATA = $MetadataPath
    $env:ANYAICAM_SIGNTOOL_PATH = $SignToolPath
    $env:ANYAICAM_TIMESTAMP_URL = 'http://timestamp.acs.microsoft.com'

    $buildParams = @{
        Version = '1.3.4.1'
        ReleaseVersion = '1.2.7'
        PythonExe = $PythonPath
        EntitlementKeyset = $keysetPath
        EntitlementKeysetSha256 = $ExpectedKeysetHash
        EntitlementTrustRegistry = $registryPath
        EntitlementTrustRegistrySha256 = $ExpectedRegistryHash
        InternalTestAzureSign = $true
    }
    $script:BuildStarted = $true
    $script:RunStatus = 'BUILD_AND_VERIFY_IN_PROGRESS'
    Save-RunReport
    & $BuildScript @buildParams
    if (-not $?) { throw 'The guarded build script reported failure; do not retry automatically.' }

    . (Join-Path $Repo 'installer\windows\publisher-pin.ps1')
    $expectedSetupName = 'AnyAiCam-VMS-Setup-1.3.4.1-' + $commitPrefix + '-INTERNAL-TEST-DO-NOT-SHIP.exe'
    $setupPath = Join-Path $OutputDir $expectedSetupName
    $catalogPath = Join-Path $OutputDir 'AnyAiCamRuntime.cat'
    $uninstallerPaths = @(Get-ChildItem -LiteralPath (Join-Path $OutputDir 'signed-uninstallers') -Filter '*.exe' -File | Select-Object -ExpandProperty FullName)
    if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) { throw 'Expected internal-test installer is missing.' }
    if (-not (Test-Path -LiteralPath $catalogPath -PathType Leaf)) { throw 'Signed runtime catalog is missing.' }
    if ($uninstallerPaths.Count -lt 1) { throw 'No generated signed uninstaller was produced.' }

    $setupInfo = [Diagnostics.FileVersionInfo]::GetVersionInfo($setupPath)
    if ($setupInfo.FileVersion -notmatch '^1\.3\.4\.1' -or $setupInfo.ProductVersion -notmatch '^1\.2\.7' -or $setupInfo.FileDescription -notmatch 'INTERNAL TEST') {
        throw 'Installer version or internal-test metadata verification failed.'
    }
    if ((Get-Item -LiteralPath $setupPath).Length -gt 750000000) { throw 'Installer exceeded the approved size limit.' }
    $script:Artifacts += Get-FileSignatureRecord $setupPath 'installer'
    $script:Artifacts += Get-FileSignatureRecord $catalogPath 'runtime-catalog'
    foreach ($uninstallerPath in $uninstallerPaths) {
        $script:Artifacts += Get-FileSignatureRecord $uninstallerPath 'signed-uninstaller'
    }

    $transcriptPath = Join-Path $OutputDir 'build-transcript.log'
    if (-not (Test-Path -LiteralPath $transcriptPath -PathType Leaf)) { throw 'Build transcript is missing.' }

    $buildEvidencePath = Join-Path $OutputDir 'build-evidence.json'
    $outputSumsPath = Join-Path $OutputDir 'SHA256SUMS'
    if (-not (Test-Path -LiteralPath $buildEvidencePath -PathType Leaf) -or -not (Test-Path -LiteralPath $outputSumsPath -PathType Leaf)) {
        throw 'Build evidence or output checksum manifest is missing.'
    }
    $buildEvidence = Get-Content -LiteralPath $buildEvidencePath -Raw | ConvertFrom-Json
    if ($buildEvidence.result -cne 'INTERNAL_TEST_BUILD_VERIFIED' -or $buildEvidence.source_commit -cne $ExpectedCommit -or $buildEvidence.source_branch -cne $ExpectedBranch -or $buildEvidence.trust_registry_mode -cne 'synthetic' -or $buildEvidence.runtime_catalog_native_files -lt 1) {
        throw 'Build evidence identity, mode, or catalog-coverage assertion failed.'
    }
    $outputEntries = @(Get-Content -LiteralPath $outputSumsPath)
    $seenOutput = @{}
    foreach ($line in $outputEntries) {
        if ($line -notmatch '^([0-9a-fA-F]{64})\s{2}(.+)$') { throw 'Build output checksum manifest is malformed.' }
        $expected = $Matches[1].ToLowerInvariant()
        $relative = $Matches[2].Replace('/', '\')
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|\\)\.\.(\\|$)' -or $relative -ceq 'SHA256SUMS') { throw 'Build output checksum path is outside the approved output.' }
        if ($seenOutput.ContainsKey($relative)) { throw 'Build output checksum manifest contains duplicate paths.' }
        $seenOutput[$relative] = $true
        $artifactPath = Join-Path $OutputDir $relative
        if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf) -or (Get-Sha256Lower $artifactPath) -cne $expected) {
            throw 'A build output artifact is missing or does not match SHA256SUMS.'
        }
    }
    $actualOutputFileCount = @(Get-ChildItem -LiteralPath $OutputDir -Recurse -File | Where-Object { $_.FullName -ne $outputSumsPath }).Count
    if ($outputEntries.Count -lt 1 -or $outputEntries.Count -ne $actualOutputFileCount) { throw 'Build output checksum manifest is empty or incomplete.' }

    $script:Artifacts += [ordered]@{
        path = $setupPath
        label = 'installer-identity'
        file_version = $setupInfo.FileVersion
        product_version = $setupInfo.ProductVersion
        file_description = $setupInfo.FileDescription
        size_bytes = (Get-Item -LiteralPath $setupPath).Length
        sha256 = Get-Sha256Lower $setupPath
    }
    $script:Artifacts += [ordered]@{
        path = $catalogPath
        label = 'runtime-catalog-coverage'
        native_files = $buildEvidence.runtime_catalog_native_files
        sha256 = Get-Sha256Lower $catalogPath
    }
    $script:RunStatus = 'BUILD_AND_VERIFICATION_PASSED'
    Save-RunReport

    $externalEvidenceFiles = @($ReportPath, $SignatureLogPath, $buildEvidencePath, $outputSumsPath, (Join-Path $OutputDir 'build-transcript.log'))
    $sumFile = Join-Path $EvidenceDir 'evidence-SHA256SUMS.txt'
    $evidenceLines = @()
    foreach ($path in $externalEvidenceFiles) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $hash = Get-Sha256Lower $path
            $evidenceLines += ($hash + '  ' + [System.IO.Path]::GetFileName($path))
        }
    }
    [System.IO.File]::WriteAllLines($sumFile, $evidenceLines)
}
catch {
    if ($script:BuildStarted) { $script:RunStatus = 'BUILD_OR_POSTBUILD_VERIFICATION_FAILED' }
    else { $script:RunStatus = 'STOPPED_BEFORE_BUILD' }
    $script:SafeFailure = 'Stopped; inspect run-report.json and, if build began, build-transcript.log. No automatic retry was performed.'
    try { Save-RunReport } catch { }
    throw
}
finally {
    if ($script:LockLease) {
        try {
            Release-SharedTestLock -Lease $script:LockLease
        }
        catch {
            $script:LockStatus = 'RELEASE_FAILED_LOCK_PRESERVED'
            $script:RunStatus = 'LOCK_RELEASE_FAILED'
            $script:SafeFailure = 'Lock release failed safely; the lock and contents were preserved for manual inspection. Do not retry or remove it automatically.'
            try { Save-RunReport } catch { }
            throw
        }
        try {
            Save-RunReport
            $externalEvidenceFiles = @($ReportPath, $SignatureLogPath, (Join-Path $OutputDir 'build-evidence.json'), (Join-Path $OutputDir 'SHA256SUMS'), (Join-Path $OutputDir 'build-transcript.log'))
            $sumFile = Join-Path $EvidenceDir 'evidence-SHA256SUMS.txt'
            $evidenceLines = @()
            foreach ($path in $externalEvidenceFiles) {
                if (Test-Path -LiteralPath $path -PathType Leaf) {
                    $hash = Get-Sha256Lower $path
                    $evidenceLines += ($hash + '  ' + [System.IO.Path]::GetFileName($path))
                }
            }
            [System.IO.File]::WriteAllLines($sumFile, $evidenceLines)
        }
        catch {
            $script:RunStatus = 'FINAL_EVIDENCE_SEAL_FAILED'
            $script:SafeFailure = 'The build path ended, but final post-lock evidence sealing failed; preserve the evidence directory and do not retry automatically.'
            try { Save-RunReport } catch { }
            throw
        }
    }
}

Write-Output ("INTERNAL TEST BUILD AND VERIFICATION PASSED. Evidence: " + $EvidenceDir)
Write-Output ("Installer SHA-256: " + (Get-Sha256Lower $setupPath))
Write-Output ("Installer size bytes: " + (Get-Item -LiteralPath $setupPath).Length)
