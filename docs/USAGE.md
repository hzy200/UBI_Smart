---
layer: L3
doc_id: usage
title: 安装、配置与使用
owner: 项目作者
status: active
last_reviewed: 2026-11-04
review_cadence: 命令或配置变动时
note: 场景化的「故障 → 处置步骤」版本见 docs/runbook.yaml。
---

# 安装、配置与使用

本文合并了原 `USAGE` / `MANUAL` / `onboarding` 三份文档：安装、配置、命令、
典型工作流、安全与排障都在这里。架构与编排细节分别见
[ARCHITECTURE.md](ARCHITECTURE.md)、[WORKFLOW.md](WORKFLOW.md)、
[HARNESS.md](HARNESS.md)。

## 0. 新人路径（约 30 分钟）

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"
cp .env.example .env          # 填入 DEEPSEEK_API_KEY
agent doctor                  # 期望全部通过，不需要 API Key
agent sandbox-init
agent run -C <任意目录> "这个项目是做什么的？只做分析，不要改任何文件。"
```

观察工具行的等级标签（如 `→ [L0 只读] shell_exec: ...`）：**等级由宿主判定，
不是模型自报**。然后按顺序读：

1. [AGENTS.md](../AGENTS.md) 第 3 节：禁区清单。
2. [THREAT_MODEL.md](THREAT_MODEL.md) 第 4 节：防线分级。
3. [glossary.md](glossary.md)：术语（项目里三套数字编号系统并存）。

按角色深入：

| 要动什么 | 读什么 |
|---|---|
| 主图 / 状态字段 | [WORKFLOW.md](WORKFLOW.md) + `src/ubismart/graph/AGENTS.md` |
| 编排 / 并发 / 写隔离 | [HARNESS.md](HARNESS.md) + `src/ubismart/orchestrator/AGENTS.md` |
| 沙箱 / 安全 | [THREAT_MODEL.md](THREAT_MODEL.md) + `src/ubismart/sandbox/AGENTS.md` |
| 工具 | `src/ubismart/tools/AGENTS.md` + [contracts/tools.yaml](contracts/tools.yaml) |
| 前端 / GUI | [FRONTEND.md](FRONTEND.md)（不得 import tools/ 或 sandbox/） |
| 配置 | [contracts/config.md](contracts/config.md)（全量，代码生成） |
| 评测 | [tests/eval/README.md](../tests/eval/README.md) |

## 1. 安装与验证

### 环境

开发环境固定使用 conda 的 **`agent`** 环境（Python 3.11）：

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"
```

必须先激活环境：`agent` 脚本装在环境自己的 `Scripts/`（或 `bin/`）下，
未激活时不在 PATH 上。验证：

```bash
python -c "import sys, sysconfig; print(sys.executable); print(sysconfig.get_path('scripts'))"
agent --help
```

用普通 venv 也可以：`python -m venv .venv` 后激活，后续命令一致。

### extras

| extra | 装了它才能用 |
|---|---|
| （无） | `doctor` / `sandbox-init` / `chat` / `run` |
| `dev` | pytest / ruff / mypy / xdist / coverage |
| `ui` | `agent tui`（Textual） |
| `web` | `agent web`（FastAPI + uvicorn） |
| `desktop` | `agent gui`（pywebview，需同时装 `web`） |
| `db` | MySQL 驱动、Alembic 迁移 |
| `documents` | PDF/DOCX/XLSX/PPTX 解析 |
| `report` | Markdown/DOCX/PDF 报告导出 |
| `secrets` | GUI 注入 API Key（OS 密钥环） |
| `embeddings` | 本地向量记忆（fastembed） |
| `otel` | OTLP 追踪 |

### 首次运行

```bash
agent init                        # 或 cp .env.example .env
agent doctor                      # 环境自检（不需要 API Key）
agent sandbox-init                # 创建沙箱工作区
agent repl                        # 带工具的交互式多轮会话
agent --install-completion        # shell 补全
```

`doctor` 检查：Python、依赖、API Key、WSL 发行版、沙箱用户是否非 root、
工作区、检索后端（ripgrep/grep）、shell 隔离后端（off/bwrap）。

前置条件、依赖版本快照与复现见 [ENVIRONMENT.md](ENVIRONMENT.md)。

## 2. 配置

### 2.1 来源与优先级

**命令行/构造函数参数 > `.env` > 环境变量 > 默认值**。

这里刻意偏离「环境变量 > `.env`」的通用约定：某些工具链会往进程环境注入
用户看不到也删不掉的变量，导致改 `.env` 完全不生效。`agent doctor` 会列出
被 `.env` 覆盖的键；`.env` 里的空占位（`KEY=`）不算配置。

### 2.2 最小配置

