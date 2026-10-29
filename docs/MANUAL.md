---
layer: L3
doc_id: manual
title: 配置指南与使用手册
owner: 项目作者
status: active
last_reviewed: 2026-10-23
review_cadence: 命令、配置或界面变动时
---

# 配置指南与使用手册

面向第一次安装、日常配置和实际操作。**全量配置项以
[`docs/contracts/config.md`](contracts/config.md) 为准**（由 `config.py` 生成，
CI 校验漂移）；本手册负责讲清楚“怎么选、怎么用、出了问题看哪里”。

---

## 0. 5 分钟上手

### 0.1 前置条件

- Python 3.11+
- WSL2 + 一个非 root 的 Linux 发行版（默认 `Ubuntu`）
- DeepSeek API Key（或兼容的 OpenAI 风格 provider）

### 0.2 安装

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"
```

只用 CLI 时可以不装 `ui` / `web`；`dev` 用于测试与 lint。

### 0.3 初始化与自检

```bash
agent init                        # 从 .env.example 创建 .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
agent doctor                      # Python/依赖/Key/WSL/锁/隔离后端
agent sandbox-init                # 创建并校验工作区
```

`agent doctor` 不需要 API Key 也能运行。它会指出 `.env` 与环境变量冲突的键；
本项目的优先级是 **命令行/构造函数 > `.env` > 环境变量 > 默认值**，与常见工具相反。

### 0.4 选择界面

| 场景 | 命令 |
|---|---|
| 一次性任务、脚本 | `agent run "…"` |
| 多轮带工具对话 | `agent repl` |
| 日常主力界面 | `agent tui` |
| 浏览器/多用户验证 | `agent web` |
| 只想先看计划 | `agent run "…" --plan-only` |

---

# 第一部分：配置指南

## 1. 配置来源与优先级

| 来源 | 说明 |
|---|---|
| 命令行 / 构造函数 | 最高优先级；如 `-C`、`--write`、`--model` 类开关 |
| `.env` | 当前目录；`agent init` 生成 |
| 环境变量 | 已存在的进程环境 |
| 默认值 | `config.py` 中的默认值 |

注意：

- `.env` **覆盖**同名环境变量。`agent doctor` 会列出被覆盖的键。
- `.env` 里的空占位（`KEY=`）不算配置，不会覆盖环境变量。
- 不同命令从不同目录启动时，`.env`、`.agent/`、steering 目录可能不一致；
  跨进程协作时显式传 `--steering-dir` 或统一工作目录。

## 2. 最小可用配置

```dotenv
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat
AGENT_WSL_DISTRO=Ubuntu
AGENT_WSL_WORKSPACE=/home/you/agent-ws
```

其余全部有默认值。建议第一次先跑只读任务验证链路：

```bash
agent run "看看当前工作区有哪些文件"
```

## 3. 关键配置分组

> 下面只列高频项。完整字段、类型与默认值见
> [`docs/contracts/config.md`](contracts/config.md)。

### 3.1 模型、provider 与追踪

| 变量 | 默认 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 单 provider 时必填 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 主模型 |
| `AGENT_LLM_PROVIDERS` | 空 | 多 provider JSON；空则用 DeepSeek 单 provider |
| `AGENT_LLM_ROUTING_STRATEGY` | `cost` | `cost` 按价格，`priority` 按配置顺序 |
| `AGENT_LLM_FALLBACK_ON_ERROR` | `true` | 非 429 错误是否也切下一个 provider |
| `AGENT_LLM_PROVIDER_MAX_WAIT_S` | `30` | 所有 provider 冷却时最多等待秒数 |
| `AGENT_RUN_COST_BUDGET_USD` | `0` | 单次运行成本预算；0 = 不告警 |
| `AGENT_ORCHESTRATOR_WORKER_MODEL` | 空 | worker 专用模型；空 = 继承主模型 |
| `LANGSMITH_TRACING` | `false` | 调用链追踪开关 |

多 provider 的冷却、用量与 AIMD 并发会写入 `.agent/rate_limits.json`，多个
`agent` 进程共享。

### 3.2 WSL2 与沙箱

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

### 3.3 审批、验证与审查

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

`allow_rules` **只覆盖原本会 ASK 的 L2**，不会绕过 L3 或 `deny` 模式。
用 `agent rules` 维护它，见 §11.8。

### 3.4 编排 / harness

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_ORCHESTRATOR_ENABLED` | `true` | 开启 harness；小仓库仍走 direct |
| `AGENT_ORCHESTRATOR_MIN_FILES` | `30` | 文件数达到后进入编排 |
| `AGENT_ORCHESTRATOR_WRITE_ENABLED` | `false` | 并发写 worker；还需 `--write` |
| `AGENT_ORCHESTRATOR_MAX_TASKS` | `6` | 任务总数上限 |
| `AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS` | `4` | 并发 worker 上限 |
| `AGENT_ORCHESTRATOR_TASK_RETRIES` | `1` | 单任务失败重试次数 |
| `AGENT_ORCHESTRATOR_TOTAL_TOKENS` | `0` | 编排阶段 token 预算；0 = 不限制 |
| `AGENT_ORCHESTRATOR_RUN_DEADLINE_S` | `900` | 编排墙钟上限 |
| `AGENT_ORCHESTRATOR_ALLOW_OVERLAP` | `false` | 允许改同一文件；3-way 合并 |
| `AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL` | `false` | 并发前一次性确认写范围 |

