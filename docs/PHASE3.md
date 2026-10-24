---
layer: L3
doc_id: phase3
title: Phase 3：工作区与项目 Python 体验
owner: 项目作者
status: active
last_reviewed: 2026-10-23
review_cadence: 工作区选择、项目解释器或测试命令探测变动时
---

# Phase 3：工作区与项目 Python 体验

Phase 3 把“能启动”推进到“用户不需要理解 WSL 路径和 Python 环境”：

- Windows 原生文件夹选择，自动转成 WSL 路径；
- `/mnt` 与 WSL 原生工作区两种模式，明确性能提示；
- 一键把 Windows 盘项目复制到 WSL 原生目录；
- 在 VS Code 中打开当前工作区（Remote-WSL）；
- 同时显示 **Agent Python** 与 **项目 Python**、包管理器、锁文件、测试命令；
- 测试命令优先使用项目自己的 `.venv/venv/env` 解释器。

Agent 自己仍运行在专用发行版的 `/opt/coding-agent/venv`，与项目环境严格分离。

## 1. 项目环境探测

`coding_agent.project_env.probe_project()` 是只读探测：

| 字段 | 含义 |
|---|---|
| `mode` | `native` / `mnt` / `other` |
| `agent_python` | Agent Server 的解释器（`sys.executable`） |
| `project.python` | 项目解释器；`.venv` → `venv` → `env` → 系统 `python3` |
| `project.version` / `project.pytest` | 版本与 pytest 可用性 |
| `package_manager` / `lockfile` | `uv` / `poetry` / `pipenv` / `conda` / `pip` |
| `test_command` | `"<project python> -m pytest -q"`，无 pytest 时为空 |
| `warnings` | `/mnt` 性能、工作区不存在/不可写、缺 venv、缺 pytest |

边界：

- 只执行两个固定命令：`<python> --version` 与 `<python> -c "import pytest"`；
- 不创建 venv、不安装依赖、不执行项目脚本；
- 超时 5 秒，失败只降级为“未检测到”，不抛异常。

HTTP：`GET /api/project?workspace=<绝对路径>`，返回同样的结构；
`/api/capabilities` 额外返回 `desktop_mode`。

## 2. 桌面桥

`coding_agent.desktop.api.DesktopApi` 作为 pywebview 的 `js_api`：

| 方法 | 行为 |
|---|---|
| `pick_folder()` | Windows 原生文件夹选择器；返回 WSL 路径 |
| `open_in_vscode(path)` | `code --remote wsl+<distro> <path>`，列表传参、无 shell |
| `copy_to_wsl(source, name)` | 仅接受 `/mnt/...`；复制到 `~/.coding-agent/workspaces/<name>`，拒绝覆盖 |
| `copy_status(job_id)` | 复制任务的 `running/done/error` 状态 |

复制在后台线程执行，UI 每秒轮询；目标名只允许
`[A-Za-z0-9_.-]{1,64}`，且目标已存在时直接拒绝。

浏览器模式没有 `window.pywebview`，相关按钮自动隐藏；其余功能不受影响。

启动器还会在 Windows 侧先 `bind` 一个空闲端口再传给 WSL，避开 Hyper-V
保留端口（`netsh interface ipv4 show excludedportrange`）；若健康检查仍失败，
最多换 3 个端口重试。这解决了“WSL 内已监听、Windows 侧无法转发”导致的
启动失败。

刚 `wsl --import` 完时，`wsl.exe` 可能返回 `0xC0000142`（DLL 初始化失败）。
`desktop/wsl.run()` 对这类瞬态错误最多重试 4 次（间隔 1 秒）。

## 3. UI

- 会话栏“工作区”旁增加“选择…”（仅桌面）；
- 设置页新增“工作区与 Python”：
  - 当前路径与模式；
  - 项目 Python / Agent Python / 包管理器 / 锁文件 / 测试命令；
  - 警告（`/mnt`、缺 venv、缺 pytest）；
  - 选择文件夹、重新探测、在 VS Code 打开、复制到 WSL 原生目录；
- 最近选择的工作区写入 `localStorage`，下次打开预填。

## 4. 测试命令

`tools/testrun.py` 探测顺序：

1. `.venv/bin/python`、`venv/bin/python`、`env/bin/python`；
2. 若该解释器能 `import pytest`，命令为 `<python> -m pytest -q`；
3. 否则回退系统 `python3`（保持旧行为）；
4. 失败测试的定点重跑也支持任意 `python -m pytest` 前缀。

自动验证仍然只跑宿主推导出的固定命令；模型主动调用 `run_tests` 仍需审批。

## 5. 验收

- 选择 Windows 文件夹后，工作区自动变成 `/mnt/<drive>/...` 并可创建会话；
- `/mnt` 工作区有明确性能提示，可一键复制到 WSL 原生目录；
- GUI 同时显示项目与 Agent 两套 Python；
- 有 `.venv` 的项目，自动测试使用项目解释器；
- 浏览器模式下相关按钮隐藏，API 仍可用；
- 探测不创建/修改工作区。

本机验证（2026-10-23）：

- 专用 `CodingAgent` 发行版内启动 server，`GET /api/project` 对
  `/mnt/d/...` 与 `/home/agent/...` 分别返回 `mode=mnt|native`、
  `agent_python=/opt/coding-agent/venv/bin/python`、`desktop_mode=true`；
- 工作区放 `.venv/bin/python` 时，探测返回 `source=.venv` 并优先使用它；
- 重新构建的 Portable 首次启动自动导入发行版，server 握手与冒烟关窗退出码 0；
- 冷启动时 Windows→WSL loopback 转发可能慢几秒，健康检查已放宽；
- 实测端口落入 Hyper-V 保留区（如 52479）时 Windows 无法转发，启动器改为
  Windows 侧选端口并最多重试 3 次。

## 6. 尚未完成

- 复制任务没有进度百分比（只有 running/done）；复制中关闭窗口会中断，
  可能留下部分目录；目标再次存在时会被拒绝，需手工清理后重试；
- 不自动创建 venv，也不触发依赖安装；
- conda/poetry/uv 环境只做 manifest 识别，不解析其内部解释器路径；
- VS Code Remote-WSL 依赖本机已安装 `code` 与 WSL 扩展。
