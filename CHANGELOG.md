# 变更日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；
版本号遵循语义化版本。**更早的历史以 `git log` 为准** —— 本文件从
2026-10-28 的这批交付就绪（P0/P1）改动开始记录。

## [Unreleased]

### 安全（阻断项修复）

- **修复多租户工作区越权**：`TenantManager.resolve_workspace` 在多用户模式下
  只校验"落在全局工作区根内"，用户 A 传 B 的目录即可拿到 B 工作区的读写执行权。
  现在收敛到 `workspaces_root/<user>`。
- **修复 provider 探测的 SSRF 与 API Key 外带**：探测请求带真实 API Key 发往
  用户可配置的 `base_url`。新增 `security/netguard.py`：解析后按 IP 判定，默认
  拒绝回环/内网/CGNAT/保留段，链路本地（云元数据）任何开关下都拒绝，且不跟随重定向。
- **修复涉密/越界读取可被自动放行**：`cat .env`、`cat ~/.ssh/id_rsa`、
  工作区外路径读取此前只升到 L2，而 L2 在 `approval_mode=approve` 下自动放行。
  现在升到 L3（唯一不会被自动批准与 `AGENT_ALLOW_RULES` 覆盖的等级）。
- **去除 A2↔A3 内部 context 的 fail-open**：`X-Agent-Context` 在未配置
  `AGENT_INTERNAL_HMAC_KEY` 时直接放行。现在 A3 绑定非 loopback 地址、或显式
  `AGENT_INTERNAL_HMAC_REQUIRED=true`、或 A2 指向非 loopback 的 A3 时，
  缺少 key 直接拒绝启动。
- **修复 `command` 内建的 L0 绕过**：此前 `command` 与 `ls/cat` 同列只读白名单，
  `command python3 -c ...`、`command bash -c ...` 会被判 L0 自动执行。现在只放行
  `command -v/-V`，其余形式递归分类，未知命令至少 L2。
- **修复 git “只读”子命令实为写操作**：`git config/remote/branch/tag/stash`
  不再整命令按 READ 放行；只读查询（`--get/--list/-v/list`）保持 L0，写形式升到
  L2，`--global/--system`、`-c/--config-env`、`--exec-path` 升到 L3。
- **补齐危险命令变体**：`rm -r -f` / `--recursive --force` 拆分写法、`find -exec`、
  `git push origin +ref`、`PATH=/tmp` / `LD_PRELOAD=` 前缀、`export PATH=` 均按 L3；
  `split_segments` 不再把 `\;` 误当命令分隔符。
- **恢复 `git restore` 的 L2 语义**：它会丢弃工作区改动，不能走 `--write` 一次性
  自动放行。
- **未登记命令升到 L3**：`approve` 模式不再自动放行未知脚本、`python -c`、
  `xargs` 等未枚举包装器；这些调用必须人工确认。常见构建/依赖/测试命令
  （pip/npm/make/pytest 等）显式登记为 L2。TD-019 已还清。
- **登录时序与锁定**：未知、禁用或无本地密码的账号也执行一次 scrypt 校验，
  避免用响应时间枚举用户名；同一账号 15 分钟内失败 10 次后返回 429
  （`Retry-After`），成功登录后计数重置。
- **非 loopback 启动校验**：启用账号 Cookie 登录但未设
  `AGENT_WEB_COOKIE_SECURE=true` 时拒绝启动。
- **工作区锁 fail-closed**：锁获取异常不再继续运行，`RunFailed` 会给出
  “单写者锁不可用”的真实原因。
- **provider 冷却边界**：所有 provider 都在冷却时，若冷却刚好在两次检查之间
  结束，再走一轮而不是直接判定全部不可用。
- **配额预留默认值**：`AGENT_WEB_QUOTA_RESERVE_TOKENS` 从 0 改为 20000，
  并发/长 run 在真正超额前就会被硬预留拦住；设为 0 仍可恢复旧的事后限额。

### 企业身份（RFC-0004 / P1-3）

