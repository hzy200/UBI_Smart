# ADR-0034：长文档与架构变更的效果评测

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)

## Context

现有 `tests/eval` 的判定是“项目自带测试是否通过”，适合代码任务，但不能
直接衡量长文档覆盖、引用可信度与跨模块接口传播。没有指标时，“更努力”无法
证明“更有效”。

## Decision

1. **架构变更评测**：新增 `tests/eval/tasks_arch.py`，套件 `arch`：
   - `arch/interface_rename`：跨 3 模块重命名，隐藏测试要求旧名字消失；
   - `arch/currency_propagation`：新参数/返回值贯通 api→orders→pricing；
   - 可见测试在种子状态通过，只有隐藏测试能判定需求是否满足。
2. **长文档指标**：新增 `tests/eval/doc_metrics.py`：
   - `coverage_metrics`：总页数、已读页、未读区间、覆盖率；
   - `citation_metrics`：anchor/quote 比例；
   - `evidence_coverage`：claim 是否有 path#anchor 证据；
   - 不把过程指标伪装成答案正确性。
3. **手动长文档评测**：`scripts/eval_documents.py` 用真实 WSL + 模型跑
   document worker，输出上述指标；答案质量仍需人工或独立 judge。
4. 两套评测都不进入默认快反馈 CI：代码套件需要 WSL/Key，文档脚本需要文档
   与模型额度。

## Consequences

**正面**

- 跨模块效果可以用隐藏测试稳定判定，不依赖模型自述。
- 长文档至少有可复现的覆盖/引用指标，能发现“读了 15 页却假装全文”。
- 指标代码是纯函数，可单测、可在 CI 之外复用。

**负面 / 代价**

- 文档答案正确性仍缺自动判定；当前只覆盖过程可信度。
- arch 套件需要维护参考解与隐藏测试，新增任务有成本。
- 指标是代理指标，覆盖率 100% 不等于结论正确。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 只看最终报告文本 | 无法区分“没读全”和“读了但写错” |
| 给文档任务写 unittest | 文档没有天然的可执行判定 |
| 只用 LLM judge | 成本高、不可复现，且 judge 也会被长文误导 |
