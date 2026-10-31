# ADR-0037：文档 judge 阈值校准

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0034](0034-deep-workflow-evals.md)

## Context

ADR-0034/0035 的文档 judge 用固定 60 分作为通过阈值，但不同文档集/模型下
分数分布不同；固定阈值要么放过坏答案，要么误杀好答案。

## Decision

1. `AGENT_DOCUMENT_JUDGE_PASS_SCORE`（默认 60）控制确定性/LLM judge 的通过阈值。
2. `knowledge/judge_calibration.py::calibrate_threshold`：
   - 输入标注样本 `[(score,label)]`；
   - 遍历 0–100，计算 accuracy/precision/recall/F1；
   - 选择 F1 最大；并列时准确率更高；再并列取更高阈值（更保守）。
3. `scripts/calibrate_judge.py` 读取 JSONL：
   `{"question","answer","sources","label":"pass|fail"}`；
   默认确定性 judge，`--llm` 时调用模型 judge；输出推荐阈值。
4. LLM judge 的 `supported` 同时要求 `score >= threshold` 且确定性检查没有
   未知引用，避免模型宽松放过坏引用。
5. 无样本/坏样本时回退默认阈值，不抛异常。
6. **种子标注集** `tests/eval/judge_dataset.py`：
   - 8 个主题 ×（有证据 / 错锚点 / 无证据）= 24 条可复现样本；
   - `scripts/calibrate_judge.py --dataset seed` 直接校准；
   - `--write-template <path>` 导出 JSONL，供人工追加真实标注后重跑。
   这不是统计意义上的大规模人工标注，而是可持续扩充的起点。
7. **大规模构建/校验/切分** `tests/eval/judge_dataset_builder.py`：
   - `build_cases(target)` 可按目标规模生成合成标注（`label_source=synthetic`）；
   - `validate_cases` 校验 schema/重复/label，`require_human=True` 只接受人工标注；
   - `dedupe_cases`、`split_cases`（train/calibration/test）确定性切分；
   - `scripts/build_judge_dataset.py` 支持生成、校验、导出与切分。
   **合成数据只用于跑通流程；真实校准必须补充 `label_source=human` 的标注。**
8. **待人工审核队列** `tests/eval/judge_human_pack.py`：
   - 默认导出 150 条 `label_source=pending-human`、`label` 为空的候选；
   - `promote_case` 强制要求合法 label 与 reviewer 才能转成 `human`；
   - `require_human=True` 会拒绝未审核数据，避免把 AI 标签冒充人工标注。

## Consequences

**正面**

- 阈值可随语料/模型重标，而不是永久写死；
- 校准是纯函数，可单测、可复现；
- LLM 与确定性结果互补，减少单边误判。

**负面 / 代价**

- 校准质量取决于标注集规模与代表性；合成集能跑通流程，但不能替代真实人工标注。
- 阈值只优化二分类，不区分“轻微缺证据”和“严重编造”。
- 标注本身需要人工成本；`require_human=True` 会拒绝合成数据，避免误用。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 固定阈值 | 不同语料分布差异大 |
| 用 LLM 自评替代阈值 | 不可复现、成本高 |
| 只优化准确率 | 类别不平衡时会偏向多数类 |
