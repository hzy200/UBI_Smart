---
layer: L2
doc_id: database
title: 控制面数据库：MySQL 长期存储与运维
owner: 项目作者
status: active
last_reviewed: 2026-10-28
review_cadence: 控制面 schema、迁移、备份或企业账号数据边界变动时
---

# 控制面数据库（MySQL）

A2 的结构化业务数据统一写入 `AGENT_CONTROL_DB_URL`。企业部署使用
**MySQL 8.0/8.4**；SQLite 只保留给本地开发与单元测试。

## 1. 数据边界

### 存入 MySQL

| 领域 | 表 | 说明 |
|---|---|---|
| 账号 | `control_users` | 用户名、邮箱、角色、访问上限、scrypt 密码哈希、状态 |
| 登录会话 | `control_auth_sessions` | 只存 session token 的 sha256、CSRF secret、过期/吊销 |
| 认证事件 | `control_auth_events` | 登录成功/失败/登出/限流/MFA/OIDC；来源 IP 与 request id |
| 身份绑定 | `control_user_identities` | `(tenant, issuer, subject)` 到本地用户的唯一映射 |
| OIDC state | `control_oidc_states` | 一次性 state/nonce/PKCE verifier 与回跳路径；消费即删 |
| MFA 密钥 | `control_user_mfa` | TOTP 密钥 AES-GCM 密文、恢复码哈希、上次使用时间步 |
| MFA 挑战 | `control_mfa_challenges` | 登录第二步的 token 哈希、失败次数、过期时间 |
| 四眼审批 | `control_approvals` | L3 审批命令、发起人、状态、管理员决定；按 `(tenant, thread, call)` 唯一 |
| 工作区 ACL | `control_workspace_acl` | 只读共享授权：`(tenant, workspace_id, grantee)` 唯一；只存 read，不含执行权 |
| 密码重置 | `control_password_resets` | 一次性 token 的 sha256、过期与使用时间；成功消费即失效 |
| 会话目录 | `control_sessions` | 标题、workspace 绑定、模型/provider、未读/置顶/归档 |
| 运行索引 | `control_runs` | 状态、起止、错误、token/成本副本 |
| 审计索引 | `control_audit_index` | 审计哈希引用，不重写 A3 哈希链 |
| 用量 | `control_user_usage_daily` | 按用户/天的 token、worker token、成本、运行次数、预留 |
| 用量快照 | `control_usage_events` | 每个 `run_end` 的累计快照，按 `record_hash` 幂等，带 user/thread/epoch |
| 用量游标 | `control_usage_cursor` | 同一 tenant/user/thread/run_epoch 的上次累计值，用于 resume 差值 |
| 配额 | `control_user_quotas` | 每日 token 上限 + 人民币成本上限与启用开关 |
| 配额预留 | `control_quota_reservations` | 运行期预留，带 owner/expires_at，支持多实例 |
| 上游 Key | `control_provider_keys` | AES-256-GCM 密文、nonce、后四位；**不存明文** |
| Provider 档案 | `control_provider_profiles` | 多用户模式下只读；不含 Key |
| 工作区 | `workspaces` / `workspace_revisions` | 工作区身份、配额、导入快照元数据 |
| 运行协调 | `run_leases` / `idempotency_keys` | 跨 A2/A3 租约与短期幂等响应 |
| schema 版本 | `control_meta` / `alembic_version` | 迁移版本 |

### 不存入 MySQL

- A3 的 checkpoint、pending approval、steering 状态；
- 审计 JSONL 原始哈希链（MySQL 只存查询索引）；
- 工作区文件字节、Git 对象、上传附件；
- provider Key 明文、日志、前端可读凭证。

这些属于执行面/文件面，迁移代价高且会改变 A3 的单写者语义，暂不进入控制面。
企业账号、额度、用量与审计索引已经全部在 MySQL 中。

## 2. 初始化

### 2.1 建库与最小权限账号

```bash
mysql -u root -p < packaging/mysql/init.sql
```

脚本创建：

- `coding_agent` 运行账号：仅 `SELECT/INSERT/UPDATE/DELETE`；
- `coding_agent_migrator` 迁移账号：额外 `CREATE/ALTER/INDEX/REFERENCES/DROP`。

生产环境必须替换两个 `change-me-*` 密码。

也可以使用脚本生成随机密码：

```bash
python scripts/setup_control_mysql.py \
  --admin-url "mysql+pymysql://root:***@127.0.0.1:3306/" \
  --database coding_agent --user coding_agent --show-url
```

### 2.2 执行迁移

```bash
export AGENT_CONTROL_DB_URL='mysql+pymysql://coding_agent:***@127.0.0.1:3306/coding_agent'
export AGENT_CONTROL_DB_AUTO_CREATE=false
coding-agent-control --migrate
# 或
python -m coding_agent.control.migrate
```

