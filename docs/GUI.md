---
layer: L3
doc_id: gui
title: 桌面 GUI
owner: 项目作者
status: active
last_reviewed: 2026-10-26
review_cadence: GUI 能力、权限模型或 API 变动时
---

# 桌面 GUI

> 前端功能、消息呈现、工作区绑定、对话内授权、视觉规范与 UI 约束
> 见 [FRONTEND.md](FRONTEND.md)。本文保留桌面壳、接口、权限与部署说明。

桌面 GUI 是 `cli/`、`tui/`、`web/` 之外的第四个前端：

```text
Windows：pywebview 窗口 + 启动器/Supervisor
  │ HTTP + SSE，127.0.0.1:随机端口，随机 Bearer token
WSL2：Agent Server（FastAPI + AgentRuntime + tools）
  │
LocalLinuxSandbox（WSL 内直接 bash，不再逐条 wsl.exe）
```

`--host-mode` 保留旧的 Windows 宿主模式用于回退。

原则不变：**前端只依赖 `AgentRuntime` 与 `events.py`，不得导入 `tools/` 或
`sandbox/`**。GUI 无法绕过审批、审计与快照。

---

## 1. 安装与启动

```bash
conda activate agent

pip install -e ".[web,desktop]"          # 桌面窗口
pip install -e ".[web,desktop,secrets]"  # 需要在 GUI 里注入 API Key 时

agent gui                     # 首次运行打开 WSL 配置向导
agent gui --setup             # 重新配置
agent gui --write             # 允许会话选择“审批写/自动写”
agent gui --workspace D:\proj # 覆盖 WSL 工作区（转成 /mnt/d/proj）
agent gui --host-mode         # 旧 Windows 宿主模式
```

WSL 模式下端口由 server 自动分配并通过握手返回；`--port` 只影响 host mode。

- 只监听 `127.0.0.1`，每次启动生成随机 token；token 放在 URL fragment，
  不进入服务端日志。
- 使用 `WorkspaceLock` 做单实例锁；同一工作区已有 GUI 时会拒绝启动。
- 桌面与 Web 共用同一套后端；`agent web` 仍然可用。
- 关闭窗口时关闭事件立即返回；取消运行与 A2/A3/A2L 停止在**后台并行**执行，
  短等待后强制 kill。窗口不会被进程清理阻塞（避免 Windows“未响应”），
  checkpoint 仍保留，可从 CLI/TUI 恢复。

---

## 2. 会话与工作区

每个会话在创建时绑定：

| 字段 | 说明 |
|---|---|
| `thread_id` | 会话 id，checkpoint 的键 |
| `workspace` | 该会话的工作区；**会话内不可变** |
| `access_mode` | `read_only` / `write_ask` / `write_auto` |
| `model` / `provider` | 会话级模型选择；空闲时可切换 |

- 换工作区请新建会话或 fork，避免 checkpoint 里的 `cwd` 与工具路径校验错位。
- 同一工作区仍受单写者锁约束，同时只能有一个运行中的会话。
- 会话配置持久化在控制面数据库 `control_sessions`；`gui-sessions.json` 只作
  无数据库时的兜底。创建但尚未运行的会话也会出现在侧栏。
- host 会话的 `workspace_kind/a2l_id/root_handle` 随会话落库（迁移 0006），
  本地 catalog 丢失时从 `workspaces` 表恢复；会话工作区不可改绑。
- 由 CLI/TUI 创建、没有 GUI 配置的旧会话，会从审计的 `run_start.workspace`
  恢复工作区，避免快照/回滚/resume 落到错误的目录。
- Web 多用户模式只允许选择 `workspaces_root/<user>` 下的目录；桌面单用户
  可以选择任意绝对路径（GUI 会显式确认）。

### 2.1 工作区与项目 Python（Phase 3）

设置页“工作区与 Python”展示：

