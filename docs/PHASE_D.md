---
layer: L3
doc_id: phase-d
title: Phase D：双平台部署与版本握手
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 部署拓扑、A2↔A3 握手协议或控制面数据库变动时
---

# Phase D：双平台部署与版本握手

Phase D 把已完成的 A2/A3/A4 零件接成目标部署形态：

```text
Windows：pywebview → A2 原生进程 + MySQL → WSL2 A3+A4
Linux：  浏览器   → A2 systemd + MySQL    → 原生 A3+A4
```

本阶段不新增 Agent 能力，只改部署链路、握手、健康检查、迁移与恢复。

## 1. 已完成：D1 版本/能力握手

### 1.1 协议契约

`src/coding_agent/__init__.py` 新增 `AGENT_API_VERSION = 1`；只有破坏性变更
才递增，A2 拒绝不同主版本。

`src/coding_agent/control/protocol.py`：

| 对象 | 作用 |
|---|---|
| `AGENT_CAPABILITIES` | A3 承诺的能力分组：run_lifecycle / workspace / state / execution |
| `AgentMeta` | `service / version / api_version / execution_backend / capabilities` |
| `check_compatibility()` | 协议版本必须一致；包版本不同只记警告 |

### 1.2 端点

| 端点 | 鉴权 | 返回 |
|---|---|---|
| A3 `GET /healthz` | 无 | `status / service / version / api_version` |
| A3 `GET /v1/meta` | 内部 token | 完整 `AgentMeta` |
| A2 `GET /healthz` | 无 | `status / version / agent_state` |
| A2 `GET /api/capabilities` | 用户认证 | 额外包含 `agent` 状态与元信息 |

### 1.3 A2 启动握手

`control_server.py` 在 uvicorn 启动前调用一次 `HttpAgentClient.probe()`：

- `ok`：版本兼容，正常启动；
- `incompatible`：写入 stderr，A2 仍启动，前端可见明确原因；
- `unavailable`：A3 未就绪，A2 仍启动，状态为 `unavailable`；
- 未配置 `AGENT_INTERNAL_URL`：`in_process`，保持原单进程行为。

A2 与 A3 的所有内部 HTTP 调用都显式 `trust_env=False`，避免 Windows 用户级
代理把 `127.0.0.1` 请求转走。

### 1.4 验证

- `tests/unit/test_agent_handshake.py` 覆盖版本、token、不可达、不兼容与
  A2 健康状态；
- `tests/unit/test_agent_server.py`、`test_web_auth.py`、`test_layer_contracts.py`
  回归通过；
- 真实进程验证：本机启动 A3 与 A2 后，
  `GET /healthz` 返回 `{"agent_state":"ok"}`；
  `GET /v1/meta` 无 token 返回 401，带 token 返回完整能力清单。

### 1.5 已知边界

- D2 已改为周期探测，见下节。

## 2. 已完成：D2 桌面双进程拓扑

### 2.1 目标拓扑

```text
pywebview → Windows A2（control_server，127.0.0.1:8780）
                    │ loopback + AGENT_INTERNAL_TOKEN
                    ▼
              WSL A3（agent_server，127.0.0.1:8790）
```

桌面不再启动旧的单进程 `coding_agent.server`；`--host-mode` 仍保留旧
Windows 宿主模式一个版本，standalone 不受影响。

### 2.2 Supervisor

`src/coding_agent/desktop/supervisor.py`：

- `start_agent()`：WSL 内启动 `coding_agent.agent_server`，默认端口 8790；
- `start_control()`：Windows 本机启动 `coding_agent.control_server --desktop`，
  默认端口 8780，并把 `AGENT_INTERNAL_URL/TOKEN` 注入环境；
- 端口可用性在 Windows 侧预检；被占用时直接报错并提示
  `AGENT_DESKTOP_AGENT_PORT` / `AGENT_DESKTOP_CONTROL_PORT`；
- token 每次启动随机生成，只保存在 supervisor 内存；重启复用同一 token
  与端口，A2 的 `AGENT_INTERNAL_URL` 不需要热更新；
- 打包后优先使用同目录 `CodingAgentControl.exe`，开发模式用
  `python -m coding_agent.control_server`。

### 2.3 入口参数

