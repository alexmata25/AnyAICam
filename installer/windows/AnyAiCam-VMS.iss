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
#define ShortCommit Copy(SourceCommit, 1, 7)
; 2026-10-08: the entitlement keyset's digest from build.ps1 (empty: none).
#ifndef EntitlementKeysetSha256
  #define EntitlementKeysetSha256 ""
#endif
[Setup]
AppId={{E7B7D8B3-2EE7-4A24-8B02-F6DFA8D99B38}
AppName={#AppName}
AppVersion={#AppVersion}
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
Source: "install-runtime.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "firewall.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "runtime-preflight.py"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "AnyAiCamVMS.xml"; DestDir: "{app}\service"; Flags: ignoreversion
; Cloud agent (2026-10-07): the same appliance agent as the Linux appliance,
; run on the bundled Python as the AnyAiCamAgent service.
Source: "..\..\appliance-agent\anyaicam_agent\*"; DestDir: "{app}\agent\anyaicam_agent"; Excludes: "__pycache__\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "agent-main.py"; DestDir: "{app}\agent"; Flags: ignoreversion
Source: "agent-launcher.ps1"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "AnyAiCamAgent.xml"; DestDir: "{app}\service"; Flags: ignoreversion
Source: "vendor\WinSW-x64.exe"; DestDir: "{app}\service"; DestName: "AnyAiCamAgent.exe"; Flags: ignoreversion
Source: "cloud-link.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
; 2026-10-08: the signed catalog of the runtime's native files (build.ps1,
; runtime-catalog.ps1); Smart App Control blocks them without it.
Source: "runtime-catalog.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "output\AnyAiCamRuntime.cat"; DestDir: "{tmp}"; Flags: deleteafterinstall
; 2026-10-08: the entitlement trust anchor (entitlement-keyset.ps1).
Source: "entitlement-keyset.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "output\entitlement_signing_keys.json"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist
Source: "open-claim-link.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "vendor\WinSW-x64.exe"; DestDir: "{app}\service"; DestName: "AnyAiCamVMS.exe"; Flags: ignoreversion
Source: "vendor\python-3.12.10-embed-amd64.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "vendor\get-pip.py"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "vendor\wheels\*"; DestDir: "{tmp}\wheels"; Flags: deleteafterinstall recursesubdirs createallsubdirs
Source: "vendor\ffmpeg-8.1.2-essentials_build.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "vendor\mediamtx_v1.21.0_windows_amd64.zip"; DestDir: "{tmp}"; Flags: deleteafterinstall
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
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\installer\firewall.ps1"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAnyAiCamFirewall"
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\installer\runtime-catalog.ps1"" -Remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAnyAiCamRuntimeCatalog"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
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

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  ServiceExecutable: String;
begin
  Result := '';
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
    ExecRequired(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installer\install-runtime.ps1') + '" -InstallRoot "' + ExpandConstant('{app}') + '" -DataRoot "' + ExpandConstant('{commonappdata}\AnyAiCam') + '" -SourceCommit "{#SourceCommit}" -PythonArchive "' + ExpandConstant('{tmp}\python-3.12.10-embed-amd64.zip') + '" -GetPipScript "' + ExpandConstant('{tmp}\get-pip.py') + '" -WheelRoot "' + ExpandConstant('{tmp}\wheels') + '" -FFmpegArchive "' + ExpandConstant('{tmp}\ffmpeg-8.1.2-essentials_build.zip') + '" -MediaMtxArchive "' + ExpandConstant('{tmp}\mediamtx_v1.21.0_windows_amd64.zip') + '" -AppVersion "{#AppVersion}" -PortalUrl "' + ExpandConstant('{param:PortalUrl|}') + '" -RuntimeCatalog "' + ExpandConstant('{tmp}\AnyAiCamRuntime.cat') + '" -EntitlementKeyset "' + ExpandConstant('{tmp}\entitlement_signing_keys.json') + '" -EntitlementKeysetSha256 "{#EntitlementKeysetSha256}"', 'Installing AnyAiCam private runtime and dependencies...');
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
      'To see this computer''s cameras from anywhere, keep "Link this computer to my AnyAiCam account" ticked and click Finish. ' +
      'Your browser opens AnyAiCam: sign in (or create an account), choose a site and confirm. There is no code to type.' + #13#10#13#10 +
      'Later: Start menu > AnyAiCam > Link this computer to AnyAiCam (needs an administrator of this PC).'
  else
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'To see this computer''s cameras from anywhere, link it to your AnyAiCam account when this PC is online: ' +
      'Start menu > AnyAiCam > Link this computer to AnyAiCam (needs an administrator of this PC).';
end;
