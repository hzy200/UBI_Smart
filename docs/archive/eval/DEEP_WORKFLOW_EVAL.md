---
layer: L3
doc_id: deep-workflow-eval
title: 深层工作流首轮真实评测记录
owner: 项目作者
status: active
last_reviewed: 2026-10-31
review_cadence: 每次真实评测后追加，不改历史数字
---

# 深层工作流首轮真实评测记录（2026-10-31）

> 这是**首轮真实运行**，不是统计结论。单任务分辨率 1/1，且工作区有未提交改动，
> 结果被 harness 标记 `comparable=false`。数字只能用于定位问题，不能用于宣称提升。
>
> 环境：WSL Ubuntu、`deepseek-chat`、沙箱 python3；arch 套件任务
> `arch/interface_rename`（6 个文件）。

## 1. 架构变更 A/B（单任务）

| 指标 | deep 关 | deep 开（强制编排） |
|---|---|---|
| 通过率 | 1/1 | 1/1 |
| 工作流 | direct | `architecture_change` |
| worker / 任务数 | 0 / 0 | 1 / 5 |
| 总耗时 | 30.3s | **42.1s（+39%）** |
| 主 agent token | in 174,858 / out 1,784 | in 134,012 / out 1,421 |
| worker token | — | in 33,857 / out 1,090 |
| 总 token（主+worker） | 176,642 | 169,394（约 -4%） |
| 合并 | — | 1/1 成功，冲突 0 |

**结论（仅本样本）**：对该小任务，deep 路径没有提高通过率，墙钟 +39%；
token 总量略低但结构变了（更多预算花在 worker/契约）。这是一次“正确性没退化、
成本/延迟有代价”的结果。需要更大任务和 `--repeat 2` 才能判断是否值得默认开启。

> 运行中修正：`--deep` 最初只开 deep 标志，但写编排默认关闭且任务只有 6 个文件，
> 两次实际都走了 direct。现 `--deep` 会同时设 `orchestrator_write_enabled=true`、
> `orchestrator_min_files=1`、`smart_entry=false`，才真正测到新链路。

## 2. 长文档（真实长文，非 PDF）

语料：`docs/ARCHITECTURE.md`（77,496 字节）。问题：“总结分层、核心不变量和主要风险”。

| 指标 | 值 |
|---|---|
| 状态 | success |
| coverage | complete（非分页文档，页数为 0） |
| citations | 50，全部带 path/anchor/quote（100%） |
| map-reduce token | in 22,408 / out 18,960 |
| findings | 20 |
| 确定性 judge | **score 40 / supported=false** |
| 原因 | 结论有证据来源，但答案文本没有内联 `path#anchor` 引用 |

**结论**：覆盖与证据提取工作正常；但 document worker 的 summary/findings
不带内联引用，judge 正确判为“未引用”。这不是 judge 误杀，而是暴露了
“证据在 sources 里、但答案没有回指”的真实问题。

### 运行中修复
1. 非 PDF 读取仍被 `document_read.max_chars=12k` 截断，deep 模式提高到 200k；
   解析器 `truncated=True` 会写进 coverage（`unread=["tail"]`）。
2. map-reduce 的结构化调用补 `include_raw`，token 用量现在能回报。
3. 确定性 judge：有证据但答案无内联引用时，分数封顶 40，不再误判为有据。

## 3. 未完成项（需要语料/人工）

- **真实长 PDF**：仓库内没有 PDF 语料，本轮只跑了 markdown；PDF 页覆盖
  （15 页上限/全文覆盖）仍需 2–3 份真实 PDF 验证。
- **统计显著性**：所有数字是单次运行；需 `--repeat 2` 以上并固定 commit。
- **真人标注**：提供 150 条 `pending-human` 审核队列
  （`tests/eval/judge_human_pack.py`），但 AI 不能替代真人填写标签；
  校准仍以 `require_human=True` 拒绝未审核数据。

## 4. 复现命令