- **OIDC Authorization Code + PKCE**：discovery/JWKS、RS/PS/ES 验签、
  `iss/aud/exp/iat/nonce` 校验；state/nonce/verifier 一次性落库并消费即删；
  按 `(tenant, issuer, subject)` 映射本地用户；角色只接受显式 group 映射。
- **TOTP MFA**：本地密码账号登录分两步；密钥 AES-256-GCM（AAD 绑定
  tenant/user）、恢复码只存 SHA-256、同一时间步不可重放；挑战最多失败 5 次；
  管理员可重置 MFA（同时吊销该用户会话）。
- **四眼审批**：A2 从 SSE 记录 L3 审批，管理员在 `/admin` 审批页决定；
  发起人的 `/api/resume` 不能自批，A2 合并管理员决定后再交给 A3；
  未知 call_id 在四眼开启时 fail closed。
- Alembic `0012_enterprise_identity` 新增身份/OIDC state/MFA/审批表；
  迁移只增表，降级只回写版本号。
- **RBAC 增加 `approver` / `auditor`**：审批队列只对 owner/admin/approver
  开放；账号、用量、Provider、登录事件等读接口对 owner/admin/auditor 开放；
  `auditor` 的非 GET `/api/*` 由 `AuditorReadOnlyMiddleware` 统一 403
  （`/api/auth/*` 除外），不依赖每个新路由记得检查。
- **工作区只读 ACL**：新增 `control_workspace_acl`（Alembic `0013`），
  管理员可把工作区元数据与 revision 只读共享给同租户成员；列表/详情/修订
  对被授权者可见，但 `PATCH/DELETE/import/upload/rollback/restore/prune`
  仍要求 owner，创建会话的 `_bind_workspace` 也仍只认 owner——**共享可见，
  不共享执行**。非 owner 视图不返回 `root_path`/`root_handle`。
- 无 `alembic_version` 的旧 `create_all` 库不再一律 stamp 到 0001：先识别
  已具备的最近版本（0012）再增量升级，避免在已有表上重复建表。

### 测试与覆盖率（P1-8）

- 承重模块补齐到 85% 门槛：`control/oidc.py` 99.2%、`web/routes/auth.py`
  99.0%、`web/routes/admin.py` 90.2%、`control/credentials.py` 95.8%、
  `graph/nodes/approve.py` 100%、`control/store.py` 87.9%、
  `control/approvals.py` 97.8%。
- 新增错误/降级分支测试：OIDC discovery/JWKS/验签、认证路由的 MFA/OIDC
  失败路径、管理员 API 的 503/422/404、凭据轮换与解密失败、配额与迁移
  边界。`coverage_budget.json` 下限随之上调。
- `web/routes/runs.py`（73.0%）仍低于 85% 目标；WSL 三件套继续由
  `.github/workflows/sandbox.yml` 单独设下限。
- CI 增加 Python 3.11 快反馈 job；快反馈统一排除 `wsl_env` 标记；
  `coverage.json` 与 `reports/` 作为生成产物不再入库。
- `tools/git.py` 增加无 WSL 的假沙箱单测（路径越界、`.agent` 保护、暂存区
  检查、失败回报），覆盖率 47.9% → 97%；`tools/code_intel.py` 补假 FS/LSP
  分支（未配置、只读、文件错误、LSP 崩溃），37.5% → 99%；两者均纳入棘轮。

### 输出与持久化加固

- **终端控制序列清洗**：CLI/TUI 在写终端前删除 OSC/DCS/APC、光标/清屏/
  标题/剪贴板序列与 C0 控制字符，只保留 SGR 颜色；模型与工具输出不再能
  直接操纵终端。
- **审计记录 fsync**：每条审计写入后 `flush + os.fsync`，崩溃/断电不会
  只留下用户态缓冲里的“已审计”假象。
- **本地授权覆盖**：`host_broker/consent.py` 从 18.7% 提到 97%，覆盖 Tk
  面板、远程轮询、stdin 与失败关闭路径，并纳入覆盖率棘轮。
