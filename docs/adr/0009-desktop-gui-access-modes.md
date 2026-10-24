---
layer: L2
doc_id: adr-0009
title: 桌面 GUI 复用本地 FastAPI，并引入三档会话访问模式
owner: 项目作者
status: accepted
last_reviewed: 2026-10-23
review_cadence: 权限模型或 GUI 架构变动时
---

# ADR-0009：桌面 GUI 复用本地 FastAPI，并引入三档会话访问模式

## 背景

需要在不复制安全链路的前提下提供桌面图形界面，并支持：

- 每会话选择工作区；
- 默认只读、用户显式开启写；
- 只读时**不运行**任何项目命令或测试；
- 上传文件、模型选择、GUI 注入 API Key；
- 未来扩展到 Web 多用户。

现有 `web/` 已经有 FastAPI、SSE、租户隔离、runtime registry 与共享 governor；
`AgentRuntime` 已经有快照、checkpoint、审批、steering 等能力。缺的是桌面壳
与更细的会话级权限。

## 决策

1. **桌面壳复用本地 FastAPI**：`desktop/` 只负责启动 loopback 服务、
   随机 token、单实例锁与 pywebview 窗口；事件、审批、审计仍由同一套
   `web/` + `AgentRuntime` 处理。未来 Web 不需要另起一套前端协议。
2. **会话级 `access_mode`**：`read_only` / `write_ask` / `write_auto`。
   - `read_only` 在 `SessionPolicy` 层拒绝 shell、测试、Git、依赖等“运行”类
     工具；在 `AgentRuntime` 入口强制关闭自动 verify 与编排 implement。
   - `write_ask` 暴露写工具，但 L1/L2/L3 逐条审批。
   - `write_auto` 保留传统 `--write` 语义（L1 自动，L2/L3 审批）。
3. **上传单独授权**：文件只写入工作区 `.agent/uploads`，不授予模型改源码的
   权限；路径仍走 `SandboxFs` 双重校验，并写审计。
4. **模型切换只在空闲时**：保留 thread/checkpoint，下一次 run 重建 runtime。
5. **密钥优先管理员统一配置**：桌面端写入 OS 密钥环；多用户 Web 默认禁用
   用户自带 key，后续如开放需按用户加密并做 SSRF 防护。
   （实施更新：多用户 key 已按 `coding-agent:<user>` 隔离，见文末。）

## 代价

- `read_only` 不是仅靠工具列表就能表达的：`shell_exec` 的 L0 命令、`git_status`
  等“只读工具”仍会启动进程，因此必须在策略层按工具名额外拒绝。
- 只读会关闭自动 verify 与编排 implement，长任务能力比 `write_auto` 弱；
  这是有意的安全取舍。
- SSE 仍与 HTTP 连接绑定：窗口关闭/刷新会中断本轮，checkpoint 保留但需要
  后续做运行与连接解耦，才能支撑 Web 多用户。
  （实施更新：已改为后台 task + 事件重放，见文末。）
- 会话配置落 `gui-sessions.json`（租户审计目录）；多进程写同一租户目录仍需
  外部互斥，当前 Web 明确按单进程运行。
- GUI 注入 key 依赖可选 `keyring`；未安装时显式报错，不退化成明文文件。

## 被放弃的方案

- **PySide6 原生重写**：功能最全，但要复制事件映射与大量 runtime 调用，且未来
  Web 仍要另做一套 HTTP 协议。
- **Tauri/Electron**：界面现代，但需要 Node/Rust 工具链，并扩大 API 面。
- **允许 GUI 直接读写工作区/执行终端**：会绕过审批、审计与快照，违反
  不变量 5 与“唯一执行咽喉”。
- **请求级 `allow_write`**：旧 Web 已有过这个误导性字段；权限必须由服务端
  会话配置决定。

## 验证

- `tests/unit/test_approve.py`：三档访问模式的策略矩阵；
- `tests/unit/test_access_mode.py`：runtime 入口强制关闭 verify/orchestrator；
- `tests/unit/test_desktop_bootstrap.py`：loopback + token + 单实例；
- `tests/unit/test_web_gui_api.py`：会话配置、模型切换、上传、快照/审批接口；
- `tests/unit/test_architecture.py`：`desktop/` 不导入 tools/sandbox。

## 实施更新（2026-10-23）

原始决策中的两条边界已经落地，保留原文作为历史：

- 多用户 Web 的 key 现在按用户隔离在 `coding-agent:<user>` 密钥环命名空间；
  用户仍不能提交任意 `base_url`，SSRF 面保持在管理员配置侧。
- SSE 已改为后台 run task + 递增事件 id + `after`/`Last-Event-ID` 重放；
  断线不再取消运行。事件缓冲仍是单进程内存（有字节/条数/TTL 上限），
  多实例部署仍需共享存储或粘性会话。
- 上传增加会话累计配额；桌面关窗前会协作式取消运行并最多等待 5 秒。
