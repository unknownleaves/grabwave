; Inno Setup script: turns dist\Grabwave (the PyInstaller folder) + ffmpeg into one Grabwave-Setup-<version>.exe
#ifndef MyAppVersion
  #define MyAppVersion "2.9"
#endif

[Setup]
AppId={{0B058868-F976-4934-9DC1-A03DDB4BD4E7}
AppName=Grabwave
AppVersion={#MyAppVersion}
AppPublisher=Grabwave
DefaultDirName={autopf}\Grabwave
DefaultGroupName=Grabwave
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Grabwave-Setup
SetupIconFile=..\assets\grabwave.ico
UninstallDisplayIcon={app}\Grabwave.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installs for the current user without asking for admin rights; the dialog lets people choose "all users".
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Grabwave\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\ffmpeg\*"; DestDir: "{app}\ffmpeg"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Grabwave"; Filename: "{app}\Grabwave.exe"
Name: "{autodesktop}\Grabwave"; Filename: "{app}\Grabwave.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Grabwave.exe"; Description: "Launch Grabwave"; Flags: nowait postinstall skipifsilent
; after an in-app update (silent install) open the new version again
Filename: "{app}\Grabwave.exe"; Flags: nowait; Check: RelaunchAfterUpdate

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;

// On uninstall, offer to remove settings, the download queue and browser sign-ins too.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if MsgBox('Also delete Grabwave settings, queue and saved sign-ins?' + #13#10 +
              '(Your downloaded videos are never touched.)', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\Grabwave'), True, True, True);
end;