- **版本与 SBOM**：`pyproject.toml` 改为从 `coding_agent.__version__`
  动态取版本；`scripts/check_version.py` 校验前端 `package.json` /
  `version.ts`；Security 流水线新增 CycloneDX SBOM artifact。

### 无障碍与错误本地化（P2）

- Playwright 新增 `@axe-core/playwright` 扫描用户端与管理端（WCAG A/AA，
  颜色对比度先禁用）；修复未关联 label 的 select 与 tablist 子节点结构，
  当前零违规。
- 前端 `ProblemError` 按错误码用当前语言渲染文案（`errors.codes.*`），
  后端文案保留为未知码的兜底；会话/密码重置文案中英文同步。

### 账户与前端（P2）

- 设置页新增“登录会话”：列出有效会话、一键登出其他会话（`/api/auth/sessions*`）。
- 自助密码重置：`/api/auth/password-reset/request|confirm`，token 只存
  sha256、30 分钟有效、成功即失效并吊销该用户全部会话；邮件通过
  `AGENT_SMTP_*` 投递（未配置 SMTP 只生成 token），未知账号/无邮箱/发送失败
  统一返回 200，避免枚举。Alembic `0015_password_reset` 新增表。
- 登录页加入“忘记密码”与令牌确认表单（中英文）。
- provider 探测把连接钉到校验通过的 IP，Host/SNI 与证书仍按原域名，
  DNS rebinding 不再依赖白名单兜底。

### 授权与限流（P1-3 剩余）

- 账号新增 `max_access_mode`（Alembic `0014`）：管理员可按用户限制
  `read_only` / `write_ask` / `write_auto`；新会话创建与存量会话运行前都会
  按上限钳制，请求体不能自行提权。`0014` 只增列，降级只回写版本号。
- 限流新增 Redis 后端（`AGENT_RATE_LIMIT_BACKEND=redis` +
  `AGENT_RATE_LIMIT_REDIS_URL`，空则复用事件传输 URL）：多实例共享固定窗口；
  启动时 ping 失败直接拒绝启动，运行期 Redis 故障按 fail-closed 拒绝。

### 会话管理

- 新增 `GET /api/auth/sessions` 与 `POST /api/auth/sessions/revoke-others`：
  只暴露创建/过期时间与“当前会话”标记，不返回 token 或哈希；可一键登出
  同账号的其他会话。会话仍只存 token 的 sha256。
- 新增 `AGENT_WEB_SESSION_IDLE_TTL_S`：复用 `created_at/expires_at` 实现
  空闲超时（活动时顺延，但不超过绝对 `AGENT_WEB_SESSION_TTL_S`），
  不新增表或列；默认 0 = 关闭。

### 可观测性与工程门禁（P1-6/P1-7/P1-9/P1-11）

- 审计新增 SIEM 导出：`AGENT_AUDIT_SYSLOG_ADDRESS=host:port` 以 UDP syslog
  每行一条 JSON；`AGENT_AUDIT_WEBHOOK_URL` 用后台队列 POST 每条 JSON
  （可选 Bearer token）。失败分别计数（`audit_syslog` / `audit_webhook`），
  不影响本地审计。
- 可选 OTLP HTTP trace：`AGENT_OTEL_ENDPOINT` + `.[otel]` extra；A2/A3 的
  HTTP 请求生成 span（method/route/status/request_id），未配置时完全 no-op。
- 可选 OpenAPI：`AGENT_WEB_OPENAPI=true` 才暴露 `/openapi.json` 与 `/docs`，
  默认关闭。
- 实际执行并记录故障演练：`phase_d_smoke.py` 7/7、`phase_f_drill.py`
  upgrade→备份→downgrade→再 upgrade→恢复；runbook RB-016/RB-017 更新，
  新增 RB-018（A3 不可用/版本不匹配）。
- mypy 引入 strict 基线（`security/`、`access.py`、`terminal_text.py`），
  接入 `make check` 与 CI；修复 netguard 的 `str|int` 地址类型。
