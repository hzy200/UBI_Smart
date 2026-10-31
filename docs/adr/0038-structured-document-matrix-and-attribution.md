# ADR-0038：结构化文档矩阵、分层归并与跨文档归属校验

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0030](0030-deep-document-flow.md)、
  [ADR-0034](0034-deep-workflow-evals.md)、
  [ADR-0038 的评测记录](../DEEP_WORKFLOW_EVAL.md)

## Context

ADR-0030 用 map-reduce 解决长文档截断，但最终归并仍是“把所有摘要塞进一次
调用”。3 篇时可用，扩到 10 篇（230 页）后暴露四类问题：

1. 单次归并输入随文档数线性增长，模型会遗漏/混淆文档；
2. 散文摘要把每篇压成一两句，方法/实验细节丢失；
3. 证据点数被前几篇长文档占满，后几篇 sources 为 0；
4. 模型会把 A 的结论写到 B 名下，而 judge 只看锚点是否存在，看不出归属。

继续放大 `summary` 长度只会增加成本与遗漏，不能解决结构问题。

## Decision

1. **每篇固定槽位矩阵** `DocumentMatrix`：
   `problem / method / algorithm / experiments / limitations / innovations /
   conclusion`，evidence 只复制已验证证据；空槽位用已验证 claim 按关键词
   确定性回填。矩阵随 `WorkerResult.matrix` 下传。
2. **分层/树状归并**：每篇矩阵/摘要作为叶节点，按
   `AGENT_DOCUMENT_TREE_FANOUT`（默认 5）逐层分组归并；单次输入受
   `AGENT_DOCUMENT_TREE_INPUT_CHARS`（默认 16k）约束。树高 O(log N)，
   不再放大单次调用。
3. **最终答案附紧凑矩阵**：树状归并后仍把每篇的固定槽位以紧凑行附在
   `## 证据矩阵`，并加“已解析文档（N 篇）”头，避免把已覆盖文档说成缺失。
4. **引用跨度校验**：quote 在归一化文档文本中定位，记录
   `quote_verified` / `quote_span{start,end,page}`；章节号/作者名/页码范围
   回写成解析器真实锚点，无法定位的证据不进入下游。
5. **跨文档归属校验** `knowledge/attribution.py`：从路径 stem 与矩阵标题
   生成别名，按句子检查“提到的文档”与引用路径；错配写入
   `WorkerResult.attribution_issues`，评测默认要求 0 条。
6. **报告评测**：`tests/eval/papers.py` 的 `papers/report` 真实执行
   `document → author → report_check`，断言覆盖、章节、逐篇引用、矩阵槽位、
   引用跨度、归属与长度。
7. 矩阵默认关闭（`AGENT_DOCUMENT_MATRIX_ENABLED=false`）；评测/长文档场景
   显式开启。树状归并始终用于多节点摘要。

## Consequences

**正面**

- 10 篇从“后 4 篇证据为 0 / 漏引”变为 10/10、每篇 35–67 条证据；
- 矩阵槽位覆盖 87–94%，方法/实验不再被树状归并压掉；
- 归属错配可被确定性暴露，10 篇两次运行均为 0 条；
- 树状归并的单次输入有硬上限，Cost 随 N 近似线性而非平方。

**负面 / 代价**

- 每篇多一次结构化调用；10 篇单次约 58–60 万输入、47–55 万输出 token，
  19–24 分钟，不适合交互路径默认全量跑。
- 归属校验只能发现“提到 A 却引用 B”，发现不了“引用 A 但结论其实属于 B”
  的语义错误；10 篇报告仍可能出现“只有 7 篇有正文”的错误概述。
- 关键词回填可能把泛化 claim 放进错误槽位，仍需人工/LLM judge 复核。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 继续放大单次摘要长度 | 成本更高、遗漏更严重，且无法解决归属 |
| 只做扁平 map-reduce | 10 篇时后段文档证据/摘要会被截断 |
| 只用 LLM 判断归属 | 不可复现、成本高；确定性别名检查更便宜 |
| 每篇都生成完整报告再拼接 | 重复内容多、跨文档比较弱、token 更高 |
| 默认开启矩阵 | 所有文档任务都多一次调用；改为显式开启 |