```dotenv
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat
AGENT_WSL_DISTRO=Ubuntu
AGENT_WSL_WORKSPACE=/home/you/agent-ws
```

其余都有默认值。建议先跑只读任务验证链路。

### 2.3 关键配置分组

> 只列高频项；完整字段、类型与默认值见 [contracts/config.md](contracts/config.md)（生成）。

**模型、provider 与追踪**

| 变量 | 默认 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 单 provider 时必填 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 主模型 |
| `AGENT_LLM_PROVIDERS` | 空 | 多 provider JSON；空 = DeepSeek 单 provider |
| `AGENT_LLM_ROUTING_STRATEGY` | `cost` | `cost` 按价格，`priority` 按配置顺序 |
| `AGENT_LLM_FALLBACK_ON_ERROR` | `true` | 非 429 错误是否也切下一个 provider |
| `AGENT_LLM_PROVIDER_MAX_WAIT_S` | `30` | 所有 provider 冷却时最多等待秒数 |
| `AGENT_RUN_COST_BUDGET_USD` | `0` | 单次运行成本预算；0 = 不告警 |
| `AGENT_ORCHESTRATOR_WORKER_MODEL` | 空 | worker 专用模型；空 = 继承主模型 |
| `LANGSMITH_TRACING` | `false` | 调用链追踪开关 |

**WSL2 与沙箱**

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_WSL_DISTRO` | `Ubuntu` | WSL 发行版 |
| `AGENT_WSL_WORKSPACE` | `$HOME/agent-ws` | 工作区；Git Bash 建议 `wsl:/home/u/proj` |
| `AGENT_SHELL_SANDBOX` | `off` | `off` / `bwrap`；配置 bwrap 但不可用时拒绝执行 |
| `AGENT_SHELL_NETWORK` | `true` | 仅 bwrap 生效；`false` 时禁网 |
| `AGENT_SHELL_TIMEOUT` | `60` | 单条命令墙钟上限 |
| `AGENT_PERSISTENT_SHELL` | `true` | 常驻 WSL shell 池 |
| `AGENT_PERSISTENT_SHELL_POOL` | `4` | 常驻会话数 |
| `AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS` | `8` | 同时启动的 `wsl.exe` 上限 |
| `AGENT_SHELL_CPU_SECONDS` | `600` | CPU 时间上限 |
| `AGENT_SHELL_MAX_FILE_MB` | `512` | 单文件写入上限 |
| `AGENT_MAX_CAPTURE_BYTES` | `1000000` | stdout/stderr 各自捕获上限 |

**审批、验证与审查**

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_APPROVAL_MODE` | `ask` | `ask` / `approve`（自动放行 L2）/ `deny` |
| `AGENT_ALLOW_RULES` | 空 | L2 持久放行，如 `shell_exec:git status*,run_tests:*` |
| `AGENT_VERIFY_ENABLED` | `true` | 自动验证 |
| `AGENT_VERIFY_COMMAND` | 空 | 指定验证命令；空则按项目清单识别 |
| `AGENT_VERIFY_ONLY_FINAL` | `true` | 多步任务只在最后跑全量 |
| `AGENT_REVIEW_ENABLED` | `true` | 验证通过后的确定性代码审查 |
| `AGENT_REVIEW_LINT_BLOCKING` | `true` | 探测到的 lint/format 发现是否阻断 |
| `AGENT_WRITE_DENY_GLOBS` | 空 | 追加敏感写保护 glob |

`allow_rules` 只覆盖原本会 ASK 的 L2，不会绕过 L3 或 `deny`；用 `agent rules` 维护。

