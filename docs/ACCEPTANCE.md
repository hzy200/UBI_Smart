---
layer: L3
doc_id: acceptance
title: 需求对照与自检
owner: 项目作者
status: active
last_reviewed: 2026-10-31
review_cadence: 每次发版前
note: |
  第 1 节是需求 → 落点 → 验证的对照表。
  机读的 Given-When-Then 版本见 docs/acceptance/acceptance-criteria.yaml。
  第 4 节的「待办」已抽到 docs/tech-debt.yaml。
---

# 需求对照与自检

逐条对照原始需求 → 实现落点 → 怎么验证。附**已知限制**（诚实列出，不藏）。

## 0. 机读台账与它的两道门禁

`docs/acceptance/acceptance-criteria.yaml` 是"这个需求做完了没有"的唯一机读来源
（当前 213 条）。它由两道 CI 检查守着，各管一半：

| 门禁 | 脚本 | 查什么 |
|---|---|---|
| 引用不漂移 | `scripts/check_docs.py`（"验收锚点"） | `tests:` 里写的文件与 `::test_name` 必须真实存在 |
| 覆盖方式已声明 | `scripts/check_acceptance.py` | 每条要么有非空 `tests`，要么有 `note` 说明谁来验收；`status: planned` 豁免 |

第二道补的是"静默缺失"：一条新增需求既没测试也没说明，读台账的人会以为有人
守着，实际没有——这种腐烂不会报警，只能靠规则。

当前口径（`python scripts/check_acceptance.py`）：

```
验收条目 213 条：自动 207 / 已声明非自动 4 / planned 2 / 未声明 0
```

- **自动 207**：`tests:` 指向真实用例；
- **已声明非自动 4**：靠脚本（`phase0_wsl_smoke.py`）、真机（WSL 发行版构建）、
  文档（ADR）承担，逐条写在 `note` 里；
- **planned 2**：还没实现（A2L 断线降级、安全档不得虚报）。

> 注意"自动"不等于"在默认门禁内"：`tests/integration/` 下的用例需要真实 WSL，
> 由 `sandbox.yml` 在自托管 runner 上跑。没有注册 runner 时它会**显式跳过**并
> 打 warning（见 [CI.md](CI.md)）——跳过不等于通过。

## 1. 原始需求逐条对照