- `agent_server.py`：新增 `--token-stdin`、`POST /v1/cancel_all`、
  `POST /v1/shutdown`；SIGTERM/`/v1/shutdown` 先置 draining（拒绝新运行、
  `/readyz` 503），等待进行中运行至 `AGENT_DRAIN_TIMEOUT_S` 后再由
  `Server.should_exit` 收尾；第二次信号可强制退出。
- `control_server.py`：新增 `--desktop`、`--token-stdin`；桌面模式启用
  `/api/desktop/*` 且强制单用户；同样使用 `uvicorn.Server`，让关窗时
  `cancel_all → shutdown` 生效。

### 2.4 周期探测

`web/app.py` 在配置了 `agent_client` 时启动后台探测（5 秒间隔）：

- 成功 → `agent_state=ok`，并刷新能力元信息；
- 失败 → `agent_state=unavailable`；
- A3 重启后自动回到 `ok`，A2 不需要重启。

### 2.5 看门狗与关闭顺序

- `watchdog.supervise_many()` 同时守护 A2/A3；单个进程退出后按 1/3/7 秒
  退避重启，重启失败返回 `failed:<name>`；
- 关窗顺序：A2 `cancel_all` → A2 shutdown → A3 `cancel_all` → A3 shutdown；
- A2 重启复用原端口/token，页面 URL 不变；A3 重启由 A2 周期探测自动恢复。

### 2.6 验证

- 单测：`test_desktop_topology.py`、`test_desktop_launcher.py`、
  `test_phase5_desktop.py` 全部通过；
- 真实进程：A2 桌面模式 `/healthz` 返回 `agent_state=ok`，`/api/desktop/cancel_all`
  返回 200；杀掉 A3 后 A2 变为 `unavailable`，重启 A3 后恢复 `ok`；
- 本机 WSL 未安装项目依赖，完整 WSL 启动链需在打包后的发行版或装好依赖的
  WSL 环境中验收。

### 2.7 已知边界

- `cancel_all` 在 A3 侧按运行逐个 `steer(cancel)`；正在执行的长命令仍由
  进程退出兜底，依赖 ToolJournal/回执判断结果未知；
- A2 周期探测是单实例内存态，多实例部署需要共享健康状态（D4 之后再评估）。

## 3. 已完成：D3 控制面数据库生产化

### 3.1 Alembic 迁移

- `control/alembic.ini` + `control/migrations/`：
  - `0001_control_schema` 与 `control/schema.py` 对齐；
  - 新库 `upgrade head` 建表并写 `alembic_version`；
  - Phase B 用 `create_all` 建的旧库没有 `alembic_version`，先 `stamp head`
    标记基线，不重复建表；
- `control/migrate.py` 提供 `upgrade()` / `current_revision()`；
- `control_server --migrate` 在启动前升级，迁移前自动备份；失败只告警，
  由降级路径兜底；
- 桌面 supervisor 在 `AGENT_CONTROL_DB_URL` 非空时自动加 `--migrate`；
- 打包脚本把 `alembic.ini` 与 `migrations/` 一起放入 `CodingAgentControl`。

配置示例：

```ini
AGENT_CONTROL_DB_URL=mysql+pymysql://coding_agent:change-me@127.0.0.1:3306/coding_agent?charset=utf8mb4
```

最小权限初始化脚本：`packaging/mysql/init.sql`。

### 3.2 连接池

`ControlStore` 对 MySQL 启用：

- `pool_size=5`、`max_overflow=10`、`pool_recycle=1800`、
  `pool_timeout=30`、`pool_use_lifo=True`、`pool_pre_ping=True`；
- SQLite 保持默认池，不接受上述 QueuePool 参数；
- `ControlStore.ping()` 提供轻量连通性检查。

### 3.3 只读降级

数据库初始化或运行中写入失败时：

- A2 仍然启动，`/healthz` 的 `db_state=degraded`；
- `/api/capabilities` 返回 `db` 状态与错误摘要；
- 新建、重命名、删除、切换模型返回 503；
- 已有会话的运行与 SSE 不受影响；列表回退到本地会话文件；
- 恢复数据库后重启 A2 回到 `ok`（周期自动恢复不在本轮范围）。

### 3.4 备份与恢复

- `scripts/backup_control_db.py`：SQLite 复制（先 WAL checkpoint）或
  MySQL `mysqldump --single-transaction`；默认保留 7 份；
