; Inno Setup script: wraps the PyInstaller folder into LectureRecorder-Setup.exe.
; Installs for the current user only, so no administrator rights are needed.

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6B0B8E2A-7C4E-4D8B-9A51-3F2E1C5D7A90}
AppName=Lecture Recorder
AppVersion={#AppVersion}
AppPublisher=Lecture Recorder
DefaultDirName={localappdata}\Programs\LectureRecorder
DefaultGroupName=Lecture Recorder
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LectureRecorder-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\LectureRecorder.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\LectureRecorder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lecture Recorder"; Filename: "{app}\LectureRecorder.exe"
Name: "{autodesktop}\Lecture Recorder"; Filename: "{app}\LectureRecorder.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LectureRecorder.exe"; Description: "Start Lecture Recorder"; Flags: nowait postinstall skipifsilent
