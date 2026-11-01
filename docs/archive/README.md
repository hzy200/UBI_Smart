---
layer: L2
doc_id: archive-index
title: 归档索引
owner: 项目作者
status: active
last_reviewed: 2026-11-01
review_cadence: 引用变动时
---

# 归档索引

历史阶段、一次性评测报告与旧计划放在这里。**归档不代表当前状态**；当前
架构、编排、使用和安全边界分别见 [../ARCHITECTURE.md](../ARCHITECTURE.md)、
[../HARNESS.md](../HARNESS.md)、[../USAGE.md](../USAGE.md)、
[../THREAT_MODEL.md](../THREAT_MODEL.md)。

## 阶段文档（`phases/`）

| 文件 | 当前替代 |
|---|---|
| [`PHASE0.md`](phases/PHASE0.md) | [USAGE.md](../USAGE.md) |
| [`PHASE1.md`](phases/PHASE1.md) | [USAGE.md](../USAGE.md)、[FRONTEND.md](../FRONTEND.md) |
| [`PHASE2.md`](phases/PHASE2.md) | [RELEASING.md](../RELEASING.md)、`packaging/wsl/` |
| [`PHASE3.md`](phases/PHASE3.md) | [FRONTEND.md](../FRONTEND.md) |
| [`PHASE4.md`](phases/PHASE4.md) | [FRONTEND.md](../FRONTEND.md) |
| [`PHASE5.md`](phases/PHASE5.md) | [FRONTEND.md](../FRONTEND.md) |
| [`PHASE_A.md`](phases/PHASE_A.md) | [ARCHITECTURE.md](../ARCHITECTURE.md) §12 |
| [`PHASE_B.md`](phases/PHASE_B.md) | [ARCHITECTURE.md](../ARCHITECTURE.md) §13 |
| [`PHASE_C.md`](phases/PHASE_C.md) | [ARCHITECTURE.md](../ARCHITECTURE.md) §16 摘要 |
| [`PHASE_D.md`](phases/PHASE_D.md) | [ARCHITECTURE.md](../ARCHITECTURE.md) §17 摘要 |
| [`PHASE_E.md`](phases/PHASE_E.md) | [FRONTEND.md](../FRONTEND.md) |
| [`PHASE_F.md`](phases/PHASE_F.md) | [CI.md](../CI.md)、[THREAT_MODEL.md](../THREAT_MODEL.md) |
| [`PHASE_G.md`](phases/PHASE_G.md) | [ENVIRONMENT.md](../ENVIRONMENT.md) |
| [`ARCHITECTURE_PHASES.md`](ARCHITECTURE_PHASES.md) | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| [`HARNESS_ROADMAP.md`](HARNESS_ROADMAP.md) | [HARNESS.md](../HARNESS.md) |
| [`ACCEPTANCE_PHASES.md`](ACCEPTANCE_PHASES.md) | [ACCEPTANCE.md](../ACCEPTANCE.md) |

## 评测与快照（`eval/`）

| 文件 | 内容 |
|---|---|
| [`LANGSMITH_REPORT.md`](eval/LANGSMITH_REPORT.md) | 早期 LangSmith 评测 |
| [`LANGSMITH_REPORT_V2.md`](eval/LANGSMITH_REPORT_V2.md) / [`V3`](eval/LANGSMITH_REPORT_V3.md) | 157/158 能力任务评测 |
| [`HARD_SUITE_REPORT.md`](eval/HARD_SUITE_REPORT.md) | hard 套件（14 个高难度任务） |
| [`REAL_SUITE_REPORT.md`](eval/REAL_SUITE_REPORT.md) | real 套件（标准库回归） |
| [`REAL_ADVANCED_REPORT.md`](eval/REAL_ADVANCED_REPORT.md) | real_advanced 套件 |
| [`BLIND_SUITE_REPORT.md`](eval/BLIND_SUITE_REPORT.md) | blind 盲测套件 |
| [`CONTEXT_AB_REPORT.md`](eval/CONTEXT_AB_REPORT.md) | 上下文/worker 成本 A/B |
| [`CONTEXT_PROFILE.md`](eval/CONTEXT_PROFILE.md) | 上下文画像采样 |
| [`DEEP_WORKFLOW_EVAL.md`](eval/DEEP_WORKFLOW_EVAL.md) | 深度工作流/长文档评测 |

评测方法与当前噪声口径见 [`../../tests/eval/README.md`](../../tests/eval/README.md)。

## 计划（`plans/`）

| 文件 | 状态 |
|---|---|
| [`DOCUMENT_PIPELINE_PLAN.md`](plans/DOCUMENT_PIPELINE_PLAN.md) | implemented；当前见 RFC-0005 / ADR-0038 |
| [`POLYGLOT_PLAN.md`](plans/POLYGLOT_PLAN.md) | proposed；未实施 |

## 其他

- [`DEMO.md`](DEMO.md)：答辩演示讲稿（历史）。
- [`TEST_REPORT_2026-10-21.md`](TEST_REPORT_2026-10-21.md)：全量测试快照。

## 规矩

- 不要新建指向不存在文件的链接；`scripts/check_docs.py` 会拦断链。
- 历史快照只增不改；新结果另起一份，不要覆盖历史基线。
