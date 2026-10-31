---
layer: L0
doc_id: ci
title: 流水线（pipeline as code）
owner: 项目作者
status: active
last_reviewed: 2026-10-28
review_cadence: 工作流文件变动时
---

# 流水线

事实源是 `.github/workflows/` 里的 YAML——**改流水线就是改那些文件**，
本文只说明每条流水线跑什么、以及为什么这么切。

## CI（`.github/workflows/ci.yml`）

触发：push / pull_request。Linux runner（**没有 WSL2**）。
前后端拆成两个并行 job：`frontend` 产出发布包，`backend` 不依赖前端构建。

`frontend` job：

| 步骤 | 命令 |
|---|---|
| 前端构建/类型 | `python scripts/build_frontend.py` + `tsc --noEmit` + `git diff --exit-code` 用户端与管理端产物 |
| 前端单测 / E2E | `npm test` + `npx playwright test`（含 `/admin` smoke 与 axe WCAG A/AA 扫描） |
| 生成独立产物 | `python scripts/build_frontend_release.py --skip-build` |
| 校验产物 | `python scripts/check_frontend_release.py --dir dist/frontend/<version>` |
| 上传 artifact | `frontend-release`（含 manifest.json / SHA256SUMS） |

`backend` job：

| 步骤 | 命令 |
|---|---|
| 安装 | `pip install -e ".[dev,ui,web,db]"` |
| lint | `ruff check src tests` |
| 类型基线 | `mypy`（`pyproject.toml` 的 `[tool.mypy]`：security/access/terminal_text strict） |
| 性能冒烟 | `python scripts/perf_smoke.py`（策略/终端/限流热路径，宽松阈值） |
| SSE 并发冒烟 | `python scripts/sse_load_smoke.py`（20 路 × 200 token，假 runtime） |
| 测试 + 覆盖率 | `pytest -m "not wsl and not wsl_env and not llm" --cov --cov-report=term-missing --cov-report=json:coverage.json` |
| 承重模块覆盖率棘轮 | `python scripts/check_module_coverage.py --json coverage.json` |
| 指标快照漂移 | `python scripts/snapshot_metrics.py --coverage coverage.json --collect --check` |
| 执行契约 | `pytest -m execution -q` |
| MySQL DDL 离线校验 | `pytest tests/unit/test_mysql_ddl.py` |
| Phase D 故障演练 | `python scripts/phase_d_smoke.py --timeout 60` |
| Phase F 迁移/回滚 | `python scripts/phase_f_drill.py` |
| 显式化沙箱缺口 | `::warning::` 提示 wsl 用例未执行 |

**承重模块覆盖率**由 `scripts/coverage_budget.json` 定义，每个模块独立下限；
数字只许上升（`--update` 只抬高，故意下调需要 `--force`）。`docs/METRICS.md`
与 `docs/metrics.json` 由 `scripts/snapshot_metrics.py` 从 `coverage.json` +
`pytest --collect-only` 生成；CI 用 `--check` 校验测试数精确一致、覆盖率在
平台容差内（总 2pp / 模块 3pp），过期就红，不再手抄数字。低于 85% 目标的
模块会打印告警；夜间/发版可加 `--strict-target`。

`backend-py311` job：`ubuntu-latest` + Python 3.11（`requires-python` 的下限），
跑同一套快反馈。主 `backend` 用 3.12 产出覆盖率；这条 job 防止最低版本腐烂。

`windows` job：`windows-latest` + Python 3.12，跑
`pytest -m "not wsl and not wsl_env and not llm"`。它覆盖 Linux 上会被 skip 的 Windows
路径（路径守卫、A2L host broker、desktop 启动链），不替代真实 WSL。

`installer` job：`windows-latest`，先跑
`python scripts/check_installer.py --check`（版本单一源、免提权安装、卸载保留
`%LOCALAPPDATA%`、`.iss` 引用路径与图标存在），再用 stub 的
`dist/UBISmart` 与 WebView2 bootstrapper 跑一次 `ISCC` 编译。
**它不产出可发布包**：真包需要 WSL rootfs（`build_wsl_distro.py` 要跑 apt）与
签名证书，只能在装有 WSL2 的 Windows 机器上构建，步骤见
[RELEASING.md](RELEASING.md) 的「安装包」一节。

`mysql` job：`mysql:8.0` service container + `AGENT_TEST_MYSQL_URL`，跑
`pytest -m mysql`。覆盖迁移、账号/scrypt、会话、AES-GCM 密钥、配额预留/
结算、用量累计与备份恢复；测试库名必须包含 `test`，用例会清空该库。

