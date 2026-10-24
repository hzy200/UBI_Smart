---
layer: L3
doc_id: phase1
title: Phase 1：Windows 启动器与 WSL Server
owner: 项目作者
status: active
last_reviewed: 2026-10-23
review_cadence: 启动器、WSL 集成或打包方式变动时
---

# Phase 1：Windows 启动器与 WSL Server

Phase 1 在 Phase 0 基础上增加：

- Windows 侧 Supervisor；
- WSL 检测与首次启动向导；
- LocalAppData / WSL 数据目录；
- 随机 token（stdin 传入）；
- 端口 0 自动分配与握手；
- 单实例、日志、优雅关闭；
- Portable 构建脚本。

> 仍然需要 WSL2 与已安装的项目依赖。专用 `CodingAgent-WSL` 发行版在 Phase 2。

## 1. 结构

```text
%LOCALAPPDATA%\CodingAgent\
  launcher.json
  logs\launcher.log
  logs\server.log
  state\workspace-*.lock

WSL: ~/.coding-agent/
  audit/  steering/  checkpoints.sqlite  workspaces/  logs/
```

Windows 只负责：

- 配置向导；
- 启动 `wsl.exe` 内的 `coding_agent.server`；
- 读取握手、健康检查；
- 打开 pywebview；
- 关窗时取消运行并优雅退出。

## 2. 首次启动

```bash
pip install -e ".[web,desktop,secrets]"
agent gui
```

首次运行会打开向导：

1. 选择 WSL 发行版；
2. 选择/创建工作区（原生文件夹选择器会转换为 `/mnt/<drive>/...`）；
3. 选择 WSL Python（默认 `~/.coding-agent/venv/bin/python`）；
4. 检测 `fastapi/uvicorn/langgraph/coding_agent` 是否可导入；
5. 保存 `%LOCALAPPDATA%\CodingAgent\launcher.json`。

之后 `agent gui` 直接启动。`agent gui --setup` 重新配置；
`--workspace` / `--write` 可覆盖已保存的向导配置。

## 3. 启动命令

启动器实际执行：

```text
wsl.exe -d <distro> --cd <workspace> -- env \
  AGENT_AUDIT_DIR=... \
  AGENT_CHECKPOINT_PATH=... \
  AGENT_STEERING_DIR=... \
  AGENT_EXECUTION_BACKEND=local \
  <python> -m coding_agent.server \
    --host 127.0.0.1 --port 0 --workspace <workspace> \
    --token-stdin --log-level warning
```

server 通过 stdin 接收 token，输出一行握手 JSON：

```json
{"event":"coding_agent_server","host":"127.0.0.1","port":60735}
```

Windows 启动器读取端口后等待 `/healthz`，再打开窗口。

## 4. 关闭

- 窗口关闭前调用 `/api/desktop/cancel_all`；
- 再调用 `/api/desktop/shutdown`；
- 等待 WSL server 退出；超时才 terminate/kill；
- checkpoint/steering 保留。

## 5. 单实例与日志

- `WorkspaceLock` 锁 `%LOCALAPPDATA%\CodingAgent\state`；
- 第二个实例直接退出；
- `launcher.log` / `server.log` 都在 LocalAppData；
- 启动失败时显示一个本地错误窗口。

## 6. Portable 构建

```bash
pip install -e ".[web,desktop,build]"
python scripts/build_portable.py
```

产物：

```text
dist/CodingAgent/CodingAgent.exe
```

当前脚本使用 PyInstaller `onedir`，包含 `web/static` 与 pywebview。
WSL2 与 WSL 内 venv 仍是外部前置条件。

## 7. 验收

- 干净 Windows + WSL2 + venv：`agent gui` 能打开向导并启动；
- 第二次启动跳过向导；
- 关窗后 WSL server 不残留；
- 单实例生效；
- 日志可读；
- 启动失败有错误窗口；
- WebView 可加载 WSL server 的页面、会话、上传、审批。

## 8. 尚未完成

- 专用 `CodingAgent-WSL` 发行版（Phase 2）；
- WebView2 Runtime 检测/安装；
- 安装包、代码签名、自动更新；
- 项目 Python/解释器探测 UI；
- 多实例事件/密钥共享；
- 真实模型冒烟（避免计费）。
