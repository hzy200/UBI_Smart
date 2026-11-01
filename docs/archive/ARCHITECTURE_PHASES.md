---
layer: L3
doc_id: architecture-phases
title: 架构阶段状态（归档）
owner: 项目作者
status: snapshot
last_reviewed: 2026-11-01
review_cadence: 只读历史快照
---

# 架构阶段状态（Phase A–F）

> 从 `docs/ARCHITECTURE.md` 移出的阶段落地记录；当前边界见
> [../ARCHITECTURE.md](../ARCHITECTURE.md) 与 [../HOST_BROKER.md](../HOST_BROKER.md)。
## 15. Phase A 状态

- 文档与契约完成，代码行为未改变；
- 关键路由、事件字段、会话字段与工具清单由
  `tests/unit/test_layer_contracts.py` 冻结；
- Phase B 才引入控制面抽取与 MySQL schema，A3/A4 的功能只做接口包装，不删减。
- Phase B 的五项落地决策（A2 原生、SecretProvider 分档、事件共享传输、
  SQLite 一等公民、静态 SPA + esbuild/TS）已记录在
  [ADR-0010](../adr/0010-four-layer-phase-b-decisions.md)。
- Phase B 已落地：可选 `AGENT_CONTROL_DB_URL` 的会话目录、SecretProvider
  接口、内存/Redis 事件传输、SSE 按 seq 重放，以及独立 Agent Server 的
  运行生命周期 + 文件/快照/记忆/上传/审计路由（`AGENT_INTERNAL_URL`）。
  A2 provider key 随运行短期注入 A3 内存；审计索引在查询时拉取并幂等写入
  MySQL。默认仍走旧 JSON/内存/同进程。A2/A3 原生可执行目录与 systemd/
  Windows 开机任务已提供；前端源已迁到 `frontend/src` 并由 esbuild 生成
  `static/app.js`。consumer group、SCM 服务包装与严格 TS 类型待后续。
  见 [PHASE_B.md](phases/PHASE_B.md)。

## 16. Phase C：ExecutionService（C0/C1）

A3→A4 只通过 `ExecutionService`：`ExecutionRequest` 带
`op_id/kind/run_id/payload`，`ExecutionResult` 带 `status/seq/receipt`。
`op_id` 幂等由工作区 `.agent/execution/receipts.jsonl` 承载；`seq` 按 run
单调递增且与 Web SSE 序号独立。

当前实现 `run/read_file/write_file/list_dir/stat/git/snapshot/restore/
worktree_create/worktree_remove/patch_apply`。`AgentRuntime` 用
`ExecutionSandbox` 包住 sandbox：既有 shell/文件/Git/快照/worktree 代码仍
调用 `sandbox.run()`，但实际执行统一经过 A4 backend；工具层没有第二条路径。

工具调用由 `run_id + tool_call_id` 上下文生成稳定 op_id，重试/恢复命中执行
回执；`record_unknown/record_cancelled` 提供 unknown/cancelled 终态。
合并路径的 `git apply` 也走 ExecutionService 并写回执，但回执尚未进入
Scheduler 事件。WSL 与 Linux local 共用 `tests/contract/execution_cases.py`；
Linux CI 跑 local，Windows 本地跑 WSL。详见 [PHASE_C.md](phases/PHASE_C.md)。

## 17. Phase D：双平台部署状态

### Windows 桌面

```text
A1 pywebview → Windows A2（127.0.0.1:8780，随机前端 token）
                         │ loopback + 内部 token
                         ▼
                   WSL A3（127.0.0.1:8790）
```

- A3 先启动并返回端口；A2 注入 `AGENT_INTERNAL_URL/TOKEN` 后启动；
- 端口固定，token 只保存在 supervisor 内存，A3 重启复用同一 token/端口；
- `desktop.watchdog.supervise_many` 同时守护 A2/A3；
- A2 每 5 秒探测 A3，A3 重启后自动从 `unavailable` 回到 `ok`；
- `--host-mode` 保留旧单进程路径一个版本；standalone 不受影响。

### Linux