| 原始需求 | 实现落点 | 怎么验证 |
|---|---|---|
| 基于 LangGraph 的终端原生编程智能体 | `graph/build.py` 的 `StateGraph`；`cli/` 为终端入口 | `agent tui` / `agent run` |
| 接入 DeepSeek V4 API | `llm/deepseek.py`（OpenAI 兼容协议，改 `base_url` 即可） | `agent chat` 流式对话 |
| WSL2 作为执行安全沙箱 | `sandbox/wsl_exec.py`；非 root 用户 | `agent doctor` 看沙箱用户与发行版 |
| **模型只输出意图**，宿主代执行 | `tools/` 全部是宿主实现的 `BaseTool`；模型拿不到任何句柄 | 任一 `agent run` 的工具行 |
| 宿主经子进程启动 bash 解释执行 | `WslSandbox._argv()` → `wsl.exe -d <distro> -- /bin/bash -l -s` | `pytest tests/integration/test_wsl_exec.py` |
| 文件修改走专用工具 | `tools/files.py`；不走 shell | `agent run --write` 的 `✎` 行 |
| 路径安全 | `sandbox/pathguard.py`（词法）+ `sandbox/fs.py`（realpath）双重校验 | `test_symlink_escape_is_rejected` |
| 修改精确 | `file_edit` 要求 `old_string` 唯一，0 次/多次都拒绝 | `test_edit_refuses_ambiguous_match` |
| 可回滚 | `sandbox/snapshots.py` + `agent undo` | `test_restore_is_itself_undoable` |
| 多轮会话 | LangGraph checkpoint（SQLite）+ `--thread-id` | `agent run --thread-id X "接着上次"` |
| 任务规划 | `graph/nodes/planner.py`（结构化输出 + 重试 + 降级） | `agent run` 输出里的「计划：」 |
| 仓库检索 | `tools/search.py`（rg 优先 / grep 兜底） | `test_search_tools.py` |
| Shell 执行 | `tools/shell.py` | `test_shell_tool.py` |
| 编译测试 | `tools/testrun.py` + `graph/nodes/verify.py` | `test_verify.py` |
| Git 操作 | `tools/git.py`（status/diff/log/add/commit） | `test_git_tools.py` |
| 依赖管理 | `tools/deps.py`（uv/poetry/pnpm/yarn/npm/pip 自动识别） | `test_deps_tools.py` |
| 失败修复 | `graph/nodes/repair.py` + `routing.make_route_after_verify` | `test_repair.py` |
| diff 生成 | `protocol/diffing.py`；CLI/TUI 着色渲染 | `agent diff` |
| 命令分级审批 | `sandbox/policy.py` + `graph/nodes/approve.py` + `interrupt()` | 场景 2 演示 |
| 审计日志 | `audit/`；由 runtime 写，任何前端绕不过 | `agent audit` |
| 企业身份（OIDC/TOTP） | `control/oidc.py`、`control/mfa.py`、`web/routes/auth.py` | `test_oidc.py`、`test_mfa.py` |
| 四眼审批 | `control/approvals.py`、`web/routes/admin.py`、admin 审批页 | `test_approvals.py`、`test_admin_api.py` |
| RBAC（approver/auditor） | `control/accounts.py`、`web/dependencies.py`、`AuditorReadOnlyMiddleware` | `test_admin_api.py` |
| 工作区只读 ACL | `control/workspaces.py`、`web/routes/admin.py`、迁移 `0013` | `test_workspace_acl.py`、`test_control_migrations.py` |
| SIGTERM 优雅 drain | `servers/agent_server.py`、`servers/control_server.py`、`AGENT_DRAIN_TIMEOUT_S` | `test_agent_server_drain.py` |
| 可观测性 | `platform/observability.py`、A2/A3 `/metrics`、`docs/OBSERVABILITY.md` | `test_observability.py` |
| 备份调度 | `packaging/systemd/coding-agent-backup.*`、`UBISmartControl --backup` | `test_control_migrations.py`、`pytest -m mysql` |
| checkpoint 回滚 | checkpoint 负责**会话状态**恢复；快照负责**文件内容**回滚 | `test_checkpointer.py` + `agent undo` |
| 会话分叉/回退 | `AgentRuntime.fork/rewind`；历史不删除，回退即新建分支 | `agent fork` / `agent rewind`；`test_runtime_checkpoints.py` |
| 上下文治理 | 字符 + 估算 token 双预算、400 降档重试、确定性摘要、`ContextTrimmed` 指标 | `tests/eval/context_memory_bench.py` |
| 长期记忆治理 | BM25 + 时间/置顶/优先级、可选本地/远程向量 RRF（失败回退 BM25；远程仅 local profile 双重 opt-in）、项目/全局作用域、planner/respond 注入、`memory_search`/`memory_save`、冲突分级、过期、来源/原因、写入锁、读写密钥扫描、`kind=memory` 审计、备份与恢复 | `test_memory.py` + `test_semantic_memory.py` + `test_embeddings.py` + `test_nodes_llm.py` + `context_memory_bench.py` |
| 分层项目指令 | 发现并合并 `AGENTS.md` / `CLAUDE.md`（根目录到 4 层子目录） | `agent run` 系统提示；`test_instructions.py` |
| 运行中实时干预 | `steering.py` + `graph/nodes/steering.py` + TUI 斜杠命令 | TUI `/pause` `/resume` `/cancel` `/add` `/replan`；`test_steering.py` |
| 动态任务图 + 并发 worker | `orchestrator/`（survey/plan/scheduler/fan-in） | `test_orchestrator.py`；[tests/eval/README](../tests/eval/README.md) |
| 流式输出 | 事件流 → CLI/TUI 逐字渲染 / Web+GUI SSE | `agent chat`；`/api/run` |
| 事件重放 | 后台 run task + 递增 `id` + `GET /api/stream/{thread}?after=` | `tests/unit/test_web_stream_buffer.py` |
| 桌面 GUI | `desktop/` + pywebview + 本地 FastAPI；默认只读、会话级工作区/权限/模型 | `agent gui`；`docs/FRONTEND.md` |
| 文件上传 | 写入 `.agent/uploads`；单文件/数量/工作区累计上限；不授予模型写权限 | `test_web_gui_api.py` |
| GUI 密钥 | 单机用 OS 密钥环；企业模式只存 AES-GCM 密文+后四位；均不进 `.env`/日志 | `test_web_gui_api.py`、`test_phase_f_security.py` |
| Web/UI 验证 | `web/`（SSE 对话 + 多会话 + 联通测试）；`desktop/` 复用同一后端 | `agent web` / `agent gui` |
| LangSmith 追踪 | `runtime._trace_metadata` + `config.apply_tracing_env` | `agent doctor` + LangSmith 项目 |
| 冒烟测试 / 场景演示 | `scripts/demo.py` 四场景 | `python scripts/demo.py` |

