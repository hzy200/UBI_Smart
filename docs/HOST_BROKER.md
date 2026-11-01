---
layer: L3
doc_id: host-broker
title: 宿主能力代理（A2L）
owner: 项目作者
status: active
last_reviewed: 2026-11-01
review_cadence: A2L 协议、安全档或工作区模型变动时
---

# 宿主能力代理（A2L）

A2L 是客户端执行域：它让用户在**自己的电脑**上使用 `host` 工作区，同时保持
A2/A3/A4 在服务器侧。决策与代价见 [ADR-0015](adr/0015-host-capability-broker.md)，
授权模型见 [RFC-0002](rfc/0002-native-execution-domain-and-grant-model.md)，
安全边界以 [THREAT_MODEL.md](THREAT_MODEL.md) 的 T4 为唯一口径。

**它不是 T2，也不承诺“服务端失陷零横向风险”。** Windows host 后端与 bwrap
不等价；不可信仓库仍应使用 `sandbox` 工作区。

## 1. 拓扑与信任边界

```text
客户端 · 用户电脑                        服务端 · 企业服务器
┌──────────────────────────┐            ┌──────────────────────────┐
│ A1 前端                  │  HTTP/SSE  │ A2 控制面                │
│ A2L 宿主能力代理         │ ◀────────▶ │ A3 编排面                │
│   （随客户端启停）       │  只外连    │ A4 执行面                │
│   host 工作区（NTFS）    │            │   └─ sandbox 工作区(WSL) │
│   本地 grant/ZX/审计     │            │                          │
└──────────────────────────┘            └──────────────────────────┘
```

- 客户端只有 A1 与 A2L；A2/A3/A4 全在服务器。
- `host` 工作区在客户端 NTFS，由 A2L 执行；`sandbox` 工作区在服务端 WSL，
  由 A4 执行；两者不跨挂载。
- A2L 只主动外连，不开入站端口；桌面走 loopback，企业走 mTLS。
- 绑定客户端生命周期：随客户端启动/退出，客户端运行期间单实例、托盘可见；
  **不注册系统服务、不独立开机自启**，不以 LocalSystem/root 运行。

信任规则：

1. A3 的 `ExecutionRequest` 是**请求**，A2L 本地 grant/ZX 是最终判定。
2. A2 只持有 `workspace_id`、`root_handle`、`a2l_id` 与审计索引；不持有
   grant scope 或 host 绝对路径。
3. 跨层只传 `WorkspaceRef` 与相对路径；绝对路径只存 A2L 本地 SQLite。
4. A1 不直接调用 A2L 能力 API；本地同意 UI 由 A2L 渲染。
5. A2L 不导入 `tools/`、`sandbox/`、`graph/`、`runtime/`。

## 2. 当前能力

| 能力 | 状态 | 说明 |
|---|---|---|
| 只读文件 | 已落地 | `host.list_dir/stat/read_text/find_files` |
| 工具链探测 | 已落地 | `host.toolchains`、`host.env.inspect`；摘要不含绝对路径 |
| 工作区注册 | 已落地 | 本机选目录、生成 handle、保存本地映射 |
| 健康/心跳/离线 | 已落地 | 断线时 A4 降级 `sandbox`，不伪装 host 可用 |
| 受控写 | 已落地（P2/P3） | brokered write、写前工作区外快照、`expected_sha256` 冲突检测 |
| AppContainer exec | 已落地（受限） | 只接受已注册 toolchain 的 argv；安装永不自动执行 |
| mTLS/设备台账 | 已落地 | 设备指纹、吊销、策略验签、nonce 证明 |
| 本地审计 | 已落地 | 哈希链；A2 只建查询索引 |

`host.install` 永不自动执行；不可信仓库禁用 host 后端。Windows 上
`user-t1` 仅用于 `local-dev`，默认企业档是 `lowpriv` + ACL。

## 3. 关键组件

