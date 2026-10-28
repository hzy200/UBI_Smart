---
layer: L3
doc_id: phase-b
title: Phase B：控制面抽取（首切片）
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: A2 schema、会话持久化或事件传输变动时
---

# Phase B：控制面抽取（首切片）

Phase B 的目标是把 A2 从单进程 Web 层里抽出来。本切片完成 **B1 schema/接口 +
B2 会话目录接入 + B3 SSE 事件传输重放 + B4 进程拆分与剩余 Agent 路由迁移
（B4.2）**，默认行为不变；消费者组、原生服务打包与前端 TS 迁移留到后续切片。

## 1. 已完成

### 1.1 A2 数据 schema

新增 `src/coding_agent/control/schema.py`，同一套 SQLAlchemy 模型跑 MySQL 与 SQLite：

| 表 | 内容 |
|---|---|
| `control_sessions` | 会话目录、标题、tombstone、上传计数 |
| `control_runs` | 运行索引（状态、起止、错误、token/成本） |
| `control_audit_index` | 审计查询索引（引用 hash，不重写链） |
| `control_provider_profiles` | Provider 档案（不含 API Key） |
| `control_usage_daily` | 按天/租户/provider/model 的用量汇总（预留表） |
| `control_meta` | schema 版本 |

企业版迁移 `0010_accounts_usage_quota` + `0011_cny_reservations_auth_events` 追加：

| 表 | 内容 |
|---|---|
| `control_users` | 企业成员、角色、scrypt 密码哈希、状态 |
| `control_auth_sessions` | 登录会话哈希、CSRF secret、过期/吊销 |
| `control_auth_events` | 登录成功/失败/登出/限流事件 |
| `control_provider_keys` | 上游 Key 的 AES-256-GCM 密文、nonce、后四位 |
| `control_user_quotas` | 每日 token + 人民币（CNY）成本额度 |
| `control_user_usage_daily` | 按用户/天的 token、worker token、USD 成本、预留 |
| `control_quota_reservations` | 运行期配额预留，带 owner/expires_at |
| `control_usage_events` | `run_end` 快照，按 `record_hash` 幂等 |
| `control_usage_cursor` | 同 user/thread/run_epoch 的累计游标，用于 resume 差值 |

约束：不存 checkpoint、工作区文件、明文 API Key、Agent 运行时状态。
生产列变更只走 Alembic；`create_all` 仅用于本地测试/首次空库。
完整的 MySQL 建库、保留、备份与运维见 [DATABASE.md](DATABASE.md)。

### 1.2 会话目录接入

`AGENT_CONTROL_DB_URL` 非空时，`web/app.py` 用 `ControlStore` 读写会话配置：

- 启动后首次加载会把旧 `gui-sessions.json` 导入数据库一次；
- 之后以数据库为准，旧 JSON 只作为备份保留；
- 未设置 URL 时行为完全不变（仍写 `gui-sessions.json`）；
- 删除仍是 tombstone：列表隐藏、数据库行保留、审计/checkpoint 不动；
- 单用户租户键为认证用户名（当前 `local`），多用户为实际用户名。

当前仍是**单进程内接入**：A2 与 A3 共用 `create_app()`；拆进程与内部 API
在下一阶段做。

### 1.3 SecretProvider 接口

`src/coding_agent/control/secrets.py` 定义两档：

- `KeyringSecretProvider`：桌面 OS 密钥环；
- `SystemdCredsSecretProvider`：服务器 `$CREDENTIALS_DIRECTORY` 只读凭据。

`AGENT_SECRET_PROVIDER=auto|keyring|systemd` 选择；`auto` 在有
`CREDENTIALS_DIRECTORY` 时优先 systemd，否则 keyring。WSL 没有 Secret Service
时不落盘；A2 在后续切片负责按运行短期注入 A3。
当前尚未替换 `web/app.py` 内的密钥读写，接口与测试先落地。

### 1.4 共享事件传输与 SSE 重放

`src/coding_agent/control/transport.py`：

- `MemoryEventTransport`：单实例默认，按 thread 保留最近 N 条，支持 `after`；
- `RedisStreamEventTransport`：Redis Streams 适配器（`seq` + `data`、近似
  `maxlen`、TTL、按 `seq` 重放），客户端可注入便于测试；