**编排 / harness**

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_ORCHESTRATOR_ENABLED` | `true` | 开启 harness；小仓库仍走 direct |
| `AGENT_ORCHESTRATOR_MIN_FILES` | `30` | 文件数达到后进入编排 |
| `AGENT_ORCHESTRATOR_WRITE_ENABLED` | `false` | 并发写 worker；还需 `--write` |
| `AGENT_ORCHESTRATOR_MAX_TASKS` | `6` | 任务总数上限 |
| `AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS` | `4` | 并发 worker 上限 |
| `AGENT_ORCHESTRATOR_TASK_RETRIES` | `1` | 单任务失败重试次数 |
| `AGENT_ORCHESTRATOR_TOTAL_TOKENS` | `500000` | 编排阶段 token 预算；0 = 不限制 |
| `AGENT_ORCHESTRATOR_TOTAL_STEPS` | `100` | 四维预算：任务尝试数上限 |
| `AGENT_ORCHESTRATOR_TOTAL_TOOL_CALLS` | `1000` | 四维预算：工具调用总数上限 |
| `AGENT_ORCHESTRATOR_BUDGET_WARNING_RATIO` | `0.7` | 预算预告警水位 |
| `AGENT_ORCHESTRATOR_REPEAT_ACTION_LIMIT` | `3` | 同一 (tool,args) 重复熔断 |
| `AGENT_ORCHESTRATOR_PLAN_REVIEW` | `true` | 独立计划评审 |
| `AGENT_ORCHESTRATOR_ESTIMATION_ENABLED` | `true` | Estimation Card + gate |
| `AGENT_ORCHESTRATOR_ESTIMATION_MIN_SAMPLES` | `5` | 历史分桶最小样本 |
| `AGENT_ORCHESTRATOR_SHORT_TASK_THRESHOLD_S` | `60` | gate 短任务阈值 |
| `AGENT_ORCHESTRATOR_LONG_TASK_THRESHOLD_S` | `120` | gate 长任务阈值 |
| `AGENT_ORCHESTRATOR_NESTED_ENABLED` | `false` | 嵌套子编排器 |
| `AGENT_ORCHESTRATOR_MAX_DEPTH` | `1` | 嵌套深度上限 |
| `AGENT_ORCHESTRATOR_MAX_FANOUT` | `2` | 单层子编排器扇出上限 |
| `AGENT_ORCHESTRATION_CONTROL_CHECKPOINTS` | `true` | 阶段控制检查点 |
| `AGENT_ORCHESTRATOR_RUN_DEADLINE_S` | `900` | 编排墙钟上限 |
| `AGENT_ORCHESTRATOR_ALLOW_OVERLAP` | `false` | 允许改同一文件；3-way 合并 |
| `AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL` | `false` | 并发前一次性确认写范围 |

**上下文、记忆与循环**

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_CONTEXT_MAX_CHARS` | `60000` | 上下文字符预算 |
| `AGENT_CONTEXT_MAX_TOKENS` | `0` | 估算 token 预算；0 = 关闭 |
| `AGENT_CONTEXT_KEEP_RECENT` | `12` | 保留最近消息条数 |
| `AGENT_MEMORY_TOP_K` | `8` | 每次注入的记忆条数 |
| `AGENT_MEMORY_ROOT` | 空 | 项目记忆根；空则按 `trust_repo_memory` 推导 |
| `AGENT_MEMORY_GLOBAL_ROOT` | 空 | 全局记忆根；空 = `$HOME/.agent-memory/global` |
| `AGENT_TRUST_REPO_MEMORY` | `true` | 是否读取仓库内 `.agent/memory.md` |
| `AGENT_MEMORY_VECTOR_ENABLED` | `false` | 开启 BM25 + 向量 RRF 混合检索 |
| `AGENT_INSTRUCTIONS_ENABLED` | `true` | 注入 `AGENTS.md` / `CLAUDE.md` |
| `AGENT_MAX_PLAN_STEPS` | `5` | 计划步数上限 |
| `AGENT_MAX_TOOL_ROUNDS` | `12` | 每步工具轮次上限 |
| `AGENT_MAX_REPAIR_ROUNDS` | `3` | 每步修复上限 |
| `AGENT_MAX_REPLANS` | `2` | 执行中重建计划上限 |
| `AGENT_RUN_DEADLINE_S` | `1800` | 单次运行总墙钟上限 |
| `AGENT_VERIFY_TIMEOUT` | `300` | 自动验证独立超时 |