- 新增 `scripts/perf_smoke.py`：策略分类/终端清洗/内存限流热路径的宽松
  性能门禁，接入 CI 与 `make perf`。
- 可选 OTLP HTTP trace：A2/A3 的 HTTP 请求生成 span（method/route/status/
  request_id），`AGENT_OTEL_ENDPOINT` 为空时完全 no-op。
- 可选 OpenAPI：`AGENT_WEB_OPENAPI=true` 才暴露 `/openapi.json` 与 `/docs`。
- 新增 `scripts/sse_load_smoke.py`：20 路 × 200 token 的同进程 SSE 并发冒烟，
  接入 CI 与 `make load`；真实 WSL/模型 soak 仍需独立环境。
- `web/routes/runs.py` 用 fake RunDeps 覆盖配额/租约/心跳/代理/历史分支，
  覆盖率 73.0% → **86.5%**；`agent_server_routes/imports.py` 补导入 worker、
  上传入口/状态/取消与恢复/删除归档分支，61.7% → **88.1%**；两者均纳入棘轮。

### 资源治理（P1-10）

- 快照新增保留策略：每次运行/恢复开始时按最新 N 份与 M 天清理旧快照，
  同时删除宿主侧清单；默认 `AGENT_SNAPSHOT_KEEP_COUNT=200`、
  `AGENT_SNAPSHOT_KEEP_DAYS=30`，0 表示不按该维度清理。
- 删除只接受本模块生成的快照 id（时间戳+序号+uuid），磁盘上的异常目录名
  不会被拼进 `rm`。

### 运行可靠性（P1-5）

- **SIGTERM 优雅 drain**：A3 第一次信号停止接收新运行（`/v1/runs`、
  `/v1/resume`、`/v1/resume_steering` 返回 503），`/readyz` 立即报未就绪；
  等待进行中运行自然结束，超过 `AGENT_DRAIN_TIMEOUT_S`（默认 60 秒）才取消
  任务并关闭 runtime。第二次信号仍可强制退出；`/v1/shutdown` 走同一条路径。
- A2 用同一数值设置 uvicorn `timeout_graceful_shutdown`，让在途请求
  （含 SSE 收尾）在 SIGTERM 后有明确的等待窗口。

### 安全（加固）

- **host 工作区补上 `ensure_inside` 兜底**：注册表开启时，`workspace_kind=host`
  此前直接跳过根目录校验（理由是 A3 拿不到 A2L 的真实绝对路径），等于这条防线
  对 host 请求完全不存在。现在 host 与其他工作区走同一条判定：
  `ensure_inside` 两侧都会归一化，根写成 `D:\ws` 或 `/mnt/d/ws` 都能对上；
  注册表开着却没配 `AGENT_WORKSPACE_ROOT` 时同样 fail-closed。
  另外新增词法兜底 `ensure_host_workspace()`——UNC/设备路径（`\\host\share`、
  `\\.\PhysicalDrive0`）与相对路径直接拒绝，注册表关闭时也会执行。
  真正的越界防御仍在 A2L 的 `root_handle` + `resolve_child`，这里是第二道。

### 安全（P0 三条）

- **`tools/git.py` 输出不再裸着进上下文**：`git diff` / `git log -p` 是唯一没有被
  脱敏也没有读保护的旁路 —— 被误提交的 `.env`、过去的提交里的凭据都能原样
  回灌模型。现在做两层：输出统一过 `redact_secrets`（与 shell/file_read 同底线）；
  再从 diff/git 元数据行里取涉及的文件清单，命中秘密文件读保护的整份输出都不返回。
  显式 `git diff -- <秘密文件>` 在执行前就被拒（与 `file_read` 同一口径）。
- **`file_read` 的链接保护改为 fail-closed**：`stat` 拿不到 realpath 时原来把
  `info` 置 None 后继续读 —— 沙箱探活超时、脚本被掐断的后果与"链接绕过保护"
  完全一样，都是未经 realpath 校验的内容直接进上下文。现在拒绝并计一次
  `agent_silent_failures_total{site=file_read_stat}`。文件不存在不受影响
  （stat 对不存在的路径会正常返回 `exists=False`）。
