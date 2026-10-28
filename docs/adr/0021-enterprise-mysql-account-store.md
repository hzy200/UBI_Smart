---
layer: L2
doc_id: adr-0021
title: 企业账号、额度与上游 Key 统一存 MySQL，执行产物仍留在文件面
owner: 项目作者
status: accepted
created: 2026-10-28
last_reviewed: 2026-10-28
review_cadence: 企业账号、配额、用量或控制面 schema 变动时
---

# ADR-0021：企业账号、额度与上游 Key 统一存 MySQL，执行产物仍留在文件面

## 背景

企业版需要长期维护用户身份、每日额度、token 计量和上游模型 Key。当前实现：

- 账号来自静态 `AGENT_WEB_AUTH_TOKENS`，无法吊销、无角色、无审计；
- Provider Key 依赖 OS keyring，服务器/容器部署不可用；
- 用量只存在于进程内 router 配额，重启即丢；
- 控制面已有 MySQL/SQLite + Alembic，但 `control_usage_daily` 与
  `control_runs` 未接线。

可选方案：

1. 继续用静态 token + keyring，只加管理页；
2. 账号/额度放 MySQL，Key 继续 keyring；
3. 账号/额度/Key 全部放 MySQL，A3 执行产物不动；
4. 把 checkpoint、审计原文、transcript 也迁入 MySQL。

## 决策

选择 **3**：

- `control_users`、`control_auth_sessions`、`control_user_quotas`、
  `control_user_usage_daily`、`control_usage_events`、`control_usage_cursor`
  与 `control_provider_keys` 全部落控制面数据库；
- 生产数据库固定 MySQL 8.0/8.4 + InnoDB + utf8mb4，迁移只走 Alembic；
- 上游 Key 只存 AES-256-GCM 密文、nonce 与后四位；主密钥来自
  `AGENT_SECRET_MASTER_KEY`，缺失时拒绝写入；
- 用量以 `run_end` 审计为事实源，按 `record_hash` 幂等，resume 用
  `run_epoch` 取累计差值；
- 配额预留是独立行（owner/expires_at），运行结束按 id 释放，多实例启动
  只清理过期预留；每日成本额度用人民币配置，上游 USD 成本按固定汇率换算；
- 登录成功/失败/登出/限流写 `control_auth_events`，来源 IP 参与限流；
- checkpoint、A3 审计 JSONL、workspace 字节、附件仍留在文件/对象面。

## 代价

- MySQL 成为企业登录与计量的强依赖；数据库不可用时账号登录降级为 503，
  运行链路仍需保持可用；
- Key 加密把主密钥变成新的高价值秘密，必须走 KMS/secret 注入，不能与数据库
  备份放在同一位置；
- 用量快照会持续增长，需要保留策略与清理任务；
- SQLite 仍被单元测试使用，schema 必须同时兼容两种方言（TEXT 默认值、
  索引长度、`FOR UPDATE` 行为）；
- 账号迁移不可回退到静态 token 的“无感”状态，回滚需要保留旧 token 表。

## 被否掉的替代

- 方案 1：无法满足吊销、角色与长期计量；
- 方案 2：Key 仍是单机秘密，无法做企业统一分配；
- 方案 4：checkpoint 与 A3 单写者语义绑定，迁库会改变恢复/挂起协议，
  收益不足以覆盖风险。

## 复核

- schema 与迁移：`docs/DATABASE.md`、`control/migrations/versions/0010_*`；
- 安全边界：`docs/THREAT_MODEL.md` §T3；
- 验收：`tests/unit/test_mysql_ddl.py`、`tests/integration/test_control_mysql.py`、
  `tests/unit/test_accounts.py`、`test_quota_usage.py`。
