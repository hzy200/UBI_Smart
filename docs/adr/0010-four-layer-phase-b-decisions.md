---
layer: L2
doc_id: adr-0010
title: 四层拆分的 Phase B 落地决策
owner: 项目作者
status: accepted
last_reviewed: 2026-10-24
review_cadence: 控制面部署形态、密钥、事件传输或前端构建方式变动时
---

# ADR-0010：四层拆分的 Phase B 落地决策

## 背景

Phase A 已冻结 A1 前端 / A2 控制面 / A3 编排面 / A4 执行面的职责、
数据归属与控制 API（见 [PHASE_A.md](../PHASE_A.md)）。进入 Phase B 前，
需要把五个会影响实现形态的选择定下来，避免边做边改。

约束：

- Windows 11 与 Linux 都要支持；Windows 的 Linux 执行面由专用 WSL2 提供；
- A3/A4 必须保留原 Agent 的全部能力，standalone CLI/TUI 不能依赖 A2；
- 业务数据管理与 Agent 状态尽量分离，A2 可以使用宿主机 MySQL；
- Agent 不直连数据库，密钥不能进 MySQL/日志/前端；
- 多实例部署时 SSE 重放必须有落点，不能只靠单进程内存。

## 决策

### 1. A2 在各平台都跑原生进程

- Windows：A2 为原生进程/Windows 服务，MySQL 在本机；A3/A4 在 WSL2；
- Linux：A2 为 systemd 服务，MySQL/MariaDB 本机；A3/A4 原生 Linux。
- 不把 A2 放进 WSL 来“统一”：WSL 能跑 Python，但拿不到 Windows 服务管理器、
  Event Log、DPAPI/凭据管理器与原生安装/升级链路。

**否掉的替代**：A2 仅在 WSL 内运行；A2 只提供容器镜像、放弃宿主集成。

**代价**：两套服务托管/安装/日志/权限适配；需要同一套 API 契约测试保证行为一致。

### 2. SecretProvider 分两档

- 桌面：OS 密钥环（Windows Credential Manager / Linux Secret Service）；
- 服务器：Vault/KMS，或 systemd-creds + 受控文件；
- WSL 通常没有 Secret Service daemon：桌面密钥由 A2 持有，按运行注入 A3 内存，
  **不落 WSL 磁盘**，也不写 MySQL。

**否掉的替代**：全平台统一 keyring；密钥加密后存 MySQL。

**代价**：需要内部短期凭据通道、轮换与审计；A3 重启后要重新获取，不能持久化。

### 3. MySQL 只做索引，另加共享事件传输层

- MySQL：会话目录、运行/审批索引、用量汇总、审计索引、Provider 档案；
- 共享事件传输：建议 Redis Streams，按 thread 分区、consumer group、
  短 TTL；A3 发布，A2 订阅并代理 SSE，按 `seq` 重放；
- 单实例保留内存适配器作为默认；多实例切共享传输。
- MySQL 不承担 SSE 热重放，也不在事件热路径上同步写。

**否掉的替代**：MySQL 直接存/重放事件（写放大、保留期与热路径不合适）；
只靠单进程内存（多实例无解）；一开始就上 Kafka（运维成本过高，可后续换适配器）。

**代价**：多一个 Redis 运维对象；投递语义按 at-least-once + `seq` 去重，
不承诺 exactly-once；需要定义保留期与积压告警。

### 4. SQLite 保留为 Standalone 一等公民

- CLI/TUI、开发、测试、eval 继续使用本地文件/SQLite checkpoint；
- 只有 A2 使用 MySQL；不强制 Agent 依赖数据库；
- A2 的 MySQL schema 与迁移独立维护，迁移不要求 Agent 参与。

**否掉的替代**：全平台强制 MySQL；删除 SQLite 只留 MySQL。

**代价**：存在两条持久化路径；两者共享的是 API/事件契约而不是存储实现，
需要契约测试防止行为漂移。

### 5. 前端维持静态 SPA + esbuild/TypeScript

- 不引入 React/Vue；继续静态托管与 CSP；
- 用 esbuild + TypeScript 拆分 `app.js` 模块，类型化事件/API；
- 保留现有主题、SSE、任务/推理可视化，逐步补虚拟滚动与可访问性；
- 设定 bundle 体积预算，避免回到“单文件 65KB 手写上限”。

**否掉的替代**：继续纯手写单文件；直接上框架。

**代价**：需要构建步骤、类型维护与模块边界；状态/DOM 仍手写，
复杂交互的维护成本高于框架，但当前规模尚未达到必须付框架代价的临界点。

## 结果

### 好的

- A2 获得真实宿主能力（服务、ACL、日志、密钥、安装升级）；
- 多实例 SSE 有明确的重放路径；
- Agent 保持独立可运行，开发/评测不被 MySQL 绑架；
- 前端获得类型与模块化，不引入运行时框架成本。

### 坏的 / 代价

- Windows/Linux 两套 A2 宿主适配；
- 多一个共享传输组件；
- 密钥跨边界注入与轮换需要额外安全设计；
- 两套持久化路径与前端构建链需要持续维护。

### 需要留意

- Redis 故障时 SSE 重连应降级为“不可重放”而不是伪造完成；
- WSL 内不得缓存密钥；
- TypeScript 构建产物必须继续满足 CSP，不引入 `unsafe-eval`；
- A2 原生进程不得直接读写工作区，文件/Git 仍由 A3/A4 提供。

## 复核

- **Verified by**：Phase A 契约测试
  `tests/unit/test_layer_contracts.py`；Phase B 将补 A2↔A3 契约、SecretProvider
  与事件传输适配器测试。
- **Revisit when**：需要跨主机 worker 池、事件量超出 Redis 能力、前端复杂交互
  成本明显超过框架迁移成本，或 Windows Server 成为目标平台。