- **三条结构约束写成可执行断言**：`test_architecture.py` 新增模块级依赖图
  **无环棘轮**（已知环冻结在 `_KNOWN_CYCLIC_MODULES`，新增即红）、
  `orchestrator` 不得反向依赖 `graph.build`、CLI 跨层边冻结在 `_FROZEN_CLI_EDGES`；
  `test_host_ops.py` 的 `sandbox.run` 扫描从"只扫 orchestrator + 只认字面
  `sandbox` 接收者"扩到全 src 并接受 `self._sandbox.run`，跑出来的 7 个模块
  冻结在 `_FROZEN_DIRECT_RUN_MODULES`。对应新增 TD-032 / TD-033 / TD-034。
  另外 `test_architecture.py` 与 `test_layer_contracts.py` 去掉了 `regression`
  标记（`make regression` 改为显式带上这两个加 `test_host_ops.py`）——
  一旦有人用 `-m "not regression"` 过滤，三道结构扫描会整片静默失效。
- 安全 lint 预算回到阈值内并把 C901 的棘轮从 126 收到 **125**：`netguard`
  的 URL 校验拆成 `_split_target` / `_resolve_infos` / `_pin_address`，
  `control_server` 的启动期判据收进 `_validate_startup`，`system` 路由的
  `/readyz`、`/metrics` 主体移出注册函数，Redis 限流的 `return` 移出 `try`。
  行为未变，都是把"一个函数做三件事"拆开。

### 安全（TD-034 收口）

- **A4 补齐四类能力，7 个模块不再直接调 `sandbox.run`**：
  - `SandboxFs.find_files(names, max_depth, exclude_dirs)` → `instructions.py`
    发现仓库指令文件。这条路径的产出**直接进模型上下文**，拼错一条 `find`
    等于把任意输出当指令注入；
  - `SandboxFs.collect_sources(suffixes, …)` → `codeintel/python_index.py`
    原来往沙箱里塞 `python3 - <<EOF` 走全仓库，现在只给后缀与三个上限，
    仍是**一次**调用（逐文件读要 0.2–0.3s/个 `wsl.exe`）；
  - `SandboxFs.count_files(groups, …)` → `agents/policy.py` 的仓库画像，
    分组计数一次拿回，`limit` 用 `head` 截断；
  - `SandboxFs.mkdir` / `mkdir_exclusive` / `rmdir` / `stat().mtime` →
    `memory/longterm.py` 的写锁与 `tenancy.py` 的租户目录。写锁原来取
    `self._fs._sandbox` 私有属性拼 `mkdir`/`rmdir`/`stat -c %Y`，既依赖
    "私有属性恰好还在"，也是 A2/A3 之外的第二条命令通道。
  `test_host_ops.py` 的 `_FROZEN_DIRECT_RUN_MODULES` 7 → 0，新增即失败。

### 安全（TD-032 收口）

- **探活改成四个固定意图**：`SandboxProbe` 不再有 `run(command)`，只剩
  `identity` / `which` / `dir_exists` / `ensure_dir`，实现在
  `sandbox/probe.py`（A4）。调用方连命令都传不进来之后，此前那版
  `policy.check_probe_command()` 命令名闭集就没有触发点，连同
  `PROBE_COMMANDS` 一起删除 —— 不留永远走不到的死代码。
  `doctor` 的执行探针与用户检查合并成一次 `identity()`；`sandbox-init`
  改用 `ensure_dir()`，返回值直接就是 realpath。
- **`agent admin create` 只用于冷启动**：`cli/admin.py` 原来自己构造
  `ControlStore` + `UserStore` 建账号 —— 手里有 store 句柄就能改角色、改密码、
  删账号，A2 的鉴权与审批全绕过。现在只调
  `control.bootstrap.create_first_admin()`，该函数在租户下**已有任何账号**时
  直接拒绝并提示走 `/api/admin/users`。能力被收窄成"只此一次"。
