---
layer: L3
doc_id: polyglot-plan
title: 多语言评测方案（待启用）
owner: 项目作者
status: proposed
last_reviewed: 2026-10-25
review_cadence: 安装语言运行时或新增 polyglot 任务时
---

# 多语言评测方案（待启用）

## 结论

可以增加 Node.js / Java 任务，但**不能直接使用 Windows 主机上的运行时**。
评测沙箱是专用 WSL 发行版 `CodingAgent`，必须先把运行时装进该发行版。
本方案暂不执行，等明确批准环境变更后再落地。

## 环境现状

| 环境 | 结果 |
|---|---|
| WSL `CodingAgent` | Ubuntu 24.04；无 node / java / dotnet / go / cargo |
| apt 候选 | `nodejs 18.19.1`、`openjdk-17-jdk 17.0.20` |
| WSL 网络 | 可访问 archive.ubuntu.com / deb.nodesource.com |
| WSL 磁盘 | 955G 可用 |
| WSL Docker | 不可用（Docker Desktop 未集成到该发行版） |
| Windows 主机 | Node 24.19、JDK 17.0.18、.NET SDK 9.0.101、Docker 29.7 |

Windows 二进制不能在 WSL 内直接运行；`appendWindowsPath=false`，工作区在
WSL ext4 内。通过 `\\wsl$` 调 Windows 编译器会绕开加固环境，不建议。

## 推荐安装（需单独批准）

```bash
wsl -d CodingAgent -u root -- apt-get update
wsl -d CodingAgent -u root -- apt-get install -y nodejs openjdk-17-jdk-headless
wsl -d CodingAgent -- node -v
wsl -d CodingAgent -- javac -version
```

说明：

- Node 18 是 Ubuntu 24.04 的仓库版本；普通 `node test.js` 足够，避免依赖
  Node 20+ 语法。
- JDK 17 与主机版本一致；不引入 Maven/JUnit，测试用普通 `javac` + `java`
  和 `assert`/退出码。
- 安装后需更新 `docs/PHASE_G.md` 的环境清单，并在
  `tests/eval/harness.py::sandbox_capabilities` 增加 `node` / `javac` / `java`
  探测。
- .NET SDK 需要 Microsoft 源、构建更重；等 Node/Java 跑通后再评估。

## 任务设计

拟新增 `tasks_polyglot.py`（放在 `tests/eval/` 下），套件名 `polyglot`，
先做 4–6 个任务：

| 语言 | 任务示例 | 判定命令 |
|---|---|---|
| JS | 异步重试队列、并发限流、CSV/JSON 转换 | `node test.js` |
| Java | LRU、分组统计、字符串/集合边界 | `javac -d . src/*.java test/TestRunner.java && java TestRunner` |

每个任务与现有套件一致：

- `sources`：种子源码；
- `tests`：可见测试（种子失败）；
- `hidden_tests`：隐藏测试（spec 任务可见测试全绿）；
- `reference`：参考解；
- `judge`：按任务指定的编译/运行命令。

不引入 npm/Maven/JUnit；测试只用标准库 `assert` 与退出码，保证沙箱零依赖。

## 接入点

- `tests/eval/runner.py`：新增 `polyglot` 套件与基线 `baseline_polyglot.json`；
- `tests/eval/langsmith_suite.py`：`POLYGLOT_IDS`、`--kinds polyglot`、
  数据集 `coding-agent-polyglot`；
- `tests/eval/harness.py::sandbox_capabilities`：增加 `node` / `javac` / `java`；
- 结构守卫：拟新增 `test_eval_polyglot.py`（放在 `tests/unit/` 下），
  校验种子必失败、参考解必通过、judge 命令可执行、无第三方依赖。

## 风险与限制

- **环境变更**：安装包会改变专用发行版，需更新环境文档与验收记录；
- **Node 版本差异**：WSL 的 Node 18 与主机 Node 24 不同；任务避免新语法；
- **自动验证**：`detect_test_command` 目前只识别 Python；JS/Java 任务需要
  提示词显式给出测试命令，`verifications` 可能显示 `not_configured`；
- **命令等级**：`node` / `javac` / `java` 已确认是 L2，评测 harness 的
  `approval_mode="approve"` 会自动放行；
- **构建耗时**：Java 编译比 Python 慢，任务 `timeout` 需要适当放宽。

## 验收计划（启用后）

```bash
pytest -m "not llm" tests/unit/test_eval_polyglot.py -q -n 0
python tests/eval/runner.py --suite polyglot --verify-seeds
python tests/eval/runner.py --suite polyglot --repeat 2
```

先确认 4–6 个任务都能在 WSL 内通过种子/参考解自检，再跑真实模型。
