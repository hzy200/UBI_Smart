---
layer: L3
doc_id: phase4
title: Phase 4：桌面功能补齐
owner: 项目作者
status: active
last_reviewed: 2026-10-23
review_cadence: GUI 会话/文件/Git/Provider/恢复功能变动时
---

# Phase 4：桌面功能补齐

Phase 4 在“能安装、能选工作区”的基础上补齐常用桌面能力：

- 会话管理：搜索、重命名、删除（tombstone）、Markdown 导出；
- 只读文件树与文件查看器；
- Git 只读面板：分支、状态、最近提交、diff；
- 快照逐文件“接受 / 拒绝”；
- 上传列表、配额、删除与拖拽；
- 恢复中心：待审批、暂停、运行中、异常中断；
- Provider 档案 CRUD、`/models` 发现；
- 预算与上传上限展示、系统通知。

## 1. 会话管理

| 动作 | API | 语义 |
|---|---|---|
| 搜索 | `GET /api/sessions?q=` | 匹配标题、thread_id、工作区 |
| 重命名 | `POST /api/sessions/{id}/rename` | 写入 `SessionConfig.title` |
| 删除 | `DELETE /api/sessions/{id}` | tombstone：列表隐藏 + 删除配置；审计/checkpoint 保留 |
| 导出 | `GET /api/sessions/{id}/export?format=markdown\|json` | 从 checkpoint 还原对话 |

删除后的 thread 再创建返回 410；直接访问旧 thread 也不会重新出现在列表。
前端导出用 Blob 下载 `.md`。

## 2. 文件树与只读查看器

`GET /api/files?thread_id=&path=` 列出目录；`GET /api/file?thread_id=&path=`
读取文本。

- 路径必须是工作区内相对路径；`..`、绝对路径与符号链接越界都拒绝；
- 跳过 `.git`、`__pycache__`、`node_modules`；
- 单目录最多 400 项，单文件最多读取 256 KB；
- 含 NUL 的文件按二进制处理，只显示“二进制文件”；
- 不提供写入、重命名、删除能力。

## 3. Git 只读面板

`GET /api/git?thread_id=&diff=true`：

- `git status --porcelain=v1 --branch` → 分支、ahead/behind、改动；
- `git log -n 20` → 最近提交；
- `git diff` / `git diff --cached`，各截断到 120 K 字符；
- 只跑只读子命令，列表传参、`GIT_TERMINAL_PROMPT=0`，不拼接 shell。

## 4. 快照逐文件接受 / 拒绝

每个 `file_changed` 快照是**改动前**的内容：

- 接受：保留当前文件，仅从前端列表移除；
- 拒绝：调用既有 `/api/restore` 恢复该文件（回滚前再留底）；
- 整轮回滚仍用“回滚本轮”。

## 5. 上传

- 上传接口不变；新增 `GET /api/sessions/{id}/uploads` 列出本会话附件、
  实际占用、工作区累计与上限；
- `DELETE /api/sessions/{id}/uploads` 只允许删除
  `.agent/uploads/<内部thread>/` 下的文件；路径解析后必须仍在工作区；
- 前端显示文件名、大小、配额，可删除，也可拖拽到输入框上传。

## 6. 恢复中心

`GET /api/recovery` 扫描最近 50 个会话：

| 状态 | 判定 |
|---|---|
| `running` | 内存中有 runtime 且 `_stream_active` |
| `paused` | steering 暂停标记 |
| `approval` | checkpoint 中仍有挂起审批 |
| `interrupted` | 审计最后是 `run_start`，没有 `run_end/run_error` |

前端可“打开/恢复”（暂停调用 `/api/resume_steering`；审批走既有审批弹窗）
或删除会话。恢复中心只读扫描，不自动恢复。

## 7. Provider 档案

- `GET/POST/DELETE /api/provider-profiles`；
- 档案保存在审计目录同级的 `provider-profiles.json`；
- 字段：name / base_url / model / 成本 / 配额 / capabilities / task_types；
- 合并规则：同名覆盖环境 `AGENT_LLM_PROVIDERS` 字段，新名字追加；
- 删除全部档案后回退环境基线；
- 单机 API Key 仍由 OS 密钥环按 provider 名保存；企业模式由管理员写入
  `control_provider_keys`，只存 AES-GCM 密文+后四位；档案表不落密钥；
- `/api/providers/test` 同时返回 `/models` 的前 100 个模型名；
- **多用户模式禁用档案写入**（403）：共享 base_url 会把一个用户的密钥
  发到另一个用户指定的端点。

## 8. 设置、预算与通知

- `/api/capabilities` 增加 `run_cost_budget_usd`、`cost_alert_ratio`、
  上传上限与 `desktop_mode`；
- 设置页展示预算和单文件上限；
- 可开启系统通知：审批请求、运行完成、运行失败；
- 通知权限由浏览器/WebView 决定，未授权时自动关闭开关。

## 9. 验收与限制

本机验证（2026-10-23）：

- 在专用发行版内启动 server，对真实仓库跑通：
  `POST /api/sessions` → 文件树/文件读取 → Git 状态 → 上传/列表/删除 →
  改名/导出/删除 → Provider 档案增删 → `/api/recovery` → 优雅退出；
- 会话改名、搜索、导出、删除后列表隐藏与 410 行为由单测覆盖；
- 文件树/读取的越界、二进制、截断行为由单测覆盖；
- Git 面板在临时仓库上返回分支、改动与提交；`/mnt` 大仓库的
  `git diff` 超时只降级 diff（返回 `diff_error`），状态面板仍可用；
- 恢复中心能从 `run_start` 无收尾的审计记录识别异常中断；
- Provider 档案写入后 `/api/providers` 立即返回新档案，删除后回退；
- 前端 `node --check` 通过，快反馈全量测试通过。

已知限制：

- 文件树是懒加载逐层列表，不是虚拟滚动；
- Git 面板不做暂存/提交，commit 仍走 Agent + 审批；
- `/mnt` 上的超大 diff 可能超过 20 秒，此时只显示错误提示；
- 上传不做图片缩略图，只显示名称/大小；
- 删除是 tombstone，不清理审计、checkpoint 与工作区附件；
- provider 档案仅在单用户/桌面模式启用。
