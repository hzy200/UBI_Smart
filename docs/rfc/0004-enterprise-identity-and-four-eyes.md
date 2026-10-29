---
layer: L2
doc_id: rfc-0004
title: 企业身份（OIDC / TOTP）与四眼审批
owner: 项目作者
status: accepted
created: 2026-10-28
last_reviewed: 2026-10-28
review_cadence: 身份、审批语义或数据迁移变化时
---

# RFC-0004：企业身份（OIDC / TOTP）与四眼审批

> 目标是让企业客户能用现有 IdP 登录、给本地账号加第二因子，并让高危审批
> 不能由发起人自己完成。三者都只作用于 A2 控制面，不改变 A3/A4 的执行语义。

## 1. 目标

- **OIDC Authorization Code + PKCE**：支持标准 discovery/JWKS，验证 ID token 的
  `iss/aud/exp/nonce/signature`，按 `(issuer, subject)` 映射到本地用户。
- **TOTP MFA**：本地密码账号可启用 TOTP；登录分两步（密码 → 动态码），
  密钥 AES-GCM 加密落库，恢复码只存哈希，支持管理员重置。
- **四眼审批**：开启后，L3（可配置阈值）审批必须由**非发起人**的
  `owner/admin` 在管理员队列中决定；发起人的 `/api/resume` 不能自批。
- 以上能力默认关闭；开启时若前置条件不满足（无控制面库、无主密钥、
  非 HTTPS 的 OIDC），启动 fail-closed。

可验证目标：

- `pytest -m mysql` 覆盖迁移与身份表；
- `pytest tests/unit/test_oidc.py` 覆盖签名/claims/PKCE/state 一次性；
- `pytest tests/unit/test_mfa.py` 覆盖 TOTP、恢复码、重放与挑战失败上限；
- `pytest tests/unit/test_approvals.py` 覆盖自批拒绝、跨用户决定与
  resume 合并。

## 2. 非目标

- 不做 SAML / LDAP / SCIM / 自动用户目录同步；
- 不做 OIDC 的 front-channel/back-channel logout 协议；
- 不做 MFA 的 WebAuthn/硬件密钥；
- 不做跨租户管理端；管理员只能处理本租户审批；
- 不做真正的多实例审批推送，管理员队列用轮询；
- 不改变 A3 图内的审批执行点：`approval_gate` 仍是唯一 `interrupt()`。

## 3. 背景与证据

- `TD-019`/企业评估 P1-3：当前只有自建密码账号，缺少 SSO/MFA/职责分离；
- 现有审批模型只接收 `call_id -> bool`，A2 不知道审批人身份，发起人可自批；
- A2/A3 已拆进程，A3 不持有账号库；身份/审批事实必须留在 A2 控制面。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `control/`（schema/store/oidc/mfa/approvals）、`web/routes/auth.py`、`web/routes/admin.py`、`web/services.py`、`web/frontend/` |
| 契约 | `ApprovalRequested` 新增 `level_int`（向后兼容）；新增认证/审批路由；配置项新增 |
| 数据 | 新增 `control_oidc_states`、`control_user_identities`、`control_user_mfa`、`control_mfa_challenges`、`control_approvals`；Alembic `0012` |
| 前端 | 登录页 SSO 按钮、MFA 动态码、四眼提示；管理端审批队列 |
| 权限 | 写管理接口走 `AdminDep`；审批队列走 `ApproverDep`（owner/admin/approver）；账号/用量/事件读接口走 `AuditorDep`（owner/admin/auditor）；`auditor` 的非 GET `/api/*` 由中间件统一 403。审批决定校验 `decided_by != requested_by` |
| 工作区 ACL | `control_workspace_acl`（Alembic `0013`）：只读共享元数据/revision，不共享执行；管理员授权，绑定会话仍要求 owner |

## 5. 安全与隐私

- **OIDC**：只接受 HTTPS discovery（本地开发需显式
  `AGENT_OIDC_ALLOW_INSECURE_HTTP=true`）；校验 `iss/aud/exp/nonce`，拒绝
  `alg=none` 与未知 `kid`；state/nonce/PKCE verifier 一次性存储，TTL 默认 10 分钟；
  token/ID token 不写日志、不写审计明细。
- **MFA**：TOTP 密钥用现有主密钥 AES-256-GCM 加密，AAD 绑定 `(tenant,user)`；
  恢复码只存 SHA-256；同一时间步只能使用一次（`last_used_step`）；
  挑战令牌只存哈希，最多 5 次失败；管理员重置会吊销该用户全部会话。
