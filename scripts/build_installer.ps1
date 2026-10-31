# UBISmart Phase 2 installer build.
# 需要：conda agent、PyInstaller、Inno Setup 6、网络（首次下载 WebView2）。
#
#   powershell -ExecutionPolicy Bypass -File scripts/build_installer.ps1
#
# 可选代码签名：
#   $env:CODESIGN_CERT = "C:\path\cert.pfx"
#   $env:CODESIGN_PASSWORD = "..."
#   signtool 在 PATH 上

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "==> 构建 WSL rootfs"
python scripts/build_wsl_distro.py
if ($LASTEXITCODE -ne 0) { throw "WSL rootfs 构建失败" }

Write-Host "==> 构建 Windows Portable"
python scripts/build_portable.py
if ($LASTEXITCODE -ne 0) { throw "Portable 构建失败" }

$Vendor = Join-Path $Root "packaging\vendor"
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null
$WebView = Join-Path $Vendor "MicrosoftEdgeWebView2Setup.exe"
if (-not (Test-Path $WebView)) {
  Write-Host "==> 下载 WebView2 bootstrapper"
  Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $WebView
}

if ($env:CODESIGN_CERT) {
  Write-Host "==> 签名 UBISmart.exe"
  signtool sign /f $env:CODESIGN_CERT /p $env:CODESIGN_PASSWORD `
    /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
    "$Root\dist\UBISmart\UBISmart.exe"
}

$IsccCandidates = @(
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "D:\InnoSetup\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
  throw "未找到 Inno Setup 6（ISCC.exe）。请安装后再运行。"
}

# 版本只有一个来源：包自己的 __version__。写死在这里会让安装包与
# CHANGELOG / 前端版本各走各的（scripts/check_installer.py 会拦住）。
$Version = (& python -c "import ubismart; print(ubismart.__version__)").Trim()
if (-not $Version) { throw "无法从 ubismart.__version__ 读取版本" }

Write-Host "==> 构建安装包（版本 $Version）"
& $Iscc "/DAppVersion=$Version" "packaging\installer\UBISmart.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 构建失败" }

$Setup = Join-Path $Root "dist\UBISmart-Setup.exe"
if ($env:CODESIGN_CERT -and (Test-Path $Setup)) {
  Write-Host "==> 签名安装包"
  signtool sign /f $env:CODESIGN_CERT /p $env:CODESIGN_PASSWORD `
    /tr http://timestamp.digicert.com /td sha256 /fd sha256 $Setup
}

Write-Host "完成：$Setup"