必须装齐 `ui` / `web` extras：TUI（textual）与 Web（fastapi）测试需要它们，
缺一个会导致 collection error 而非 skip。这两条都曾导致过 CI 失败。
`desktop` 测试用 mock 覆盖启动/取消逻辑，不要求 CI 安装 pywebview；
真实窗口与 OS 密钥环只做本地冒烟。

前端纯函数（`node test.mjs`）与 jsdom 交互（`node test-dom.mjs`）当前在本地/
发版前执行，CI 尚未接入；pywebview 探针同样只做本机冒烟。该缺口登记为 TD-018。
前端发版、兼容矩阵与回滚步骤见 [RELEASING.md](RELEASING.md)。

**覆盖率门槛 `fail_under=76`** 按 CI 实际执行的子集定，不是贴着实测值
（2026-10-29 CI 子集实测 **83.34%**，3,002 个用例；Linux/Windows 会有小幅漂移）。
子系统缺口见 [COVERAGE.md](COVERAGE.md)。原理见 `pyproject.toml` 的注释。

五类测试（冒烟/单元/增量/集成/回归）的口径与放置规则见
[TESTING.md](TESTING.md)。CI 的快反馈步骤已经包含 `smoke` 用例；
增量选择器只在本地缩短反馈，不能替代这里的全量。

### 已知缺口

真实 MySQL 契约已由 `mysql` job 覆盖；本地等价命令是
`AGENT_TEST_MYSQL_URL=... pytest -m mysql`。迁移方言仍由
`test_mysql_ddl.py` 离线编译兜底。运维见 [DATABASE.md](DATABASE.md)。

Linux `backend` job 没有 WSL2，所以 `wsl` 标记的用例仍然**一行都没跑**；
`windows` job 只覆盖不依赖 WSL 的 Windows 路径。真正的沙箱门禁见下一节，
**"CI 全绿"仍不等于"沙箱层可用"**。

## Sandbox（`.github/workflows/sandbox.yml`）

触发：push 到 `main` / **pull_request** / 每天 UTC 04:00 / 手动
`workflow_dispatch`。

**先探测，再决定跑不跑**：

| job | 跑在哪 | 做什么 |
|---|---|---|
| `preflight` | `ubuntu-latest` | 查仓库有没有在线且带 `wsl` label 的 self-hosted runner，输出 `ready` |
| `wsl` | `[self-hosted, wsl]` | 预检 WSL/发行版 → 沙箱层全量 → 分层覆盖率门槛 → 上传报告 |
| `gate` | `ubuntu-latest` | **汇总门禁**：`ready != true` 或 `wsl` 非 success 时失败 |

没有在线 runner 时 `wsl` job 不启动，但 `gate` 会**显式失败**并写明
“跳过不等于通过”。确实不需要沙箱层的改动，必须在仓库变量里设置
`WSL_GATE_ALLOW_SKIP=true` 才能放行；这只会在日志里留 warning，不会被
误当成“沙箱层已通过”。

排队 24 小时后被取消既不会让 PR 通过得更快，也不比“明确失败”更可信 ——
那只是把 PR 悄悄卡住。

runner 约定：注册时打**自定义 label `wsl`**（`--labels wsl`）。`llm.yml` 的
`llm-eval` job 用同一个 label，两处不要各写一套。注册步骤见
[ENVIRONMENT.md](ENVIRONMENT.md) 的「注册 WSL 门禁 runner」一节。

注册之后必须把 **`Sandbox / sandbox-gate`** 设为分支保护的 required check
（不要 required `wsl` 本身：它被跳过时语义不可靠）。这样“没有 runner”
和“沙箱层失败”都会拦住合并，而不是静默变绿。

| 步骤 | 命令 |
|---|---|
| 预检 WSL/发行版 | `python scripts/check_sandbox_gate.py --local` |
| 沙箱层全量 | `pytest -m "not llm" --cov --cov-report=json:coverage.json` |
| 沙箱层覆盖率门槛 | `python scripts/check_layer_coverage.py --json coverage.json` |
| 上传报告 | `sandbox-coverage`（`coverage.json`） |

注册 runner 后用下面的命令核验分支保护是否真的把沙箱层设为 required：

```bash
GITHUB_TOKEN=... python scripts/check_sandbox_gate.py \
  --repo <owner>/<name> --branch main
```

它只读 GitHub API；未配置时返回非零并提示需要 `Sandbox / wsl`。分支保护
本身必须在仓库设置里由管理员开启，代码无法替代。

为什么单独一条流水线：执行真实命令的那一层（路径守卫、写前快照、命令分级、
文件写工具）**只能在有 WSL2 的机器上跑**；把它塞进 `ci.yml` 会让 Linux job
要么跳过、要么红灯。而全局 `fail_under=76` 会被其它模块的高覆盖稀释 ——
所以 `check_layer_coverage.py` 对这几个模块**另设下限**，只在真的执行过它们
的机器上判定。