### 3.5 上下文、记忆与循环

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
| `AGENT_MEMORY_EMBEDDING_PROVIDER` | `local` | `local` / `hash` / `none` |
| `AGENT_MEMORY_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 本地模型 |
| `AGENT_MEMORY_EMBEDDING_DIM` | `512` | 必须与模型输出维度一致 |
| `AGENT_MEMORY_VECTOR_TOP_N` | `20` | 两路检索的候选数 |
| `AGENT_MEMORY_FUSION` | `rrf` | 当前只支持 RRF |
| `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS` | `false` | 远程 embedding 双重开关；仅 local profile 可用 |
| `AGENT_MEMORY_EMBEDDING_BASE_URL` | 空 | OpenAI-compatible base URL |
| `AGENT_MEMORY_EMBEDDING_API_KEY` | 空 | 远程 embedding Key |
| `AGENT_MEMORY_EMBEDDING_TIMEOUT_S` / `_BATCH_SIZE` | `10` / `64` | 超时 / 批大小 |
| `AGENT_INSTRUCTIONS_ENABLED` | `true` | 注入 `AGENTS.md` / `CLAUDE.md` |
| `AGENT_MAX_PLAN_STEPS` | `5` | 计划步数上限 |
| `AGENT_MAX_TOOL_ROUNDS` | `12` | 每步工具轮次上限 |
| `AGENT_MAX_REPAIR_ROUNDS` | `3` | 每步修复上限 |
| `AGENT_MAX_REPLANS` | `2` | 执行中重建计划上限 |
| `AGENT_RUN_DEADLINE_S` | `1800` | 单次运行总墙钟上限 |
| `AGENT_VERIFY_TIMEOUT` | `300` | 自动验证独立超时 |

### 3.6 持久化与审计

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_CHECKPOINT_PATH` | `<cwd>/.agent/checkpoints.sqlite` | checkpoint 位置；`:memory:` 不落盘 |
| `AGENT_CHECKPOINT_KEEP` | `200` | 每会话保留 checkpoint 数 |
| `AGENT_AUDIT_ENABLED` | `true` | 审计开关 |
| `AGENT_AUDIT_DIR` | `<cwd>/.agent/audit` | 审计目录 |
| `AGENT_AUDIT_MAX_MB` | `256` | 审计轮转大小；0 = 不轮转 |
| `AGENT_STEERING_DIR` | `<cwd>/.agent/steering` | 跨进程 steering 队列 |
| `AGENT_RATE_LIMIT_STATE_PATH` | `<cwd>/.agent/rate_limits.json` | 跨进程限流状态 |

### 3.7 并发与稳定性