```bash
# 种子/参考解自检（不调用模型）
python tests/eval/runner.py --suite arch --verify-seeds

# A/B（单任务；成本可控）
python tests/eval/runner.py --suite arch --only arch/interface_rename \
  --baseline /tmp/arch_off.json
python tests/eval/runner.py --suite arch --only arch/interface_rename \
  --deep --baseline /tmp/arch_deep.json

# 长文档（真实文件需在工作区内）
python scripts/eval_documents.py --file eval-docs/ARCHITECTURE.md \
  --question "总结分层、核心不变量和主要风险" --judge
```

## 5. 三篇英文论文（2026-10-31，单次运行）

语料为三份英文科技论文 PDF，问题与输出要求均为中文。运行参数：
`AGENT_DOCUMENT_MAX_BYTES=40000000`（DPS 原文件 25.9MB，超过默认 20MB），
`deep_workflows_enabled` / `document_full_coverage` / `document_map_reduce`
由 `scripts/eval_documents.py` 打开。每篇仅跑一次，**不是统计结论**。

| 论文 | 页数 | 覆盖 | citations（path/anchor/quote） | 主模型 token（in/out） | 耗时 | judge |
|---|---:|---|---:|---:|---:|---|
| U-Net（1.6MB） | 8 | 8/8 | 29 / 29 / 29 | 7,960 / 5,184 | 13.5s | 100，supported |
| VMamba（3.0MB） | 25 | 25/25 | 131 / 131 / 131 | 41,771 / 24,176 | 48.1s | 100，supported |
| DPS（25.9MB） | 30 | 30/30 | 104 / 104 / 104 | 32,035 / 18,054 | 46.1s | 100，supported |

观察：
- 三篇均完整覆盖，中文摘要能正确复述方法/数字（U-Net 的 23 层、VMamba 的
  SS2D 四向扫描与 82.6% top-1、DPS 的后验分解与 FID 对比），并保留
  `path#anchor` 内联引用；
- 耗时随页数增长但非严格线性（VMamba 25 页 48s，DPS 30 页 46s）；
  DPS token 更低，主要因为其正文/附录页文本密度不同；
- 单次运行 + 确定性 judge 只能证明“引用可回指、覆盖完整”，
  不能证明语义蕴含；真实性仍需人工抽检。

### 本轮暴露并修复的问题

1. **PDF 分批读取仍被 12k 截断**：页范围读取的 `max_chars` 写死 12,000，
   5 页英文论文常超限，coverage 被标 `partial`。已改为与 deep 模式一致的
   200k 上限。
2. **map-reduce 摘要直接拼接导致重复**：分批 summary 用「；」连接，同一
   论文出现多段重复。新增一次有界的 `SummaryOnly` 合并调用，并支持从
   截断 JSON 中回捞 `summary`。
3. **模型自造证据路径**：部分分批把 `evidence_path` 写成“文件”“文档”。
   现在先按 quote 回查真实文档，再仅对占位名做单文档兜底；真实路径
   （可能是同批另一份文档）不改写，避免张冠李戴。
4. **证据上限 50 截断后页引用**：答案引用第 9/10/19/22 页时，这些锚点
   在 `sources[:50]` 之外，judge 误判。上限已提高到 200。
5. **judge 计分与解析偏严**：`anchor_ratio` 原来按“唯一锚点/总条数”
   计算，同页多结论会被重复扣分；已改为按条目是否带锚点计算。
   引用正则原先把 `page=8 Table 2` 截成 `page=8 Table`，已允许锚点含空格。
6. **评测脚本不输出答案**：`--json` 现在额外返回 `answer`、`error`、
   `duration_ms`，便于人工核对译文与定位失败原因。

复现：

```bash
AGENT_DOCUMENT_MAX_BYTES=40000000 \
python scripts/eval_documents.py --file eval-docs/VMamba.pdf \
  --question "请用中文总结方法、机制、实验与局限" --judge --json
```

## 6. 三篇联合阅读与上下文管理（2026-10-31，单次运行）