- `cli/app.py` 只剩"`agent serve` / `agent tui` 拉起另一层"两条组装边，
  已写明是允许清单而非待还债务。

### 架构（TD-033 收口）

- **拆掉 `orchestrator.scheduler ⇄ orchestrator.scheduler_run` 的模块级环**：
  5 个纯函数（`deps_met` / `wire_implement_dependents` / `task_depth` /
  `retry_context` / `fallback_result`）下沉到新的 `orchestrator/scheduler_plan.py`。
  它们只吃 `tasks` / `status` 两个字典，本来就与运行态无关；下沉后
  `scheduler_run → scheduler_plan` 是普通依赖，`scheduler → scheduler_run`
  只剩节点工厂里那一条函数内 import。
  `test_architecture.py::_KNOWN_CYCLIC_MODULES` 随之**清空** —— 这条棘轮是双向
  的：环修好了却不删基线，测试同样会红。

### 安全与文档校准（复核）

- **`git_log` 不再被提交正文误杀**：`tools/git.py` 的受保护路径识别原来把
  任何以 `+++ ` / `--- ` 开头的行都当路径，而提交正文里的"+++ 补充说明"
  同样匹配 —— 结果是合法输出被整份拒绝（方向安全，但误伤）。现在只认
  `diff --git a/X b/Y`（路径含空格时按 shell 规则切引号）与 `rename from/to`，
  信息不丢。
- **`THREAT_MODEL.md §4` 的防线表不再是"计划态"**：8 行标着
  `Phase 1 planned：test_xxx.py` 的测试其实都已存在，其中
  `test_host_workspace_store.py` **从未存在**（实际覆盖是
  `test_host_workspace_registrar.py`）；同一行还写着"AppContainer 仅受限只读"，
  与代码里已支持的受控 exec 不符。全部改成真实链接，并按代码现状改述。
  这张表是安全答辩材料，写"planned"会让评审低估已有防线。
- **复杂度明细纳入门禁**：`measure_complexity.py --check` 原来只校验 §1/§2 的
  9 个聚合数字，§3 热点表与 §4 最大文件表仍是手抄。现在两表也逐行比对
  （数字加不加粗都认），当场抓出 7 处过期（`web/app.py` 1,575→1,670、
  `config.py` 1,160→1,217 等）并已按实测更新。
- `TD-011` 补 `remaining`：把"Windows host exec 仍非 T2"的三条剩余项按
  "代码能消除 / 部署侧动作 / 不可解决"分类，并写明关闭条件 —— 它不会因
  改代码而关闭。

### 安全（TD-013 红队矩阵）

- **补"服务端失陷"方向的红队用例**：`tests/unit/test_host_broker_redteam.py`
  三条 —— 伪造 id 不匹配的 response 不能覆盖在途请求（客户端 future 按**帧 id**
  匹配）、链路被掐断时 pending 立刻 `HostBrokerOffline` 而不是挂在 30s 超时上、
  未握手时请求本地即拒（不发往服务端）。
- **`THREAT_MODEL.md §5.12.1` 新增红队矩阵**：R1–R10 逐条对上验证用例，
  并列出 5 条缺口 —— G1 设备吊销断开（有实现、无测试，需真机）、
  G2 PCR quote、G3 CSR、G4 控制面 MySQL 设备表、G5 人在环。
  结论口径写进 TD-013 的 `boundary`：**不要因为有了矩阵就宣称 T4 闭环**。

### 评测（TD-006）

- **`tests/eval` 有了定时流水线**：`llm.yml` 的 `llm-eval` job 每周跟 `llm-tests`
  一起跑，由仓库变量 `WSL_RUNNER_READY` 控制（runner 没注册时 job 会一直排队，
  那比不跑更糟，所以默认关闭 —— 注册完再打开）。