- 当前路径与模式：WSL 原生 / `/mnt`（Windows 盘）；
- 项目 Python（`.venv` → `venv` → `env` → 系统 `python3`）与版本；
- Agent Python（专用发行版 venv），两者严格分离；
- 包管理器与锁文件：uv / poetry / pipenv / conda / pip；
- 建议测试命令；项目解释器有 pytest 时自动验证会使用它；
- 警告：`/mnt` 的 9P 性能、工作区不存在/不可写、缺 venv、缺 pytest。

桌面专属动作（浏览器模式自动隐藏）：

| 动作 | 说明 |
|---|---|
| 选择文件夹 | Windows 原生对话框，返回 `/mnt/<drive>/...` 或 WSL 路径 |
| 在 VS Code 打开 | `code --remote wsl+<distro> <path>` |
| 复制到 WSL 原生目录 | 仅 `/mnt` 源；目标已存在时拒绝覆盖；后台复制并轮询状态 |

探测本身只读：不创建 venv、不安装依赖、不执行项目脚本。

### 2.2 会话、文件与 Git（Phase 4）

- 会话栏支持搜索、改名、删除、导出 Markdown、置顶、归档与标签；
  归档只影响列表；删除是 tombstone，审计与 checkpoint 保留，列表不再出现。
- “文件树”标签逐层浏览工作区（跳过 `.git`/`__pycache__`/`node_modules`），
  点击文件只读查看；单文件最多 256 KB，二进制只显示提示。
- “Git”标签只读展示分支、ahead/behind、未提交改动、最近提交与 diff；
  暂存/提交仍由 Agent + 审批完成。
- “文件”标签的每个快照可“接受”（保留当前）或“拒绝（回滚）”。
- 上传区显示本会话附件、大小与工作区配额，可删除或拖拽上传。
- 顶部“恢复中心”列出待审批、已暂停、运行中、异常中断的会话，
  可打开/恢复或删除。

### 2.3 诊断、更新与稳定性（Phase 5）

- 设置页可生成诊断包：版本、配置、日志尾部（脱敏）与 WSL 概况；
- WSL server 意外退出时，启动器按 1s/3s/7s 退避最多重启 3 次，
  成功后用新 token 重载窗口；关窗后不再自动拉起；
- 配置 `AGENT_UPDATE_MANIFEST_URL` 后可“检查更新”；
  只打开下载页，不自动安装/回滚；
- 快照 diff 超过 200 行默认折叠；
- Provider 表单提供 Ollama / LM Studio 本地端点预设。

目标态的前端能力矩阵（主题/字体、虚拟滚动、按节点推理时间线、命令面板等）
见 [PHASE_A.md](PHASE_A.md) 第 6 节；当前实现与目标之间不做隐含承诺。

---

## 3. 文件访问权限

| 能力 | `read_only` | `write_ask` | `write_auto` |
|---|---|---|---|
| 文件读取 / 列表 / 检索 / 符号索引 | ✅ | ✅ | ✅ |
| 只读探索子智能体 | ✅ | ✅ | ✅ |
| 文件写 / 编辑 / 移动 / 复制 / 删除 | ❌ | 逐条审批 | L1 自动 |
| `shell_exec` | ❌ | 逐条审批 | L2/L3 审批 |
| `run_tests` | ❌ | 逐条审批 | 逐条审批 |
| 自动 verify | ❌ | ✅ | ✅ |
| Git / 依赖命令 | ❌ | 逐条审批 | 逐条审批 |
| 编排 implement worker | ❌ | ✅ | ✅ |

实现位置：

- `access.py`：模式常量与归一化；
- `sandbox/policy.py::SessionPolicy`：按模式决定 auto/ask/deny；
- `runtime.py`：只读时强制 `verify_enabled=False`、`orchestrator_enabled=False`；
- `graph/nodes/approve.py`：工具风险等级仍按原表，未登记工具继续 fail-closed。

**只读的准确语义**：禁止模型意图执行项目命令（shell、测试、Git、依赖、
LSP）；`symbol_search` 强制退回 AST，`code_intel` 跳过。允许宿主侧的只读
文件/检索/解析工具。上传与回滚是用户动作，单独授权，不改变模型的写权限。