- **四眼**：审批事实落 MySQL/SQLite；发起人不能决定 `requested_by == 自己` 的
  行；L3 决策只从数据库读取，A2 忽略发起人对 L3 的 `decisions`；管理员决定
  会落 `kind=auth`/`kind=approval` 审计。审批文本包含命令原文，沿用现有脱敏。
- **RBAC 与工作区 ACL**：`auditor` 只读由中间件默认拒绝写操作，避免每新增
  一个路由就漏一次检查；工作区 ACL 只授予 `read`，被授权者看不到
  `root_path`/`root_handle`，也不能创建绑定该工作区的会话。跨租户授权在
  管理端直接 404。
- 威胁模型更新：OIDC 失败不得回退到本地匿名；MFA 不适用于 OIDC 登录
  （由 IdP 负责）；四眼不防“管理员与发起人合谋”，只防单人自批。

## 6. 兼容与回滚

- 所有能力默认关闭；配置关闭时不注册新路由（或返回 503），旧登录行为不变。
- `ApprovalRequested.level_int` 是新增字段，旧前端忽略；契约兼容检查为 additive。
- 迁移 `0012` 只新增表，不改旧列；`downgrade` 只回写 schema version，不删表
  （与 `0011` 一致，避免误删身份绑定/审批记录）。
- 回滚：关闭 `AGENT_OIDC_ENABLED` / `AGENT_MFA_ENABLED` /
  `AGENT_APPROVAL_FOUR_EYES` 即可恢复旧路径；已启用 MFA 的账号在关闭开关后
  仍可用密码登录（但应视为降级并审计）。

## 7. 可观测性

- 审计/认证事件新增：`oidc_login_ok`、`oidc_login_failed`、`mfa_enroll`、
  `mfa_enabled`、`mfa_disabled`、`mfa_failed`、`approval_requested`、
  `approval_decided`、`approval_self_denied`。
- 指标（复用 `observability.py`）：`agent_auth_events_total{kind}`、
  `agent_approvals_pending`、`agent_approvals_decided_total{decision}`。
- 日志不记录 token、ID token、TOTP secret、恢复码明文。

## 8. 验收标准

```text
Given 一个已配置 OIDC 的部署
When  用户完成 authorization code + PKCE 回调
Then  A2 校验 state/nonce/ID token 签名并建立本地会话；重复 state 被拒绝

Given 本地账号已启用 TOTP
When  密码正确但动态码错误 5 次
Then  挑战失效，返回 429/401，不建立会话

Given 一名 member 发起 L3 命令且四眼开启
When  同一用户调用 /api/resume 批准该 call_id
Then  A2 忽略该决定；该运行保持挂起
And   管理员在 /api/admin/approvals/{id}/decide 批准后，member 的 resume 才继续
```

验收锚点写入 `docs/acceptance/acceptance-criteria.yaml`。

## 9. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| 引入 Authlib/PyJWT | 少写验签代码 | 新依赖进入安全边界；仍需自己管 state/映射 | 现有 `cryptography` 足够，验签范围可控 |
| OIDC 只调 userinfo，不验 ID token | 实现简单 | 依赖 TLS 与 userinfo 语义，不符合 OIDC 最小验证 | 企业评审不接受 |
| 四眼只做“同账号不能自批”的前端开关 | 改动小 | 后端仍接受自批，等于没有控制 | 安全属性必须在 A2 强制 |
| 审批状态放内存 | 简单 | 多实例/重启丢审批，无法审计 | 必须落控制面库 |

## 10. 决策与后果（实现后填）

- 最终选择：OIDC discovery + JWKS（RS256/ES256）+ PKCE；TOTP + 恢复码；
  L3 审批落 `control_approvals`，管理员队列决定。
- 批准人 / 日期：项目作者 / 2026-10-28（按本轮指令直接实施）
- 真实结果与预期差异：实现后补
- 新增技术债：SAML/SCIM/WebAuthn、跨租户审批、审批推送
- 关联 ADR：0023

## 11. 任务分解与验证计划

| 任务 | 负责模块 | 验证方式 |
|---|---|---|
| schema/迁移 0012 | `control/schema.py`、migrations | `pytest -m mysql`、SQLite DDL |
| OIDC 客户端与 state | `control/oidc.py` | `tests/unit/test_oidc.py` |
| MFA/TOTP | `control/mfa.py`、`routes/auth.py` | `tests/unit/test_mfa.py` |
| 四眼审批 | `control/approvals.py`、routes | `tests/unit/test_approvals.py` |
| 前端登录/审批 | `web/frontend` | `node test.mjs` / jsdom / Playwright |
