; CodingAgent Phase 2 installer (Inno Setup 6)
; 构建前：
;   python scripts/build_wsl_distro.py
;   python scripts/build_portable.py
;   packaging/vendor/MicrosoftEdgeWebView2Setup.exe

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8E0A0E3C-6E42-4B8B-A3F2-C0D1F0A1B2C3}
AppName=CodingAgent
AppVersion={#AppVersion}
AppPublisher=CodingAgent
DefaultDirName={localappdata}\Programs\CodingAgent
DefaultGroupName=CodingAgent
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=CodingAgent-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CodingAgent.exe
CloseApplications=yes

[Files]
; dist\CodingAgent 已包含 wsl\CodingAgent-rootfs.tar（build_portable.py 复制），
; 这里不要再单独列一次，否则安装包会装两份 rootfs。
Source: "..\..\dist\CodingAgent\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
Source: "..\vendor\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\CodingAgent"; Filename: "{app}\CodingAgent.exe"
Name: "{autodesktop}\CodingAgent"; Filename: "{app}\CodingAgent.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "检查 WebView2 Runtime…"; Flags: waituntilterminated
Filename: "{app}\CodingAgent.exe"; Description: "启动 CodingAgent"; Flags: nowait postinstall skipifsilent

; 不删除 %LOCALAPPDATA%\CodingAgent：那里有 WSL 发行版、日志与会话数据。
