; UBISmart Phase 2 installer (Inno Setup 6)
; 构建前：
;   python scripts/build_wsl_distro.py
;   python scripts/build_portable.py
;   packaging/vendor/MicrosoftEdgeWebView2Setup.exe

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8E0A0E3C-6E42-4B8B-A3F2-C0D1F0A1B2C3}
AppName=UBISmart
AppVersion={#AppVersion}
AppPublisher=UBISmart
DefaultDirName={localappdata}\Programs\CodingAgent
DefaultGroupName=UBISmart
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=UBISmart-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\..\src\ubismart\web\static\icon.ico
UninstallDisplayIcon={app}\UBISmart.exe
CloseApplications=yes

[Files]
; dist\UBISmart 已包含 wsl\CodingAgent-rootfs.tar（build_portable.py 复制），
; 这里不要再单独列一次，否则安装包会装两份 rootfs。
Source: "..\..\dist\UBISmart\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
Source: "..\vendor\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\UBISmart"; Filename: "{app}\UBISmart.exe"; IconFilename: "{app}\UBISmart.exe"
Name: "{autodesktop}\UBISmart"; Filename: "{app}\UBISmart.exe"; IconFilename: "{app}\UBISmart.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "检查 WebView2 Runtime…"; Flags: waituntilterminated
Filename: "{app}\UBISmart.exe"; Description: "启动 UBISmart"; Flags: nowait postinstall skipifsilent

; 不删除 %LOCALAPPDATA%\CodingAgent：那里有 WSL 发行版、日志与会话数据。