### 一处刻意的偏离

原始描述写的是「启动 `/bin/bash -lc` 解释执行」。实现改成了 **`/bin/bash -l -s`，脚本经 stdin 传入**。

原因：Windows → `wsl.exe` → Linux 之间有一层命令行转义。把命令放进 argv 时，
引号、换行、`$` 会被各层重新解释；经 stdin 传则完全绕开这一层。
行为等价（同一份脚本、同一套 shell 语义），但不会因为转义问题出岔子。

## 2. 四个创新点

| 创新点 | 代码 | 测试 | 演示 |
|---|---|---|---|
| Shell 与文件工具分离的双工具架构 | `tools/shell.py` + `tools/files.py`；文件不走 shell | `test_file_tools.py`、`test_shell_tool.py` | 场景 1 |
| 可审计的命令安全策略 | `sandbox/policy.py`（四级 + 复合命令取最高）+ `audit/` | `test_policy.py`、`test_approve.py` | 场景 2 |
| 失败驱动的命令修复循环 | `verify` → `repair` → `act`，硬性上限 3 次 | `test_repair.py`（图级收敛与上限） | 场景 3 |
| 可回滚的修改流程 | `snapshots.py`；**回滚本身也可回滚** | `test_restore_is_itself_undoable` | 场景 4 |

**两道质量关**（`verify` 管行为对不对，`review` 管代码干不干净）：

| 关 | 代码 | 检查什么 |
|---|---|---|
| `verify` | `tools/testrun.py`、`nodes/verify.py` | 跑项目自己的测试/构建，把失败结构化 |
| `review` | `tools/review.py`、`nodes/review.py` | 语法坏没坏、测试有没有被改弱、调试残留（`breakpoint()`/`pdb`）、有没有写进 `.agent/`；Markdown 代码块闭合、相对链接与标题层级；探测到 ruff/mypy/gofmt/rustfmt 时一并跑，**改动文件**的 lint/格式问题默认阻断（mypy 因空配置噪声只作告警） |

review 的「改动前」**直接复用快照**（不变量 8：所有写操作先留底），不另建基线；
只审水位线之后**新增**的行，水位线跨步骤保留 —— 否则每一步都会重审前面所有步骤
的改动。阻断项（结构性缺陷）与验证失败**共用**同一份修复预算，走同一条
`repair` 出口；其余告警只记录，不阻断。`AGENT_REVIEW_LINT_BLOCKING=false`
可把 lint/格式问题降回告警。