迁移条目在 `src/coding_agent/control/migrations/versions/`，当前 head 为
`0015_password_reset`。`0015` 新增 `control_password_resets`（一次性重置
token 哈希）；`0014` 只给 `control_users` 增加 `max_access_mode` 列；`0013`
只新增 `control_workspace_acl` 表。三者对在线库幂等，降级都只回写版本号
（授权/访问上限/重置 token 是策略事实，不删表/列）。`0012` 新增身份/OIDC/
MFA/审批表；`0011` 补人民币额度列、认证事件表、多实例预留表以及带 user
的用量游标；两者的离线 `--sql` 都只更新版本标记。

约束：

- 生产必须 `AGENT_CONTROL_DB_AUTO_CREATE=false`，只由 Alembic 改表；
- `create_all` 只用于本地测试/首次空库，不保证后续列迁移；
- 迁移账号与运行账号分离，运行账号没有 DDL 权限。

### 2.3 企业模式环境变量

```dotenv
AGENT_CONTROL_DB_URL=mysql+pymysql://coding_agent:***@db:3306/coding_agent
AGENT_CONTROL_DB_AUTO_CREATE=false
AGENT_WEB_ACCOUNTS_ENABLED=true
AGENT_WEB_ACCOUNT_TENANT=default
AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL=true   # 生产默认；SQLite 仅测试时设 false
AGENT_WEB_COOKIE_SECURE=true          # 跨源/HTTPS 部署
AGENT_SECRET_MASTER_KEY=<base64 32B>  # 上游 Key 加密主密钥
AGENT_SECRET_MASTER_KEY_VERSION=1      # 轮换后递增
AGENT_WEB_QUOTA_RESERVE_TOKENS=20000  # 可选：运行前预留
AGENT_RUN_COST_BUDGET_USD=0.50        # 可选：单次运行成本上限（USD）
AGENT_COST_CURRENCY=CNY               # 展示/额度货币
AGENT_USD_TO_CNY=7.2                  # 固定汇率，保证配额判断可复现
AGENT_WEB_LOGIN_RATE_LIMIT_PER_MIN=10 # 每 IP 登录尝试上限
AGENT_QUOTA_RESERVATION_TTL_S=3600    # 预留过期时间
```

创建首个管理员：

```bash
agent admin create --username admin --role owner
```

## 3. 表设计与增长

- 全部使用 InnoDB + `utf8mb4`；时间字段沿用 ISO8601 字符串，跨 SQLite/MySQL
  一致，便于审计对照。
- 复合主键天然带租户/用户前缀，例如
  `control_user_usage_daily(tenant, user, day)`。
- 索引：
  - `control_users(tenant, status)`；
  - `control_auth_sessions(user_id)`、`control_auth_sessions(expires_at)`；
  - `control_usage_events(tenant, ts)`；
  - `control_user_usage_daily(tenant, day)`；
  - `workspaces(tenant, status)`、`(tenant, owner)`。
- `control_usage_events` 是增长最快的表；`control_user_usage_daily` 是长期报表表。
- **成本口径**：上游价格、运行时 `cost_usd` 与审计仍以 USD 记账；
  `control_user_quotas.daily_cost_cny` 用人民币配置，管理端按
  `AGENT_USD_TO_CNY` 换算展示与判断。汇率固定配置，避免网络行情导致
  配额结果不可复现。

## 4. 保留与清理

| 数据 | 建议保留 | 方式 |
|---|---|---|
| 登录会话 | 过期即删 | 登录时 `SessionStore.purge_expired()`；索引 `expires_at` |
| 认证事件 | 180 天 | 按 `ts` 清理；用于登录审计与异常排查 |
| 配额预留 | 过期即删 | 预留 TTL 到点后由 `purge_expired_reservations()` 回收 |
| 用量快照 `control_usage_events` | 180 天 | `ControlStore.purge_usage_events(cutoff)` 或定时 SQL |
| 用量日汇总 | 24 个月 | 保留；必要时归档到数据仓库 |
| 审计索引 | 12 个月 | 按 `ts` 清理；原始审计链仍保留在 A3 文件 |
| 会话 tombstone | 与审计周期一致 | 不物理删除，列表过滤 |
| 工作区 revision | 按业务策略 | `pinned` 不参与清理 |

示例（每日低峰执行）：

```sql
DELETE FROM control_usage_events
WHERE ts < DATE_FORMAT(UTC_TIMESTAMP() - INTERVAL 180 DAY, '%Y-%m-%dT%H:%i:%s+00:00');
```

## 5. 备份与恢复

```bash
python scripts/backup_control_db.py --keep 14
python scripts/restore_control_db.py --source backups/control-20261028-020000.sql
```