语料改为仓库根目录 `pdf/`；`tests/eval/papers.py` **自动扫描全部 PDF**，
文件数量不写死，新增/删除后用例自动扩展/收缩。已知论文（U-Net / VMamba /
DPS）附加概念与数值要点；未知 PDF 仍参与覆盖、逐篇引用与长度预算检查。
运行参数同上（deep + 全文覆盖 + map-reduce，`max_bytes=40MB`）。

| 用例 | 覆盖 | citations（逐篇） | token（in/out） | 耗时 | 判定 |
|---|---|---|---:|---:|---|
| `papers/cross_method` | 3/3 | 256（15/67/71） | 80,737 / 41,449 | 113.8s | 通过，judge 76 |
| `papers/cross_numbers` | 3/3 | 173（56/13/50） | 75,543 / 28,510 | 88.2s | 通过，judge 78 |
| `papers/cross_limits` | 3/3 | 203（56/10/49） | 77,572 / 38,589 | 109.1s | 通过，1 条软警告 |

观察：
- 三篇都被完整覆盖，答案都引用三篇；跨文档比较（先验/骨干/求解器、
  边界/组合风险）能给出可核验结论；
- `cross_limits` 出现 1 条 `pdf/UNet.pdf#page=7`：该页已读，但没有被抽成
  证据点。判定把它记为**软警告**（不失败），因为覆盖报告证明页在上下文中；
  若页码不在已读范围，仍是硬失败；
- 三篇联合的 token 约为单篇之和，耗时 88–114s；证据上限随文档数自适应
  （`120 × 文档数`，上限 600），避免后页引用被截断。

### 联合评测暴露并修复的问题

1. **多文档占位路径**：模型把 DPS/VMamba 的证据写成“文档内容#page=7”
   “文档#page=24”。现在先看引用前 600 字内最近提到的论文名（来自文件名），
   再尝试“该锚点只属于一份文档”的唯一匹配；单字母文件名不作为线索。
2. **粗粒度锚点**：证据抽到 `page=8 Table 2`，答案写 `page=8` 或
   `Table 2`，judge 现在视为同一段原文；编造的新页码仍然失败。
3. **证据上限固定**：多文档时 `sources[:200]` 会截掉第三篇的后页证据，
   已改为按文档数放大；合并摘要的输入上限也随文档数放宽。
4. **数量可变**：`papers.py` 用 `discover_papers()` + `build_cases()` 生成
   用例；`tests/unit/test_papers_eval.py` 用 2 篇/5 篇合成样本校验，
   `cross_numbers` 只强制“被问到的已知论文”被引用，未知 PDF 只要求覆盖。
5. **评测入口**：新增 `tests/integration/test_paper_documents.py`
   （`-m llm`，真实 WSL + 模型）；离线判定与样本结构测试进常规 CI。

复现：

```bash
python tests/eval/papers.py --list
python tests/eval/papers.py --case papers/cross_method --json
python tests/eval/papers.py --all --json
pytest -m llm tests/integration/test_paper_documents.py -q
```

## 7. P0：报告评测 + 章节感知 + 引用跨度（2026-10-31）

三项能力已落地：

1. **章节感知切分**：`_split_sections` 按标题（编号标题、Abstract/Introduction/
   Method/…、全大写短行）切分 map 单元；超长章节再按段落切并保留标题；
   页码前缀与 Figure/Table 标题不会误判为章节。
2. **引用跨度校验**：每条证据的 quote 在归一化文档文本中定位，记录
   `quote_verified`、`quote_span{start,end,page}`；章节号/公式号/作者名等
   非解析器锚点在定位成功后回写成真实 `page=N`；完全对不上的证据不进入
   下游引用集。judge 与评测输出 `quote_verified_ratio`。
3. **论文报告评测**：新增 `papers/report`，真实执行
   `document → author → report_check`，断言覆盖 3/3、每篇被引用、
   摘要/方法/实验/局限/创新五个章节、报告长度、引用跨度与确定性 judge。

最终实跑（单次）：