## 3. 自检清单

代码冻结前跑一遍，全部应当通过。

```bash
# 1. 静态检查
ruff check .

# 2. 全量测试（含真实 WSL 沙箱）
pytest -m "not llm"          # 全量含 WSL；先运行 agent sandbox-init
# 另有 3 条 llm 标记用例需要 DEEPSEEK_API_KEY；不要用裸 pytest 代替 ——
# 不加 -m 时它们会真的调用模型（本机配了 key 就产生费用）。

# 3. 环境自检
agent doctor                 # 期望「全部通过」

# 4. 端到端冒烟（无需 API Key 的部分）
agent sandbox-init
pytest -m wsl -q             # 沙箱链路

# 5. Phase D 双平台故障演练（无需 API Key / WSL / MySQL）
python scripts/phase_d_smoke.py

# 6. Linux systemd 安装验收（需要 root 与 Linux 主机）
sudo packaging/systemd/install.sh --python /opt/coding-agent/venv/bin/python
curl -fsS http://127.0.0.1:8790/healthz
curl -fsS http://127.0.0.1:8780/healthz
sudo packaging/systemd/install.sh --uninstall   # 保留配置与数据

# 7. 前端构建与类型检查
python scripts/build_frontend.py
npx --yes -p typescript@5.9.3 tsc --noEmit -p src/ubismart/web/frontend/tsconfig.json

# 8. Phase F 安全与迁移验收
pytest -m "not llm" tests/unit/test_phase_f_security.py -q
python scripts/phase_f_drill.py          # upgrade/备份/downgrade/恢复
python scripts/phase_f_bench.py          # 记录性能趋势，不设阈值

# 9. 场景演示（需要 API Key，会真实调用模型）
python scripts/demo.py --list
python scripts/demo.py --only 1
```

当前状态（2026-10-29；WSL 全量未在本轮重跑）：

| 项 | 结果 |
|---|---|
| 快反馈 | `pytest -m "not wsl and not wsl_env and not llm"` 选中 **3,002**（1 skip，其余通过）；MySQL 集成用例在无 `AGENT_TEST_MYSQL_URL` 时 skip |
| 覆盖率 | **83.34%**（CI 子集，门槛 76）；P3 模块目标见 [COVERAGE.md](COVERAGE.md) |
| lint | 干净（`ruff check src tests`） |
| 全量含 WSL | 本轮未重跑；旧 1561 passed / 87.5% 不再作为当前结论 |
| 能力评测 | 常规短任务套件已饱和：`default` 29/29 ×2、`hard` 14/14 ×2、`real` 5/5 ×2、`real_advanced` 14/14 ×2、`blind` 5/5 ×3、LangSmith 158/158（均为历史实测，未在本轮重跑）。唯一未饱和档是 `long`：历史 2/5（当时 5 个任务，现 6 个，spread 20pp）。详见 [tests/eval/README](../tests/eval/README.md) |
| LangSmith | **158/158（2026-10-25，L3 审批修复后）**：`coding-agent-full-v3`，139 capability + 2 agent + 17 mechanism；实验 `coding-agent-full-v3-fixed-7a6830ab`，commit `9a6995b`。详见 [LANGSMITH_REPORT_V3.md](archive/eval/LANGSMITH_REPORT_V3.md) |
| 难度套件（hard） | **14/14 ×2（100%，spread 0，comparable=true，commit `f062a78`）**：14 个并发/spec/多层/排障/性能任务；修正任务定义后已饱和，需继续加难度。详见 [HARD_SUITE_REPORT.md](archive/eval/HARD_SUITE_REPORT.md) |
| 真实代码套件（real） | **5/5 ×2（100%，spread 0，comparable=true，commit `3f34a2b`）**：5 个完整标准库模块回归；可见失败测试使定位空间过窄，已饱和。详见 [REAL_SUITE_REPORT.md](archive/eval/REAL_SUITE_REPORT.md) |
| 真实代码进阶（real_advanced） | **14/14 ×2（100%，spread 0，comparable=true，commit `8c47527`）**：11 个隐藏需求（症状式提示）+ 3 个多回归；上一版翻转已确认是 L3 审批未 resume 的 harness 假象。详见 [REAL_ADVANCED_REPORT.md](archive/eval/REAL_ADVANCED_REPORT.md) |
| 盲测套件（blind） | **5/5 ×3（100%，spread 0，comparable=true，commit `e1a2610`）**：无可见测试，只给症状；通过率饱和，但单任务工具/失败验证约为 spec 的 2–3 倍。详见 [BLIND_SUITE_REPORT.md](archive/eval/BLIND_SUITE_REPORT.md) |
| 上下文画像 | 已接入 baseline/LangSmith：repo_map、worker 摘要、裁剪报告；采样见 [CONTEXT_PROFILE.md](archive/eval/CONTEXT_PROFILE.md) |
| P2 运维 | 审计修复工具与 token 软护栏已落地；上下文/worker 组合 A/B 已完成但**未采纳**（token −6%，spec 通过率 2/2→0/2）；本机控制面已切换到 `coding_agent_app`（仅 DML）；root 密码待人工轮换 |
| System E2E（本地） | **通过（2026-10-25）**：导入/更新/回滚/归档恢复/上传/HMAC/真实审批/快照；`make system-e2e`，不进 CI |
| CI | `.github/workflows/ci.yml`：`pip install -e ".[dev,ui,web,db]"` → ruff → 前端构建/类型检查/单测（`npm ci` + `npm test`）→ `pytest -m "not wsl and not wsl_env and not llm"` → `pytest -m execution` → Phase D/F 演练 |