**持久化、审计与并发**

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_CHECKPOINT_PATH` | `<cwd>/.agent/checkpoints.sqlite` | checkpoint 位置；`:memory:` 不落盘 |
| `AGENT_CHECKPOINT_KEEP` | `200` | 每会话保留 checkpoint 数 |
| `AGENT_AUDIT_ENABLED` | `true` | 审计开关 |
| `AGENT_AUDIT_DIR` | `<cwd>/.agent/audit` | 审计目录 |
| `AGENT_AUDIT_MAX_MB` | `256` | 审计轮转大小；0 = 不轮转 |
| `AGENT_AUDIT_ASYNC_WRITE` | `true` | 审计队列 + 专用写线程 |
| `AGENT_REGRESSION_CAPTURE` | `true` | 跑偏轨迹写 `regression.jsonl` |
| `AGENT_CARD_MAX_SILENCE_S` | `120` | 通知面最长静默 heartbeat |
| `AGENT_CARD_MIN_INTERVAL_S` | `10` | 进度卡最小间隔 |
| `AGENT_APPROVAL_ACK_TIMEOUT_S` | `300` | 审批卡超时提示 |
| `AGENT_APPROVAL_DEFAULT_ACTION` | `pause` | 审批卡默认动作 |
| `AGENT_DOCUMENT_PROCESS_ISOLATION` | `true` | PDF/Office 解析走 spawn 子进程 |
| `AGENT_DOCUMENT_PROCESS_TIMEOUT_S` | `60` | 文档解析进程超时 |
| `AGENT_PROCESS_RUNNER_MAX_CONCURRENT` | `2` | 进程兜底并发 |
| `AGENT_STEERING_DIR` | `<cwd>/.agent/steering` | 跨进程 steering 队列 |
| `AGENT_RATE_LIMIT_STATE_PATH` | `<cwd>/.agent/rate_limits.json` | 跨进程限流状态 |
| `AGENT_CONCURRENCY_LLM_PER_PROVIDER` | `4` | 每 provider LLM 并发 |
| `AGENT_CONCURRENCY_LLM_GLOBAL` | `6` | 全局 LLM 并发 |
| `AGENT_MAX_CONCURRENT_TOOLS` | `4` | 同一消息内只读工具并发 |
| `AGENT_WORKSPACE_LOCK` | `true` | 同一工作区单写者锁 |
| `AGENT_RETRY_ATTEMPTS` | `3` | 节点级瞬时错误总尝试次数 |
| `AGENT_WSL_BREAKER_FAILURES` | `3` | WSL 熔断阈值 |

**安全 profile 与 Web 多用户**

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_SECURITY_PROFILE` | `local` | `local` / `untrusted` / `production` / `ops` |
| `AGENT_WEB_AUTH_TOKENS` | 空 | JSON `{"token":"user"}`；支持 `sha256:<hex>` |
| `AGENT_WEB_AUTH_TOKENS_FILE` | 空 | 从文件读取 token 表 |
| `AGENT_WEB_WORKSPACES_ROOT` | `$HOME/agent-ws/users` | per-user 工作区根 |
| `AGENT_WEB_DATA_ROOT` | `./.agent/web/users` | per-user 审计/checkpoint/steering |
| `AGENT_WEB_MEMORY_ROOT` | `$HOME/agent-memory` | per-user 记忆根 |
| `AGENT_WEB_RATE_LIMIT_PER_MIN` | `60` | 每用户 run/steer/resume 限流 |
| `AGENT_WEB_MAX_RUNTIMES` | `8` | 进程级活跃 runtime 上限 |
| `AGENT_WEB_MAX_RUNTIMES_PER_USER` | `2` | 每用户活跃 runtime 上限 |
| `AGENT_WEB_RUNTIME_TTL_S` | `1800` | 挂起 runtime 空闲保留 |
| `AGENT_WEB_ALLOW_ANONYMOUS_LOCAL` | `true` | 无 token 时是否允许 loopback 匿名 |

### 2.4 常用配方

```dotenv
# 只读分析（不传 --write）
AGENT_ORCHESTRATOR_ENABLED=true
AGENT_APPROVAL_MODE=ask

# 受控改代码
AGENT_ORCHESTRATOR_WRITE_ENABLED=true
AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE=true

# 降低 token 成本
AGENT_ORCHESTRATOR_WORKER_MODEL=deepseek-chat
AGENT_ORCHESTRATOR_TOTAL_TOKENS=120000
AGENT_CONTEXT_MAX_CHARS=40000
AGENT_ORCHESTRATOR_MIN_FILES=60

# 内核级隔离（不可用时拒绝执行）
AGENT_SHELL_SANDBOX=bwrap
AGENT_SHELL_NETWORK=false
```

```bash
agent run "修复 tests/test_calc.py 的失败" --write -y
# -y 只自动批准 L2；L3 危险命令仍逐条询问
```

### 2.5 配置自检

```bash
agent doctor
python scripts/gen_contracts.py --check   # 契约是否与源码一致
python scripts/check_drift.py             # 文档/注释路径是否存在
```

## 3. 权限模型

默认**只读**。要改文件必须显式开：

| 开关 | 放开什么 |
|---|---|
| （默认） | 只有 L0 只读命令与 `file_read` / `file_list` |
| `--write` | L1：`file_write`/`file_edit`/`file_edit_many`/`file_restore`/`file_move`/`file_copy`/`file_mkdir`、`git_add` |
| `-y` / `--yes` | L2 自动批准；**L3 危险命令仍逐条确认** |

L2/L3 默认停下来问；非交互环境（管道、CI）一律按拒绝处理（fail closed）。

桌面 GUI 默认只读；`agent gui --write` 才允许会话选择一档：

| 模式 | 行为 |
|---|---|
| 只读 | 只允许读取/检索；shell、测试、Git、依赖、自动 verify、implement worker 全部拒绝 |
| 审批写 | L1 文件写与 L2/L3 变更逐条审批 |
| 自动写 | L1 自动；L2/L3 仍需审批 |

