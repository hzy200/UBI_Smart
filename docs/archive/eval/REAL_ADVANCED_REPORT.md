---
layer: L3
doc_id: real-advanced-report
title: real_advanced 套件评测报告（隐藏需求与多回归）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: real_advanced 任务或基线变动时
warning: |
  这是 14 个任务、2 次重复的修正后快照。上一版报告的翻转与低通过率
  已被确认是评测 harness 未处理 L3 审批挂起导致的假象，不能作为能力结论。
---

# real_advanced 套件评测报告

## 总览（修正后）

| 指标 | 值 |
|---|---|
| 任务数 | 14（11 个隐藏需求 + 3 个多回归） |
| 模型 | `deepseek-chat` |
| 重复次数 | 2 |
| 逐轮通过率 | **14/14（100%）**、**14/14（100%）** |
| 波动 `spread` | 0.0 个百分点 |
| 翻转任务 | 无 |
| 两轮 token | 输入 3,682,712 / 输出 57,820 |
| 缓存命中率 | 11.1% |
| L3 审批 | 10 次请求，10 次由 harness 代为批准 |
| 基线 | `tests/eval/baseline_real_advanced.json` |
| comparable | **`true`**（commit `8c47527`，工作区干净） |

## 任务与结果

| 任务 | 类别 | 两轮 | 工具调用 | 耗时 |
|---|---|---|---:|---:|
| `real/spec/statistics/median_even` | 隐藏需求 | 2/2 | 13 | 38.2s |
| `real/spec/fractions/negative_denominator` | 隐藏需求 | 2/2 | 20 | 49.4s |
| `real/spec/heapq/reverse_merge` | 隐藏需求 | 2/2 | 25 | 56.1s |
| `real/spec/textwrap/replace_whitespace` | 隐藏需求 | 2/2 | 18 | 48.3s |
| `real/spec/argparse/allow_abbrev` | 隐藏需求 | 2/2 | 21 | 54.3s |
| `real/spec/heapq/nlargest_key` | 隐藏需求 | 2/2 | 20 | 50.0s |
| `real/spec/statistics/variance_xbar` | 隐藏需求 | 2/2 | 20 | 56.7s |
| `real/spec/fractions/limit_denominator` | 隐藏需求 | 2/2 | 27 | 68.8s |
| `real/spec/argparse/dest_hyphen` | 隐藏需求 | 2/2 | 26 | 59.7s |
| `real/spec/shlex/escaped_space` | 隐藏需求 | 2/2 | 26 | 58.0s |
| `real/spec/csv/extrasaction` | 隐藏需求 | 2/2 | 19 | 51.1s |
| `real/multi/configparser/escaped_and_recursive` | 多回归 | 2/2 | 18 | 56.7s |
| `real/multi/argparse/type_and_default` | 多回归 | 2/2 | 21 | 50.2s |
| `real/multi/csv/restkey_and_restval` | 多回归 | 2/2 | 16 | 37.4s |

## 重要更正：上一版的翻转不是模型能力

上一版报告（14 任务、2 轮）得到 13/14、13/14，并报告
`fractions/limit_denominator` 与 `shlex/escaped_space` 翻转；
对两个任务做 `--repeat 5` 得到 3/5、4/5，当时解释为“模型提前收手”。

**这个解释是错的。** 逐事件复查发现失败运行的尾部是：

```text
ToolCallStarted(shell_exec) → ApprovalRequested → 事件流结束
```

失败运行没有 `RunFinished`、没有写入、没有最终回答，`tokens=None`。
根因是评测 harness 没有消费 `ApprovalRequested`：

- `approval_mode="approve"` 只自动放行 L2；L3 仍会挂起；
- 模型提出的 `cd ... && git log && git diff && cat pytest.ini` 被策略判为 L3；
- harness 没有调用 `runtime.resume`，运行停在挂起状态，外部看起来像“1 步就停了”。

这不是模型提前收手，也不是任务不可满足。修复方式：

- `run_task` 在 `ApprovalRequested` 时**代为批准并 resume**；命令仍进审计；
- 新增 `approvals` / `approvals_approved` 指标，baseline 与 LangSmith 都会记录；
- 真实审批暂停/恢复仍由 `scripts/phase_g_e2e.py` 单独验收，不受影响。

修复后同一套 14 个任务两轮均为 14/14，L3 审批 10 次全部由 harness 批准。

## 结论

1. **上一版的区分度是假象。** 评测 harness 的行为会伪装成模型失败；
   任何“1 步、未验证、无写入”的结果都必须先检查审批/异常，而不是直接归因能力。
2. **修正后 real_advanced 已饱和。** 11 个 spec + 3 个多回归全部两轮通过。
   “信息更少”在当前任务上没有形成难度。
3. **多回归依旧没有区分度**，保留作覆盖。
4. 下一步若要继续提升难度，需要换一个轴：更长的依赖链、跨服务/跨语言、
   或要求 agent 自己发现需求缺口；仅把提示词写得更模糊已经不奏效。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite real_advanced --verify-seeds
python tests/eval/runner.py --suite real_advanced --repeat 2
```

LangSmith 数据集：`coding-agent-real-advanced`（14 条）。
