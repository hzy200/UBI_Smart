# ADR-0014：多 A2 事件传输用 Streams 重放，不用 consumer group

- **Status**：accepted
- **Date**：2026-10-25
- **Deciders**：项目作者
- **Owner**：项目作者

## Context

多 A2 实例时，A3 的事件需要让任意 A2 在断线重连后按 `seq` 重放。
可选方案：

1. Redis Streams + consumer group + ACK；
2. Redis Streams + 按 `seq` 重放（当前）；
3. Redis Pub/Sub；
4. MySQL 事件表。

SSE 是**扇出**语义：同一事件要送达所有订阅该会话的 A2/A1，而不是只送达
一个消费者。consumer group 会把每条事件分配给组内一个消费者；用在这里会
导致部分 A2 看不到事件，除非每个 A2 各自建 group，失去“组”的意义。

## Decision

采用方案 2：每个 thread 一个 Redis Stream，事件字段包含 `seq` 与 `data`；
A2 按 `seq` 重放，SSE 断线使用 `Last-Event-ID`。不使用 consumer group/ACK。

不选方案 1：扇出语义与“单消费者投递”冲突。
不选方案 3：Pub/Sub 没有重放，断线即丢事件。
不选方案 4：MySQL 不应进入 SSE 热路径。

## Consequences

### 好的

- 任意 A2 都能独立重放完整事件流，多实例部署简单；
- 不依赖 Redis 的 ACK/积压清理逻辑，故障面更小；
- 单实例仍可退回内存缓冲，无 Redis 依赖。

### 坏的 / 代价

- 每个 A2 都会读取同一份事件，读放大；
- 当前 `replay` 按 `seq` 过滤，极端情况下扫描量随 stream 增长；
- 没有消费确认，事件保留完全依赖 `maxlen`/TTL；
- 如果未来需要“每个订阅者独立游标 + 可靠投递”，要重新设计。

### 中性 / 需要留意

- `maxlen` 与 TTL 必须覆盖最长断线重连窗口；
- 可用阻塞读或专用索引优化扫描量，但不改变扇出语义。

## 复核

- **Verified by**：`tests/unit/test_control_services.py::test_redis_transport_two_instances_share_stream`。
- **Revisit when**：需要严格 per-subscriber ACK、事件量导致重放扫描成为瓶颈，
  或引入多消费者工作组。