`production` / `ops` profile 永远强制只读 + deny。详见 [FRONTEND.md](FRONTEND.md)。

## 4. 界面与命令

| 界面 | 适合 | 特点 |
|---|---|---|
| `agent run` | 一次性任务、CI/脚本 | 单轮执行，事件流输出 |
| `agent repl` | 多轮带工具对话 | 复用 runtime/thread；`/new`、审批 |
| `agent gui` | 日常图形界面 | SSE、计划/工具/文件面板、审批、steering、diff/回滚 |
| `agent tui` | 终端主力 | 计划/任务面板、流式 Markdown、审批弹窗、steering |
| `agent web` | 浏览器/多用户 | SSE、审批、pause/cancel/add/replan、跨进程 resume |

### CLI 命令

| 命令 | 作用 |
|---|---|
| `agent init` | 从 `.env.example` 创建 `.env`；`--force` 覆盖 |
| `agent doctor` | 环境自检 |
| `agent sandbox-init` | 创建并校验沙箱工作区 |
| `agent chat` | 纯流式对话（无工具） |
| `agent run <指令>` | 跑一轮任务；`-C`、`--write`、`-y`、`--thread-id`、`--plan-only` |
| `agent repl` | 带工具的交互式多轮会话 |
| `agent resume --thread-id <id>` | 恢复审批/steering 挂起 |
| `agent steer --thread-id <id> <action>` | 注入 `pause/cancel/abort/rollback/add/replan/patch` |
| `agent snapshots` / `diff` / `undo` | 快照 / 查看改动 / 回滚 |
| `agent sessions` / `history` | 历史会话 / 对话记录（支持 `--json`） |
| `agent checkpoints` / `fork` / `rewind` | checkpoint / 分叉 / 回退 |
| `agent memory` | 查看/添加/删除记忆；`--global`、`--history`、`--restore` 等 |
| `agent rules` | 查看/添加/删除 L2 持久放行规则 |
| `agent audit` | 查看/验证审计日志；`--verify` |
| `agent gui` / `tui` / `web` | 启动对应界面 |
| `agent version` / `--version` | 显示版本 |

Shell 补全：`agent --install-completion` / `--show-completion`。

### TUI

```bash
agent tui -C /home/you/proj --write
```

快捷键：`Ctrl+Q` 退出、`Ctrl+N` 新会话、`Ctrl+L` 清屏、`↑/↓` 历史、
`→` 接受补全、`F2`/`Ctrl+J` 多行、`y/n/Esc` 审批、`a/d` 批量批准/拒绝。

斜杠命令：`/help` `/new` `/clear` `/workspace` `/sessions` `/switch`
`/checkpoints` `/fork` `/rewind` `/snapshots` `/diff` `/undo` `/audit`
`/status` `/cost` `/memory` `/remember` `/forget` `/pause` `/resume`
`/cancel` `/add [explore|test|review] <目标>` `/replan`。

TUI 的 `/resume` 同时支持 steering 暂停与跨进程审批挂起。

### REPL

```bash
agent repl -C /home/you/proj --write -y
```

输入任务回车即可；`/new` 开新会话、`/workspace` 查看工作区、`/help` 帮助、
`exit`/`quit`/`:q` 退出。每轮仍受 deadline、工具轮次与修复预算约束。

### 桌面 GUI

```bash
pip install -e ".[web,desktop,secrets]"
agent gui                 # 默认只读
agent gui --write         # 允许会话选择审批写/自动写
agent gui --workspace D:\proj
```

首次启动向导选择 WSL 发行版、工作区与 WSL Python；支持原生文件夹选择、
`/mnt` → WSL 复制、VS Code Remote-WSL、会话绑定工作区、文件树/Git 面板、
上传、恢复中心、Provider 档案、诊断包与崩溃重启。关闭窗口前协作式取消
运行并最多等待 5 秒；断线可用 `GET /api/stream/{thread}?after=<seq>` 重放。
完整交互与约束见 [FRONTEND.md](FRONTEND.md)。

## 5. 典型工作流

### 只读分析

```bash
agent run -C "D:\proj" "这个项目的命令安全策略分几级？每一级怎么判定？"
agent run "解释 orchestrator 的调度流程" --plan-only
```

### 改代码并验证

```bash
agent run --write -C "D:\proj" "把 config.py 里的超时从 30 改成 60，改完跑测试"
```

流程：读文件 → 精确替换 → 自动验证 → 失败带结构化错误重试（上限 3 次）。
每次编辑留快照。计划步进、verify、review、repair、replan 的细节见
[WORKFLOW.md](WORKFLOW.md)。

### 撤销改动