systemd 顺序：`coding-agent-agent` → `coding-agent-control`；A2 通过
`ExecStartPre=... --migrate-only` 在启动前迁移。`packaging/systemd/install.sh`
创建用户/目录/env/单元，支持打包二进制与源码 venv 两种模式。

### 健康、降级与恢复

- A2 `/healthz`：`version / agent_state / db_state`；
  A3 `/healthz`：`version / api_version`；
- `ControlStore` 初始化或写入失败 → 只读降级，写操作 503，运行/SSE 不受影响；
- 迁移前自动备份；SQLite 复制、MySQL `mysqldump`；恢复脚本默认先备份当前库；
- `scripts/phase_d_smoke.py` 覆盖启动握手、会话、A3 崩溃、A2 重启、
  数据库降级、版本不匹配与迁移失败（7/7 通过）。

### 数据边界不变

Phase D 没有把 checkpoint、工作区或运行状态迁入 MySQL；企业版后续只把
provider Key 的 AES-GCM 密文与后四位写入控制面，明文和主密钥仍不落库。
A2 仍不执行工具，A3/A4 仍持有执行语义。桌面 WSL 完整启动链与真实 LLM run
仍需在打包环境验收。

## 18. Phase E：前端状态

完整的功能边界、消息模型、工作区绑定、对话内授权、视觉规范与 UI 约束见
[FRONTEND.md](../FRONTEND.md)。架构层只保留以下不变量：

- A1 只消费后端领域事件；`frontend/src/sse.ts` 负责帧解析、`seq` 去重与
  `after` 重放参数。前端不得导入 `tools/` 或 `sandbox/`。
- 助手 token 用 60ms 节流 + 非响应式缓冲渲染；同一轮重复的相同回答去重，
  最终回答固定在 transcript 末尾。
- 消息按“你 / 工作中 / 最终回答 / 过程 / 工具”分层着色；工具调用以卡片
  展示命令、输出、状态与耗时；长回答/长输出可折叠。
- 会话与工作区绑定不可变：`workspace_id + workspace_kind + a2l_id +
  root_handle` 持久化在 `control_sessions`；会话列表按工作区分组。
- host 文件授权走桌面对话内卡片（A2L 授权桥），A3 按工作区缓存 grant；
  前端只提交用户决定，不能代替本地确认。
- 桌面端每轮结束保存完整 UI 轨迹（工具卡片/过程/最终回答）到
  `transcripts/<thread>.json`，重开会话优先恢复。
- 主题令牌集中在 `style.css`：深/浅/跟随系统，字体、字号、密度与快捷键
  持久化；关键弹窗与对话区带 aria 语义。
- 长会话用 `content-visibility: auto` 降低渲染成本；代码块使用本地
  highlight.js 与复制按钮；窗口启用文本选择与每条消息复制。
- `static/app.js` 由 esbuild 生成，CI 重建并检查漂移，同时跑 `tsc --noEmit`。
- 关闭桌面窗口时 UI 立即返回，服务停止/取消在后台并行执行，避免“未响应”。

前端状态已按会话视图化（[ADR-0019](../adr/0019-per-session-frontend-views.md)）：
`sessionViews[threadId]` 持有消息、计划、工具、审批、用量与流式游标；每条 SSE 在
发起时绑定自己的 view，切换会话不中断后台流，主区与分屏可同时运行。长会话使用
变量高度虚拟列表，流式 Markdown 做块级增量渲染，单条超大工具输出按行分页。

测试：74 个纯函数用例（`node test.mjs`）+ 40 个 jsdom 交互用例
（`node test-dom.mjs`）+ 6 个 Playwright E2E（用户端 + `/admin`）+ pywebview 严格 CSP 探针。
手测清单见 [FRONTEND.md](../FRONTEND.md) 第 12 节。

### 18.1 前后端分离（设计冻结，九步已实现）

完整契约见 [RFC-0003](../rfc/0003-frontend-backend-separation.md) 与
[ADR-0016](../adr/0016-frontend-backend-topology-and-runtime-config.md)–
[ADR-0018](../adr/0018-desktop-bridge-boundary.md)。架构层不变量：

- 同一份前端产物通过运行时配置覆盖桌面/同域反代/独立源站/BFF；
  默认同源相对基址，配置非法失败关闭；