`production` / `ops` profile 永远强制只读 + deny，不能被会话设置覆盖。

---

## 4. 审批与 steering

- 审批事件会收集成队列，GUI 用应用内模态逐条展示；默认拒绝，Esc 拒绝。
- 全部答复后一次 `POST /api/resume/{thread}`；恢复流里可能出现新的审批。
- `pause` / `cancel` / `add` / `replan` 走 `POST /api/steer/{thread}`。
- 暂停后流会以 `steering_applied(pause)` 收尾；恢复走
  `POST /api/resume_steering/{thread}`。
- 暂停状态下取消：GUI 会先提交 cancel，再自动调用 resume_steering；
  否则指令只会留在队列里，任务永久暂停。
- host 工作区的本机文件授权不再弹独立窗口，而是在对话输出区显示
  “允许本次 / 拒绝”卡片；细节见 [FRONTEND.md](FRONTEND.md) 第 6 节。

---

## 5. 文件、回滚与上下文

- `file_changed` 事件直接进入文件面板；快照列表来自
  `GET /api/snapshots`，diff 来自 `GET /api/diff`。
- 单文件回滚：`POST /api/restore`（回滚前会给当前内容留底）。
- 本轮回滚：`GET /api/run_started/{thread}` 取起点，再以 `prefix` 调
  `POST /api/restore`；先 dry-run 展示影响文件。
- checkpoint 回退/分叉：`POST /api/rewind/{thread}`、
  `POST /api/fork/{thread}`；历史不会删除。
- `ContextTrimmed` 会显示裁剪条数与 token 变化。
- 桌面端每轮结束把完整 UI 轨迹（含工具卡片、过程与最终回答）保存到
  `transcripts/<thread>.json`，重开会话时优先恢复；见
  [FRONTEND.md](FRONTEND.md) 第 5.3 节。
- “压缩并分叉”：`POST /api/context/{thread}/compact` 用确定性摘要 + 最近
  若干轮纯文本消息创建一个新 thread；原 thread 保留，不原地删除消息，
  也不会复制 `tool_calls` / `ToolMessage`，因此不破坏配对。

---

## 6. 模型与密钥

- 模型列表来自 `GET /api/providers`（名称、model、base_url、是否有 key）。
- 会话创建时选择 provider/model；运行中不能切换。
- 空闲切换：`POST /api/sessions/{thread}/model`；保留 thread 与 checkpoint，
  下一次 run 重建 runtime。
- 服务端 `--write` 是硬上限：即使 `gui-sessions.json` 里残留 `write_auto`，
  未带 `--write` 启动时也会按只读运行。
- GUI 注入的密钥写入操作系统密钥环（`keyring`），不写 `.env`、不回传前端、
  不写日志。未安装 `keyring` 时显式返回 503。
- 默认 provider 的密钥环优先于 `.env` 的 `DEEPSEEK_API_KEY`；显式写在
  `AGENT_LLM_PROVIDERS[].api_key` / `api_key_env` 里的 key 仍是最先匹配的。
- `/api/health` 与 runtime 使用同一租户密钥存储；provider 配置损坏时返回
  200 + `error`，不会 500。
- 多用户 Web 模式的 key 按用户隔离在 OS 密钥环（service 为
  `coding-agent:<user>`）；管理员仍可通过环境变量统一配置默认 key。
- 用户只能提交 key，不能提交任意 `base_url`；provider 的 base_url 仍由管理员
  配置，避免 SSRF。

---

## 7. 文件上传

- `POST /api/sessions/{thread}/uploads`，`multipart/form-data`。
- 写入工作区 `.agent/uploads/<thread>/<random>-<name>`，**不修改源码目录**。
- 单文件上限 `AGENT_WEB_MAX_UPLOAD_BYTES`（默认 20MB），单次文件数上限
  `AGENT_WEB_MAX_UPLOAD_FILES`（默认 20），**同一工作区跨会话**累计上限
  `AGENT_WEB_MAX_UPLOAD_TOTAL_BYTES`（默认 200MB）。