```bash
agent snapshots
agent diff
agent undo
agent undo --path src/a.py
agent undo --last-run --dry-run   # 先看会改哪些文件
agent undo --last-run             # 整体回滚最近一次运行
agent undo --since 2026-10-21T02:40:18+00:00
```

回滚本身也可回滚：恢复前先给当前内容留底。TUI 对应 `/snapshots` `/diff` `/undo`。

### 继续、分叉与回退

```bash
agent sessions
agent run --thread-id <id> "继续"
agent checkpoints --thread-id <id>
agent fork --thread-id <id>          # 从当前状态分叉
agent rewind --thread-id <id> -n 1   # 回退 1 个 checkpoint
```

会话存 SQLite，跨进程可恢复；回退在旧 checkpoint 上新建分支，历史不删除。
默认每会话保留 200 个 checkpoint（`AGENT_CHECKPOINT_KEEP`）。

### 记忆

```bash
agent memory --add "这个仓库用 uv 管理依赖，不要用 pip"
```

写入 `<工作区>/.agent/memory.md`，每次运行按相关度注入 top-K 条；来源/时间/
过期在 `.agent/memory.meta.json`。写入拒绝疑似密钥；相似事实自动替换或标记
`conflicts_with`。模型可用 `memory_search` 检索，`--write` 会话可用
`memory_save` 写入带 TTL 的记忆。仓库 `AGENTS.md` / `CLAUDE.md` 作为分层指令
单独注入（最多 4 层）。

### 资料库

资料库是**每个用户一份、跨工作区保留**的参考资料集合，与单会话附件
`.agent/uploads/<thread>` 和 run 级知识产物 `knowledge/` 不同。

```dotenv
AGENT_LIBRARY_ENABLED=true
AGENT_LIBRARY_SEARCH_MODE=lexical   # lexical | vector | hybrid
AGENT_LIBRARY_EMBEDDING_PROVIDER=   # 空 = 继承 MEMORY_EMBEDDING_PROVIDER
AGENT_LIBRARY_EMBEDDING_DIM=0       # 0 = 继承 MEMORY_EMBEDDING_DIM
```

使用：

1. 打开 Web/GUI 左侧活动栏「资料库」；
2. 上传 PDF/DOCX/PPTX/Markdown/TXT/XLSX/HTML/EPUB/CSV；
3. 上传后条目先进入 `queued/processing`，页面自动轮询并显示解析/切块/
   embedding 阶段；完成后变为「可检索」，模型可调用 `library_search`；
4. 修改 embedding 配置后，用「重建索引」或「全部重建」重新生成向量；
5. 需要中止时可在条目上取消任务（阶段边界生效）；
6. 支持按状态/标签/名称筛选与分页，点击「详情」查看 outline 与 chunk；
   「编辑标签」可维护条目标签，检索时可用 `tags` 过滤。

默认根目录：多用户 `<AGENT_WEB_LIBRARY_ROOT>/<user>`，单机
`$HOME/.agent-library`。资料库正文不在 workspace 内，不随仓库提交/回滚。

检索默认词法；`vector`/`hybrid` 需要 embedding provider 可用，失败自动回退
词法。远程 embedding 仍受 `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS` 与安全
profile 门禁。当前不支持 OCR、共享资料库或多设备同步。详见
[LIBRARY.md](LIBRARY.md)。

### 审计与追踪

```bash
agent audit -n 100
agent audit --thread-id abc123
agent audit --verify
LANGSMITH_TRACING=true agent run "..."
```

审计按天一个 JSONL，默认 `<cwd>/.agent/audit/`；记录 `run_start`/`plan`/
`replan`/`tool_call`/`file_change`/`verify`/`review`/`repair`/`worker`/
`task_graph`/`orchestration`/`steering`/`rollback`/`run_end` 等。审计由
runtime 写，写入失败转 `RunFailed`；`orchestration` 记录带 `est_*/actual_*`、
`invalid_step_ratio`、`checkpoint_id`、`isolation_level` 等结构化字段。

追踪带 `run_name`（`agent:<会话id>`）、`tags` 与 `thread_id`/`workspace`/`model`/
`allow_write`/`approval_mode` metadata；**只放标识与开关，不放提示词或文件内容**。

### 编排与实时干预

```bash
AGENT_ORCHESTRATOR_ENABLED=true agent tui
# 写 worker 还需（工作区必须是干净 git 仓库根）：
AGENT_ORCHESTRATOR_ENABLED=true AGENT_ORCHESTRATOR_WRITE_ENABLED=true \
  agent tui --write
```

运行中可 `/pause` `/resume` `/cancel` `/abort` `/rollback n`
`/add [角色] <目标>` `/replan` `/patch {json}`；CLI 用 `agent steer` 注入、
`agent resume` 恢复，Web 提供对应按钮。`rollback` 在运行停止后回退
checkpoint；`patch` 只改契约（goal/constraints/known/unknown）并递增
`plan_version`。