> **口径提醒**：单次运行的通过率**说明不了任何事**。同一份代码曾跑出 52% 与 83%
> （`spread` 31 个百分点、11/29 任务翻转）—— 上一版把这读作「已落进 30–70% 区间、
> 有区分度」是过度解读。版本对比至少要 `--repeat 2`，且差异必须大于 `spread`。
>
> **常规短任务套件已饱和**：default/hard/real/real_advanced/blind 与 LangSmith 全量
> 都是 100%、`spread=0`。这不是「智能体完美」，而是这些尺子对当前模型没有区分度；
> 继续用它们比较改动只会得到 0pp。唯一未饱和档是 `long`（历史 2/5，任务少、
> `spread` 20pp），结论必须超出噪声带。口径与下一把尺子的要求见
> [tests/eval/README](../tests/eval/README.md)。

> **口径提醒**：全量一律写 `pytest -m "not llm"`。裸 `pytest` 会把那 3 条 `llm` 用例一起跑、
> 真的调用模型 —— 本机 `.env` 配了 key 就计费，而它们本意是"需要时手动跑"。
>
> **CI 与本地不是一回事**：Linux `backend` job 没有 WSL2，只跑快反馈那 3,002 个用例
> （其中 1 个用例 skip），覆盖率 83.34%；`windows` job 覆盖 Windows 专属路径，
> `mysql` job 覆盖真实 MySQL 契约。沙箱层与能力层的覆盖仍依赖
> `-m wsl`，本轮未重跑；CI 会以 `::warning::` 显式提示这个缺口。
> 缺陷与待办集中在 [tech-debt.yaml](tech-debt.yaml)。
>
> 上面每个数字都来自 2026-10-29 的实测；改动后请重跑核对，别手抄。

## 4. 已知限制

按“会不会影响答辩结论”排序。「有意为之」里都是**有意的取舍**，其余是**待办**。

### 有意为之