- **`scripts/eval_gate.py` 把 JSON 读成结论**：饱和只警告（尺子失效不是代码坏了，
  让它变红会训练人忽略红灯）、通过率下降超过阈值才失败、`comparable=false`
  一律不下结论。结果写进 `$GITHUB_STEP_SUMMARY`，不会静默。
  判定逻辑由 `tests/unit/test_eval_gate.py` 8 条用例钉住。
- 口径写进 `tests/eval/README.md`「读结果」一节。**任务集本身仍然饱和** ——
  这是内容工作（设计新尺子），流水线只负责让"饱和"变得可见。

### 可靠性（TD-016）

- **核实并关闭"SQLite 首次插入游标可能多记"**：台账里记的是"两个进程同时看到
  空 cursor 时各按全量累加，极端情况多记一次"。实测（拿掉进程内 `_USAGE_LOCK`、
  两个独立 engine 指向同一文件、放大"读完再写"窗口）20 轮全部收敛到正确值，
  无异常 —— 因为 cursor 是复合主键，后写方撞唯一键后**整个事务回滚**，重试时
  按差值补。
  这意味着那次 `except IntegrityError` 重试不是兜底而是正确性的一部分，已在
  `record_usage` 的 docstring 里写明，并新增
  `test_concurrent_first_cursor_insert_does_not_double_count` 守这条路径。
  SQLite 的残余限制回到它本身：高并发下等锁/超时表现为少记或报错，而不是多记。

### 可靠性

- 控制面 SQLite 补 `PRAGMA journal_mode=WAL` / `busy_timeout` / `synchronous=NORMAL`。
- `RunLeaseStore.acquire` 与 `IdempotencyStore.begin` 不再依赖
  `SELECT ... FOR UPDATE`（SQLite 会静默忽略行锁），改为条件 UPDATE + 条件 INSERT。
- 新增 `orchestrator/merge_journal.py`：并发补丁合并**前**落盘登记，崩溃后可由
  恢复提示告知"上次合到哪一步"（不自动回滚）；调度启动时清理残留 worktree。
- **工作区导入/上传任务不再裸 `create_task`**：task 存入 `job["_task"]` 保持强引用，
  加 1 小时超时与 done callback 兜底终态；任务被 GC/异常退出不会让前端永久卡在
  `queued/running`。
- **日用量与配额预留原子化**：日用量改方言 upsert + `x = x + delta`；配额预留改为
  带额度条件的单条 UPDATE；结算/过期清理改为条件 DELETE 成功后再扣减。SQLite
  忽略行锁时也不会再丢更新或重复扣减；并发用例覆盖超额预留、重复结算与重复清理。
- **租约独立心跳与取消**：新增 `LeaseHeartbeat`（TTL/3 续租，不依赖 SSE 事件）；
  `WslSandbox`/`LocalLinuxSandbox` 登记在飞进程，runtime 收到 `/cancel` 时直接终止；
  proxy 在流断开/异常时兜底触发 `on_terminal`，避免租约被心跳永久续着。

### 可观测性（P1-6）

- A2 `/metrics` 增加 HTTP 请求计数、活跃运行、SSE 连接、登录/MFA/OIDC 事件、
  四眼审批待办与决定计数；`AGENT_METRICS_TOKEN` 可给抓取器独立 Bearer，
  空值仍复用普通 Web 认证。
- `AGENT_LOG_FORMAT=json` 输出稳定字段（`ts/level/logger/message/request_id`），
  A2/A3 入口统一配置；不记录提示词/请求体/密钥。
- 新增 `packaging/prometheus/coding-agent-alerts.yml` 与 `docs/OBSERVABILITY.md`：
  审计写入失败、5xx 比例、审批积压、静默失败、在飞运行/SSE 连接告警基线。

### 备份与容灾（P1-7）

- `CodingAgentControl --backup` 支持只备份后退出；新增
  `packaging/systemd/coding-agent-backup.{service,timer}`，默认每天 03:30、
  保留 14 份，未配置控制面库时 `ExecCondition` 跳过。