| 指标 | 值 |
|---|---|
| 覆盖 | 3/3，DPS 30 页 / U-Net 8 页 / VMamba 25 页 |
| 证据 | 359 条（DPS/UNet/VMamba = 185/50/124），原文命中 100% |
| 报告 | 5,695 字，五个章节齐全；report_check 校验通过 |
| judge | 100，supported |
| token | 140,988 in / 117,311 out |
| 耗时 | 294.6s（document + author + deterministic review） |

### 这轮暴露并修复的问题

1. **报告复核误读上游 PDF**：review runner 会把依赖任务的 PDF 当成待复核报告，
   对 27MB 的 DPS 直接报“文件过大/缺少章节”。现在只复核显式输入的报告。
2. **长文档独占证据/上下文**：DPS 单篇可产出数百条证据，固定上限会挤掉
   VMamba。现在证据按文档轮转取样，author 每篇至少保留 6 条证据。
3. **工具元信息头污染 map**：`document_read` 的路径/字节/页数/任务意图
   被模型当成论文内容。现在在 map 前剥离元信息头。
4. **合并摘要按“同一文档”写**：多文档时模型会误判“只有一篇有正文”。
   提示词改为多文档、按 `## 路径` 保留每篇结论；摘要输入按文档轮转并放宽预算。
5. **author 自造锚点**：模型会写章节号、作者名、`page=13-15` 等非法锚点。
   现在 author 输出做引用清洗（只保留能对应证据的锚点），附录只使用
   page/outline/table 等解析器锚点；`page=8 Table 2` 这类描述会回写成实际页码。
6. **必含章节未落实**：author 提示词显式列出 `required_sections`，
   report_check 再独立校验；缺失“创新”会直接失败。

边界：仍是单次运行；`quote_verified` 是“归一化空白后的精确子串”，
改写/转述的 quote 会被判为未命中并从引用集中剔除；这提高了可核验性，
但会牺牲一部分召回。真正的语义蕴含仍需人工或 LLM judge。

复现：

```bash
python tests/eval/papers.py --case papers/report --json
pytest -m llm tests/integration/test_paper_documents.py -q -k report
```

## 8. 扩到 10 篇：稳定性与扩展性（2026-10-31）

`pdf/` 增至 10 篇、230 页、112MB（最大单文件 27MB）。自动发现无需改代码；
两次完整运行均通过：

| 用例 | 覆盖 | 证据（逐篇） | token（in/out） | 耗时 | 结果 |
|---|---|---|---|---:|---|
| `cross_method` | 10/10 | 600（38–64/篇），原文命中 100% | 539,234 / 460,621 | 1,074.9s | 通过，judge 100 |
| `report` | 10/10 | 600（44–62/篇），原文命中 100% | 545,008 / 526,949 | 1,311.8s | 通过；报告 10,023 字，五章节齐全，report_check 通过，judge 100 |

### 10 篇暴露并修复的扩展性问题

1. **硬编码 6 个输入上限**：`_MAX_FILES = 6` 会把第 7–10 篇直接丢掉。
   已提高到 64，并补“10 个输入不被截断”的单测。
2. **合并阶段 2000 点上限**：前 6 篇即可产生超过 2000 个点，后 4 篇证据为 0。
   现在上限随文档数放大（`800 × 文档数`，上限 20,000），最终 sources 仍按
   文档轮转收窄到 600。
3. **只补一次引用**：摘要只要已有任意引用就不再追加，导致其余文档漏引。
   现在补齐“缺失文档”的引用（每个文档至少一条）。
4. **路径含空格的引用无法清洗**：`WaveFace 摘要#Abstract` 漏过正则。
   引用正则已允许路径含空格。
5. **编造的 outline 锚点**：模型会写不存在的 `outline=12`。现在从文档文本
   提取真实锚点白名单（page/outline/table/…），`anchor_valid=False` 的证据
   不进入 author/报告。

### 质量边界（10 篇）

- 机械指标稳定：覆盖 10/10、600 条证据全部可定位、每篇都被引用、
  report_check 与 judge 通过。