| 路径 | 职责 |
|---|---|
| `host_broker/protocol.py` | 版本化帧、DTO、错误码；无 I/O |
| `host_broker/transport.py` | 传输抽象 |
| `host_broker/client.py` | A4 侧外连、握手、心跳、请求/取消 |
| `host_broker/acceptor.py` | A3 侧监听与 `AcceptedHostCaller` |
| `host_broker/server.py` | A2L 进程：握手、分发、暂停、优雅退出 |
| `host_broker/policy.py` | grant/ZX/verb 判定的唯一入口 |
| `host_broker/paths.py` | handle 级 canonicalization、junction/UNC/ADS 防护 |
| `host_broker/ops.py` | 只读/写/快照等 typed ops |
| `host_broker/exec.py` | AppContainer 受限执行 |
| `host_broker/win_isolation.py` | lowpriv/AppContainer 隔离档 |
| `host_broker/attestation.py` | 设备证明与策略验签 |
| `host_broker/devices.py` | 设备台账与吊销 |
| `host_broker/store.py` / `op_journal.py` | 本地映射、grant、op 日志 |
| `host_broker/consent.py` | 本地同意 UI/回调 |
| `host_broker/workspace_registry.py` | 本机选目录与 handle 映射 |
| `execution/host_backend.py` | `ExecutionBackend` 映射到 `host.*` |
| `execution/host_file_access.py` | A2L typed ops 的 `FileAccess` 适配 |
| `execution/host_fs.py` / `host_snapshots.py` | host 文件与快照 |

配置项以 [contracts/config.md](contracts/config.md) 的 `AGENT_HOST_*` 为准；
新增配置必须先改 `config.py` 再重跑 `scripts/gen_contracts.py`。

## 4. 安全不变量

1. **只外连**：不监听 TCP/Unix socket，没有入站控制面。
2. **服务端请求 ≠ 授权**：每次执行重新做本地 grant + ZX 判定。
3. **ZX 优先于 grant**：任何 grant 都不能覆盖 ZX。
4. **不传绝对路径**：跨层只传 `root_handle` + 相对路径；解析在 A2L 内完成。
5. **无副作用默认关闭**：未启用的写/执行/安装返回 `OP_NOT_ENABLED`。
6. **不泄露宿主环境**：`host.env.inspect` 过滤敏感名，不返回完整 `PATH`。
7. **审计不可由 agent 改写**：本地审计在 agent 不可写目录，哈希链 + 设备签名。
8. **离线不伪装**：A2L 离线时 A4 降级 `sandbox`，不改道到 WSL。
9. **安全档诚实**：`user-t1` 必须在握手、UI 与文档中标注 T1，不得宣称 T2。
10. **协议 fail closed**：版本/未知 op/畸形帧一律拒绝并断开。
11. **绑定客户端生命周期**：单实例、按交互用户运行、可暂停/离线。
12. **handle-only**：本地映射丢失时 `WORKSPACE_HANDLE_UNKNOWN`，不猜路径。
13. **本地 UI 是最终闸门**：服务端不能替用户选目录或批准授权。
14. **op_journal 先于执行**：未完成 op 重启后返回 `unknown`，不盲目重放。
15. **根目录限制**：拒绝 OneDrive/云占位、网络/映射盘、UNC、WSL/VM 共享、
    reparse 根与设备路径。
16. **敏感根逐次确认**：Documents/Desktop/OneDrive/浏览器 profile/.ssh 等
    必须 `sensitive=True` 且每次本地确认。

## 5. 测试与验收

```bash
conda activate agent
pytest -m "not wsl and not llm" \
  tests/unit/test_host_policy.py \
  tests/unit/test_host_paths.py \
  tests/unit/test_host_broker_protocol.py \
  tests/unit/test_host_broker_ops.py \
  tests/unit/test_host_backend.py \
  tests/unit/test_host_attestation.py \
  tests/unit/test_host_devices.py
pytest -m host -q                      # Windows 真机只读/隔离用例
```

## 6. 完成度与限制

- 已落地：双工作区引用、typed RPC、只读能力、受控写、快照、AppContainer exec、
  mTLS/设备证明、本地审计与离线降级。
- 后续：revision、企业分发/设备注册、安装包与自动更新；编译器级宏展开与
  LSP 调用层级不在此范围。
- 残余边界：Windows 没有等价 bwrap，host 后端不能宣称 T2；被攻陷客户端、
  宿主管理员或用户误点“同意”不在防护范围内。

## 7. 相关文档

- [ARCHITECTURE.md §12](ARCHITECTURE.md#12-目标四层架构与-a2l-宿主能力代理phase-a-契约phase-bd-已落地)
- [THREAT_MODEL.md](THREAT_MODEL.md) T4
- [ADR-0015](adr/0015-host-capability-broker.md)
- [RFC-0002](rfc/0002-native-execution-domain-and-grant-model.md)
- [glossary.md](glossary.md)