- `build_event_transport(settings)`：`memory|redis`，redis 必须提供 URL。

`web/app.py` 已接入：

- 运行时每发一个事件，同时写入本地 `StreamEntry` 与 EventTransport；
- 新流起始 `seq = max(本地旧流, transport.latest_seq)`，重启后不覆盖旧序号；
- `/api/stream/{thread}` 找不到本地流时，从 transport 按 `after`/`Last-Event-ID`
  重放；已追平返回 204；未收尾则每 0.5s 轮询新事件，15s keepalive；
- 看到 `run_finished`/`run_failed`/`approval_requested`/pause 即结束重放；
- 传输故障只记录日志，不伪造事件，也不中断正在运行的任务。

投递语义为 at-least-once + `seq` 去重；MySQL 只做索引，不承担热重放。
当前仍是轮询式重放，不是 Redis consumer group；积压监控与跨 A2 运行接管
留到后续切片。

### 1.5 独立 Agent Server 与 A2 内部客户端

新增 `src/coding_agent/agent_server.py`（`coding-agent-agent` 入口）：

- 只绑 loopback，必须配置 `AGENT_INTERNAL_TOKEN`；
- A2 用 `Authorization: Bearer` + `X-Agent-Context`（base64 JSON：
  tenant/user/workspace/access_mode/model/provider/allow_write）调用；
- 按租户隔离 audit/checkpoint/steering/memory 目录；
- 已迁移运行生命周期：`/v1/runs`、`/v1/stream`、`/v1/resume`、
  `/v1/resume_steering`、`/v1/steer`、`/v1/approvals`、`/v1/status`、
  `/v1/history`；
- SSE 原始字节透传，`A2` 不重新序列化事件；序号跨 resume 连续；
- A2 配置 `AGENT_INTERNAL_URL` 后自动使用 `HttpAgentClient`；
  未配置时完全走原同进程路径。

B4.2 继续迁移了：

| 领域 | Agent Server 端点 | A2 行为 |
|---|---|---|
| 文件/Git/项目环境 | `/v1/files`、`/v1/file`、`/v1/git`、`/v1/project` | 代理，A2 不读 WSL 工作区 |
| 快照/回滚 | `/v1/snapshots`、`/v1/diff`、`/v1/restore` | 代理 |
| checkpoint | `/v1/run_started`、`/v1/checkpoints`、`/v1/fork`、`/v1/rewind` | 代理；fork/rewind 后 A2 新建会话配置 |
| 压缩 | `/v1/compact` | 代理并记录新 thread |
| 记忆 | `/v1/memory` | 代理 |
| 上传 | `/v1/uploads`（POST/GET/DELETE） | 代理，A2 同步 uploaded_bytes |
| 审计 | `/v1/audit` | 代理；配置控制面 DB 时写入审计索引 |
| 会话/恢复 | `/v1/sessions`、`/v1/recovery` | 代理并与 A2 会话配置合并 |

**密钥注入**：A2 把当前用户的 provider key 放进 `AgentContext.api_keys`
（base64 `X-Agent-Context`），只随 run/resume 请求发送；Agent Server 用
内存 `_ContextSecretStore` 构造 runtime，密钥不落盘、不进日志。未配置 key
时仍由 Agent Server 报缺 key。

**审计索引回传**：当前是**拉取式**——A2 调 `/api/audit` 时从 Agent Server
取记录，并在配置了 `AGENT_CONTROL_DB_URL` 时幂等写入
`control_audit_index`。不是实时推送，也没有断线补采。

**当前迁移边界**：A2 的认证/租户/会话目录/Provider/配额仍由 A2 负责；
Agent Server 不持有控制面数据库。恢复中心的 pending approval 扫描仍主要
依赖 Agent Server 内存状态，进程重启后只按审计/steering 判断。

### 1.6 原生服务打包

新增 `src/coding_agent/control_server.py`（`coding-agent-control`）与
`scripts/build_services.py`：

```bash
python scripts/build_services.py --target agent
python scripts/build_services.py --target control
python scripts/build_frontend.py
```

产物：