- MySQL 备份走 `mysqldump --single-transaction --default-character-set=utf8mb4`；
- 密码通过 `MYSQL_PWD` 传入，不出现在命令行；
- 恢复默认先备份当前库；恢复后仍需启动 `--migrate` 对齐版本；
- 备份不包含工作区文件、checkpoint 与 A3 审计原文。

### 5.1 调度与目标（基线）

- Linux：`packaging/systemd/coding-agent-backup.timer` 每天 03:30（随机延迟
  15 分钟）执行 `CodingAgentControl --backup --backup-keep 14`；未配置
  `AGENT_CONTROL_DB_URL` 时 service 由 `ExecCondition` 跳过。
- Windows：用任务计划程序按同样口径调用
  `python scripts/backup_control_db.py --keep 14`。
- **RPO 基线 24 小时**（日备）；需要更低 RPO 时启用 MySQL binlog/PITR，
  并在合同里单独承诺。
- **RTO 基线 1 小时**：恢复备份 → `--migrate` → `/healthz` 与
  `/readyz` 通过 → 抽查账号/会话/用量。实际 RTO 以企业环境演练为准。
- 演练证据：`scripts/phase_f_drill.py`（SQLite）、`pytest -m mysql`
  （MySQL 迁移/备份/恢复）、runbook `RB-017`。

## 6. 主密钥生命周期

主密钥**不属于数据库**：它来自 KMS/secret 注入或文件，只用于加解密
`control_provider_keys`。数据库备份可以包含密文，但绝不能包含主密钥。

轮换流程：

```bash
# 1) 先备份（备份脚本会自动扫描备份目录是否含主密钥）
python scripts/backup_control_db.py --keep 14

# 2) 预演：只统计待轮换行数
python scripts/rotate_secret_master_key.py \
  --old-key-file /run/secrets/old.key \
  --new-key-file /run/secrets/new.key --dry-run

# 3) 正式轮换（事务内重加密全部密文；失败整体回滚）
python scripts/rotate_secret_master_key.py \
  --old-key-file /run/secrets/old.key \
  --new-key-file /run/secrets/new.key --backup

# 4) 立即更新部署侧密钥与版本号，然后重启 A2
#    AGENT_SECRET_MASTER_KEY=...
#    AGENT_SECRET_MASTER_KEY_VERSION=<脚本输出>
```

- 旧密钥在轮换成功后**立即失效**；滚动发布前先完成所有实例的密钥更新。
- `key_version` 记录每条密文的代次；轮换脚本按事务整体递增。
- 备份目录检查：
  ```bash
  python scripts/check_backup_secrets.py --dir backups \
    --key-file /run/secrets/master.key
  ```
- 演练（无需 MySQL）：
  ```bash
  python scripts/phase_secret_key_drill.py
  ```
  覆盖：写密文、备份不含主密钥、旧→新轮换、旧密钥被拒绝、故意放入密钥
  文件时检查器必须发现。

## 7. 运维查询

今日用量与配额：

```sql
SELECT u.username, q.daily_tokens, q.daily_cost_usd,
       d.input_tokens + d.output_tokens + d.worker_input_tokens
         + d.worker_output_tokens AS used_tokens,
       d.cost_usd, d.runs, d.reserved_tokens
FROM control_users u
LEFT JOIN control_user_quotas q
  ON q.tenant = u.tenant AND q.user = u.username
LEFT JOIN control_user_usage_daily d
  ON d.tenant = u.tenant AND d.user = u.username
 AND d.day = DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%d')
WHERE u.tenant = 'default' AND u.status = 'active';
```

活跃会话：

```sql
SELECT user_id, COUNT(*) AS sessions
FROM control_auth_sessions
WHERE revoked_at = '' AND expires_at > DATE_FORMAT(UTC_TIMESTAMP(), '%Y-%m-%dT%H:%i:%s+00:00')
GROUP BY user_id;
```

## 8. 迁移与回滚

- 升级前：`backup_control_db.py`；
- 升级：`coding-agent-control --migrate`；
- 回滚：Alembic `downgrade` 只用于受控窗口，生产更推荐“备份恢复”；
- 版本不匹配时 A2 进入只读降级，不阻塞运行与 SSE。

## 9. 验收

- `tests/unit/test_mysql_ddl.py`：离线编译全部迁移，检查新表与
  “TEXT 不得带普通默认值”等 MySQL 方言约束；
- `tests/integration/test_control_mysql.py`（`-m mysql`）：真实 MySQL 上执行
  `upgrade → 账号/会话/加密 Key/配额/用量 → 备份 → 恢复 → 校验`；
- `tests/unit/test_control_store.py`：表清单、SQLite roundtrip；
- `tests/unit/test_control_backup.py`：MySQL 命令行与密码传递。
