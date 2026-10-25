---
layer: L3
doc_id: real-suite-report
title: real 套件评测报告（真实标准库代码）
owner: 项目作者
status: snapshot
last_reviewed: 2026-10-25
review_cadence: real 套件任务或基线变动时
warning: |
  这是一次本机实跑的快照。real 套件用完整标准库模块做回归定位，
  不 vendor 源码；任务在导入时从宿主 Python 读取。
---

# real 套件评测报告

## 总览

| 指标 | 值 |
|---|---|
| 任务数 | 5 |
| 模型 | `deepseek-chat` |
| 重复次数 | 2 |
| 逐轮通过率 | **5/5（100%）**、**5/5（100%）** |
| 波动 `spread` | 0.0 个百分点 |
| 逐轮翻转 | 无 |
| 两轮 token | 输入 1,012,919 / 输出 13,696 |
| 缓存命中率 | **7.6%**（73,088 hit / 883,633 miss） |
| 基线 | `tests/eval/baseline_real.json` |
| comparable | **`true`**（commit `3f34a2b`，工作区干净） |

## 任务

| 任务 | 标准库模块 | 源文件 | 注入回归 |
|---|---|---:|---|
| `real/configparser/interpolation` | `configparser.py` | 56KB | `%%` 转义被当成非法插值 |
| `real/textwrap/long_words` | `textwrap.py` | 20KB | `break_long_words=True` 不再断长词 |
| `real/csv/restkey` | `csv.py` | 16KB | `DictReader` 多余列不再进 `restkey` |
| `real/shlex/comments` | `shlex.py` | 14KB | `comments=True` 时 `#` 注释被当成词 |
| `real/argparse/type_conversion` | `argparse.py` | 100KB | 单参数不再应用 `type=` 转换 |

每个任务只注入一处回归，参考解就是**未修改的标准库原文**。锚点必须唯一；
标准库版本变化时任务构造会显式报错，不会静默生成无效任务。

## 机制指标

```text
验证：5/5 任务触发
工具调用：13–21 次/任务（两轮合计）
步数：3–4 步/轮
修复：0
重规划：1（第二轮 shlex）
子智能体：0
```

## 结论

1. **文件规模本身没有构成难度。** 100KB 的 `argparse.py` 与 14KB 的
   `shlex.py` 一样，模型都在 3–4 步内定位到回归；可见测试直接指出了
   失败行为，搜索空间被测试名和断言大幅缩小。
2. **real 套件也已饱和**（5/5 ×2，spread 0）。它仍然有价值：
   - 覆盖“在真实大文件里读代码/定位”的能力；
   - 基线可比较，且缓存/上下文指标齐全；
   - 作为后续“隐藏需求/多回归”任务的对照组。
3. 要真正恢复区分度，下一轮应改变**信息条件**，而不是继续加大文件：
   - **可见测试全绿、隐藏测试才失败的 real-code 任务**（spec 化）；
   - 同一模块注入**多个互相掩盖的回归**；
   - 只给症状、不给失败测试名（例如“某些输入下行为不一致”）；
   - 性能/复杂度约束（同一 API 在大输入下退化）。

## 复现

```bash
conda activate agent
python tests/eval/runner.py --suite real --verify-seeds
python tests/eval/runner.py --suite real --repeat 2
```

LangSmith 数据集：`coding-agent-real`（5 条）。
