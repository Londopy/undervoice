; undervoice - Inno Setup script
;
; Built by the release workflow with
;   ISCC.exe /DAppVersion=1.0.0 installer\undervoice.iss
; after PyInstaller has written the onedir build (undervoice.exe plus its
; _internal folder) to ..\build\onedir\undervoice; see release.yml.
;
; Output: installer\output\undervoice-<version>-setup-x64.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

; Folder holding the PyInstaller onedir build, relative to this script.
#ifndef BinDir
  #define BinDir "..\build\onedir\undervoice"
#endif

; VersionInfoVersion must be plain numeric (x.x.x[.x]); strip any prerelease
; suffix like "-rc1" so prerelease tags still compile.
#if Pos("-", AppVersion) > 0
  #define NumericVersion Copy(AppVersion, 1, Pos("-", AppVersion) - 1)
#else
  #define NumericVersion AppVersion
#endif

#define AppName      "undervoice"
#define AppPublisher "Londopy"
#define AppURL       "https://github.com/Londopy/undervoice"
#define AppExeName   "undervoice.exe"

[Setup]
; Unique GUID - do NOT reuse this for a different app.
; The doubled opening brace is Inno's escape for a literal "{".
AppId={{C69046E9-5EC7-4227-87CB-F31581881D30}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases

; Per-user install, no UAC prompt. The dialog still lets the user pick an
; all-users install if they want one.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; With lowest privileges {autopf} resolves to %LOCALAPPDATA%\Programs.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes

LicenseFile=..\LICENSE

OutputDir=output
OutputBaseFilename=undervoice-{#AppVersion}-setup-x64
SetupIconFile=undervoice.ico

; Compression
Compression=lzma2/ultra64
SolidCompression=yes

; Windows 10+ only (64-bit)
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

WizardStyle=modern

; Restart Manager closes a running copy before its files are replaced.
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no

UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
VersionInfoVersion={#NumericVersion}
VersionInfoDescription={#AppName} {#AppVersion} setup
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; \
  Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; PyInstaller's support folder (Python, numpy, scipy, Tk). Clear the old one
; first so an upgrade leaves no stale modules or DLLs behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; The whole onedir build - built before running iscc
Source: "{#BinDir}\*"; \
  DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; \
  Filename: "{app}\{#AppExeName}"; \
  Tasks: desktopicon

[Run]
; Offer to launch after install (user can uncheck)
Filename: "{app}\{#AppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up the settings (config.toml) left in AppData
Type: filesandordirs; Name: "{userappdata}\{#AppName}"

[Code]
// Kill a running copy before install and uninstall so its files aren't locked.
procedure TerminateApp();
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM {#AppExeName}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    TerminateApp();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    TerminateApp();
end;