**Web 支持审批、多用户与公网部署。**
`web/app.py::WEB_APPROVAL_MODE = "ask"`：L2/L3 通过 SSE 发
`approval_requested`，页面收集答复后调用 `/api/resume` 恢复；同时支持
pause/cancel/add/replan。认证使用 Bearer Token；每个用户有独立
workspace/audit/checkpoint/memory；非 loopback 必须 token + TLS（本进程
或可信反向代理）。默认仍只监听 `127.0.0.1`，未配置 token 时只允许 loopback
匿名。主交付界面是 TUI/CLI，桌面 GUI 复用同一套 runtime 与审批语义。

**长期记忆只支持显式增删，不做 LLM 自动提炼。**
自动记忆容易积累噪声，也难以回答"这条结论是哪来的"。
在一个主打可审计的系统里，这个交换不划算。模型可以在运行中调用
`memory_search` 检索、在 `--write` 会话调用 `memory_save` 写一条具体事实，
但宿主不做后台蒸馏；每一条记忆仍是一次明确的、可见的工具调用。

**内存上限默认关闭。**
`ulimit -v` 默认 0（不限）。JVM / Node / 编译器会索取远超实际使用的
虚拟地址空间，贸然开启会把正常构建打死。CPU 时间、文件大小、进程数都有限制。

**经 shell 的修改不产生快照。**
文件修改的正道是 `file_*` 工具（精确替换 + diff + 写前备份）。模型若用 shell
改文件（`sed -i` 等，属 L2、需确认），会置 `dirty` 触发验证，但**不生成快照、
不发 `FileChanged`** —— shell 输出无法结构化解析。「可回滚的修改流程」覆盖文件
工具路径与 orchestrator 的 patch 合并（合并前对目标文件留底），不覆盖模型经
shell 的修改。

**当前架构下不存在真正的协作开发。**
多人可以各自使用 AI，也能通过 Git 交换代码，但没有共享工作区、共享编译环境或
跨人接管——**这不是缺陷，是安全边界的必然结果**。`host` 工作区的字节在用户
NTFS，A2 只持 `workspace_id + root_handle`，A2L 只外连且本地 grant/ZX 是最终
闸门；跨人共享等于把他人机器变成执行节点，会逐条推翻 ADR-0015 的信任模型。
`sandbox` 侧虽可为成员制改造，但受「工作区必须 ext4」与「源码上服务器需过安全
评审」两条约束。判据（三层资源）与协作基座二选一见
[ADR-0022](adr/0022-collaboration-boundary-and-substrate.md)。

**高风险安全项的边界声明（必读）。**
TD-011：Windows host 后端是 T1 + lowpriv/ACL 的部分 OS 级 ZX，**不是 bwrap
等价的内核隔离**；toolchain/构建脚本本身就是代码执行，不可信仓库必须留在
`sandbox`。TD-013：A2L 是 T4 客户端执行域，服务端失陷可触达所有在线客户端；
当前没有 PCR quote/远程启动度量、CSR、控制面设备表或红队结论，默认关闭。
完整的“可宣称 / 不可宣称 / 启用前置 / 禁止措辞”见
[THREAT_MODEL.md §6.1](THREAT_MODEL.md)。出现“T2”“内核级隔离”“远程证明”
“零横向风险”等表述即视为安全表述缺陷。

### 待办