- 只取 basename，拒绝 NUL、`.`、`..`；路径仍走 `SandboxFs` 双重校验。
- 上传写审计；不自动执行、不自动解压。
- 上传是用户动作，read_only 会话也可以上传，但 Agent 仍只能读文件。

---

## 8. 主要接口

```text
GET    /api/health
GET    /api/workspace
GET    /api/providers
POST   /api/providers/{name}/key
POST   /api/providers/test
DELETE /api/providers/{name}/key

GET    /api/sessions
POST   /api/sessions
POST   /api/sessions/{thread}/model
POST   /api/sessions/{thread}/uploads
GET    /api/capabilities?thread_id=

POST   /api/run
POST   /api/resume/{thread}
POST   /api/resume_steering/{thread}
POST   /api/steer/{thread}
GET    /api/approvals/{thread}
GET    /api/status/{thread}
POST   /api/desktop/cancel_all（仅桌面模式）
GET    /api/stream/{thread}?after=<seq>
GET    /api/thread/{thread}

GET    /api/snapshots?thread_id=
GET    /api/diff?thread_id=
POST   /api/restore
GET    /api/run_started/{thread}
GET    /api/checkpoints/{thread}
POST   /api/context/{thread}/compact
POST   /api/fork/{thread}
POST   /api/rewind/{thread}

GET    /api/memory
POST   /api/memory
GET    /api/audit
```

桌面端所有 `/api/*` 都要求随机 Bearer token；Web 端按 `AuthConfig` 处理
（配置 token 后要求 Bearer，未配置时仅允许 loopback 匿名）。
接口按会话能力与租户授权；前端不能通过请求体自行提升权限。

---

## 9. 安全边界

- **审批不能绕过**：GUI 只消费 `ApprovalRequested`，没有直接调用工具的能力。
- **只读在服务端强制**：UI 隐藏按钮只是显示层，策略层仍会拒绝。
- **密钥只进密钥环**：前端只看到 `has_key`，不返回明文。
- **上传不执行**：上传目录在 `.agent/uploads`，不由 Agent 自动运行。
- **XSS**：模型输出经 `marked + DOMPurify` 清洗；前端依赖全部本地 vendored，
  CSP 为 `default-src 'self'`。当前使用标准 Alpine，`script-src` 需要
  `'unsafe-eval'`（TD-016，公网部署前复核）。
- **本地服务**：只绑 loopback、随机 token、`TrustedHostMiddleware` 限制 Host，
  无 CORS、无 `/docs`；无 Content-Length 的 chunked 写请求直接拒绝。
- **Provider 测试直连**：`trust_env=False`，不使用环境 HTTP(S)_PROXY，避免把
  Authorization 发给代理。
- **单写者**：运行中的会话持有工作区锁；回滚/fork/rewind 在运行中拒绝。

---

## 10. 已知限制与后续

- 运行由后台任务持有，SSE 连接只是订阅者；`GET /api/stream/{thread}`
  支持 `after` / `Last-Event-ID` 重放。桌面关闭窗口会停止本地服务，因此
  桌面端仍建议等一轮结束再关闭；Web 端可重连。
- 事件缓冲在单进程内存中，按字符上限（8MB）/5000 条/5 分钟淘汰最旧事件；
  多实例部署仍需要共享事件存储/粘性会话。
- reasoning 流是 best-effort：provider 在 `additional_kwargs.reasoning_content`
  或 `reasoning`/`thinking` content block 中提供时，GUI 会显示一个默认折叠的
  “模型思考”面板；拿不到就不显示，不伪造。
- 压缩已实现为“压缩并分叉”；不做原地删消息。
- Web 多实例共享事件存储/集中密钥、细粒度租户配额与审计策略仍属部署阶段。
- GUI 内不提供直接终端/文件编辑器/Git commit；这些路径会绕过审批。
