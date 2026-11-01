<!-- owner: 项目作者 · last_reviewed: 2026-11-01 · review_cadence: 命令、状态数字或架构变动时 -->

# UBISmart

[![CI](https://github.com/hzy200/UBI_Smart/actions/workflows/ci.yml/badge.svg)](https://github.com/hzy200/UBI_Smart/actions/workflows/ci.yml)
[![Docs](https://github.com/hzy200/UBI_Smart/actions/workflows/docs.yml/badge.svg)](https://github.com/hzy200/UBI_Smart/actions/workflows/docs.yml)
[![Sandbox](https://github.com/hzy200/UBI_Smart/actions/workflows/sandbox.yml/badge.svg)](https://github.com/hzy200/UBI_Smart/actions/workflows/sandbox.yml)

基于 LangGraph 的终端原生编程智能体。

核心原则只有一条：**宿主内置工具，模型只输出意图**。LLM 不直接接触操作系统，
只产出结构化调用 `{tool, args}`，由宿主校验后在 WSL2 沙箱中执行；文件修改走
专用工具（精确替换 + diff + 写前备份），不经 shell。

编排层采用 harness 范式：**orchestrator + 动态任务图 + 并发 worker + fan-in 汇总**。
小任务走 `direct` 快速路径，大任务进入独立 orchestrator 子图。

> 本文件是入口页。架构、编排、使用、安全、运维的细节分别在
> [ARCHITECTURE.md](docs/ARCHITECTURE.md)、[HARNESS.md](docs/HARNESS.md)、
> [USAGE.md](docs/USAGE.md)、[THREAT_MODEL.md](docs/THREAT_MODEL.md) 等文档中。

## 当前状态

| 项 | 结果 |
|---|---|
| 快反馈 | `pytest -m "not wsl and not wsl_env and not llm"` 选中 **3,475**（2026-11-01 快照） |
| 覆盖率 | **84.31%**（CI 子集；承重模块另有棘轮，见 [METRICS.md](docs/METRICS.md)） |
| 全量含 WSL | Sandbox workflow 在 self-hosted `ubismart-wsl` runner 上通过 |
| lint / 类型 | `ruff check src tests` 干净；`mypy` 安全/终端边界 strict 通过 |
| 前端 | 构建、`tsc --noEmit`、单元测试、Playwright E2E 通过 |
| 企业版 | 账号/额度/用量/上游 Key 落 MySQL；`/admin` 独立 SPA |

> 数字会随代码变化，长期事实源是 CI 与 `docs/contracts/`、`docs/METRICS.md`。
> 另有 `llm` 标记用例需要 `DEEPSEEK_API_KEY`，默认排除；不加 `-m` 会真实调用模型。

## 核心设计

- **双工具架构**：Git、依赖、检索、测试都是结构化工具，参数无法逃逸，等级可判定。
- **审批唯一咽喉**：`approval_gate` 只做决定，`tools` 节点负责强制；所有工具调用
  都经过这里。
- **命令分级**：L0 只读 / L1 低风险写 / L2 变更 / L3 危险；复合命令取最高级别，
  无法解析一律降级。
- **可回滚**：写盘前留快照；回滚本身也可回滚。
- **可审计**：审计由 runtime 写，前端绕不过；哈希链可校验。
- **fail-closed**：审批缺失、答复无法解析、非交互环境一律拒绝。

## 架构

四层部署形态：

```text
A1 前端：CLI / TUI / Web / Desktop
        │ HTTP / SSE（只消费 events.py）
A2 控制面：control_server + control/ + web/routes
        │ agent_protocol + event_transport
A3 编排面：agent_server + runtime + graph + orchestrator + llm
        │ ExecutionService
A4 执行面：execution + tools + sandbox
```

前端不得导入 `tools/` 或 `sandbox/`，由 AST 测试锁定。A2 不执行工具；A3 决定审批
与分级；A4 做最终路径/能力校验。

两条执行路径：

```text
direct:      planner → act ⇄ approval_gate → tools → verify → review
             → advance / replan → respond

orchestrated: survey → select_and_plan → write_gate → scheduler ⇄ steering
             → fan_in → planner → act → verify → review → respond
```

- `direct`：小仓库、单点修改、单一功能线。
- `orchestrated`：大任务拆成 DAG，`explore`/`review`/`test`/`implement` 并发，
  implement 在独立 git worktree 中修改，补丁由宿主串行合并。
- 状态生命周期、路由与重置规则见 [WORKFLOW.md](docs/WORKFLOW.md)。
- 调度、worker、fan-in 与干预语义见 [HARNESS.md](docs/HARNESS.md)。

## 快速开始

开发环境固定使用 conda 的 `agent` 环境（Python 3.11）：

```bash
conda create -n agent python=3.11 -y
conda activate agent
pip install -e ".[dev,ui,web]"
```

验证安装：

```bash
agent --help
agent doctor          # 不需要 API Key
agent sandbox-init    # 创建沙箱工作区
```

配置与运行：

```bash
agent init            # 或 cp .env.example .env，填入 DEEPSEEK_API_KEY
agent run "看看当前工作区有哪些文件"
agent tui             # 终端界面
agent gui             # 桌面图形界面（默认只读）
agent gui --write     # 允许会话选择审批写/自动写
agent web             # Web 验证/多用户界面
```

默认启用 harness；小仓库仍走 `direct`。并发写 worker 需要
`AGENT_ORCHESTRATOR_WRITE_ENABLED=true` 且使用 `--write`。

可选 extras：

| extra | 用途 |
|---|---|
| `dev` | pytest / ruff / mypy / xdist / coverage |
| `ui` | `agent tui`（Textual） |
| `web` | `agent web`（FastAPI + uvicorn） |
| `desktop` | `agent gui`（pywebview） |
| `db` | MySQL 驱动、Alembic 迁移 |
| `documents` | PDF/DOCX/XLSX/PPTX 解析 |
| `report` | Markdown/DOCX/PDF 报告导出 |
| `secrets` / `embeddings` / `otel` | OS 密钥环 / 向量记忆 / OTLP 追踪 |

## 命令一览

| 命令 | 用途 |
|---|---|
| `agent init` | 从 `.env.example` 创建 `.env`（默认不覆盖） |
| `agent doctor` | 环境自检：Python、依赖、API Key、WSL、检索后端、shell 隔离 |
| `agent sandbox-init` | 创建并校验沙箱工作区 |
| `agent chat` | 纯流式对话（无工具），验证 API 联通 |
| `agent repl` | 带工具的交互式多轮会话 |
| `agent run <指令>` | 跑一轮任务；`-C` 指定工作区，`--write` 放开写入，`-y` 跳过 L2 确认 |
| `agent resume --thread-id <id>` | 从 checkpoint 恢复挂起会话 |
| `agent tui` | 终端界面（能力最全） |
| `agent gui` | 桌面图形界面（需 `.[web,desktop]`） |
| `agent web` | Web 验证/多用户界面（需 `.[web]`） |
| `agent steer --thread-id <id> <action>` | 注入 pause/cancel/add/replan |
| `agent snapshots` / `undo` / `diff` | 查看快照、回滚、看改动 |
| `agent sessions` / `history` | 历史会话 / 对话记录（支持 `--json`） |
| `agent memory` / `rules` / `audit` | 记忆 / 持久放行规则 / 审计日志 |
| `agent admin create` | 创建企业管理员（需 `.[db]` + `AGENT_CONTROL_DB_URL`） |

Shell 补全：`agent --install-completion` / `agent --show-completion`。

## 安全模型

| 级别 | 示例 | 行为 |
|---|---|---|
| L0 只读 | `ls` `cat` `grep` `git status` | 自动执行 |
| L1 低风险写 | `mkdir`、`file_edit`、`git add` | 需 `--write`，自动执行 + 审计 |
| L2 变更性 | `pip install`、`git commit`、`sed -i` | 需用户确认 |
| L3 危险 | `rm -rf`、`git push -f`、`curl \| sh`、`sudo` | 需用户确认（标注危险） |

- 审批决定与执行分离：`approval_gate` 纯函数决定，`tools` 节点强制。
- 审批结果一次性有效，用后即清；跨挂起/恢复仍带调用参数。
- 路径校验做两次：词法归一化 + 沙箱内 `realpath`；符号链接逃逸被拒。
- `.agent/`、`.env`、私钥/证书、凭据文件默认拒绝读写。
- 可选 bwrap 内核级隔离；配置了却不可用时拒绝执行，不静默降级。
- 完整威胁模型与残余风险见 [THREAT_MODEL.md](docs/THREAT_MODEL.md)。

## 工具

| 类别 | 工具 |
|---|---|
| Shell | `shell_exec`（按命令内容分级） |
| 文件 | `file_read/list/write/edit/edit_many/restore/move/copy/delete/mkdir` |
| Git | `git_status/diff/log/add/commit` |
| 依赖 | `deps_install/list` |
| 检索 | `search_code/find_files/symbol_search/code_intel` |
| 测试 | `run_tests` |
| 记忆 / 知识 | `memory_search/save`、`knowledge_read/search` |
| 文档 / 视觉 / 报告 | `document_*`、`image_inspect`、`report_export/check/read` |
| 委派 / 宿主 | `delegate_explore`、`host_exec` |

等级与参数以 [docs/contracts/tools.yaml](docs/contracts/tools.yaml) 为准；未登记的新工具
按 L3 处理（fail-closed）。L1 需要 `--write`，L3 始终需要人工确认。

## 开发与测试

```bash
conda activate agent

make check                                             # lint + 快反馈
pytest -m "not wsl and not wsl_env and not llm"        # 单元 + TUI + Web + Desktop
pytest -m "not llm"                                    # 全量（含真实 WSL）
pytest -m wsl                                          # 只跑沙箱层
ruff check src tests
python scripts/check_docs.py --strict                  # 文档门禁
python scripts/check_lint_budget.py                    # lint 预算棘轮
```

- CI 在无 WSL 的 runner 上跑快反馈 + 覆盖率；WSL 门禁在
  [sandbox.yml](.github/workflows/sandbox.yml) 的 self-hosted `wsl` runner 上执行。
- 覆盖率门槛 `fail_under=76`；承重模块有独立棘轮，见 [COVERAGE.md](docs/COVERAGE.md)。
- 测试分层、标记与增量选择见 [TESTING.md](docs/TESTING.md)。
- 复杂度热点与拆分建议见 [COMPLEXITY.md](docs/COMPLEXITY.md)。

## 文档地图

| 想… | 读 |
|---|---|
| 上手跑起来 | 本文件 → [USAGE.md](docs/USAGE.md) |
| 理解分层、数据流与不变量 | [ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 接续任务时看状态生命周期 | [WORKFLOW.md](docs/WORKFLOW.md) |
| 改编排、调度、worker | [HARNESS.md](docs/HARNESS.md) |
| 查配置项 | [contracts/config.md](docs/contracts/config.md)（生成） |
| 查工具/命令等级 | [contracts/tools.yaml](docs/contracts/tools.yaml)（生成） |
| 安全边界与威胁模型 | [THREAT_MODEL.md](docs/THREAT_MODEL.md) |
| 部署、数据库与多实例 | [DATABASE.md](docs/DATABASE.md)、[MULTI_INSTANCE.md](docs/MULTI_INSTANCE.md) |
| 桌面 GUI 与前端约束 | [FRONTEND.md](docs/FRONTEND.md) |
| 主机能力代理（A2L） | [HOST_BROKER.md](docs/HOST_BROKER.md) |
| CI、环境、可观测性、发布 | [CI.md](docs/CI.md)、[ENVIRONMENT.md](docs/ENVIRONMENT.md)、[OBSERVABILITY.md](docs/OBSERVABILITY.md)、[RELEASING.md](docs/RELEASING.md) |
| 验收某条需求 | [acceptance-criteria.yaml](docs/acceptance/acceptance-criteria.yaml) |
| 了解为什么这么做 | [adr/](docs/adr/README.md) |
| 新增跨模块能力 | [rfc/](docs/rfc/README.md) |
| 历史阶段与一次性报告 | [archive/](docs/archive/README.md) |
| 出问题了 | [runbook.yaml](docs/runbook.yaml) |
| 改某个子系统 | 对应目录的 `AGENTS.md` |

## 配置入口

优先级：**命令行/构造函数参数 > `.env` > 环境变量 > 默认值**。

常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | 单 provider 时必填 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 主模型 |
| `AGENT_LLM_PROVIDERS` | 空 | 多 provider JSON；空 = 单 provider |
| `AGENT_ORCHESTRATOR_ENABLED` | `true` | 启用 harness |
| `AGENT_ORCHESTRATOR_WRITE_ENABLED` | `false` | 并发写 worker |
| `AGENT_ORCHESTRATOR_MIN_FILES` | `30` | 进入编排的文件数门槛 |
| `AGENT_APPROVAL_MODE` | `ask` | `ask` / `approve` / `deny` |
| `AGENT_VERIFY_ENABLED` | `true` | 自动验证 |
| `AGENT_SECURITY_PROFILE` | `local` | `untrusted` / `production` / `ops` 强制收紧 |
| `AGENT_WSL_DISTRO` | `Ubuntu` | WSL 发行版 |
| `AGENT_SHELL_SANDBOX` | `off` | `off` / `bwrap` |
| `AGENT_CONTEXT_MAX_CHARS` | `60000` | 上下文裁剪 |
| `AGENT_RUN_DEADLINE_S` | `1800` | 单次运行墙钟上限 |

全量配置（400 项）以 [contracts/config.md](docs/contracts/config.md) 为准；不要以本表当全量清单。

## 企业版与多用户

启用 `AGENT_WEB_ACCOUNTS_ENABLED=true` 后，账号、登录会话、会话目录、每日配额、
token 用量与上游 Key 密文统一写入 MySQL；支持 OIDC、TOTP MFA、四眼审批、
按用户访问上限与工作区只读共享。建库、迁移、备份见 [DATABASE.md](docs/DATABASE.md)；
Redis 事件传输与多实例约束见 [MULTI_INSTANCE.md](docs/MULTI_INSTANCE.md)。

## 相关入口

- [AGENTS.md](AGENTS.md)：AI/开发者操作规则、禁区与命令。
- [docs/glossary.md](docs/glossary.md)：术语。
- [docs/archive/README.md](docs/archive/README.md)：历史阶段与快照索引。
- [CHANGELOG.md](CHANGELOG.md)：版本变更。