- **语义归属仍会退化**：`cross_method` 的摘要出现过“把 DiffFNO 的结论
  引到 paper-6”的错配，并倾向用“碎片化片段”描述完整覆盖的材料；
  10 篇时模型对长上下文的全局归属能力明显弱于 3 篇。
- 成本/延迟：单次 10 篇约 54 万输入 + 46–53 万输出 token，18–22 分钟；
  不建议在交互路径默认全量跑 10 篇，应使用缓存、分批或按需子集。

复现：

```bash
python tests/eval/papers.py --case papers/cross_method --json
python tests/eval/papers.py --case papers/report --json
```

## 9. 结构化证据矩阵 + 分层归并 + 归属校验（2026-10-31）

决策与代价见 [ADR-0038](adr/0038-structured-document-matrix-and-attribution.md)。
实现三项：

1. **每篇固定槽位矩阵**：`DocumentMatrix` 输出 `problem/method/algorithm/
   experiments/limitations/innovations/conclusion`，evidence 只允许复制
   已验证证据。空槽位用已验证 claim 按关键词回填，避免长文被压成一句。
2. **分层/树状归并**：不再把所有摘要塞进一次调用。每篇矩阵作为叶节点，
   按 `document_tree_fanout`（默认 5）逐层分组归并，单次输入受
   `document_tree_input_chars`（默认 16k）限制。最终答案附紧凑“证据矩阵”，
   保证树状归并后每篇槽位仍可见。
3. **跨文档归属校验**：`knowledge/attribution.py` 从路径 stem 与矩阵标题
   生成别名；按句子检查“提到的文档”与“引用路径”是否匹配。错配写入
   `attribution_issues`，评测默认要求 0 条。

真实结果：

| 用例 | 矩阵 | 槽位覆盖 | 归属错配 | 结果 |
|---|---|---:|---:|---|
| 3 篇 `cross_method` | 3 | 86% | 0 | 通过，judge 100，139k/110k，262s |
| 10 篇 `cross_method` | 10 | 87% | 0 | 通过，600 证据、10/10 引用，582k/475k，1,129s |
| 10 篇 `report` | 10 | 94% | 0 | 通过，报告 14,002 字、五章节、report_check 与 judge 100，597k/549k，1,452s |

### 本轮暴露并修复的问题

1. **矩阵槽位截断切断引用**：槽位内联锚点在 220/300 字处被切断，产生
   `pdf/pap；算法：...` 这类畸形引用。现在先剥离引用标记再截断，矩阵末尾
   单独列真实证据。
2. **空槽位**：部分文档的 method 为空，导致概念检查只命中 1/2。现在用
   已验证 claim 按关键词回填 problem/method/experiments/limitations 等槽位。
3. **树状归并压掉细节**：最终散文仍可能把某篇压成一句。现在把紧凑矩阵
   作为固定附录并入答案，并加“已解析文档（N 篇）”头，防止把已覆盖文档
   说成缺失。
4. **归属校验**：按句子检查提及/引用路径；10 篇两次运行均为 0 条错配。
   但它只能发现“提到 A 却引用 B”，无法发现“引用 A 但结论其实属于 B”
   的语义错误，后者仍需 LLM judge 或人工。

配置：`AGENT_DOCUMENT_MATRIX_ENABLED=true`、
`AGENT_DOCUMENT_TREE_FANOUT=5`、`AGENT_DOCUMENT_TREE_INPUT_CHARS=16000`
（已重新生成 `docs/contracts/config.md`）。

质量边界：矩阵与归属校验把机械一致性拉住了，但 10 篇报告仍出现
“只有 7 篇有正文”的错误概述（实际每篇有 50–62 条证据）；树状归并的
语义忠实度、以及“结论属于哪篇”的深层校验仍是下一步。

复现：

```bash
python tests/eval/papers.py --pdf-dir pdf --case papers/cross_method --json
python tests/eval/papers.py --pdf-dir pdf --case papers/report --json
```