- `dist/CodingAgentAgent/CodingAgentAgent.exe`（A3+A4）
- `dist/CodingAgentControl/CodingAgentControl.exe`（A2）

部署文件：

- Linux：`packaging/systemd/coding-agent-agent.service`、
  `coding-agent-control.service`、两份 `.env.example`；
  服务用户 `coding-agent`，数据根 `/var/lib/coding-agent`，
  `ProtectSystem=strict` + `ReadWritePaths`；
- Windows：`packaging/windows/install-startup-task.ps1` 注册 SYSTEM 开机任务
  （Task Scheduler），环境文件 ACL 只给 SYSTEM/Administrators。
  这是**开机任务**，不是 SCM 服务；需要真正的 Windows Service 时可在外层
  套 NSSM 或 pywin32 包装。A2 默认只绑 loopback，公网必须反向代理 + TLS。
- 两份 env 示例都要求 `AGENT_INTERNAL_TOKEN`；控制面额外配置
  `AGENT_CONTROL_DB_URL` 与 `AGENT_INTERNAL_URL`。

Linux 安装概要：

```bash
sudo useradd --system --home /var/lib/coding-agent coding-agent
sudo install -d -o coding-agent -g coding-agent /var/lib/coding-agent \
  /srv/coding-agent/workspaces /opt/coding-agent/bin
sudo cp dist/CodingAgentAgent/CodingAgentAgent* /opt/coding-agent/bin/
sudo cp dist/CodingAgentControl/CodingAgentControl* /opt/coding-agent/bin/
sudo install -d /etc/coding-agent
sudo install -m 600 -o coding-agent packaging/systemd/agent.env.example /etc/coding-agent/agent.env
sudo install -m 600 -o coding-agent packaging/systemd/control.env.example /etc/coding-agent/control.env
# 填好 token/数据库后：
sudo cp packaging/systemd/coding-agent-*.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now coding-agent-agent coding-agent-control
```

Windows 安装概要：把 `dist/CodingAgentAgent`、`dist/CodingAgentControl` 复制到
`%LOCALAPPDATA%\Programs\CodingAgent`，运行
`packaging/windows/install-startup-task.ps1 -Target agent|control`；
环境文件在 `%PROGRAMDATA%\CodingAgent\<target>.env`。

### 1.7 前端 esbuild / TypeScript 首切片

- 源文件迁移到 `src/coding_agent/web/frontend/src/app.ts`
  （原 `static/app.js` 的内容），`main.ts` 负责入口，`contracts.ts` 提供
  最小事件/会话类型；
- `static/app.js` 现在由 esbuild 生成（IIFE、无 `eval`，保持 CSP 兼容），
  **不要直接编辑**；
- 构建：

```bash
python scripts/build_frontend.py   # 直接走 npx esbuild，无需 node_modules
# 或：
cd src/coding_agent/web/frontend
npm install && npm run build
```

- 当前环境用 `npx esbuild` 完成构建；`tsc --noEmit` 需要先
  `npm install`。`app.ts` 仍是 JS 风格，后续再拆模块与收紧类型；
- 生成的 `static/app.js` 带 `Generated by esbuild` 头，CI 契约测试会校验。

## 2. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_CONTROL_DB_URL` | 空 | 空 = 旧 JSON；例 `mysql+pymysql://user:pass@127.0.0.1/coding_agent?charset=utf8mb4` |
| `AGENT_CONTROL_DB_AUTO_CREATE` | `true` | 开发/首次建表；生产建议关掉并走迁移 |
| `AGENT_SECRET_PROVIDER` | `auto` | `auto` / `keyring` / `systemd` |
| `AGENT_EVENT_TRANSPORT` | `memory` | `memory` / `redis` |
| `AGENT_EVENT_TRANSPORT_URL` | 空 | Redis URL；`redis` 模式必填 |
| `AGENT_EVENT_TRANSPORT_PREFIX` | `coding-agent:events` | stream key 前缀 |
| `AGENT_EVENT_TRANSPORT_MAXLEN` | `10000` | 每 thread 近似保留条数 |
| `AGENT_EVENT_TRANSPORT_TTL_S` | `3600` | stream TTL |
| `AGENT_INTERNAL_URL` | 空 | 空 = 同进程；设置后运行生命周期走独立 Agent Server |
| `AGENT_INTERNAL_TOKEN` | 空 | A2↔A3 内部 token；Agent Server 启动必填 |
| `AGENT_AGENT_DATA_ROOT` | 空 | Agent Server 租户数据根；空 = 审计目录旁的 `agent/` |

