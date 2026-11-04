---
layer: L2
doc_id: rfc-index
title: RFC 索引与流程
owner: 项目作者
status: active
last_reviewed: 2026-11-02
review_cadence: 新增或状态变更时
---

# RFC 索引与流程

**RFC 回答“要不要做、做到什么边界”；ADR 回答“已经决定了什么、代价是什么”。**
前者在动手前写，后者在决策后写。两者都不替代测试与契约。

## 什么时候写 RFC

- 新增跨模块能力、改变运行拓扑或状态字段；
- 引入外部依赖、协议、数据迁移或兼容性变化；
- 改动安全边界（审批语义、沙箱、路径守卫、密钥处理）；
- 需求本身有多个可行方案，且代价差异明显。

小修小补不必写 RFC —— 直接写测试和 ADR（如果确实有决策）即可。

## 状态机

```text
draft ──批准──▶ accepted ──实现完成──▶ implemented
  │                 │
  │                 └──放弃──▶ rejected
  └──被新 RFC 取代──▶ superseded
```

规则：

1. **只增不改**。被推翻时新增 RFC，旧条目改 `status: superseded` 并链接新条目。
2. **必须写非目标与替代方案**。只写目标的 RFC 无法判断边界，也无法解释为什么没选另一条路。
3. **批准后必须落地**：至少更新一份契约（`docs/contracts/` 或测试）、一条 ADR、以及验收锚点
   （`docs/acceptance/acceptance-criteria.yaml` 的 `tests:`）。
4. **实现完成后补 Consequences**：记录真实结果、新增技术债与未达成的目标。
5. RFC 不自动批准。安全、权限、审计、数据迁移的 RFC 必须有人工批准记录。

## 与其它文档的关系

| 文档 | 关系 |
|---|---|
| [`../adr/`](../adr/README.md) | RFC 批准后，把“为什么这么选、放弃什么”沉淀成 ADR |
| [`../contracts/`](../contracts/index.md) | RFC 涉及的接口/事件/配置必须进入代码生成契约 |
| [`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml) | 验收标准落到可运行断言，而不是停留在 RFC 正文 |
| [`../tech-debt.yaml`](../tech-debt.yaml) | RFC 明确不做、但将来要还的部分登记为技术债 |
| [`../THREAT_MODEL.md`](../THREAT_MODEL.md) | 涉及安全边界时，先对照威胁模型 |

## 索引

| 编号 | 标题 | 状态 | Owner | 关联 ADR |
|---|---|---|---|---|
| [0001](0001-workspace-registry.md) | 工作区注册表与托管存储 | accepted | 项目作者 | 0012、0013 |
| [0002](0002-native-execution-domain-and-grant-model.md) | 客户端执行域、授权凭证模型与 A2L 宿主能力代理（五层） | accepted | 项目作者 | 0015 |
| [0003](0003-frontend-backend-separation.md) | 前后端分离的运行时契约、错误协议与传输矩阵 | accepted | 项目作者 | 0016、0017、0018 |
| [0004](0004-enterprise-identity-and-four-eyes.md) | 企业身份（OIDC / TOTP）与四眼审批 | accepted | 项目作者 | 0023 |
| [0005](0005-document-image-and-report-pipeline.md) | 二进制文档与图像理解、报告撰写能力（文档管线） | accepted | 项目作者 | 0024、0025 |
| [0006](0006-incremental-implement-merge.md) | implement 补丁按连通分量增量合并 | accepted | 项目作者 | 0026 |
| [0007](0007-sequential-write-waves.md) | implement 顺序写波次与 worktree 基线注入 | accepted | 项目作者 | 0027 |
| [0008](0008-orchestrated-fix-rounds.md) | verify 失败后的有界编排修复轮 | accepted | 项目作者 | 0028 |
| [0009](0009-deep-workflows-and-knowledge-artifacts.md) | 复杂长文档与代码架构的深层工作流 | accepted | 项目作者 | 0029、0030、0031 |
| [0010](0010-per-user-library.md) | 每用户跨工作区资料库 | implemented | 项目作者 | 0039 |

## 模板

[`TEMPLATE.md`](TEMPLATE.md)。文件名从 `0001` 起递增，不回收已用编号。
