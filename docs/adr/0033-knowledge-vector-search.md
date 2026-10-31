# ADR-0033：知识检索的向量/远程 embedding 适配

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0029](0029-knowledge-artifacts.md)

## Context

知识产物检索最初只有确定性词法匹配。长文档的语义召回需要向量能力，但项目
已经有一套 memory embedding provider（hash/local/remote）与远程门禁，重复
实现会带来两套密钥、两套安全开关。

## Decision

1. `knowledge/embedding.py` 复用 `memory/embeddings.py::build_embedder`：
   - `AGENT_KNOWLEDGE_SEARCH_MODE=lexical|vector|hybrid`，默认 lexical；
   - `AGENT_KNOWLEDGE_EMBEDDING_PROVIDER` 为空时继承
     `AGENT_MEMORY_EMBEDDING_PROVIDER`；
   - remote 仍要求 `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS=true` +
     base_url/key，安全 profile 仍可禁止。
2. `KnowledgeStore.search(..., embedder, mode)`：
   - vector：纯余弦排序；
   - hybrid：词法归一化分与余弦各占一半；
   - embedding 失败/不可用时**回退词法**，不阻断检索。
3. 批量 embedding + 进程内缓存（provider+文本哈希），避免重复远程调用。
4. `knowledge_search` 工具新增可选 `mode`，留空使用会话配置。

## Consequences

**正面**

- 语义召回与既有 memory 能力一致，无新增依赖或密钥体系。
- 默认零依赖、零网络；远程必须双重显式开启。
- 失败回退词法，检索是增强而不是正确性前提。

**负面 / 代价**

- hybrid 权重是固定经验值，未按语料调优。
- 远程 embedder 的审计目前只体现在配置门禁，尚未逐次写审计记录。
- 缓存是进程内的，跨进程不共享。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 新建独立 embedding 客户端 | 两套密钥/门禁，安全面扩大 |
| 默认开启 local 模型 | 首次下载与 CPU 开销会拖慢普通会话 |
| 纯向量检索 | 代码标识符/精确路径检索会退化 |
