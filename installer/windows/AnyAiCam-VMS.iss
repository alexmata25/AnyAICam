#define AppName "AnyAiCam VMS"
; 2026-10-07: version and source commit come from build.ps1
; (/DAppVersion=... /DSourceCommit=...), never a stale literal. A bare ISCC
; run without them produces an obviously unusable "0.0.0-unset" setup name.
#ifndef AppVersion
  #define AppVersion "0.0.0-unset"
#endif
#ifndef SourceCommit
  #define SourceCommit "0000000000000000000000000000000000000000"
#endif
; 2026-10-20 (owner): the customer-facing AnyAiCam VMS release (1.2.7, the same release as Linux) is separate from
; this Windows setup's own version (AppVersion, 1.3.2): Windows was published as 1.3.0/1.3.1, and the downgrade guard
; orders setups by AppVersion, so it must keep rising. build.ps1 passes both. A bare ISCC run shows the setup version.
#ifndef ReleaseVersion
  #define ReleaseVersion AppVersion
#endif
#define ShortCommit Copy(SourceCommit, 1, 7)
#ifndef KeysetSha256
  #error KeysetSha256 is required: build with build.ps1 -EntitlementKeyset ... -EntitlementKeysetSha256 ...
#endif
[Setup]
AppId={{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}
AppName={#AppName}
AppVersion={#AppVersion}
; What the customer reads: the wizard title, Apps & Features / uninstall name, the setup file's Product version.
; DisplayVersion stays AppVersion (the downgrade guard reads it); the build ID is the full source commit.
AppVerName={#AppName} {#ReleaseVersion}
UninstallDisplayName={#AppName} {#ReleaseVersion}
VersionInfoProductName={#AppName}
VersionInfoProductTextVersion={#ReleaseVersion}
VersionInfoTextVersion={#AppVersion}
VersionInfoDescription={#AppName} {#ReleaseVersion} setup (Windows setup {#AppVersion}, build {#SourceCommit})
VersionInfoCompany=AnyAiCam
AppPublisher=AnyAiCam
DefaultDirName={autopf}\AnyAiCam
DefaultGroupName=AnyAiCam
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
OutputDir=output
OutputBaseFilename=AnyAiCam-VMS-Setup-{#AppVersion}-{#ShortCommit}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=no
#ifdef EnableSigning
SignTool=AnyAiCamSign
SignedUninstaller=yes
SignedUninstallerDir=output\signed-uninstallers
#endif
[Dirs]
Name: "{commonappdata}\AnyAiCam"; Flags: uninsneveruninstall
Name: "{commonappdata}\AnyAiCam\config"; Flags: uninsneveruninstall
Name: "{commonappdata}\AnyAiCam\database"; Flags: uninsneveruninstall
Name: "{commonappdata}\AnyAiCam\recordings"; Flags: uninsneveruninstall
Name: "{commonappdata}\AnyAiCam\hls"; Flags: uninsneveruninstall
Name: "{commonappdata}\AnyAiCam\logs"; Flags: uninsneveruninstall
Name: "{commonappdata}\AnyAiCam\mediamtx"; Flags: uninsneveruninstall
; Cloud agent state (credential, offline queue): kept on uninstall like the
; rest of the data, so a reinstall stays linked. (2026-10-08: no label folder;
; the permanent claim code is retired, cloud-link.ps1.)
Name: "{commonappdata}\AnyAiCam\agent"; Flags: uninsneveruninstall
[InstallDelete]
; An upgrade replaces the program code completely, so modules removed from the
; VMS never linger next to the new ones. Data lives in {commonappdata}\AnyAiCam.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\agent"
; 2026-10-08: the claim code is no longer shown; an upgrade from 1.3.0-aedecb6
; removes that release's "Show AnyAiCam claim code" shortcut.
Type: files; Name: "{group}\Show AnyAiCam claim code.lnk"
[Files]
Source: "..\..\app\*"; DestDir: "{app}\app"; Excludes: "tests\*,__pycache__\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "requirements-windows.txt"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "service-launcher.ps1"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "stop-stale-vms.ps1"; DestDir: "{app}\service"; Flags: ignoreversion
; 1.3.4: the VMS Job Object (CPU cap, optional memory limit) the launcher applies before starting Python.
Source: "vms-job-limits.ps1"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "install-runtime.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "gateway-edition.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "python-launchers.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "data-root-guard.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
; Stops processes still running from {app} after the services are removed (PrepareToInstall from {tmp}; the
; uninstaller from {app}\installer).
Source: "stop-app-processes.ps1"; Flags: dontcopy
Source: "stop-app-processes.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
; Run from {tmp} by InitializeSetup before anything is installed (downgrade / unknown-version guard).
Source: "install-version-guard.ps1"; Flags: dontcopy
Source: "output\entitlement-signing-public-keys.json"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "firewall.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "runtime-preflight.py"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "AnyAiCamVMS.xml"; DestDir: "{app}\service"; Flags: ignoreversion
; Cloud agent (2026-10-07): the same appliance agent as the Linux appliance,
; run on the bundled Python as the AnyAiCamAgent service.
Source: "..\..\appliance-agent\anyaicam_agent\*"; DestDir: "{app}\agent\anyaicam_agent"; Excludes: "__pycache__\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "agent-main.py"; DestDir: "{app}\agent"; Flags: ignoreversion
; Windows step 4 (signed capacity acceptance, Codex 07e934a ported): the Windows helper reuses the Linux helper's pure
; signed-format / identity / expiry / replay policy; vms_capacity.apply_sync() runs it from {app}\agent\system.
Source: "..\..\appliance-agent\system\capacity_accept.py"; DestDir: "{app}\agent\system"; Flags: ignoreversion
Source: "..\..\appliance-agent\system\capacity_accept_windows.py"; DestDir: "{app}\agent\system"; Flags: ignoreversion
Source: "agent-launcher.ps1"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "AnyAiCamAgent.xml"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "vendor\WinSW-x64.exe"; DestDir: "{app}\service"; DestName: "AnyAiCamAgent.exe"; Flags: ignoreversion
Source: "cloud-link.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
; 2026-10-08: the signed catalog of the runtime's native files (build.ps1,
; runtime-catalog.ps1); Smart App Control blocks them without it.
Source: "runtime-catalog.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "publisher-pin.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "output\AnyAiCamRuntime.cat"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "open-claim-link.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "vendor\WinSW-x64.exe"; DestDir: "{app}\service"; DestName: "AnyAiCamVMS.exe"; Flags: ignoreversion
Source: "vendor\python-3.12.10-embed-amd64.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "vendor\get-pip.py"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "vendor\wheels\*"; DestDir: "{tmp}\wheels"; Flags: deleteafterinstall recursesubdirs createallsubdirs
Source: "vendor\ffmpeg-8.1.2-essentials_build.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "vendor\mediamtx_v1.21.0_windows_amd64.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
; 1.3.4: the Tesseract OCR engine for licence plate reading (prepare-tesseract-bundle.py); install-runtime.ps1 verifies
; every file against the bundle's manifest and installs the bundled licences to {app}\licenses\tesseract.
Source: "vendor\tesseract-5.5.2-win64-anyaicam.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
; 1.3.2: Voice Call listening -- the Linux images' pinned Vosk small English model (Apache-2.0), extracted and verified
; file by file by install-stt-model.ps1 into %ProgramData%\AnyAiCam\models\stt; its licence notice ships with the program.
Source: "vendor\vosk-model-small-en-us-0.15.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "install-stt-model.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "vosk-model-small-en-us-0.15.sha256"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "notices\vosk-model-small-en-us-0.15-NOTICE.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion
; Microsoft Visual C++ 2015-2022 runtime (x64): torch/cv2 DLLs need it and a
; clean Windows does not have it. Installed before the Python runtime; left in
; place on uninstall (a shared system component other programs may use).
Source: "vendor\vc_redist.x64-14.44.35211.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
[Icons]
Name: "{group}\Open AnyAiCam VMS"; Filename: "http://127.0.0.1:8000"
Name: "{commondesktop}\AnyAiCam VMS"; Filename: "http://127.0.0.1:8000"
; 2026-10-08: asks the agent for a fresh, one-time account link and opens it
; without showing any code; only an administrator of this PC can
; (open-claim-link.ps1 elevates; the agent's request folder is
; Administrators-only).
Name: "{group}\Link this computer to AnyAiCam"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\installer\open-claim-link.ps1"""
[Run]
; The claim page with a fresh one-time code in the URL fragment (never sent to
; a server; /claim moves it into the tab and out of the address bar); the setup
; asked the agent for it at the end of the install (RequestAccountLink).
; runasoriginaluser: the browser opens as the person who started the setup,
; never elevated. The link is a PowerShell parameter, not the Filename, so
; dontlogparameters keeps the code out of the setup log (Inno logs every
; [Run] Filename and, unless told not to, its Parameters). Both entries are
; skipped in a silent install.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command ""Start-Process -FilePath '{code:ClaimLink}'"""; Description: "Link this computer to my AnyAiCam account"; Flags: postinstall runasoriginaluser runhidden nowait dontlogparameters skipifsilent; Check: HasClaimLink
Filename: "http://127.0.0.1:8000"; Description: "Open AnyAiCam VMS"; Flags: postinstall shellexec runasoriginaluser skipifsilent unchecked
[UninstallRun]
Filename: "{app}\service\AnyAiCamAgent.exe"; Parameters: "stopwait"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAnyAiCamAgent"
Filename: "{app}\service\AnyAiCamAgent.exe"; Parameters: "uninstall"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveAnyAiCamAgent"
Filename: "{app}\service\AnyAiCamVMS.exe"; Parameters: "stopwait"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAnyAiCamVMS"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -Command ""Start-Sleep -Seconds 3"""; Flags: runhidden waituntilterminated; RunOnceId: "WaitForAnyAiCamVMS"
Filename: "{app}\service\AnyAiCamVMS.exe"; Parameters: "uninstall"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveAnyAiCamVMS"
; 1.3.2: MediaMTX / FFmpeg children outlive the service; stop them so the program folder can be removed.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\installer\stop-app-processes.ps1"" -InstallRoot ""{app}"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopAnyAiCamFolderProcesses"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\installer\firewall.ps1"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAnyAiCamFirewall"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\installer\runtime-catalog.ps1"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAnyAiCamRuntimeCatalog"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  ProductModePage: TInputOptionWizardPage;
  GatewayEditionPage: TInputOptionWizardPage;

// Requirement 9 (1.2.7): Local or Hybrid. An existing installation keeps its saved mode (no page, nothing passed).
function ExistingInstall(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\AnyAiCam\config\vms.env'));
end;

// Downgrade and unknown-version protection (Red 2026-10-18; made fail-closed after Codex's FAIL on ef93b503).
// install-version-guard.ps1 decides from this AppId's uninstall entry (both registry views), the data folder's vms.env
// (ANYAICAM_VERSION, which survives an uninstall) and the install folder. Setup continues ONLY when the guard exits 0
// AND printed its own "install-version-guard: exit 0:" line (a script that fails to parse can exit 0 silently): a
// clean install, a same-version repair or an upgrade from an older version. A newer recorded version, a missing or
// malformed installed version, a malformed setup version, a PowerShell that cannot start, or any other result stops
// the setup before anything is changed. A silent install ends with Setup's initialization failure exit code; the
// guard's own explanation is in the setup log. There is no override.
const
  VersionGuardDowngrade = 10;
  VersionGuardUnknownInstalled = 11;

function InitializeSetup(): Boolean;
var
  ResultCode, I: Integer;
  Output: TExecOutput;
  Allowed: Boolean;
  Message: String;
begin
  Result := False;
  ResultCode := -1;
  Allowed := False;
  ExtractTemporaryFile('install-version-guard.ps1');
  if not ExecAndCaptureOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\install-version-guard.ps1') +
      '" -SetupVersion "{#AppVersion}" -DefaultAppDir "' + ExpandConstant('{autopf}\AnyAiCam') + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode, Output) then
    ResultCode := -1;
  for I := 0 to GetArrayLength(Output.StdOut) - 1 do begin
    Log(Output.StdOut[I]);
    if Pos('install-version-guard: exit 0: ', Output.StdOut[I]) = 1 then Allowed := True;
  end;
  for I := 0 to GetArrayLength(Output.StdErr) - 1 do
    Log(Output.StdErr[I]);
  if (ResultCode = 0) and Allowed then begin
    Result := True;
    exit;
  end;
  if ResultCode = 0 then ResultCode := -1;
  case ResultCode of
    VersionGuardDowngrade: begin
      Log('Downgrade refused: a newer AnyAiCam VMS is installed; this setup is {#AppVersion} (release {#ReleaseVersion}).');
      Message := 'A newer AnyAiCam VMS is already installed on this computer. This setup (AnyAiCam VMS {#ReleaseVersion}, ' +
        'Windows setup {#AppVersion}) cannot replace it. Download the current AnyAiCam setup from your AnyAiCam account.';
    end;
    VersionGuardUnknownInstalled: begin
      Log('Install refused: the installed AnyAiCam VMS version is missing or not valid; this setup is {#AppVersion} (release {#ReleaseVersion}).');
      Message := 'AnyAiCam VMS is already installed on this computer, but its version cannot be determined. ' +
        'Setup has stopped without changing anything, so that a newer version is not replaced. ' +
        'Contact AnyAiCam support.';
    end;
  else
    Log('Install refused: the AnyAiCam version check did not pass (result ' + IntToStr(ResultCode) + ').');
    Message := 'Setup could not confirm that it is safe to install AnyAiCam VMS {#ReleaseVersion} on this computer ' +
      'and has stopped without changing anything. Contact AnyAiCam support.';
  end;
  if not WizardSilent() then
    MsgBox(Message, mbError, MB_OK);
end;

procedure InitializeWizard;
begin
  GatewayEditionPage := CreateInputOptionPage(wpSelectDir, 'Choose your AnyAiCam edition',
    'One Windows VMS, with edition-specific cloud video controls.',
    'All editions keep camera discovery, local recording, playback, notifications and licensing. ' +
    'Gateway selection does not enable cloud uploads. Link once online to activate cameras.', True, False);
  GatewayEditionPage.Add('Full VMS: existing Local or Hybrid configuration');
  GatewayEditionPage.Add('Local Gateway: local video; no cloud video uploads');
  GatewayEditionPage.Add('Cloud Gateway: eligible event clips only; uploads require explicit configuration and a plan');
  GatewayEditionPage.SelectedValueIndex := 0;
  // Supported first-time activation (1.3.2, Codex FAIL on ef93b503): cameras run only after this PC is linked to an
  // AnyAiCam account ONCE, online (the cloud's first signed camera capacity). After that the accepted capacity has no
  // expiry, so Local cameras, recording and live view keep working without internet. Never claim fully offline
  // (air-gapped) activation: there is no approved offline licence yet (docs/windows-1.3.2-local-licensing-proposal.md).
  ProductModePage := CreateInputOptionPage(GatewayEditionPage.ID, 'How will you use AnyAiCam?',
    'You can change this later from your AnyAiCam account.',
    'Choose the mode for this computer, then click Next.' + #13#10#13#10 +
    'Both modes need this computer linked to your AnyAiCam account once, with an internet connection, before your ' +
    'cameras start. After that, Local mode keeps recording and live view working on this computer without internet.',
    True, False);
  ProductModePage.Add('Local: cameras, recording and live view stay on this computer (link once online to activate)');
  ProductModePage.Add('Hybrid: adds Cloud Video Backup and remote features (needs an AnyAiCam plan)');
  ProductModePage.SelectedValueIndex := 0;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ProductModePage.ID) and (ExistingInstall() or (ExpandConstant('{param:ProductMode|}') <> ''));
  if PageID = GatewayEditionPage.ID then
    Result := ExistingInstall() or (ExpandConstant('{param:GatewayEdition|}') <> '');
  if (PageID = ProductModePage.ID) and
     ((GatewayEditionPage.SelectedValueIndex > 0) or
      (ExpandConstant('{param:GatewayEdition|}') = 'local_gateway') or
      (ExpandConstant('{param:GatewayEdition|}') = 'cloud_gateway')) then Result := True;
end;

function GatewayEditionChoice(): String;
begin
  Result := Lowercase(ExpandConstant('{param:GatewayEdition|}'));
  if (Result = '') and not ExistingInstall() and not WizardSilent() then
    case GatewayEditionPage.SelectedValueIndex of
      0: Result := 'full_vms';
      1: Result := 'local_gateway';
      2: Result := 'cloud_gateway';
    end;
end;

// What install-runtime.ps1 receives. A silent NEW install without /ProductMode= passes nothing, so it is refused
// there -- the same rule as the Linux unattended install (never a silent default).
function ProductModeChoice(): String;
begin
  Result := Lowercase(ExpandConstant('{param:ProductMode|}'));
  if (Result = '') and not ExistingInstall() then begin
    if GatewayEditionChoice() = 'local_gateway' then Result := 'local';
    if GatewayEditionChoice() = 'cloud_gateway' then Result := 'hybrid';
  end;
  if (Result = '') and not ExistingInstall() and not WizardSilent() then
  begin
    if ProductModePage.SelectedValueIndex = 1 then Result := 'hybrid' else Result := 'local';
  end;
end;

var VcRuntimeNeedsRestart: Boolean;

procedure ExecRequired(const FileName, Parameters, Description: String);
var ResultCode: Integer;
begin
  WizardForm.StatusLabel.Caption := Description;
  // Output goes to the setup log, so a failure there can be diagnosed.
  if (not ExecAndLogOutput(FileName, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode, nil)) or (ResultCode <> 0) then
    RaiseException(Description + ' failed with exit code ' + IntToStr(ResultCode) + '.');
end;

// Microsoft's documented results: 0 installed, 1638 this or a newer version is
// already installed, 3010 installed but Windows must restart to finish. Any
// other result is a real failure and stops the setup before the service exists.
procedure InstallVcRuntime;
var ResultCode: Integer;
begin
  WizardForm.StatusLabel.Caption := 'Installing the Microsoft Visual C++ runtime...';
  if not Exec(ExpandConstant('{tmp}\vc_redist.x64-14.44.35211.exe'), '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RaiseException('Could not start the Microsoft Visual C++ runtime installer: ' + SysErrorMessage(ResultCode));
  Log('Microsoft Visual C++ runtime installer exit code: ' + IntToStr(ResultCode));
  case ResultCode of
    0, 1638: ;
    3010: VcRuntimeNeedsRestart := True;
  else
    RaiseException('Installing the Microsoft Visual C++ runtime failed with exit code ' + IntToStr(ResultCode) + '.');
  end;
end;

function NeedRestart(): Boolean;
begin
  Result := VcRuntimeNeedsRestart;
end;

// The saved Local/Hybrid mode in vms.env (ANYAICAM_PRODUCT_MODE, else ANYAICAM_PRODUCT_MODE_BOOTSTRAP: the same order
// as install-runtime.ps1). False when vms.env is missing or cannot be read (install-runtime then decides, after its
// permission repair).
function SavedProductMode(var Mode: String): Boolean;
var
  Lines: TArrayOfString;
  I, Pass: Integer;
  Key: String;
begin
  Mode := '';
  Result := LoadStringsFromFile(ExpandConstant('{commonappdata}\AnyAiCam\config\vms.env'), Lines);
  if not Result then exit;
  for Pass := 0 to 1 do begin
    if Pass = 0 then Key := 'ANYAICAM_PRODUCT_MODE=' else Key := 'ANYAICAM_PRODUCT_MODE_BOOTSTRAP=';
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos(Key, Lines[I]) = 1 then Mode := Copy(Lines[I], Length(Key) + 1, Length(Lines[I]));
    if Mode <> '' then exit;
  end;
end;

// 1.3.2 Sandbox acceptance (2026-10-20): the product-mode rules are checked HERE, before any file is copied and before
// the services are stopped. install-runtime.ps1 enforces them too, but only after both services were already
// uninstalled, so a refused mode switch used to leave the PC with no running VMS (setup exit 20).
// The rules and messages are exactly install-runtime.ps1's (and Linux installer/03-product-mode.sh):
//   a requested mode must be exactly local or hybrid; a saved mode must be exactly local or hybrid; Setup never switches it.
function ProductModeProblem(): String;
var
  Requested, Saved: String;
begin
  Result := '';
  Requested := ProductModeChoice();
  if (Requested <> '') and (Requested <> 'local') and (Requested <> 'hybrid') then begin
    Result := 'The product mode must be exactly local or hybrid.';
    exit;
  end;
  if not ExistingInstall() then begin
    if ((GatewayEditionChoice() = 'local_gateway') and (Requested <> 'local')) or
       ((GatewayEditionChoice() = 'cloud_gateway') and (Requested <> 'hybrid')) then begin
      Result := 'The requested product mode conflicts with the Gateway edition.';
      exit;
    end;
  end;
  if not SavedProductMode(Saved) or (Saved = '') then exit;
  if (Saved <> 'local') and (Saved <> 'hybrid') then
    Result := 'The product mode saved on this computer (' + Saved + ') is not exactly local or hybrid; fix it before installing.'
  else if (Requested <> '') and (Requested <> Saved) then
    Result := 'Setup cannot switch the existing product mode (' + Saved + '); change it from your AnyAiCam account instead.';
end;

function GatewayEditionProblem(): String;
var
  Requested, Saved, Candidate: String;
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  Requested := GatewayEditionChoice();
  if (Requested <> '') and (Requested <> 'full_vms') and
     (Requested <> 'local_gateway') and (Requested <> 'cloud_gateway') then begin
    Result := 'The edition must be full_vms, local_gateway or cloud_gateway.';
    exit;
  end;
  Saved := '';
  if ExistingInstall() then begin
    if not LoadStringsFromFile(ExpandConstant('{commonappdata}\AnyAiCam\config\vms.env'), Lines) then begin
      // Blue's legacy repair path fixes old data ACLs in install-runtime.ps1.
      // With no requested edition there is no selection to switch; preserve it.
      if Requested <> '' then Result := 'Cannot read the saved edition; repair access before installing.';
      exit;
    end;
    for I := 0 to GetArrayLength(Lines) - 1 do begin
      Candidate := 'ANYAICAM_GATEWAY_EDITION=';
      if Pos(Candidate, Lines[I]) = 1 then Saved := Copy(Lines[I], Length(Candidate) + 1, Length(Lines[I]));
    end;
  end;
  if (Saved <> '') and (Saved <> 'full_vms') and
     (Saved <> 'local_gateway') and (Saved <> 'cloud_gateway') then
    Result := 'Invalid saved Gateway edition; fix it before installing.'
  else if (Saved <> '') and (Requested <> '') and (Saved <> Requested) then
    Result := 'Setup cannot switch the saved Gateway edition.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  ServiceExecutable: String;
begin
  Result := GatewayEditionProblem();
  if Result <> '' then begin
    Log('Install refused before any change: ' + Result);
    exit;
  end;
  Result := ProductModeProblem();
  if Result <> '' then begin
    Log('Install refused before any change: ' + Result);
    exit;
  end;
  // The agent talks to the VMS: stop and remove it first. (No service
  // dependency: the agent restarts the VMS on request, and stopping a
  // service stops its dependents -- the agent itself.)
  // `stopwait`, not `stop`: WinSW's `stop` returns before the service has
  // stopped, and a still-running wrapper cannot be replaced -- the Sandbox
  // reinstall of 1.3.0-4a90e4c aborted on AnyAiCamAgent.exe (exit 5).
  ServiceExecutable := ExpandConstant('{app}\service\AnyAiCamAgent.exe');
  if FileExists(ServiceExecutable) then
  begin
    Exec(ServiceExecutable, 'stopwait', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(2000);
    Exec(ServiceExecutable, 'uninstall', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  ServiceExecutable := ExpandConstant('{app}\service\AnyAiCamVMS.exe');
  if FileExists(ServiceExecutable) then
  begin
    Exec(ServiceExecutable, 'stopwait', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(3000);
    Exec(ServiceExecutable, 'uninstall', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
  // 1.3.2 Sandbox acceptance (P-H): the VMS's child processes (MediaMTX live view, FFmpeg) outlive the service and kept
  // runtime files locked, so a Hybrid reinstall/upgrade failed (exit 20). Stop whatever still runs from this folder.
  if DirExists(ExpandConstant('{app}')) then
  begin
    ExtractTemporaryFile('stop-app-processes.ps1');
    ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\stop-app-processes.ps1') +
      '" -InstallRoot "' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode, nil);
  end;
end;

// Inno Setup reports a failure raised during ssPostInstall to the user but
// still ends with exit code 0, so a silent install, reinstall or upgrade that
// stopped before the service existed looked successful. Such a setup now
// exits with PostInstallFailedExitCode (outside Inno's own codes 1-8).
const PostInstallFailedExitCode = 20;
var PostInstallFailed: Boolean;

function GetCustomSetupExitCode(): Integer;
begin
  if PostInstallFailed then
    Result := PostInstallFailedExitCode
  else
    Result := 0;
end;

// ------------------------------------------------------------ account link
// 2026-10-08: no claim code is ever shown, stored or logged by the setup.
// At the end of an interactive install the setup (elevated) asks the agent
// for a fresh, one-time link through open-claim-link.ps1 -Prepare, which
// writes "<status>\n<link>" into the setup's own temporary folder; it is read
// and deleted at once and kept only in AccountLink. The finish page's [Run]
// entry hands it to the browser of the person who ran the setup
// (runasoriginaluser, dontlogparameters).
var
  AccountLink: String;
  AccountLinkStatus: String;

// https://<cloud>/claim#code=XXXXXXXX&link=windows and nothing else: it is
// put inside single quotes on a PowerShell command line.
function IsAccountLink(const Link: String): Boolean;
var
  I, Code: Integer;
begin
  Result := False;
  if (Pos('https://', Link) <> 1) or (Length(Link) > 200) then Exit;
  Code := Pos('/claim#code=', Link);
  if (Code = 0) or (Length(Link) <> Code + Length('/claim#code=') + 8 + Length('&link=windows') - 1) then Exit;
  if Copy(Link, Length(Link) - Length('&link=windows') + 1, Length('&link=windows')) <> '&link=windows' then Exit;
  for I := 1 to Length(Link) do
    if Pos(Copy(Link, I, 1), 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.:/-#=&') = 0 then Exit;
  Result := True;
end;

procedure RequestAccountLink;
var
  ResultFile: String;
  Lines: TArrayOfString;
  ResultCode: Integer;
begin
  AccountLink := '';
  AccountLinkStatus := 'unavailable';
  ResultFile := ExpandConstant('{tmp}\anyaicam-account-link.txt');
  DeleteFile(ResultFile);
  WizardForm.StatusLabel.Caption := 'Preparing the link to your AnyAiCam account...';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + ExpandConstant('{app}\installer\open-claim-link.ps1') + '" -Prepare "' + ResultFile + '"',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then Exit;
  if LoadStringsFromFile(ResultFile, Lines) and (GetArrayLength(Lines) >= 1) then
  begin
    AccountLinkStatus := Trim(Lines[0]);
    if (AccountLinkStatus = 'ok') and (GetArrayLength(Lines) >= 2) and IsAccountLink(Trim(Lines[1])) then
      AccountLink := Trim(Lines[1])
    else if AccountLinkStatus = 'ok' then
      AccountLinkStatus := 'unavailable';
  end;
  DeleteFile(ResultFile);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  try
    // Order matters: VC++ runtime, then the Python runtime and its preflight
    // (install-runtime.ps1), and only then the service.
    InstallVcRuntime;
    ExecRequired(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installer\install-runtime.ps1') + '" -InstallRoot "' + ExpandConstant('{app}') + '" -DataRoot "' + ExpandConstant('{commonappdata}\AnyAiCam') + '" -SourceCommit "{#SourceCommit}" -PythonArchive "' + ExpandConstant('{tmp}\python-3.12.10-embed-amd64.zip') + '" -GetPipScript "' + ExpandConstant('{tmp}\get-pip.py') + '" -WheelRoot "' + ExpandConstant('{tmp}\wheels') + '" -FFmpegArchive "' + ExpandConstant('{tmp}\ffmpeg-8.1.2-essentials_build.zip') + '" -MediaMtxArchive "' + ExpandConstant('{tmp}\mediamtx_v1.21.0_windows_amd64.zip') + '" -TesseractArchive "' + ExpandConstant('{tmp}\tesseract-5.5.2-win64-anyaicam.zip') + '" -SttModelArchive "' + ExpandConstant('{tmp}\vosk-model-small-en-us-0.15.zip') + '" -AppVersion "{#AppVersion}" -ReleaseVersion "{#ReleaseVersion}" -PortalUrl "' + ExpandConstant('{param:PortalUrl|}') + '" -RuntimeCatalog "' + ExpandConstant('{tmp}\AnyAiCamRuntime.cat') + '" -EntitlementKeyset "' + ExpandConstant('{tmp}\entitlement-signing-public-keys.json') + '" -EntitlementKeysetSha256 "{#KeysetSha256}" -ProductMode "' + ProductModeChoice() + '" -GatewayEdition "' + GatewayEditionChoice() + '"', 'Installing AnyAiCam private runtime and dependencies...');
    ExecRequired(ExpandConstant('{app}\service\AnyAiCamVMS.exe'), 'install', 'Installing the AnyAiCam Windows service...');
    ExecRequired(ExpandConstant('{app}\service\AnyAiCamVMS.exe'), 'start', 'Starting the AnyAiCam Windows service...');
    ExecRequired(ExpandConstant('{app}\service\AnyAiCamAgent.exe'), 'install', 'Installing the AnyAiCam cloud agent service...');
    ExecRequired(ExpandConstant('{app}\service\AnyAiCamAgent.exe'), 'start', 'Starting the AnyAiCam cloud agent service...');
    // Interactive installs only: a silent install opens no browser, so it
    // asks for no link (and opens no claim).
    if not WizardSilent then
      RequestAccountLink;
  except
    PostInstallFailed := True;
    Log('AnyAiCam setup failed; exit code will be ' + IntToStr(PostInstallFailedExitCode) + ': ' + GetExceptionMessage);
    // Re-raised so an interactive install still shows the error.
    RaiseException(GetExceptionMessage);
  end;
end;

// ------------------------------------------------------------ claim code
function ClaimLink(Param: String): String;
begin
  Result := AccountLink;
end;

// Linked once the agent holds its cloud credential (a repair, upgrade or
// reinstall of a linked PC offers no link).
function IsLinked(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\AnyAiCam\agent\credential.json'));
end;

function HasClaimLink(): Boolean;
begin
  Result := (not PostInstallFailed) and (not IsLinked) and (AccountLink <> '');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID <> wpFinished) or PostInstallFailed then Exit;
  if IsLinked or (AccountLinkStatus = 'linked') then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'This computer is linked to your AnyAiCam account.'
  else if HasClaimLink then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'Your cameras start once this computer is linked to your AnyAiCam account (needed once, with an internet ' +
      'connection). Keep "Link this computer to my AnyAiCam account" ticked and click Finish. ' +
      'Your browser opens AnyAiCam: sign in (or create an account), choose a site and confirm. There is no code to type.' + #13#10#13#10 +
      'After this first link, Local mode keeps recording without internet. ' +
      'Later: Start menu > AnyAiCam > Link this computer to AnyAiCam (needs an administrator of this PC).'
  else
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'Your cameras start once this computer is linked to your AnyAiCam account (needed once, with an internet ' +
      'connection). When this PC is online: Start menu > AnyAiCam > Link this computer to AnyAiCam (needs an ' +
      'administrator of this PC). After this first link, Local mode keeps recording without internet.';
end;
