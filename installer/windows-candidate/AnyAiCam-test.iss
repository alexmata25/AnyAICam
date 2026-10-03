#ifndef PayloadRoot
#error Supply an offline validated PayloadRoot using build.ps1
#endif
#ifndef CandidateVersion
#error Supply CandidateVersion using build.ps1
#endif

[Setup]
AppId={{52F1E745-77E4-43DA-B1EC-1E2CC10E95AF}
AppName=AnyAiCam Installation Test
AppVersion={#CandidateVersion}
DefaultDirName={autopf}\AnyAiCam-Installation-Test
DefaultGroupName=AnyAiCam Installation Test
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
OutputBaseFilename=AnyAiCam-test
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
SetupLogging=no

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Run]
; Only after successful runtime installation, in the original desktop user's context.
Filename: "{app}\ConnectAnyAiCam.exe"; Description: "Sign in and connect AnyAiCam"; Flags: postinstall runasoriginaluser skipifsilent; Check: RuntimeSucceeded

[Code]
var
  InstalledSuccessfully: Boolean;

function RuntimeSucceeded(): Boolean;
begin
  Result := InstalledSuccessfully;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ExitCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    InstalledSuccessfully := False;
    if (not Exec(ExpandConstant('{app}\InstallRuntime.exe'), '--fresh-install',
        ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ExitCode)) or (ExitCode <> 0) then
      RaiseException('AnyAiCam installation failed. Connection has not started.');
    InstalledSuccessfully := True;
  end;
end;
