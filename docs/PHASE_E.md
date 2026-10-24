---
layer: L3
doc_id: phase-e
title: Phase E：前端补齐
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 前端交互、SSE 客户端或主题令牌变动时
---

# Phase E：前端补齐

目标：把 A1 从“功能可用”补齐到主流桌面 Agent 界面：主题/字体/密度、
SSE 连接治理、任务与思维链、长输出治理、代码高亮/复制与能力界面收口。

## 1. 已完成

### E1 设计令牌与基础体验

- 深色 / 浅色 / 跟随系统；`theme-auto` 走 `prefers-color-scheme`；
- 字体字族（系统/衬线/等宽）、字号、密度持久化到 localStorage；
- 快捷键：`Ctrl/Cmd+K` 搜索、`Ctrl/Cmd+Enter` 发送、`Esc` 关闭弹窗、
  `?` 打开快捷键帮助；
- 可访问性：`:focus-visible`、`aria-live`/`role`/`aria-label` 与
  `prefers-reduced-motion`。

### E2 SSE 客户端

- 新增 `frontend/src/sse.ts`：帧解析与 `seq` 去重；
- 断线后自动重连（0.5s/1s/2s/4s/8s 退避，最多 3 次），header 显示连接状态；
- 失败后可“重试上一条”；
- 审批/steering 恢复不再把 `lastSeq` 重置为 0，避免重放历史事件。

### E3 任务与思维链

- 任务卡显示 id / role / status / goal / 依赖 / worker / 耗时 / token /
  tool_calls / merge_error；
- reasoning 按节点分块折叠；隐私开关默认关闭，关闭时只接收不渲染；
- 明确区分“模型推理”与系统计划/工具轨迹。

### E4 输出治理

- marked + DOMPurify + highlight.js；代码块复制按钮；
- 代码块 `max-height` 折叠，diff 保留折叠/展开全部；
- 消息/工具/文件/任务卡启用 `content-visibility: auto`，长会话只渲染可视区。

### E5 能力界面收口

- 工具卡与 diff 支持复制；
- 既有审批、pause/resume/cancel/add/replan、文件接受/拒绝、快照/检查点、
  文件树、Git、上传配额、恢复中心、Provider、审计、记忆、预算与通知保持；
- 本轮不新增后端能力。

### E6 门禁与验收

- `tests/unit/test_web_frontend_assets.py`：CSP 仅本地资源、构建产物、
  vendor 文档、主题与可访问性标记；
- CI：重建 `static/app.js` 后 `git diff --exit-code` 防生成物漂移，
  并跑 `tsc --noEmit`；
- 手测清单见下节。

## 2. 手测清单

- 主题：深/浅/跟随系统切换，刷新后保留；字体、字号、密度生效；
- SSE：运行中断网 → 显示“重连中”，恢复后继续；审批恢复不重复旧消息；
  失败可“重试上一条”；
- 推理：默认不显示；设置里打开后按节点折叠；
- 代码：多语言代码块高亮、复制；长代码块可滚动；diff 可复制/展开；
- 任务：编排运行后任务卡显示依赖、worker、耗时、token；
- 快捷键：`Ctrl+K`、`Ctrl+Enter`、`Esc`、`?`；
- 长会话：滚动与输入不卡顿（`content-visibility` 生效）。

## 3. 未覆盖

- 不是 JS 窗口化虚拟列表；长会话用浏览器原生 `content-visibility` 优化，
  未重写 DOM 列表；
- 没有浏览器自动化测试，交互靠上面的手测清单；P4 也未引入 Playwright
  （如需要，应作为独立变更单独引入，不混进性能/验收轮次）；
- reasoning 是否落盘仍由后端日志策略决定，前端只控制展示；
- highlight.js 使用通用包，未按语言裁剪体积。