| 变量 | 默认 | 用途 |
|---|---|---|
| `AGENT_CONCURRENCY_WORKERS` | `0` | 统一 worker 池；0 = 回退旧配置 |
| `AGENT_CONCURRENCY_TOOLS` | `0` | 工具并发；0 = 回退旧配置 |
| `AGENT_CONCURRENCY_WSL` | `0` | WSL 并发；0 = 回退旧配置 |
| `AGENT_CONCURRENCY_WSL_RESERVE` | `1` | 留给健康探针的槽位 |
| `AGENT_CONCURRENCY_LLM_PER_PROVIDER` | `4` | 每 provider LLM 并发 |
| `AGENT_CONCURRENCY_LLM_GLOBAL` | `6` | 全局 LLM 并发 |
| `AGENT_MAX_CONCURRENT_TOOLS` | `4` | 同一消息内只读工具并发 |
| `AGENT_WORKSPACE_LOCK` | `true` | 同一工作区单写者锁 |
| `AGENT_RETRY_ATTEMPTS` | `3` | 节点级瞬时错误总尝试次数 |
| `AGENT_WSL_BREAKER_FAILURES` | `3` | WSL 熔断阈值 |

### 3.8 安全 profile 与 Web 多用户

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

profile 行为：

| profile | 沙箱 | 网络 | 审批 | 写入 |
|---|---|---|---|---|
| `local` | 按 `AGENT_SHELL_SANDBOX` | 默认开 | `ask` | 由 `--write` 决定 |
| `untrusted` | 强制 bwrap | 强制关 | 强制 `ask` | 由 `--write` 决定 |
| `production` / `ops` | 强制 bwrap | 强制关 | 强制 `deny` | 强制关闭 |

