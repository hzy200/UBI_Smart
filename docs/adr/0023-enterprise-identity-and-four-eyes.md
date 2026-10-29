# ADR-0023：OIDC + TOTP + 四眼审批放在 A2，而不是 A3

- **Status**：accepted
- **Date**：2026-10-28
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

企业客户要求用现有 IdP 登录、本地账号支持第二因子，并且高危审批不能由发起人
自己完成。A2/A3 已经拆进程：A3 只持有 runtime/图/沙箱，不连接控制面数据库；
A2 持有账号、会话、配额与审计索引。审批决定目前只是 `call_id -> bool`，
A3 的 `approval_gate` 看不到“谁批的”。

需要在以下方案间选择：

- 在 A3 内做账号与审批身份；
- 在 A2 做身份，把审批人身份编码进 resume 决策；
- 在 A2 记录审批事实，管理员队列决定，A3 只消费最终 `call_id -> bool`。

## Decision

选择第三种：**身份与审批事实留在 A2，A3 保持只消费布尔决策**。

- OIDC 用 Authorization Code + PKCE；discovery/JWKS 验签只依赖
  `httpx` + `cryptography`，不引入 Authlib/PyJWT。
- state/nonce/verifier 落控制面库并一次性消费；ID token 校验
  `iss/aud/exp/iat/nonce` 与签名，拒绝 `alg=none` 与未知 kid。
- 本地账号 TOTP：密钥 AES-GCM（AAD 绑定 tenant/user），恢复码只存 SHA-256，
  登录分两步；OIDC 的 MFA 交给 IdP。
- 四眼审批：A2 从 SSE 的 `approval_requested` 落 `control_approvals`；
  L3 决定只能由非发起人的 owner/admin 通过管理员队列写入；发起人的
  `/api/resume` 对 L3 的决定被忽略，A2 合并管理员决定后再交给 A3。

不选在 A3 做身份：A3 不连控制面库，也没有会话/角色概念，会把账号数据
扩散到执行面，违反 A2/A3 分工。不选“把审批人身份编码进 resume”：A3 仍会
接受发起人自批，安全属性只存在于前端/调用方，后端没有强制点。

## Consequences

### 好的

- 身份、会话、审批、审计在同一控制面库，跨 A3 重启仍可恢复；
- A3 的审批语义不变，旧会话/旧前端仍兼容（`level_int` 是新增字段）；
- OIDC 失败不会降级到本地匿名；四眼未知 call_id fail closed；
- 管理员重置 MFA 会吊销该用户全部会话。

### 坏的 / 代价

- 管理员必须轮询审批队列，没有推送；
- A2 需要解析 SSE 事件并多一次 DB 写入；
- 高等级审批在管理员批准前会一直挂起，发起人不能自己推进；
- OIDC 只支持 Authorization Code + PKCE，未做 front/back-channel logout；
- 没有 SAML/SCIM/WebAuthn/跨租户审批；TOTP 主密钥轮换不会自动重加密历史密钥。

### 中性 / 需要留意

- 审批命令文本落库前做了 best-effort 脱敏，仍可能包含业务敏感信息；
- 管理员与发起人合谋不在防护范围内（四眼只防单人自批）；
- OIDC state 与审批记录默认保留到过期/手动清理，需按保留策略运维。

## 复核

- **Verified by**：
  `pytest tests/unit/test_oidc.py tests/unit/test_mfa.py tests/unit/test_approvals.py tests/unit/test_admin_api.py -q`
  与 `pytest -m mysql -q`。
- **Revisit when**：需要 SAML/SCIM、多实例审批推送、跨租户管理、或 IdP 要求
  front-channel logout 时。