前置条件：必须先注册一个带 WSL2 与项目发行版的 self-hosted runner。
没注册时 `wsl` job 直接跳过（`ci.yml` 不受影响，仍是绿灯），并在该次运行的
摘要里留下一条 warning —— **跳过的运行不能当成沙箱层通过**。

本地等价：`pytest -m "not llm" --cov --cov-report=json:coverage.json` +
`python scripts/check_layer_coverage.py --json coverage.json`。

## Security（`.github/workflows/security.yml`）

| Job | 内容 |
|---|---|
| `python-deps` | 干净环境装依赖后 `pip-audit --strict`，报告上传为 artifact |
| `npm-deps` | 前端 `npm audit --audit-level=high` |
| `sbom` | `cyclonedx-py environment` 生成 Python 依赖 SBOM，保留 90 天 |
| `secrets-full` | gitleaks 全量历史扫描（`fetch-depth: 0`） |

**为什么不在 `ci.yml` 里**：依赖 CVE 是随时间变化的 —— 今天全绿的 PR，明天有
新 CVE 发布就会变红。把这种门禁挂在 PR 上会让"别人的 PR 红了"和"我的改动
有问题"混为一谈。确定性、可归因的门禁（`ruff` 含 `S`、lint 预算、本次改动的
密钥扫描）才留在 `ci.yml`。

## Docs（`.github/workflows/docs.yml`）

触发：push / pull_request / workflow_dispatch。

| 步骤 | 命令 |
|---|---|
| 契约漂移 | `python scripts/gen_contracts.py --check` |
| 契约兼容性（PR） | `python scripts/gen_contracts.py --check-compat --base <base>` |
| 文档一致性 | `python scripts/check_docs.py --strict` |
| 文档—代码漂移 | `python scripts/check_drift.py` |

一致性检查包含七项：契约漂移、断链（含 anchor）、每份文档必须有 owner、
过期标记（超 90 天）、文档里不许出现裸 `pytest`、验收标准引用的测试文件与
用例必须存在、常驻上下文（`AGENTS.md` / `llms.txt` / 模块 AGENTS）不得超长。

`check_drift.py` 补的是链接检查覆盖不到的一类漂移：代码注释/docstring 里引用的
`docs/...`、以及文档里反引号标注的 `src/` `tests/` `scripts/` 路径必须存在。
它只做存在性检查，不判断内容是否过时。

本地 `make docs` 会把文档一致性、漂移、环境快照与契约兼容一起跑一遍。

**契约兼容性**在 PR 上以目标分支为基线，阻断这些破坏性变更：删除事件类型 /
事件字段、删除配置项或改变类型、删除工具或降低工具等级（更宽松）、删除审计
记录类型 / 审批决定 / 字段。新增与等级升高只提醒。有意破坏时给 PR 加
`contracts-breaking` 标签；本地可用 `--allow-breaking` 仅报告。YAML 契约的
比较需要 PyYAML，CI 在这一步临时安装。

每次运行还会生成 `docs-metrics.json` 并作为 artifact 上传（文档数、断链、
无 owner、过期、契约漂移、验收锚点、模块 AGENTS 覆盖率、最大文档）。
它是趋势指标，不是门禁；门禁仍是上一步的 `--strict`。

**为什么单独一个 workflow**：文档检查失败不该和代码测试混在一起判断，
但也不该被跳过——它是防腐化的唯一自动闸门。

## 本地等价命令

```bash
make check                        # ruff + mypy + 版本一致性 + 快反馈
make smoke                        # 冒烟（秒级，无外部依赖）
make perf                         # 热路径性能冒烟
make load                         # SSE 并发冒烟
make regression                   # 承重不变量回归集
make cov                          # 覆盖率 + 承重模块棘轮
make metrics                      # 由 coverage.json 重生成 docs/METRICS.md
python scripts/check_module_coverage.py --json coverage.json
python scripts/snapshot_metrics.py --coverage coverage.json --collect --write
python scripts/select_tests.py --worktree   # 增量：只跑受影响的测试
pytest -m "not llm"               # 全量（含真实 WSL）
python scripts/gen_contracts.py   # 重生成契约
python scripts/check_docs.py      # 文档一致性（过期只警告）
python scripts/check_docs.py --strict   # 与 CI 同口径
```

## 规矩

- **文档与代码同 PR**。改了 `events.py` / `config.py` / `approve.py` /
  `audit/models.py` 必须重跑 `gen_contracts.py`，否则契约检查会红。
- 新增 CI 步骤时想清楚它能不能在无 WSL 的 runner 上跑；不能就标 `wsl`
  并接受它只在本地执行。
