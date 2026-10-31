---
layer: L3
doc_id: phase2
title: Phase 2：专用 WSL 发行版与安装包
owner: 项目作者
status: active
last_reviewed: 2026-10-23
review_cadence: 安装/发行版/升级方式变动时
---

# Phase 2：专用 WSL 发行版与安装包

Phase 2 把 Phase 1 的“现有发行版 + 手动 venv”升级为可分发形态：

```text
UBISmart-Setup.exe
  ├─ Windows 薄壳（PyInstaller onedir）
  ├─ wsl/CodingAgent-rootfs.tar
  └─ WebView2 bootstrapper（安装时下载/打包）

首次启动
  → wsl --import CodingAgent <LocalAppData>
  → /opt/coding-agent/venv 已预装依赖
  → /home/agent/.coding-agent 数据目录
```

## 1. 构建专用 WSL 发行版

```bash
python scripts/build_wsl_distro.py
```

流程：

1. 下载 Ubuntu 24.04 base rootfs；
2. `wsl --import CodingAgentBuild`；
3. apt 安装 Python 3.12、venv、git、ripgrep、bubblewrap、CA；
4. `/opt/coding-agent/venv` 安装 `ubismart[web]`；
5. 创建 `agent` 用户、工作区与 `/etc/wsl.conf`；
6. `wsl --export` → `packaging/wsl/CodingAgent-rootfs.tar`；
7. 注销构建发行版（`--keep` 可保留）。

产物不提交 git（`.gitignore` 已忽略）。

## 2. 构建 Portable 与安装包

```bash
pip install -e ".[web,desktop,build]"
python scripts/build_portable.py
powershell -ExecutionPolicy Bypass -File scripts/build_installer.ps1
```

`build_installer.ps1` 会：

- 构建 rootfs；
- 构建 `dist/UBISmart/UBISmart.exe`；
- 下载 WebView2 Evergreen bootstrapper 到 `packaging/vendor/`；
- 调用 Inno Setup 6 生成 `dist/UBISmart-Setup.exe`；
- 可选调用 `signtool` 签名 exe/installer。

`ISCC.exe` 依次在 `%ProgramFiles%`、`%ProgramFiles(x86)%`、
`D:\InnoSetup\Inno Setup 6` 下查找；脚本已带上 `packaging/vendor` 与
rootfs 的重复来源防护（`dist\UBISmart\wsl` 只打包一次）。

**版本号只有一个来源**：`src/ubismart/__init__.py::__version__`。
`build_installer.ps1` 读它并通过 `/DAppVersion=` 传给 ISCC，`.iss` 里只保留
`{#AppVersion}` 占位符（`#ifndef` 的兜底值必须与之一致）。
`scripts/check_installer.py` 会校验这条链路，写死版本会直接失败。

### CI 只做编译演练，不出可发布包

真包需要 WSL rootfs（`build_wsl_distro.py` 要跑 apt）与签名证书，只能在装有
WSL2 的 Windows 机器上构建，托管 runner 做不了。`ci.yml` 的 `installer` job
因此只做两件事：

1. `python scripts/check_installer.py --check` —— 版本单一源、免提权安装、
   卸载保留 `%LOCALAPPDATA%`、`.iss` 引用路径存在；
2. 用 stub 的 `dist/UBISmart/UBISmart.exe` 与 WebView2 bootstrapper
   跑一次 `ISCC`，确认脚本还能编译。

它守住的是"脚本站不站得住"，**不是**"这个安装包能发"。发布前仍需在真机上
按上面的命令完整构建一次并签名。

## 3. 首次启动

安装包把 `CodingAgent-rootfs.tar` 放在 `{app}\wsl`。

启动器发现：

- 没有 `launcher.json`；
- 存在 bundled rootfs；
- `CodingAgent` 发行版不存在；

则自动：

```text
wsl --import CodingAgent %LOCALAPPDATA%\CodingAgent\wsl\CodingAgent ...tar
```

并写入默认配置：

```text
distro=CodingAgent
workspace=/home/agent/.coding-agent/workspaces/default
python=/opt/coding-agent/venv/bin/python
```

失败时回退到 Phase 1 手动向导。

## 4. 数据、升级与卸载

- Windows 数据：`%LOCALAPPDATA%\CodingAgent`
- WSL 数据：`/home/agent/.coding-agent`
- 安装包升级替换 `{app}` 与 rootfs 文件；
- 卸载不删除 `%LOCALAPPDATA%\CodingAgent`（WSL 发行版与会话数据保留）；
- **已知限制**：已导入的 WSL 发行版不会因 app 升级自动重建。需要：
  - 导出 `/home/agent/.coding-agent`；
  - 导入新 rootfs；
  - 恢复数据；
  - 或后续实现 `--upgrade-wsl` 数据迁移工具。

## 5. 安全

- rootfs 只包含 Agent 依赖，不含用户仓库；
- WSL 默认用户 `agent`，非 root；
- `appendWindowsPath=false`，避免继承 Windows PATH；
- bwrap 预装，安全 profile 可选择强制；
- 安装包安装到 `%LOCALAPPDATA%\Programs\CodingAgent`，不要求管理员权限；
- 代码签名通过 `signtool` 可选接入。

## 6. 验收

- 干净 Windows + WSL2：安装包可启动；
- 首次启动自动导入 `CodingAgent` 发行版；
- server 使用 `/opt/coding-agent/venv/bin/python`；
- 不依赖用户现有 Ubuntu；
- 卸载保留数据；
- WebView2 已安装/自动安装；
- 升级说明明确。

本机验证（2026-10-23）：

- `dist/UBISmart` 约 648 MB（exe 51 MB + `_internal` 190 MB + rootfs 407 MB）；
- `dist/UBISmart-Setup.exe` 约 211 MB；
- 静默安装到 `%LOCALAPPDATA%\Programs\CodingAgent` → 首次启动自动导入
  `CodingAgent` 发行版（约 22 s）→ server 握手成功 → 冒烟关窗退出码 0；
- 静默卸载后安装目录移除，WSL 发行版与 `%LOCALAPPDATA%\CodingAgent`
  数据目录保留。

## 7. 尚未完成

- rootfs 版本化升级与数据迁移自动化；
- 安装包实际签名证书；
- 独立干净 Windows 虚拟机上的真实模型冒烟（本机为开发环境）；
- 安装包体积优化（rootfs + PyInstaller 仍偏大）；
- Windows Server/企业多实例部署（不属于桌面版）。