**思考模式的 `reasoning_content` 未修。**
`deepseek-v4-flash` 偶发 400。W12 查清了根因（langchain-openai 不提取该字段，
且序列化时不透传任意 `additional_kwargs`），但**捕获侧无法稳定复现**，
所以没有把无法验证的改动放进请求路径。规避方式是 `DEEPSEEK_MODEL=deepseek-chat`。
详见 [USAGE.md 故障排查](USAGE.md#模型报-reasoning_content-must-be-passed-back)。

**实时干预只在节点边界生效。**
pause/cancel 不会打断正在跑的 LLM 轮次或同步工具；direct 模式的 cancel 额外在
每轮 act/tools 之间检查，但长工具本身仍不可中断。入口包括 TUI 命令、Web 控制
按钮与 CLI `agent steer`；steering 队列与暂停标记原子持久化，重启后未消费的
add/replan 仍可恢复。

**检索默认后端是 grep。**
沙箱里没装 ripgrep，自动退到 `grep`/`find`。两个后端输出格式一致（grep 分支显式 `-E`），
但大仓库上 grep 会更慢。`agent doctor` 会显示当前用的是哪个。

**上下文裁剪用字符 + 估算 token 双预算，不引入 tokenizer；用量则按 provider 回报累计。**
`AGENT_CONTEXT_MAX_TOKENS` 默认 0（只按字符），打开后按「CJK 约 1 字/token、
其余约 4 字符/token」的保守估算再兜一层。遇到 context-length 400 时 act/respond
会用减半预算重裁并重试一次。每次运行的 token 用量仍取自模型的 `usage_metadata`，
累计写入 `run_end` 审计（provider 未回报时为 `None`）。
**成本预算已接入调度准入**：任务级 `max_tokens` 会在 implement worker 内硬停；
scheduler 的全局 token 预算在派发前按「显式 `max_tokens` 或同角色历史 EWMA ×1.2」
预留，放不下时 next-fit 选其它任务；预留仍是估算，不是精确 prompt token 计数。
模型分层（按任务难度选模型）仍未实现。

**共享文件的并发写默认关闭。**
默认仍要求 write_scope 不重叠；开启 `AGENT_ORCHESTRATOR_ALLOW_OVERLAP` 后
用 `git apply --3way` 预演与合并；合并按共享文件的连通分量隔离，真实冲突
只回滚该分量。写 worker 取消时未合并的 patch 丢弃，已合并改动保留。

### 未验证

**TUI 在真实交互终端下的视觉效果。**
自动化测试用 Textual 无头驱动（62 个用例），覆盖了事件映射、计划/任务面板、
审批弹窗、斜杠命令，但**配色、边框、滚动行为需要人工看一眼**。

**Phase D 的桌面 WSL 启动链与真实 LLM 链路。**
`phase_d_smoke.py` 在 Windows/Linux 本机用 loopback 进程验证了 A2/A3 的握手、
崩溃恢复、数据库降级、版本不匹配与迁移失败，但**没有经过桌面启动器拉起 WSL
发行版**（本机 WSL 未安装项目依赖），也**没有跑真实模型/工具/审批/快照**。
发版前需要在一台装好 WSL 发行版与 API Key 的机器上走一遍
`agent gui` + 一次真实 run。

**沙箱隔离：默认到 WSL 发行版级，可选用 bwrap 做内核级隔离。**
默认（`AGENT_SHELL_SANDBOX=off`）是非 root 用户 + 工作区限定 + 资源上限 +
**命令参数的词法越界升级**（只读命令引用 `~`/工作区外绝对路径/`..`、或含 `$()`/变量替换时
升级到人工确认）。若要**内核级**隔离，设 `AGENT_SHELL_SANDBOX=bwrap`：命令在挂载命名空间里执行，
`/home` `/root` `/mnt`（Windows 盘）不可见、仅工作区可写；配置了 bwrap 但沙箱内不可用时会
**拒绝执行**（fail closed），不静默降级。默认威胁模型仍是"防误操作与静默越权"；
完整的资产/假设/边界与"从 T1 升到 T2 还差什么"见 [THREAT_MODEL.md](THREAT_MODEL.md)。

## 5. Harness 阶段验收（已归档）

3A–4D 的逐条验收清单与证据已移至
[archive/ACCEPTANCE_PHASES.md](archive/ACCEPTANCE_PHASES.md)。

## 6. 冻结

代码冻结后不再改动功能，只接受缺陷修复。任何改动都应：

1. 跑 `pytest -m "not llm"`（1420 个用例全过；见第 3 节口径提醒）
2. 跑 `ruff check .`
3. 如果是安全相关的改动，对照本文件第 2 节确认对应测试仍在