- API 版本与前端版本通过 `/healthz` 握手；不兼容时阻断，不静默降级；
- Web 凭证按形态分档：桌面一次性 token、内网短期 Bearer（仅内存）、
  公网 BFF httpOnly Cookie + CSRF；前端不把凭证写 localStorage/URL；
- CORS 只对精确允许列表开放，禁止 `*`/`null`；凭证仅 Cookie 模式；
  CORS 最外层，错误响应也带允许头与 `Vary: Origin`；
- 错误体使用稳定 `code` + `request_id`；A2→A3→日志全链路透传；
- SSE 经过反代禁缓冲、禁压缩、保留心跳；代理不重试已发送的流；
- 桌面桥只对本地页面开放；远程页面不注入 `js_api`；
- A2 仍只监听 loopback，分离只改变浏览器到边缘的路径，不改变执行面位置。

实现顺序：基址 → 认证 → CORS/代理 → 桥；前两步完成前不启用跨域或 Cookie。

## 19. Phase F：安全与性能验收

- **密钥边界**：企业 provider Key 在 `control_provider_keys` 只存 AES-256-GCM
  密文、nonce 与后四位；主密钥与明文不落库。Provider API 只返回 `has_key`/
  后四位，不返回明文；日志与审计在落盘/回灌前经过 `redact_secrets`。
  `test_phase_f_security.py` 会按字节扫描该表密文列与 SQLite 文件，断言
  `sk-`/`ghp_`/`AKIA` 等明文片段不存在；主密钥不随备份分发，轮换由
  `rotate_secret_master_key.py` 事务重加密并递增 `key_version`；
- **内部边界**：A2/A3 只绑 loopback，内部接口必须 token，路径越界在文件工具
  与 ExecutionService 两处拒绝；
- **迁移/回滚**：SQLite 与 MySQL 都完成
  `upgrade → 备份 → downgrade base → upgrade → 恢复` 演练；
- **性能基线**：`phase_f_bench.py` 覆盖 A2 启动 → `/healthz` 就绪、
  ExecutionService 代理、控制面写入与 A2 代理 SSE 首事件延迟；不设硬阈值，
  按同平台趋势比较。P4 将 A3 runtime 改为延迟导入后，A2 启动从约 27s
  降到约 5.6s；
- **平台门禁**：WSL 隔离用 `pytest -m wsl`，Linux bwrap 用
  `pytest -m execution` / `test_bwrap.py`；CI 只跑 SQLite 演练与快反馈，
  平台缺口在 [ACCEPTANCE.md](../ACCEPTANCE.md) 中显式保留。

P1 已把 A2↔A3 DTO、事件传输与工作区浏览下沉到 `agent_protocol` /
`event_transport` / `workspace`；`agent_server` 不再依赖 `web/` 或
`control.agent_client`。同进程回退已隔离到 `web/local_runtime.py`；
CLI 的沙箱探测已收窄到 `cli/platform.py`。A2 的系统/能力路由已拆到
`web/routes/system.py` / `providers.py` / `sessions.py` / `workspace.py` /
`uploads.py` / `ops.py` / `snapshots.py` / `runs.py` / `desktop.py` /
`compact.py`，A3 的 RunState/SSE 与租户上下文已拆到
`agent_server_support.py` / `agent_context.py`，A3 路由已全部移到
`servers/agent_server_routes/{lifecycle,workspace,state}.py`；
`create_agent_app` 已降到 321 行，只负责装配。A2 的会话目录/Provider/
Agent 代理已下沉到 `web/services.py`，`create_app` 降到 758 行。

P2 把 `runtime._stream`、scheduler、`run_tools`、implement 与 CLI/TUI 事件
分发拆小或表驱动化；P3 新增宿主执行白名单 `execution/host_ops.py`（ADR-0011），
并把 implement / control_server / sandbox_backend / 桌面入口的覆盖率拉到目标；
P4 延迟导入 A3 runtime，缩短 A2 启动。

详见 [PHASE_F.md](phases/PHASE_F.md)、[COVERAGE.md](../COVERAGE.md)、
[COMPLEXITY.md](../COMPLEXITY.md)。
