---
layer: L3
doc_id: testing
title: 测试分层与运行口径
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 测试分层、标记或 CI 口径变动时
---

# 测试分层与运行口径

测试不是只有“全绿”一个维度。不同阶段要回答不同问题，运行成本和外部依赖也不同。
本页定义六类测试的口径、入口与放置规则；具体命令以 `AGENTS.md` 第 1 节和
`Makefile` 为准。

## 1. 六类测试

| 类型 | 回答的问题 | 入口 | 外部依赖 | 典型耗时 |
|---|---|---|---|---|
| 冒烟 | 主链路还能不能启动 | `pytest -m smoke` | 无 WSL / 无 Key | 秒级 |
| 单元 | 单个函数/分支是否符合契约 | `pytest tests/unit -m "not wsl and not llm"` | 无 | 十秒级 |
| 增量 | 本次改动可能弄坏哪些既有行为 | `python scripts/select_tests.py --exec` | 无 | 按命中范围 |
| 集成 | 模块拼起来是否还能工作 | `pytest tests/integration -m "not wsl and not llm"` | 部分需要 WSL | 分钟级 |
| 回归 | 承重不变量和历史事故是否被重新破坏 | `pytest -m regression` | 无 | 十秒级 |
| 智能体 E2E | 智能体本身能否真的把任务做完（direct + 编排） | `pytest -m agent_e2e` | 真实 WSL + API Key | 分钟级 |
| 全量 | 所有非 LLM 用例 | `pytest -m "not llm"` | 真实 WSL | 数分钟 |

`pytest` 永远带 `-m`：裸跑会执行 `llm` 标记用例，真的调用模型并计费。

## 2. 冒烟测试

`tests/smoke/` 只放**不依赖 WSL、数据库服务或 API Key** 的主链路检查：

- 配置能构造、A2 Web 能创建并响应健康检查；
- A3 拒绝无 token 启动、健康检查与鉴权边界正确；
- 会话/审计等核心写入路径可用，敏感信息不落盘；
- 关键模块可导入，防止发布包里缺文件。

冒烟失败说明“系统起不来”，应先于任何单点测试排查。它**不替代**集成测试：
冒烟只证明入口通，不证明沙箱、并发、迁移等深层行为正确。

## 3. 增量测试选择

`scripts/select_tests.py` 用 `git diff` 找出改动源文件，沿 AST 导入图计算
反向依赖（谁 import 了它），再选出直接或间接引用这些模块的测试。

```bash
# 工作区未提交改动
python scripts/select_tests.py --worktree

# 相对某个基线（CI/合并前常用）
python scripts/select_tests.py --base origin/main --print

# 直接执行选中的测试
python scripts/select_tests.py --base HEAD --exec
```

选择器保守回退：改动涉及 `pyproject.toml`、测试基础设施、文档或依赖图无法解析时，
直接回退到快反馈全量（`-m "not wsl and not llm"`）。宁可比必要范围大，也不漏测。
增量结果**不能替代**提交前的 `make check` 与 CI 全量。

## 4. 回归测试

`regression` 标记用于承重不变量和真实出过的事故，例如：

- 未登记工具的审批等级必须 fail-closed（按 L3 处理）；
- 审批记录用后即清，不能跨轮复用；
- `review_watermark` 只能由 planner 归零；
- `estimate_recursion_limit` 必须覆盖图节点数；
- 前端不得 import `tools/` / `sandbox/`；
- 补丁路径不得越界或写入 `.agent/`；
- 审计链与脱敏边界。

这些测试通常也在全量里运行；`-m regression` 只是提供一个更快、可在改动后反复执行
的稳定子集。**不要为了让它通过而放宽断言**——它记录的是已经付过代价的行为。

## 5. 智能体级 E2E

前面几类测的是**机制**：函数、路由、沙箱行为是否按契约工作。它们可以全绿，
而智能体仍然“只读不写”或“根本没用编排”。`agent_e2e` 回答的是另一个问题：
**把整个 A3+A4 智能体接到真实 WSL 上，它能不能把任务做完。**