- `scripts/restore_control_db.py`：恢复前默认备份当前库；
- MySQL 密码通过 `MYSQL_PWD` 传递，不出现在命令行；
- 处置步骤写入 `docs/runbook.yaml`（RB-016 / RB-017）。

### 3.5 验证

- `test_control_migrations.py`：新库升级、幂等、旧库 stamp；
- `test_control_backup.py`：SQLite 备份 → 修改 → 恢复；
- `test_control_degraded.py`：启动降级、写失败切降级、503 边界；
- `test_control_store.py` 增加 MySQL 有界连接池断言；
- 真实 MySQL 8.0.27 验证：迁移 → 写入 → mysqldump → 修改 → 恢复 →
  revision 保持，连接池 `size=5`；
- `tests/integration/test_control_mysql.py` 提供可重复的 MySQL 契约；
  默认跳过，设置 `AGENT_TEST_MYSQL_URL` 且库名包含 `test` 时运行：
  `pytest -m mysql`
- CI 安装增加 `db` extras，SQLite 迁移测试进入快反馈门禁。

### 3.6 已知边界

- CI runner 没有 MySQL，MySQL 契约只在本地用 `AGENT_TEST_MYSQL_URL` 跑；
- 运行中数据库恢复后需要重启 A2，尚未做自动重连；
- Alembic 旧库 stamp 假设表结构已经与 `0001` 对齐；若旧库有手工改列，
  需要先人工核对；
- MySQL 不允许 TEXT 列带字面量默认值，`0001` 已按此处理；后续迁移遇到
  TEXT/BLOB 默认值同样需要在 ORM 侧兜底。

## 4. 已完成：D4 Linux systemd 与安装脚本

### 4.1 systemd 单元

`packaging/systemd/`：

| 单元 | 关键设置 |
|---|---|
| `coding-agent-agent.service` | `StateDirectory=coding-agent`（0700）、`ReadWritePaths=/srv/coding-agent/workspaces`、`ProtectSystem=strict`、`ProtectHome=read-only`、`NoNewPrivileges`、`PrivateTmp`、`UMask=0077` |
| `coding-agent-control.service` | `After=... coding-agent-agent.service`、`Wants=...`、`ExecStartPre=... --migrate-only`、`ProtectHome=true`、`PrivateDevices=true`、`RestrictNamespaces=true` |

A2 的 `ExecStartPre` 迁移失败会阻止 A2 启动；未配置数据库时
`--migrate-only` 直接返回 0。A3 不执行迁移。

### 4.2 安装脚本

`packaging/systemd/install.sh`：

- 创建系统用户 `coding-agent`、`/var/lib/coding-agent`、`/srv/coding-agent/workspaces`、
  `/etc/coding-agent`；
- 支持两种安装源：PyInstaller 目录（默认 `dist/CodingAgent*`）或
  `--python /path/to/venv/bin/python` 源码模式；
- 首次安装生成内部 token 与前端 token，写入 `agent.env` / `control.env`，
  另存 `install-secrets.txt`（0600）；已存在 env 时拒绝覆盖；
- env 文件 `0640 root:coding-agent`；数据目录 `0700`；
- 安装/启用 systemd 单元，启动后轮询 A2/A3 `/healthz`；
- `--no-start`、`--uninstall`（保留配置与数据）。

`agent.env.example` / `control.env.example` 补齐数据目录、执行后端、
备份目录与 credential 说明。

### 4.3 验证

- `test_linux_deploy.py`：单元关键指令（顺序、迁移、权限）、安装脚本
  `bash -n`、`--migrate-only` 空 URL 跳过与 SQLite 建表；
- WSL Ubuntu 24.04 + systemd 255 完整演练通过：
  - `install.sh --python /opt/coding-agent/venv/bin/python --no-start`
    创建用户/目录/env/单元，权限符合预期（env 0640、数据目录 0700）；
  - `systemctl start` 后 A3 `/healthz` 正常，A2 `agent_state=ok`；
  - 给 `control.env` 配置 SQLite URL 后重启，`ExecStartPre` 输出
    `control_db_migrated`，`alembic_version=0001_control_schema`，
    `db_state=ok`；
  - 通过 A2 创建/列出/删除会话成功；重启 A2 后会话仍在；
  - 重启 A3 后 A2 周期探测自动回到 `agent_state=ok`；
  - `install.sh --uninstall` 停止并移除程序/单元，保留配置与数据。