依赖：SQLAlchemy 已是主依赖；MySQL 驱动与 Alembic 在 `.[db]`，
Redis 客户端在 `.[events]`。

## 3. 迁移与回滚

**MySQL 首次迁移**：

1. 先备份 `gui-sessions.json` 与数据库目录；
2. 配置 `AGENT_CONTROL_DB_URL`，启动 A2；
3. 首次加载自动导入旧会话；检查 `control_sessions` 行数；
4. 保留旧 JSON 至少一个发布周期。

**回滚**：清空 `AGENT_CONTROL_DB_URL`，A2 回到 JSON。注意回滚后数据库中的
新会话不会自动写回 JSON，需要人工导出/迁移。

**多实例注意**：当前 `save_sessions` 是“整租户快照 upsert”，两个 A2 同时写
同一租户会 last-write-wins；A2 独立进程/多实例前需要改成按会话行的事务写
或加版本列。

## 4. 本切片测试

- `tests/unit/test_control_store.py`：
  - schema 表齐全且可编译 MySQL DDL；
  - SQLite 会话 roundtrip、tombstone、租户隔离；
  - Web 重启后从数据库恢复；
  - 旧 JSON 只导入一次；
  - 未配置 DB 时仍走文件。
- `tests/unit/test_control_services.py`：
  - keyring/systemd 两档；
  - 内存/Redis 传输的发布、按 seq 重放与 latest_seq。
- `tests/unit/test_web_stream_buffer.py`：
  - 本地流消失后从 transport 重放；
  - 重启后新流序号接续；
  - 审批事件使重放停止；
  - 已追平返回 204。
- `tests/unit/test_agent_server.py`：
  - Agent Server token/context 鉴权；
  - run SSE、审批 resume 序号连续；
  - A2 通过 `HttpAgentClient` + ASGI 传输端到端代理；
  - 文件/快照/记忆/审计/上传代理；
  - A2 provider key 短期注入 A3；
  - 内部错误码透传（401）。
- `tests/unit/test_control_store.py`：
  - 审计索引幂等写入。

## 5. 尚未完成

- 恢复中心的 pending approval 扫描在 Agent 重启后仍不完整；
- 审计索引是拉取式，不是实时推送/断线补采；
- Redis consumer group、积压监控与跨 A2 运行接管（当前是轮询重放）；
- MySQL Alembic 迁移、按会话行写、多实例并发控制；
- 真正的 Windows SCM 服务包装（当前是 SYSTEM 开机任务）；
- 前端 `app.ts` 的模块拆分与严格类型检查（当前是 esbuild 首切片）。

## 6. 退出条件（本切片）

- [x] 默认部署行为不变，旧 JSON 路径仍可用；
- [x] 配置 DB 后会话目录可跨 A2 重启恢复；
- [x] 旧会话一次性导入；
- [x] SecretProvider 与事件传输接口有测试；
- [x] SSE 默认内存、配置 Redis 后可按 seq 跨 A2 重启重放；
- [x] 独立 Agent Server 提供运行生命周期 API，A2 可通过 HTTP 代理；
- [x] 文件/Git/快照/checkpoint/记忆/上传/审计/会话索引路由已迁移；
- [x] A2 provider key 短期注入 A3 内存，不落盘/日志；
- [x] 审计索引在 A2 查询时幂等回传 MySQL（拉取式）；
- [x] A2/A3 原生可执行目录（`CodingAgentControl` / `CodingAgentAgent`）；
- [x] Linux systemd unit、Windows SYSTEM 开机任务与环境文件示例；
- [x] 前端切到 `frontend/src` + esbuild 生成 `static/app.js`，CSP 兼容；
- [x] 未配置内部 URL 时行为不变，Standalone/A2 同进程仍可用；
- [x] schema 同时兼容 SQLite/MySQL（DDL 级别）；
- [x] 文档、契约与测试通过。
