# ADR-0036：跨语言调用图与跨机器索引缓存

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0035](0035-incremental-sampling-and-doc-judge.md)

## Context

ADR-0035 的增量缓存按仓库根存放，换 clone/机器会重建；架构索引只有 import 边，
跨语言调用方（TS/Go/Rust/JVM）不会进入影响面。两者都会让跨模块变更漏掉真实调用方。

## Decision

### 1. 轻量跨语言调用图

- `knowledge/callgraph.py`：
  - Python 用 AST 提取顶层函数/方法定义与函数体里的调用；
  - 其他语言用正则提取定义/调用（带行号）；
  - 按**唯一符号名**解析：同文件优先；其次全局唯一；同名多定义直接跳过；
  - 边带 `confidence`（同文件 high / 同语言唯一 medium / 跨语言唯一 low）。
- `ArchIndex.calls` 进入产物；`arch_brief` 显示调用边数量。
- `impact_analysis` 在 import 闭包之外，沿**反向 call 边**继续扩文件/模块；
  无法解析的调用不猜。
- `build_change_contract` 把调用图发现、但不在 import 波次里的文件补到最后一波
  （或并入最后一波），保证跨语言调用方不丢。
- **动态/反射/宏**：`getattr/setattr(obj,"name")()`、`globals()["name"]()`、
  `eval/exec/import_module/partial`、计算属性 `obj["m"]( )`、宏 `trace!(...)`、
  `eval/Function/import()/reflect./::method` 会被识别或记录到
  `ArchIndex.dynamic_calls`；变更附近存在动态调用时 `impact_analysis.fallback=True`。
- **宏定义/调用**：Rust `macro_rules!` 与 C `#define` 的定义会被提取，宏调用形成
  `kind=macro` 边；这是宏调用图，不是编译器级展开。
- **LSP 动态解析（可选）**：`AGENT_ARCH_LSP_RESOLUTION_ENABLED=true` 且配置
  `AGENT_LSP_COMMAND` 时，对 `dynamic_calls` 位置调用
  `textDocument/definition`，把可定位目标补成 `kind=lsp` 文件级边；
  成功解析的条目标记 `resolved`，不再触发 fallback；LSP 不可用则保持原状。

### 2. 内容寻址的跨机器缓存

- `knowledge/index_cache.py` 缓存键改为**文件内容 SHA-256**，不再按仓库根/路径；
- 默认目录 `~/.codingagent/index-cache/files/<hash>.json`，可用
  `AGENT_ARCH_INDEX_CACHE_DIR` 指向共享盘；
- 内容相同即可命中：换 clone、换机器、文件改名都能复用；
- 写入用临时文件 + `os.replace` 原子替换；缓存只存符号/imports，不存源码；
- 读写失败回退全量解析；
- 自动淘汰：按 `ARCH_INDEX_CACHE_TTL_DAYS`、`MAX_ENTRIES`、`MAX_BYTES`
  做 best-effort LRU；每 100 次写入触发一次，也可手动 `prune()`；
- 分布式锁：`knowledge/cache_lock.py` 用 `O_CREAT|O_EXCL` 锁文件 + token +
  stale 接管；每个内容哈希一把锁，`prune` 用全局锁。锁超时只跳过本次写/清理，
  由后续运行重试；锁不可用时退化为短暂竞争，不会损坏内容。

## Consequences

**正面**

- 跨语言调用方进入影响分析与 ChangeContract，减少“只改了 TS 调用方却漏了 Go”；
- 同一内容在团队共享缓存下只解析一次，跨机器也生效；
- 解析不确定时跳过（同名多定义），保持保守。

**负面 / 代价**

- 正则调用图有噪声/漏报；跨语言 low-confidence 边仍需人工复核。
- 共享缓存锁依赖文件系统 `O_EXCL` 语义；SMB/NFS 上是否原子取决于服务器，
  仍是 best-effort 协调而非强一致。
- 淘汰是 best-effort，不保证严格上限；共享盘延迟会影响 prune。
- 宏只建调用图、不做真正展开；LSP 只能补 definition 边，不能替代完整
  调用层级/类型推断。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 引入 tree-sitter/LSP 全语言解析 | 依赖与运行成本高，先要能覆盖主要语言 |
| 按路径缓存 | 换 clone/机器即失效，无法共享 |
| 同名多定义时取第一个 | 会把调用连到错误实现，污染影响面 |
| 跨语言调用边全部忽略 | 会漏掉真实的跨语言调用方 |
