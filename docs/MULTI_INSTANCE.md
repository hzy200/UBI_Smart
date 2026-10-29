# 多实例部署

本文只列 A2 Web 控制面横向扩展的**硬约束**与检查项；单机部署保持默认值即可。

## 目标拓扑

```text
             ┌──────────────┐
LB / 反代 ───│ A2 #1        │──┐
             ├──────────────┤  │   MySQL：账号/会话/用量/租约/审批/令牌
             │ A2 #2        │──┼─  Redis：SSE 事件传输 + 分布式限流
             └──────────────┘  │   A3（共享或每实例）：运行/沙箱/checkpoint
                               └─ 共享文件系统（审计/轨迹/工作区，按部署形态）
```

## 必须一致的配置

| 配置 | 值 | 说明 |
|---|---|---|
| `AGENT_CONTROL_DB_URL` | 同一 MySQL | 账号、会话、配额、租约、审批、PAT/Webhook/分享 |
| `AGENT_CONTROL_DB_AUTO_CREATE` | `false` | 生产只由 Alembic 迁移 |
| `AGENT_EVENT_TRANSPORT` | `redis` | 共享 SSE 事件流；默认 `memory` 只适合单实例 |
| `AGENT_EVENT_TRANSPORT_URL` | 同一 Redis | 也可用 `AGENT_RATE_LIMIT_REDIS_URL` |
| `AGENT_RATE_LIMIT_BACKEND` | `redis` | 每用户 API 限流与登录限流跨实例生效 |
| `AGENT_SECRET_MASTER_KEY` | 所有实例相同 | 上游 Provider Key 的 AES-256-GCM 主密钥 |
| `AGENT_WEB_ACCOUNTS_ENABLED` | `true` | 企业账号 + DB 加密密钥托管 |

入口（`agent serve` / `coding-agent-control` / 桌面）会把
`stream_buffer=True` 传给 `create_app`；自定义入口若设置
`AGENT_EVENT_TRANSPORT=redis` 却关闭 stream buffer，启动会直接失败。

## 就绪探针

`GET /readyz` 在原有 `control_db` / `agent` 之外返回：

```json
{
  "checks": {
    "event_transport": "redis",
    "rate_limit": "redis",
    "credentials": "db-encrypted",
    "shared_state": "true"
  }
}
```

`shared_state=true` 需要三者同时满足。只配了 Redis、但密钥仍走本机
keyring 时，会显示 `credentials=keyring` 且 `shared_state=false`。

## 会话与 SSE

- 事件从 Redis Stream 重放，前端用 `Last-Event-ID` 续传；**不要求
  粘性会话**才能续流。
- 运行租约、幂等键、配额预留、会话配置、审批都在 MySQL，跨实例可见。
- 正在运行的 runtime 仍在单个 A2 进程内；另一个实例只能重放事件，
  不能直接操作该 runtime。`/cancel`、`/resume` 需要落到持有 runtime 的
  实例（或通过共享 A3 / 反代亲和策略）。
- 若使用独立 A3，请让所有 A2 指向同一个 `AGENT_INTERNAL_URL`；否则
  resume/fork 可能找不到原运行时。

## 文件与工作区

- 审计、transcript、checkpoint、快照、host 工作区都是宿主文件系统。
  多实例必须挂载**同一份共享存储**，或把 A3 放到共享存储侧。
- host 工作区的 `root_handle`/A2L 仍是客户端句柄；不要假设另一实例
  能直接解析同一宿主路径。
- 备份策略见 `docs/DATABASE.md`；多实例不要同时运行备份脚本，交给
  systemd timer 的单实例执行。

## 密钥托管

- 企业账号下上游 Key 写入 `control_provider_keys`，AES-256-GCM 加密，
  AAD 绑定 `(tenant, user, provider)`；所有实例共享同一主密钥。
- 未配置主密钥时回退 OS keyring，**keyring 不跨机器共享**，不适合
  多实例；`/readyz` 会显示 `credentials=keyring`。
- 轮换主密钥用 `CredentialStore.rotate(old, new)`；轮换期间旧密文仍可读，
  但不要在两套主密钥之间漂移。

## 限流与故障语义

- Redis 限流和事件传输在 Redis 故障时 **fail closed**：限流拒绝请求、
  SSE 退回本地缓冲/断开重连，而不是静默放行。
- Redis Stream 有 `MAXLEN` 与 TTL；超出窗口的旧事件无法重放，前端需
  从 checkpoint/历史恢复。
- 多实例共享同一 Redis 前缀（`AGENT_EVENT_TRANSPORT_PREFIX` /
  `AGENT_RATE_LIMIT_REDIS_URL`），不同环境必须隔离。

## 验收清单

1. 两个实例分别启动，`/readyz` 均返回 `shared_state=true`。
2. 在实例 A 发起运行，实例 B 用同一 `thread_id` 连接 `/api/stream` 能
   收到事件（`Last-Event-ID` 续传）。
3. 同一用户在两实例高频请求，限流计数合并。
4. 实例 A 创建 PAT，实例 B 用该 PAT 调用 `/api/me` 成功。
5. 停掉 Redis：限流返回 429，SSE 不再假装正常；恢复后重连。
6. 停掉 MySQL：`/readyz` 返回 503，不再接新流量。
