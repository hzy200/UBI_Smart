# ADR-0039：资料库放在服务器执行面，按用户跨工作区隔离

- **Status**：accepted
- **Date**：2026-11-02
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0010](../rfc/0010-per-user-library.md)

## Context

Web 多用户与 `sandbox` 工作区需要一个跨会话、跨工作区复用的参考资料库。
现有 `.agent/uploads/<thread>` 只服务单次会话，`knowledge/` 只服务单次运行；
两者都不能作为用户长期资料库。

部署目标包含 Web 多用户、团队与 `sandbox`，但暂不要求多人共享或多设备同步。
资料正文属于执行面数据，控制面 MySQL 只应保存元数据，不应保存正文或 chunk。

## Decision

1. **资料库按用户隔离，跨工作区保存**：
   - Web 多用户由 `TenancyManager` 为每个用户创建
     `<AGENT_WEB_LIBRARY_ROOT>/<user>`；
   - 根目录不在 workspace 内；配置成 workspace 内部直接拒绝。
2. **正文与索引放 A4**：
   - `catalog.json`、`items/<id>/source.bin`、`chunks.jsonl` 均由
     `SandboxFs` 读写；
   - A2 只负责认证、限额与代理，不解析、不执行。
3. **A3 负责摄取与检索**：
   - 解析复用 `tools/documents`；
   - 切块保留 page/sheet/heading/line anchor；
   - `library_search` 是唯一暴露给模型的工具，只读 L0。
4. **默认词法，向量可选**：
   - `AGENT_LIBRARY_SEARCH_MODE=lexical|vector|hybrid`，默认 lexical；
   - 词法用 BM25；hybrid 用两路名次的 RRF 融合；
   - 可选确定性 rerank；结果默认经 MMR 去重，`AGENT_LIBRARY_MMR_LAMBDA` 可调；
   - 复用 memory embedding provider；remote 仍受原有门禁；
   - 向量索引按 chunk 存 `vectors.json`，provider/model/dim 不匹配即失效；
   - 可选 hnswlib 后端，失败/未安装回退 linear；
   - embedding 失败回退词法，不阻断检索。
5. **不自动注入**：
   - 模型必须主动调用 `library_search`；
   - 资料内容是引用材料，不是指令。
6. **不做共享**：
   - 团队能力先体现为多用户隔离，不做 ACL、共享目录或多设备同步。

## Consequences

**正面**

- 资料库生命周期与 workspace、run 解耦，换工作区不丢资料；
- 控制面不存正文，符合 A2/A3/A4 数据归属；
- 解析、路径守卫、审计与配额全部复用现有执行面能力；
- 默认关闭，不影响既有行为。

**负面 / 代价**

- 向量索引在摄取时生成；切换 embedding provider 后需重建索引；
- 选择 remote embedding 时，资料片段会发送到外部服务，仍受
  `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS` 与 security profile 门禁；
- 异步任务只有阶段级进度，取消在阶段边界生效；任务队列仍是 A3 进程内；
- 扫描件没有 OCR，只能标记 `needs_ocr`；
- catalog 是文件式，多 A3 实例依赖共享存储与文件锁；
- 团队仍没有共享资料库，产品能力有明确缺口。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 扩 `knowledge/` | run 作用域与资料库生命周期不同，开关耦合 |
| 存 MySQL | 正文属于执行面，违反控制面数据边界 |
| 纯客户端 | Web A3 读不到，sandbox 工作区不可用 |
| 自动注入 top-K | 会放大 token 成本与提示注入面；本期先让模型主动查 |