```bash
# 专用发行版 + .env 中已配置 DEEPSEEK_API_KEY
AGENT_WSL_DISTRO=CodingAgent pytest -m agent_e2e \
  tests/integration/test_agent_e2e.py -q
# 或
make agent-e2e
```

`tests/integration/test_agent_e2e.py` 复用 `tests/eval/harness.py` 的任务、
种子、判定与机制计数，覆盖两条执行路径：

| 用例 | 断言的关键机制 |
|---|---|
| `test_direct_agent_completes_task` | 工具调用、写源码、自动验证、判定通过、token 回报 |
| `test_orchestrated_implement_parallel_workers` | `orchestrated_implement` 工作流、任务图、≥2 worker、implement 角色、worktree 内工具调用、补丁合并成功 |

该标记**同时带 `llm`**：默认 `pytest -m "not llm"` 与 CI 都会跳过，避免误烧额度。

### 系统级 E2E（本地）

审批暂停/恢复、快照回滚、工作区导入/更新/回滚/归档恢复/上传/HMAC 由
`tests/integration/test_system_e2e.py` 固定下来。它把
`scripts/phase_g_e2e.py` 的 JSON 输出当作结构化断言对象：

```bash
AGENT_SYSTEM_E2E=1 AGENT_WSL_DISTRO=CodingAgent make system-e2e
```

默认不执行：需要专用 WSL 发行版、已安装进该发行版的当前代码、以及
`.env` 中的 `DEEPSEEK_API_KEY`。标记同时带 `llm`，PR CI 会跳过。
它**不进 CI**，因为托管 runner 没有 WSL2 与专用发行版；需要时在本地
或自托管 Windows runner 上手动执行。

更大规模的全量口径在 `tests/eval/langsmith_suite.py`：158 条（139 capability +
2 agent + 17 mechanism；历史重复的 `agent/parallel_readonly` 已并入 capability
的 workflow 断言，并新增子智能体委派用例），最近一次实跑 157/157，见
[LANGSMITH_REPORT_V2.md](LANGSMITH_REPORT_V2.md)。

现有 default/full 已饱和时，用 `tests/eval/tasks_hard.py` 的 **hard 套件**
（14 个并发/隐藏需求/多层/排障/性能任务）恢复区分度：

```bash
python tests/eval/runner.py --suite hard --verify-seeds
python tests/eval/runner.py --suite hard --repeat 2
```

`--repeat 2` 会报告 `spread` 与 `flipped`；单次通过率不能作为能力变化的证据。

`real` 套件（5 个完整标准库模块回归）用于测试“在几万行真实代码里定位回归”：

```bash
python tests/eval/runner.py --suite real --verify-seeds
python tests/eval/runner.py --suite real --repeat 2
```

## 6. 放置规则

| 测试内容 | 目录 |
|---|---|
| 纯函数、分支、错误路径、伪造依赖 | `tests/unit/` |
| 需要真实 WSL / 文件系统 / 多模块装配 | `tests/integration/` |
| 无外设的主链路可用性 | `tests/smoke/` |
| 前端 TUI（textual） | `tests/tui/` |
| 评测任务与基准脚本 | `tests/eval/` |
| 契约测试夹具 | `tests/contract/` |

真实 WSL 用例必须标 `@pytest.mark.wsl`，探测宿主环境标 `@pytest.mark.wsl_env`，
真实执行后端契约标 `@pytest.mark.execution`。`tests/conftest.py` 的自动守卫会在
任何未标记用例里拦截真实沙箱调用。

智能体级 E2E 是例外：它标 `agent_e2e`（同时带 `llm`），**不挂 `wsl`**。
这样 `pytest -m wsl` 这条“只跑沙箱层”的常用命令不会顺带调用真实模型；
conftest 单独放行 `agent_e2e` 的真实沙箱调用。

## 7. 覆盖率

CI 与本地使用同一口径：

```bash
pytest -m "not wsl and not llm" --cov --cov-report=term-missing
```

门槛与模块缺口见 [覆盖率基线](COVERAGE.md)。覆盖率是**可见性工具**，不是唯一目标：
沙箱层在 CI 的 Linux runner 上本来就不执行，低覆盖要通过 `-m wsl` 在本地补，
不要用 omit 抹平。
