# 安全策略

## 支持范围

| 项 | 说明 |
|---|---|
| 受支持版本 | `main` 分支的最新提交（本项目尚未发正式版，见 `CHANGELOG.md`） |
| 主要运行环境 | Windows 宿主 + WSL2 沙箱；执行后端可切到 Linux 原生 |
| 威胁模型 | [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) —— 先看它，再说某个行为是不是漏洞 |

## 报告漏洞

**请不要开公开 issue。** 走 GitHub 的 *Report a vulnerability*
（仓库的 Security 页，`SECURITY.md` 就是为它准备的）。如果仓库是私有部署、
没有该入口，就用维护者约定的私下渠道，不要贴在 issue 里。

请附上：

1. 能复现的最小步骤（命令、配置、工作区布局）；
2. 影响面：是**越权**、**审批被绕过**、**密钥外泄**、还是**沙箱逃逸**；
3. 你期望的行为与实际行为的差异。

我们会在 7 天内确认收到，修复或给出结论后同步。

## 什么算漏洞

优先级最高的几类（与 `docs/THREAT_MODEL.md` 第 4 节防线分级一致）：

- **沙箱逃逸**：任何让命令逃出工作区、或逃出 WSL 发行版的路径；
- **审批绕过**：`approve` 模式或 `AGENT_ALLOW_RULES` 让 L3 命令未经确认执行；
- **越权**：跨用户读到别人的工作区、审计、checkpoint 或记忆；
- **密钥外泄**：`.env`、私钥、provider API Key 出现在日志、审计、追踪或对外请求中；
- **审计链断裂**：写入失败被静默吞掉，导致日志有 `run_end` 而没有 `run_start`。

## 什么不算漏洞（先读这些再报）

这些都是**已知且刻意**的设计，报告前请先读对应文档：

| 现象 | 为什么不是漏洞 |
|---|---|
| 模型可以读工作区外的文件 | 不行：路径守卫之外还需要审批；见 `docs/THREAT_MODEL.md` |
| 审计、快照、checkpoint 在 `.agent/` 里且不被 git 跟踪 | 刻意为之：含审计与记忆，`git_add` 会拒绝暂存 |
| 单用户模式下改 provider 配置不需要管理员 | 单用户模式没有"别的用户"；跨机部署由 A2 的 token 与 HMAC 保护 |
| 未登记的新工具按 L3 处理 | 这是 **fail-closed**，不是缺失 |
| 判定命令固定用 stdlib `unittest` | 历史上 pytest 缺失导致"看起来干净、实际没测"，见 `AGENTS.md` 第 3 节 |

## 自动化门禁

| 门禁 | 位置 |
|---|---|
| `ruff` 含 `S`（bandit）规则 + 存量预算棘轮 | `.github/workflows/ci.yml` + `scripts/check_lint_budget.py` |
| 本次改动的密钥扫描（gitleaks） | `.github/workflows/ci.yml` |
| 依赖漏洞审计 / 全量密钥扫描 | `.github/workflows/security.yml`（定时） |
| 沙箱层真实执行 + 分层覆盖率 | `.github/workflows/sandbox.yml`（WSL2 self-hosted） |
