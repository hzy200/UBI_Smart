---
layer: L3
doc_id: phase0
title: Phase 0：WSL Agent Server 验证
owner: 项目作者
status: active
last_reviewed: 2026-10-23
review_cadence: Phase 0 验收或执行后端变动时
---

# Phase 0：WSL Agent Server 验证

Phase 0 的目标不是做安装包，而是验证：

> **Agent Server 整体运行在 WSL/Linux 内，Windows 只通过浏览器/WebView
> 访问；工具不再逐条启动 `wsl.exe`。**

## 1. 结构

```text
Windows
  WebView / browser
      │ HTTP + SSE，127.0.0.1:8765
WSL2
  ubismart.server
      ├─ FastAPI / SSE
      ├─ AgentRuntime / LangGraph
      ├─ tools / audit / checkpoint / steering
      └─ LocalLinuxSandbox（直接 bash，不再 wsl.exe）
```

`AGENT_EXECUTION_BACKEND`：

| 值 | 行为 |
|---|---|
| `auto` | Windows 用 `WslSandbox`；Linux/WSL 内用 `LocalLinuxSandbox` |
| `local` | 强制本地 bash |
| `wsl` | 强制 `wsl.exe`（兼容旧 Host 模式） |

## 2. 启动

在 WSL 内准备 venv：

```bash
python3 -m venv --without-pip ~/.coding-agent/venv
curl -sS -o /tmp/get-pip.py https://bootstrap.pypa.io/get-pip.py
~/.coding-agent/venv/bin/python /tmp/get-pip.py
~/.coding-agent/venv/bin/python -m pip install -e \
  "/mnt/d/User/PycharmProjects/CodingAgent[web]"
```

启动 server：

```bash
cd /home/<user>/agent-ws
PYTHONPATH=/mnt/d/User/PycharmProjects/CodingAgent/src \
DEEPSEEK_API_KEY=... \
~/.coding-agent/venv/bin/python -m ubismart.server \
  --port 8765 \
  --token phase0token \
  --workspace /home/<user>/agent-ws
```

Windows 浏览器打开：

```text
http://127.0.0.1:8765/#token=phase0token
```

> 若本机配置了 HTTP_PROXY，浏览器/curl 可能把 loopback 也走代理。
> 用 `curl --noproxy '*'` 验证；桌面启动器应显式绕过 localhost 代理。

## 3. 已验证

- `LocalLinuxSandbox`：bash、`SandboxFs`、快照/恢复、bwrap；
- Windows WebView2 连接 WSL server，Alpine 页面加载；
- WSL server 健康检查、静态页、会话创建、上传；
- 冒烟脚本：

```bash
PYTHONPATH=src ~/.coding-agent/venv/bin/python scripts/phase0_wsl_smoke.py
```

覆盖 SSE 审批挂起、resume、事件序号、重放、快照接口、压缩接口。

## 4. 尚未完成

- Windows 启动器/Supervisor（Phase 1）；
- 专用 `CodingAgent-WSL` 发行版；
- 端口 0 自动握手与随机 token 下发；
- 项目 Python/解释器探测 UI；
- 事件与密钥的跨实例共享（多实例 Web）。

## 5. 已知限制

- Phase 0 仍手写 port/token，Windows 用户需要手动打开 URL；
- server 仍使用当前 WSL 发行版和手动 venv，不是最终分发形态；
- 真实模型调用未在冒烟中执行，避免计费；SSE/审批用 fake runtime 验证；
- 本地后端与 WSL 后端共享同一套接口，但尚未覆盖全部 WSL 集成用例。