`untrusted` 与 `production` 还会关闭仓库指令/仓库记忆信任，并忽略
`allow_rules`。公网部署必须配置 token 与 TLS，见
[`USAGE.md#公网--多用户部署`](USAGE.md#公网--多用户部署)。

## 4. 常用配置配方

### 4.1 只读分析

```dotenv
AGENT_ORCHESTRATOR_ENABLED=true
AGENT_APPROVAL_MODE=ask
# 不传 --write，文件工具保持只读
```

```bash
agent run "梳理这个仓库的鉴权入口"
```

### 4.2 允许改代码（受控）

```dotenv
AGENT_ORCHESTRATOR_WRITE_ENABLED=true
AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE=true
```

```bash
agent run "修复 test_calc.py 的失败" --write -y
```

`-y` 只自动批准 L2；L3 危险命令仍逐条询问。

### 4.3 降低 token 成本

```dotenv
AGENT_ORCHESTRATOR_WORKER_MODEL=deepseek-chat
AGENT_ORCHESTRATOR_TOTAL_TOKENS=120000
AGENT_CONTEXT_MAX_CHARS=40000
AGENT_ORCHESTRATOR_MIN_FILES=60
```

worker 用更便宜的模型，并给编排阶段设置总预算。

### 4.4 内核级隔离

```dotenv
AGENT_SHELL_SANDBOX=bwrap
AGENT_SHELL_NETWORK=false
```

配置了 bwrap 但沙箱内不可用时会**拒绝执行**，不会静默降级。

### 4.5 公网 Web

```bash
export AGENT_WEB_AUTH_TOKENS='{"tok-1":"alice","tok-2":"bob"}'
agent web --host 0.0.0.0 --port 8765 --allow-remote \
  --ssl-keyfile /path/key.pem --ssl-certfile /path/cert.pem
```

或在可信反向代理终止 TLS 时使用 `--behind-proxy`，并确保代理传递认证头。

### 4.6 企业账号与管理控制台（MySQL）

**建库与迁移**：

```bash
mysql -u root -p < packaging/mysql/init.sql   # 先改两个 change-me 密码
export AGENT_CONTROL_DB_URL='mysql+pymysql://coding_agent:***@127.0.0.1:3306/coding_agent'
export AGENT_CONTROL_DB_AUTO_CREATE=false
python -m coding_agent.control.migrate --backup
# 或 coding-agent-control --migrate-only
```

**启动企业模式**：

```bash
export AGENT_WEB_ACCOUNTS_ENABLED=true
export AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL=true
export AGENT_SECRET_MASTER_KEY="$(openssl rand -base64 32)"
export AGENT_COST_CURRENCY=CNY
export AGENT_USD_TO_CNY=7.2
agent admin create --username admin --role owner
agent web --host 127.0.0.1 --port 8765
```

- 账号、登录会话、会话目录、每日额度、token 用量与上游 Key 密文全部写入
  MySQL；完整表结构、保留策略与备份见 [DATABASE.md](DATABASE.md)。
- 管理员打开 `/admin` 登录，注册成员、设置每日 token/人民币成本额度、
  分配上游 provider Key 并查看登录安全事件；Key 加密落库，页面只显示后四位。
- 成本显示与额度使用人民币；上游价格仍按 USD 记账，按
  `AGENT_USD_TO_CNY` 固定汇率换算。登录按来源 IP 限流。
- 用户端使用同一套账号密码登录；服务端会话 Cookie 为 HttpOnly，写请求
  校验 CSRF。
- 独立静态站部署时，把发布包的 `admin/` 与 `static/` 一起托管，复制
  `config.example.json` 为 `config.json` 并填写 `apiBaseUrl`；跨源部署需
  设置 `AGENT_WEB_COOKIE_SECURE=true` 与精确 CORS 允许列表。
- 配额按 UTC 日结算；运行前预留、结束后释放，实际用量来自审计 `run_end`
  记录，resume 不会重复计费。
- 备份/恢复：
  ```bash
  python scripts/backup_control_db.py --keep 14
  python scripts/restore_control_db.py --source backups/control-*.sql
  ```
- 主密钥轮换与备份隔离：
  ```bash
  python scripts/rotate_secret_master_key.py \
    --old-key-file old.key --new-key-file new.key --backup
  python scripts/check_backup_secrets.py --dir backups --key-file new.key
  python scripts/phase_secret_key_drill.py
  ```
  轮换后立即更新 `AGENT_SECRET_MASTER_KEY` / `_VERSION`；旧密钥不可再用于解密。

## 5. 路径与状态文件

宿主侧（相对启动目录）与沙箱侧（相对工作区）是两处：

| 位置 | 内容 |
|---|---|
| `<cwd>/.agent/checkpoints.sqlite` | 会话 checkpoint |
| `<cwd>/.agent/audit/*.jsonl` | 审计日志 |
| `<cwd>/.agent/steering/` | 未消费的 steering 指令与暂停标记 |
| `<cwd>/.agent/rate_limits.json` | provider/全局冷却与 AIMD 状态 |
| `<workspace>/.agent/backups/<snapshot_id>/` | 文件快照（写前留底） |
| `<workspace>/.agent/memory.md` | 项目长期记忆 |
| `<workspace>/.agent/memory.meta.json` | 记忆来源/原因/时间/过期/线程信息 |
| `<workspace>/.agent/memory.backups/` | 记忆写入前备份（保留 20 份） |
| `<workspace>/.agent/memory.vectors.json` | 可选向量索引（开启向量检索时） |
| `$HOME/.agent-memory/global/.agent/memory.md` | 跨工作区全局记忆（可配置） |
| `$HOME/.agent-memory/<workspace-hash>/` | 不可信仓库的记忆根（按工作区隔离） |
| `<workspace>/.agent/artifacts/<run>/<task>/attempt-N/result.json` | fan-in 完整 worker 结果 |
| `<cwd>/.agent/web/users/` | Web per-user 审计/checkpoint/steering |

`.agent/` 不入版本控制；`git_add` / `git_commit` 会拒绝把它纳入提交。

## 6. 配置自检

```bash
agent doctor
python scripts/gen_contracts.py --check
python scripts/check_drift.py
```

- `gen_contracts.py --check`：`docs/contracts/` 是否与源码一致。
- `check_drift.py`：文档里的源码/测试路径是否仍然存在。
- `agent doctor`：运行环境、Key、WSL、锁、隔离后端、配置覆盖关系。

---

# 第二部分：使用手册

## 7. 界面选择

| 界面 | 适合 | 特点 |
|---|---|---|
| `agent run` | 一次性任务、CI/脚本 | 单轮执行，事件流输出，支持 `--json` 类查询命令 |
| `agent repl` | 多轮带工具对话 | 复用 runtime/thread；支持 `/new`、审批 |
| `agent gui` | 日常主力（图形界面） | pywebview 窗口、SSE、计划/工具/文件面板、审批、steering、diff/回滚、模型/密钥、上传 |
| `agent tui` | 终端主力 | 计划/任务面板、流式 Markdown、审批弹窗、steering、历史 |
| `agent web` | 浏览器/多用户 | SSE、审批、pause/cancel/add/replan、跨进程 resume、事件重放 |

## 8. CLI 命令参考

| 命令 | 作用 |
|---|---|
| `agent init` | 从 `.env.example` 创建 `.env`；`--force` 覆盖 |
| `agent doctor` | 环境自检 |
| `agent sandbox-init` | 创建并校验沙箱工作区 |
| `agent chat` | 纯流式对话（无工具），验证 API |
| `agent run <指令>` | 跑一轮任务；`-C`、`--write`、`-y`、`--thread-id`、`--plan-only` |
| `agent repl` | 带工具的交互式多轮会话 |
| `agent resume --thread-id <id>` | 恢复审批/steering 挂起；`--write`、`-y` |
| `agent steer --thread-id <id> <action>` | 注入 `pause/cancel/add/replan` |
| `agent snapshots` | 列出快照 |
| `agent diff` | 查看最近改动或指定快照/文件 |
| `agent undo` | 回滚；`--snapshot`、`--path`、`--last-run`、`--since`、`--dry-run`、`-y` |
| `agent sessions` | 历史会话；`--json` |
| `agent history --thread-id <id>` | 会话对话记录；`--limit`、`--offset`、`--json` |
| `agent checkpoints --thread-id <id>` | 列出 checkpoint |
| `agent fork --thread-id <id>` | 从 checkpoint 分叉新会话 |
| `agent rewind --thread-id <id>` | 回退 checkpoint（历史保留） |
| `agent memory` | 查看/添加/删除记忆；`--global`、`--history`、`--restore`、`--reason`、`--ttl-days`、`--thread-id`、`--pin/--unpin`、`--priority N=值`、`--conflicts`、`--vector-status`、`--reindex/--reindex-force` |
| `agent rules` | 查看/添加/删除 L2 持久放行规则 |
| `agent audit` | 查看/验证审计日志；`--thread-id`、`--path`、`--verify`、`--json` |
| `agent gui` | 启动桌面图形界面；`--write` 允许会话选择写模式 |
| `agent tui` | 启动终端界面 |
| `agent web` | 启动 Web 界面 |
| `agent version` / `agent --version` | 显示版本 |

Shell 补全：`agent --install-completion`、`agent --show-completion`。

## 8.1 桌面 GUI

```bash
pip install -e ".[web,desktop,secrets]"
agent gui                 # 默认只读
agent gui --write         # 允许会话选择审批写/自动写
agent gui --workspace D:\proj
```

- 首次启动向导选择 WSL 发行版、工作区与 WSL Python；`--workspace` /
  `--write` 可覆盖已保存配置。
- 设置页可用 Windows 原生对话框选择工作区，自动转成 WSL 路径；同时显示
  项目 Python / Agent Python / 包管理器 / 锁文件 / 测试命令。`/mnt` 工作区
  可一键复制到 WSL 原生目录（拒绝覆盖），也可用 `code --remote` 在 VS Code
  打开。详见 [PHASE3.md](PHASE3.md)。
- 会话级绑定工作区、文件权限（只读/审批写/自动写）与模型；空闲时可切换模型。
- 会话可搜索、改名、删除（tombstone）与导出 Markdown；文件树/Git 面板只读；
  上传附件可查看配额与删除；恢复中心列出待审批/暂停/异常中断。
- 设置页可管理 Provider 档案（单用户/桌面）并发现 `/models`，配置预算展示与系统通知。
  详见 [PHASE4.md](PHASE4.md)。
- 可生成脱敏诊断包；WSL server 意外退出时启动器自动重启并重载窗口；
  `AGENT_UPDATE_MANIFEST_URL` 配置后可检查更新（只提示，不自动安装）。
  详见 [PHASE5.md](PHASE5.md)。
- GUI 可注入 API Key（OS 密钥环）并测试 provider 连通性；多用户 Web 的 key
  按用户隔离。
- 文件上传写入 `.agent/uploads`，有单文件、单次数量与会话累计大小限制。
- 运行由后台 task 持有；断线可用 `GET /api/stream/{thread}?after=<seq>` 重放。
- 关闭窗口前会协作式取消运行并等待最多 5 秒；超时 checkpoint 保留。
- 详细说明见 [GUI.md](GUI.md)。

## 9. TUI

```bash
agent tui -C /home/you/proj --write
```

快捷键：

| 键 | 作用 |
|---|---|
| `Ctrl+Q` | 退出（运行中会确认） |
| `Ctrl+N` | 新会话（运行中/暂停时拒绝） |
| `Ctrl+L` | 清屏 |
| `↑` / `↓` | 浏览输入历史 |
| `→` | 接受斜杠命令补全 |
| `F2` / `Ctrl+J` | 多行输入；`Ctrl+Enter` 提交 |
| `y` / `n` / `Esc` | 审批批准 / 拒绝 / 拒绝 |
| `a` / `d` | 批量批准 / 批量拒绝 |

斜杠命令：

| 命令 | 作用 |
|---|---|
| `/help` `/quit` | 帮助 / 退出 |
| `/new` `/clear` | 新会话 / 清屏 |
| `/workspace` | 显示工作区 |
| `/sessions` `/switch` | 历史会话 / 切换 |
| `/checkpoints` | 列出当前会话 checkpoint |
| `/fork` `/rewind` | 分叉 / 回退 |
| `/snapshots` `/diff` `/undo` | 快照 / 查看改动 / 回滚（会确认） |
| `/audit` `/status` `/cost` | 审计 / 状态 / 成本 |
| `/memory` `/remember` `/forget` | 项目记忆 |
| `/pause` `/resume` `/cancel` | 暂停 / 恢复 / 取消 |
| `/add [explore\|test\|review] <目标>` | 追加只读任务 |
| `/replan` | 重规划剩余工作 |

TUI 的 `/resume` 同时支持 steering 暂停和跨进程审批挂起：没有 steering 暂停时，
它会从 checkpoint 读取待批请求。

## 10. REPL

```bash
agent repl -C /home/you/proj --write -y
```

- 输入任务并回车，工具调用与审批和 `agent run` 一致。
- `/new` 开新会话；`/workspace` 查看工作区；`/help` 查看帮助。
- `exit` / `quit` / `:q` 退出。
- 每轮仍受 `AGENT_RUN_DEADLINE_S`、工具轮次与修复预算约束。

## 11. 典型工作流

### 11.1 只读分析

```bash
agent run "找出所有读取 .env 的代码，并说明风险"
agent run "解释 orchestrator 的调度流程" --plan-only
```

### 11.2 修改代码并验证

```bash
agent run "修复 tests/test_calc.py 里的失败" --write -y
```

流程：planner → act/tools → verify → review → 必要时 repair/replan。
失败会回灌结构化错误；修复预算用尽后换做法。

### 11.3 人工审批

L2/L3 默认停下来询问：

```
需要确认（L2 变更性）
  pip install requests
  执行？[y/N]
```

- `-y` 自动批准 L2；L3 仍需人工确认。
- 非交互环境（EOF）按拒绝处理（fail closed）。
- TUI 中按 `y`/`n`，多条请求可按 `a`/`d` 批量处理。

### 11.4 暂停、取消与追加任务

运行中在 TUI 输入 `/pause`、`/cancel`、`/add review 检查边界`、`/replan`。
CLI 可在另一个终端：

```bash
agent steer --thread-id <id> pause
agent steer --thread-id <id> add --role review --text "检查错误处理"
```

指令在节点边界消费；`pause` 通过 checkpoint 挂起。

### 11.5 跨进程恢复

```bash
agent resume --thread-id <id>          # 审批或 steering 挂起
agent resume --thread-id <id> --write  # 恢复后允许 L1 写入
```

TUI 的 `/resume`、Web 的 resume 按钮等价。恢复次数受 `AGENT_MAX_RESUMES` 限制。

### 11.6 查看与回滚改动

```bash
agent snapshots
agent diff
agent undo --dry-run
agent undo
agent undo --last-run
agent undo --since 2026-10-23T10:00:00+08:00
```

回滚前会先为当前内容留底，所以回滚本身也可以再回滚。

### 11.7 会话与 checkpoint

```bash
agent sessions
agent history --thread-id <id>
agent checkpoints --thread-id <id>
agent fork --thread-id <id> --checkpoint <checkpoint-id>
agent rewind --thread-id <id> --steps 1
agent run --thread-id <id> "继续"
```

`fork` 产生新会话；`rewind` 保留历史，可再次找回。

### 11.8 项目记忆与放行规则

```bash
agent memory --add "本仓库用 uv" --reason "团队约定" --ttl-days 30
agent memory --forget 2
agent memory --history
agent memory --restore <备份 id>
agent memory --pin 2
agent memory --priority 2=10
agent memory --conflicts
agent memory --global --add "跨项目偏好：提交信息用中文"
agent memory --vector-status
agent memory --reindex
agent rules
agent rules --add "shell_exec:git status*"
agent rules --remove "shell_exec:git status*"
```

- 记忆写入前和读取时都会扫描密钥；命中时只阻止注入，不改写用户文件。
- 每次写入前备份 `.agent/memory.md` 与 meta，`--history/--restore` 可回退。
- 可选向量检索：安装 `.[embeddings]` 并设置
  `AGENT_MEMORY_VECTOR_ENABLED=true`；向量索引在
  `.agent/memory.vectors.json`，任何失败都回退 BM25。
- 远程 embedding：`AGENT_MEMORY_EMBEDDING_PROVIDER=remote` +
  `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS=true` + base_url/Key；
  仅 local profile 可用，untrusted/production 会拒绝，因为记忆内容会离开本机。
- 用户与模型的记忆写入都会落 `kind=memory` 审计。
- 不可信/生产 profile 下 `memory_save` 会升级为 L2 确认或直接拒绝。
- `agent rules` 修改 `.env` 的 `AGENT_ALLOW_RULES`，**新进程生效**。
  规则只放宽原本会 ASK 的 L2，不会绕过 L3 或 `deny`。

### 11.9 机器可读输出

```bash
agent sessions --json
agent history --thread-id <id> --json
agent audit --json
```

## 12. 安全模型速查

| 级别 | 示例 | 行为 |
|---|---|---|
| L0 只读 | `ls` `cat` `grep` `git status` | 自动执行 |
| L1 低风险写 | `mkdir`、`file_edit`、`git add` | 需 `--write`，自动执行 + 审计 |
| L2 变更性 | `pip install`、`git commit`、`sed -i` | 需确认 |
| L3 危险 | 强制推送、递归删除、管道执行远端脚本 | 需确认并标注危险 |

- 复合命令按 `&&` `||` `;` `|` 取最高级别；无法解析降级为 L2。
- 未登记的新工具按 L3 处理。
- 敏感文件默认拒写/拒读；可用 `AGENT_WRITE_DENY_GLOBS` /
  `AGENT_READ_DENY_GLOBS` 调整。
- 详细威胁模型见 [`THREAT_MODEL.md`](THREAT_MODEL.md)。

## 13. 排障

| 现象 | 先做什么 |
|---|---|
| `DEEPSEEK_API_KEY` 未配置 | `agent doctor`；检查 `.env` 是否被环境变量覆盖 |
| WSL 不可用 | `wsl -l -v`；确认 `AGENT_WSL_DISTRO` |
| 工作区被占用 | 同一工作区只允许一个 runtime；检查 `AGENT_WORKSPACE_LOCK` |
| 所有命令都判“工作目录非法” | 传 `-C`，或检查 `AGENT_WSL_WORKSPACE` 的 `wsl:` 前缀 |
| 模型 400 | 见 [`USAGE.md`](USAGE.md#模型报-reasoning_content-must-be-passed-back) |
| 检索很慢 | `agent doctor` 看后端；大仓库可装 ripgrep |
| 审计链校验失败 | `agent audit --verify`，确认没有被手工改写 |
| Web 无法远程访问 | 必须 token + TLS 或 `--behind-proxy` |
| 配置不生效 | 记住优先级：`.env` 覆盖环境变量 |

更多故障排查见 [`USAGE.md`](USAGE.md) 与 [`ENVIRONMENT.md`](ENVIRONMENT.md)。

## 14. 文档地图

| 想了解 | 看 |
|---|---|
| 分层架构、不变量、取舍 | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| 编排器、调度、worker、fan-in | [`HARNESS.md`](HARNESS.md) |
| 运行/开发/使用工作流 | [`WORKFLOW.md`](WORKFLOW.md) |
| 环境与依赖版本 | [`ENVIRONMENT.md`](ENVIRONMENT.md) |
| 常见操作与故障排查 | [`USAGE.md`](USAGE.md) |
| 桌面 GUI、权限模式、上传/密钥/事件重放 | [`GUI.md`](GUI.md) |
| 全量配置契约 | [`contracts/config.md`](contracts/config.md) |
| 威胁模型与 T1/T2 边界 | [`THREAT_MODEL.md`](THREAT_MODEL.md) |
| 验收清单与已知限制 | [`ACCEPTANCE.md`](ACCEPTANCE.md) |
| 术语 | [`glossary.md`](glossary.md) |