- `systemd-analyze verify` 解析通过；只报告 `/opt` 二进制不存在（安装前）
  与 `/mnt` 文件系统权限提示。

### 4.4 已知边界

- 演练使用 SQLite；MySQL 在 systemd 下的 `ExecStartPre` 迁移未重复演练，
  D3 已单独验证 MySQL 迁移/备份恢复；
- 真实 run/工具执行需要 API Key，留到 D5 的 smoke 演练；
- systemd 单元默认 A2/A3 同机 loopback；跨机部署需要 TLS/mTLS 与反向代理；
- 服务器密钥应使用 systemd Credentials/KMS，安装脚本只生成传输 token；
- 工作区根默认 `/srv/coding-agent/workspaces`，如改路径需同步
  `ReadWritePaths` 与 `agent.env`。

## 5. 已完成：D5 故障演练

### 5.1 `phase_d_smoke.py`

`scripts/phase_d_smoke.py` 在本机启动 loopback A2/A3，不依赖 WSL、MySQL
或 API Key：

| # | 场景 | 断言 |
|---|---|---|
| 1 | A3/A2 启动与握手 | A3 healthz ok；A2 `agent_state=ok` |
| 2 | 会话 CRUD | 创建/列表/tombstone 删除通过 |
| 3 | A3 崩溃 | A2 变为 `unavailable`；A3 重启后恢复 `ok` |
| 4 | A2 重启 | SQLite 中会话仍在 |
| 5 | 数据库不可达 | `db_state=degraded`；GET 200 / POST 503 |
| 6 | 版本不匹配 | A2 `agent_state=incompatible` |
| 7 | 迁移失败 | `--migrate-only` 非零退出并输出失败事件 |

运行：

```bash
python scripts/phase_d_smoke.py
python scripts/phase_d_smoke.py --keep        # 保留临时目录与日志
```

### 5.2 验证

- 本机（Windows + conda agent）实际运行：**7/7 通过**；
- `test_phase_d_smoke.py` 锁定脚本入口与辅助函数，完整演练不进入快反馈；
- 脚本显式绕过系统代理访问 loopback。

### 5.3 未覆盖

- 真实 LLM run、工具执行、审批、快照回滚需要 API Key 与可执行沙箱，
  未纳入协议级 smoke；发版前用 `agent run` / 桌面手工验收补一次；
- 多实例共享事件传输（Redis）与跨机部署不在本轮。

## 6. 已完成：D6 文档与 CI 门禁

- `ACCEPTANCE.md` 增加 5.14 Phase D 验收、自检命令与未验证项；
- `acceptance-criteria.yaml` 增加 REQ-119…128（握手、双进程、探测恢复、
  迁移、连接池、降级、备份、systemd、安装脚本、故障演练）；
- `ARCHITECTURE.md` 第 12/13 节更新为 Phase B–D 已落地，新增第 17 节
  Phase D 部署状态；
- `USAGE.md` / `ENVIRONMENT.md` 增加 Linux 安装、备份/恢复与故障演练入口；
- CI 安装 `db` extras，并在执行契约之后运行 `phase_d_smoke.py`；
  D4 的单元文件/安装脚本断言由 `test_linux_deploy.py` 进入快反馈。

## 7. Phase D 退出状态

| 维度 | 状态 |
|---|---|
| 功能 parity | 未删减；D5 协议 smoke 与原测试通过 |
| 数据分域 | MySQL 无 checkpoint/文件/Key；A2 不执行工具 |
| Windows | A2/A3 双进程、握手、崩溃恢复已验证；桌面 WSL 完整启动链待打包验收 |
| Linux | systemd 安装、启动、迁移、A2/A3 重启恢复演练通过 |
| 前端 | 属于方案 Phase E，D 阶段未补齐主题/虚拟滚动/思维链可视化 |
| 安全 | 内部 token、loopback、systemd 加固、MySQL 最小权限脚本 |
| 性能 | A2↔A3 只传控制/事件；工作区与工具 I/O 留在 A3/A4 |
| 故障 | `phase_d_smoke.py` 7/7；WSL 安装/重启/迁移演练通过 |

### 剩余边界

- Windows 桌面 WSL 完整启动链需要在打包发行版中验收；
- 真实 LLM run、工具、审批、快照未纳入协议级 smoke；
- 前端 Phase E 能力与 MySQL 在 systemd 下的重复演练。
