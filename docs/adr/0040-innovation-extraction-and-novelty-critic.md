# ADR-0040：创新点提炼与 novelty critic

- **Status**：accepted
- **Date**：2026-10-31
- **Deciders**：项目作者
- **Owner**：项目作者
- **Related**：[RFC-0009](../rfc/0009-deep-workflows-and-knowledge-artifacts.md)、
  [ADR-0038](0038-structured-document-matrix-and-attribution.md)、
  [评测记录](../archive/eval/DEEP_WORKFLOW_EVAL.md)

## Context

ADR-0038 已能把多篇文献压成固定槽位矩阵并校验证据归属，但“提炼创新点”
仍是开放问题：模型很容易把论文自己的贡献复述成“新创新”，或给出无法证伪的
宏大叙事。仅靠一次生成无法区分“有证据的候选”和“听起来不错的空话”。

同时，AI 无法凭内部知识证明外部文献没做过某件事；任何“首次/新颖”主张都
必须限定在**给定材料**的范围内，并交给人工或外部检索复核。

## Decision

1. **结构化候选** `InnovationCandidate`：
   `title / gap / insight / hypothesis / novelty_claim / prior_art_contrast /
   falsifiable_experiment / risks / confidence / evidence`。
2. **两阶段**：
   - 生成阶段只依据 `DocumentMatrix` + 已验证 sources，要求每个候选至少一条
     可归属证据；
   - novelty critic 第二层只依据候选与给定证据，判断证据是否支持 gap、
     是否只是复述已有贡献、实验是否可证伪，输出 `supported/score/issues`。
3. **证据路径归一**：模型常把证据写成标题或 basename（`U-Net`、`VMamba.pdf`），
   按 path → basename → stem → 矩阵标题别名逐级解析；无法归属的候选直接丢弃。
4. **跨文档补充**：首轮候选只覆盖一篇长文档时，对未覆盖文档做一次有界补充
   生成；最终评测要求至少 2 篇文档参与证据。
5. **不宣称新颖性已被证明**：`novelty_claim` 只相对给定材料；critic 也不使用
   外部知识。真实新颖性需要外部检索与人工评审。
6. 能力以 `knowledge/innovation.py::extract_innovations` 暴露；评测入口是
   `tests/eval/papers.py --case papers/innovation`。

## Consequences

**正面**

- 3 篇实测：8 个候选、6 个通过 critic、3/3 文档参与证据、归属错配 0；
- 候选必须带 gap/假设/可证伪实验/风险，空话会被 critic 或结构校验拦住；
- 证据路径归一后，标题/basename 引用不会整批丢失。

**负面 / 代价**

- 3 篇单次约 7 万输入 / 5 万输出 token、约 2 分钟；10 篇会显著更高。
- critic 仍是 LLM，可能宽松或保守；score 不是新颖性证明。
- 只覆盖给定材料，未做外部 prior-art 检索；“首次”类主张必须降级表述。
- 目前是独立能力与评测，尚未接入 orchestrator 常规任务图。

## Alternatives

| 方案 | 为什么没选 |
|---|---|
| 单次生成 + 无 critic | 无法区分空话与有证据候选 |
| 让模型直接声称“新颖” | 不可验证，容易误导 |
| 只用确定性规则 | 无法判断 gap/hypothesis 的语义质量 |
| 复用 author 报告 | 报告没有固定创新槽位，也没有 critic |
| 接入外部文献检索 | 成本/隐私/许可复杂，先限定在给定材料 |