需要嵌套子编排器时显式开启（默认关闭，起步单层）：

```bash
AGENT_ORCHESTRATOR_NESTED_ENABLED=true \
AGENT_ORCHESTRATOR_MAX_DEPTH=1 \
AGENT_ORCHESTRATOR_MAX_FANOUT=2 \
  agent tui
```

任务图、worker 成本/并发/冲突与干预延迟评测见
[tests/eval/README.md](../tests/eval/README.md)。

## 6. 沙箱与安全

### 沙箱

默认工作区是沙箱内 `$HOME/agent-ws`；可指向别处：

```bash
agent run -C "D:\proj" "..."      # Windows 路径
agent run -C /mnt/d/proj "..."    # WSL 路径
```

或写 `.env` 的 `AGENT_WSL_WORKSPACE`。

资源上限：CPU 600s、单文件 512MB、进程数 1024；内存默认不限；单命令墙钟
`AGENT_SHELL_TIMEOUT`（默认 60s）。

`AGENT_SHELL_SANDBOX=bwrap` 时命令在挂载命名空间执行，`/home`、`/root`、
`/mnt` 不可见，仅工作区可写；配置了却不可用时**拒绝执行**。资产、假设与
“从防误操作到防越狱还差什么”见 [THREAT_MODEL.md](THREAT_MODEL.md)。

### 路径与环境

- 词法归一化 + 沙箱内 `realpath` 双重校验；符号链接（含悬空链接）逃逸被拒。
- 每条 WSL 命令以 `env -i` 启动，宿主环境变量不进入 Linux 子进程；
  `AGENT_SHELL_ENV_ALLOWLIST` 可放行构建变量，含 KEY/TOKEN/SECRET/PASSWORD
  的名称始终忽略。`printenv` / `env` 归为 L3。
- shell/search/file_read 回灌前脱敏常见凭据；这是模式匹配 best-effort，
  不是完整 DLP。
- 默认拒绝读写 `.env`、私钥/证书、`.npmrc`、`.netrc`、`.aws/**`、`.kube/**`、
  `credentials*.json`、`*.tfstate` 等；用 `AGENT_READ_DENY_GLOBS` /
  `AGENT_WRITE_DENY_GLOBS` 追加。

### 安全 profile

| profile | 隔离 | 网络 | 审批 | 写入 |
|---|---|---|---|---|
| `local` | 按 `AGENT_SHELL_SANDBOX` | 默认开 | `ask` | 由 `--write` 决定 |
| `untrusted` | 强制 bwrap | 强制关 | 强制 `ask` | 由 `--write` 决定 |
| `production` / `ops` | 强制 bwrap | 强制关 | 强制 `deny` | 强制关闭 |

`untrusted` 与 `production` 还关闭仓库指令/记忆信任，并忽略 `allow_rules`。
Web 会话固定只读：`/api/run` 不接受写权限。

## 7. 状态文件

宿主侧（相对启动目录）与沙箱侧（相对工作区）是两处：

| 位置 | 内容 |
|---|---|
| `<cwd>/.agent/checkpoints.sqlite` | 会话 checkpoint |
| `<cwd>/.agent/audit/*.jsonl` | 审计日志 |
| `<cwd>/.agent/audit/estimates.jsonl` | 预估历史分桶样本 |
| `<cwd>/.agent/audit/control_checkpoints/<thread>/` | 阶段控制检查点（契约/产物引用/预算） |
| `<cwd>/.agent/audit/regression.jsonl` | 跑偏轨迹回归集 |
| `<cwd>/.agent/steering/` | 未消费的 steering 指令与暂停标记 |
| `<cwd>/.agent/rate_limits.json` | provider/全局冷却与 AIMD 状态 |
| `<workspace>/.agent/backups/<snapshot_id>/` | 文件快照（写前留底） |
| `<workspace>/.agent/memory.md` | 项目长期记忆 |
| `<workspace>/.agent/memory.meta.json` | 记忆来源/原因/时间/过期 |
| `<workspace>/.agent/artifacts/<run>/<task>/attempt-N/result.json` | fan-in worker 结果 |
| `<cwd>/.agent/web/users/` | Web per-user 审计/checkpoint/steering |

`.agent/` 不入版本控制；`git_add` / `git_commit` 会拒绝把它纳入提交。

## 8. 公网 / 多用户部署

Web 默认按不可信仓库运行（`untrusted`：bwrap + 禁网 + ask + 不信任仓库指令），
只监听 `127.0.0.1`。

### 企业账号（推荐）

