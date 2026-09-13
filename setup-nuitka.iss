#define MyAppName "Bills Music Player"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Bill"
#define MyAppExeName "Bills Music Player.exe"
#define MyAppDistDir "nuitka-castfix-dist\Main.dist"
; v1.0.71 correction (section 3): pass /DMyBuildSuffix=-r2 on the ISCC
; command line to produce a distinguishably-named installer for an interim
; acceptance-retest build that intentionally does not bump MyAppVersion --
; defaults to empty so every ordinary build's filename is unchanged.
#ifndef MyBuildSuffix
  #define MyBuildSuffix ""
#endif

[Setup]
AppId={{77616689-0c64-4887-ae17-4b50e608864f}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer
OutputBaseFilename=BillsMusicPlayerSetup-{#MyAppVersion}{#MyBuildSuffix}-Nuitka-CastFix
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=assets\app_icon.ico
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#MyAppDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\zeroconf"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
