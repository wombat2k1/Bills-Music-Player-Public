; Bills Music Player Inno Setup script
;
; Expected fresh build layout:
;   dist\Bills Music Player\Bills Music Player.exe
;
; Run build_installer.bat before compiling this script so the dist folder is
; rebuilt from the current source, not an older PyInstaller output.

#define MyAppName "Bills Music Player"
#define MyAppVersion "1.1.0"
#define MyAppVersionInfo "1.1.0.0"
#define MyAppPublisher "Bill"
#define MyAppExeName "Bills Music Player.exe"
#define MyAppDistDir "dist\Bills Music Player"
#define MyAppIcon "assets\app_icon.ico"

[Setup]
AppId={{77616689-0c64-4887-ae17-4b50e608864f}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=BillsMusicPlayerSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile={#MyAppIcon}
SetupLogging=yes
VersionInfoVersion={#MyAppVersionInfo}
VersionInfoTextVersion={#MyAppVersion}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#MyAppDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#MyAppIcon}"; DestDir: "{app}\assets"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\{#MyAppExeName}"
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app_icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