- 新增跨版本迁移用例：0011 的账号/用量升级到 0012 后保留；
- 审计轮转默认从 0 改为 **256MB**；`docs/DATABASE.md` 补充 RPO/RTO 基线，
  runbook RB-017 记录 2026-10-28 演练。

- 新增 `/readyz`（A2 探控制面库与 A3 状态；A3 探执行后端与 checkpoint），
  依赖不健康返回 503 —— `/healthz` 只说明进程活着。
- 新增 `/metrics`（Prometheus 文本导出，需认证）与进程内指标注册表
  `observability.py`；标签刻意只取有界枚举。
- 关键路径的静默失败改为"计数 + warning"：配额结算、用量回写、租约释放/续租、
  审计写入、快照清单写入。对应指标 `agent_silent_failures_total{site}`。

### 前端 / 资产

- **系统图标统一为一张源图**：`web/frontend/assets/icon-source.png` 成为唯一事实源，
  `scripts/make_icon.py` 由它缩放出 `favicon-32 / icon-192 / icon-512 /
  apple-touch-icon` 与 Windows 用的 `icon.ico`（原来是脚本里用代码另画一套造型，
  两端迟早漂移）。PWA 清单改指向 PNG（maskable 用 512），Service Worker 缓存
  抬到 `v2`，管理端侧栏品牌位换成图标图。旧的 `icon.svg` 已删除。
- 发布脚本的 SW 缓存名改为按 `codingagent-shell-` 前缀替换，不再写死 `-v1`；
  PNG 纳入内容哈希，避免换图标后旧客户端一直拿到旧图。
- **升级 vendored sanitizer / Markdown**：DOMPurify 3.1.6 → **3.3.2**，
  marked 12.0.2 → **18.1.2**；`footer.html` 的四个 vendor `<script>` 全部加
  `integrity` + `crossorigin`，`vendor/README.md` 记录版本、来源与 SHA-256。
  新增单测校验 SRI 与文件内容一致，防止“只换文件不换哈希”。

### CI / 交付

- 新增 `.github/workflows/sandbox.yml`：沙箱层跑在带 WSL2 的 self-hosted runner 上，
  并对安全承重墙模块**单设**覆盖率下限（`scripts/check_layer_coverage.py`）。
- 新增 `.github/workflows/security.yml`：依赖漏洞审计（pip-audit / npm audit）与
  全量密钥扫描。刻意不挂在 PR 阻塞路径上 —— 新 CVE 会随时间出现。
- `ruff` 开启 `S`（bandit）规则；存量以 `scripts/lint_budget.json` 记账，
  CI 卡"不许变多"（棘轮）。
- 新增 `ci.yml` 的 `windows` job（`windows-latest` 快反馈，覆盖 Linux 上被 skip 的
  Windows 路径）与 `mysql` job（`mysql:8.0` service + `-m mysql`，覆盖迁移、账号、
  密钥、配额、用量与备份恢复）。
- `sandbox.yml` 增加 `pull_request` 触发；注册自托管 runner 后应把
  `Sandbox / wsl` 设为分支保护的 required check。
- **承重模块覆盖率棘轮**：新增 `scripts/coverage_budget.json` 与
  `scripts/check_module_coverage.py`，账号/审批/执行契约/命令分级等模块
  各自设下限，CI 卡“不许回退”，目标 85%；`--update` 只抬高下限。
- **指标生成化**：新增 `scripts/snapshot_metrics.py`，从 `coverage.json` +
  `pytest --collect-only` 生成 `docs/METRICS.md` 与 `docs/metrics.json`；
  CI 用 `--check` 校验，文档不再手抄用例数/覆盖率。
- 删除仓库根目录误建的 `C:` 空目录（仅空目录，无文件）。

### 破坏性变更

- `verify_context()` 新增 `required` 关键字参数（默认 `False`，行为不变）。
- 秘密文件 / 工作区外路径的**读取**由 L2 升为 L3：`approve` 模式下会开始弹确认。
  如果要把某些只读命令放回自动放行，请显式登记分级，不要退回 L2。