```bash
mysql -u root -p < packaging/mysql/init.sql
export AGENT_CONTROL_DB_URL='mysql+pymysql://coding_agent:***@127.0.0.1:3306/coding_agent'
export AGENT_CONTROL_DB_AUTO_CREATE=false
export AGENT_WEB_ACCOUNTS_ENABLED=true
export AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL=true
export AGENT_SECRET_MASTER_KEY="$(openssl rand -base64 32)"
python -m ubismart.control.migrate --backup
agent admin create --username admin --role owner
agent web --port 8765
```

账号、会话、配额、用量与上游 Key 密文写 MySQL；`/admin` 注册用户、设置额度、
分配 Key、查看登录事件。完整表结构、备份与主密钥轮换见 [DATABASE.md](DATABASE.md)；
多实例与 Redis 事件传输见 [MULTI_INSTANCE.md](MULTI_INSTANCE.md)。

### 静态 Bearer（内网/过渡）

```bash
AGENT_WEB_AUTH_TOKENS_FILE=/etc/coding-agent/tokens.json
# {"token-a":"alice","token-b":"bob"}；也支持 sha256:<hex>
AGENT_WEB_WORKSPACES_ROOT=/srv/agent/workspaces
AGENT_WEB_DATA_ROOT=/var/lib/coding-agent/web
AGENT_WEB_MEMORY_ROOT=/srv/agent/memory
```

Web 不接受任意主机路径，用户只能在自己目录里工作。

```bash
# 本机开发
agent web
# 公网（本进程 TLS）
agent web --host 0.0.0.0 --allow-remote \
  --ssl-certfile /etc/ssl/agent.crt --ssl-keyfile /etc/ssl/agent.key
# 反向代理终止 TLS
agent web --host 127.0.0.1 --behind-proxy
```

非 loopback 且无 token 会被拒绝；无 TLS 且未声明 `--behind-proxy` /
`--allow-insecure-remote` 也会被拒绝。同一数据目录只允许一个 Web 进程。

## 9. 故障排查

### `DEEPSEEK_API_KEY` 已配仍报未配置

看「配置来源」；跑 `agent doctor` 看 WARN，或确认改的是当前目录的 `.env`。

### LangSmith 一直 403

1. `agent doctor` 会提前报追踪凭据问题。
2. 真实 key 与合法但不存在的 key 都返回 403，空 key 返回 401；403 通常是
   钥匙不存在/已吊销。
3. 换了 key 仍 403：旧值还在环境变量里盖着新的。

追踪开关在进程启动时读取并缓存，运行中改要重启进程。

### 追踪没有数据也没报错

`LANGSMITH_TRACING=true` 才开启；默认关闭（数据会离开本机）。

### 所有命令都被判「工作目录非法」

工作区根写错；`agent doctor` 会打印实际解析路径。显式指定 `agent run -C`。
`wsl.exe` 会继承 Windows 当前目录，所以工具始终显式 `cd` 到工作区。

### 检索很慢

`agent doctor` 显示后端；没装 ripgrep 时退到 grep，大仓库更慢。

### `find … | xargs grep …` 被拦

`xargs` 不在 L0 白名单，降级 L2；改用 `search_code`。

### 模型报 `reasoning_content must be passed back`

`deepseek-v4-flash` 思考模式偶发；改用 `DEEPSEEK_MODEL=deepseek-chat`（默认）。

### 测试太慢

```bash
pytest -m "not wsl and not wsl_env and not llm"
```

集成测试开销几乎全在 `wsl.exe` 启动。分层见 [TESTING.md](TESTING.md)。

## 10. 常见误解

| 误解 | 事实 |
|---|---|
| “命令等级是模型自己报的” | 不是。宿主看命令文本判定 |
| “`-y` 就全自动了” | 只跳过 L2，L3 始终要人确认 |
| “经 shell 改的文件也能回滚” | 不能。经 shell 的修改不产生快照 |
| “TUI/Web/GUI 可以自己放行审批” | 不能。审批强制在 `tools` 节点 |
| “default 评测 100% 说明智能体很强” | 说明尺子饱和、没有区分度 |
| “词法守卫能挡住逃逸” | 不能。T2 必须靠 bwrap 兜底 |

## 相关文档

- 架构与不变量：[ARCHITECTURE.md](ARCHITECTURE.md)
- 状态生命周期：[WORKFLOW.md](WORKFLOW.md)
- 编排与 worker：[HARNESS.md](HARNESS.md)
- 安全边界：[THREAT_MODEL.md](THREAT_MODEL.md)
- 配置全量：[contracts/config.md](contracts/config.md)
- 验收台账：[acceptance/acceptance-criteria.yaml](acceptance/acceptance-criteria.yaml)
- 历史文档：[archive/README.md](archive/README.md)
