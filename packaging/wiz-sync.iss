; Inno Setup script: wraps the PyInstaller one-folder build into an installer.
; Build: iscc packaging\wiz-sync.iss  ->  dist\WiZSync-Setup-<ver>.exe
; Per-user install (no admin prompt); WinGet runs it with /VERYSILENT.
#define AppVersion "1.1.0"

[Setup]
AppId={{6F1C2B9E-4D7A-4E7B-9C3A-5B2E8D1F0A42}
AppName=WiZ Sync
AppVersion={#AppVersion}
AppPublisher=Mihir Gupta
DefaultDirName={localappdata}\Programs\WiZ Sync
DefaultGroupName=WiZ Sync
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=WiZSync-Setup-{#AppVersion}
SetupIconFile=wiz-sync.ico
UninstallDisplayIcon={app}\wiz-sync.exe
UninstallDisplayName=WiZ Sync
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\wiz-sync\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\WiZ Sync"; Filename: "{app}\wiz-sync.exe"
Name: "{autodesktop}\WiZ Sync"; Filename: "{app}\wiz-sync.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\wiz-sync.exe"; Description: "Launch WiZ Sync"; Flags: nowait postinstall skipifsilent
