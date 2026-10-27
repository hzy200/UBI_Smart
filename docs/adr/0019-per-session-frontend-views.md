---
layer: L2
doc_id: adr-0019
title: 前端按会话隔离 SessionView 与 SSE，支持后台流和实时分屏
owner: 项目作者
status: accepted
last_reviewed: 2026-10-27
review_cadence: 前端会话状态、SSE 生命周期或分屏交互变动时
---

# ADR-0019：前端按会话隔离 SessionView 与 SSE

## 背景

早期 GUI 把所有会话运行态放在 Alpine 根对象上：`messages`、`tools`、`plan`、
`approvals`、`busy`、`lastSeq` 等只有一份；同一时间只允许一条 SSE，切换会话
必须先重置并重新拉历史。结果是：

- 运行中不能切走，后台完成/审批无法在别的会话里提示；
- 重复打开会话会丢消息、草稿与工具状态；
- 分屏只能做只读历史快照，无法并行运行；
- 全局 Map/缓存（`toolMap`、token 定时器）容易被误清，跨会话存在串扰风险。

服务端的会话租约、SSE 序号与重放本来就是按 thread 隔离的；瓶颈在前端。

## 可选方案

1. 保持全局单份状态，只做 UI 标签切换；
2. 按会话保存 JSON 快照，切回时重建；
3. 每个 thread 一个响应式 `SessionView`，事件入口显式绑定 view；
4. 引入第三方状态库/组件框架，重写渲染层。

方案 2 会丢流式中间态与计时器；方案 4 与静态 SPA + Alpine CSP 的约束冲突；
方案 1 无法支持后台流。选择方案 3。

## 决策

1. **SessionView 作为状态边界。**
   `state/session-view.ts` 定义消息、计划、工具、任务、审批、上传、用量、
   流式游标与连接状态；`sessionViews[threadId]` 为唯一注册表。
2. **兼容 facade。**
   `features/session-view.ts` 用 getter/setter 把旧字段转发到 `activeView`，
   模板与既有 mixin 不需一次性重写；构建期守卫保证文件仍在。
3. **事件显式绑定。**
   SSE 发起时捕获 view，`consume(response, view)` 的事件经
   `applyEvent(view, event)` 写入该 view；后台事件不滚动、不播报、不切标签。
4. **每视图生命周期。**
   每个 view 持有独立 `AbortController`、重连计时器与 `lastSeq`；删除/清空
   会话时 abort；LRU 最多保留 6 个 view，跳过运行中与当前分屏 view。
5. **切换与未读。**
   切换会话不再被 busy/审批阻挡；后台完成/失败/审批通过通知与未读提示；
   分屏可见的会话不重复标未读。
6. **实时分屏。**
   分屏绑定真实 view，可独立接收 SSE 与发送任务；审批在分屏只提示，
   操作切回主区。

## 后果

- 正：后台运行、会话复用、实时双分屏成为可能；状态按会话可预测。
- 正：服务端无需改动，SSE/租约本就按 thread 隔离。
- 负：会话视图需要显式清理（abort、计时器、缓存键），否则会泄漏或串扰；
  相关缓存键必须带 `threadId`。
- 负：编辑器标签与焦点仍是全局的；分屏审批只提示，操作需切回主区。
- 负：后台流数量需要上限（当前 6 个 view 的 LRU + 运行中保护）。
- 取舍：没有引入状态库或前端框架，继续用 Alpine CSP；复杂度集中在
  `session-view`/`run` 两处。

## 验证

- `node test.mjs`：纯函数与缓存键；
- `node test-dom.mjs`：视图隔离、后台 SSE、实时分屏、审批、拖拽、主题；
- `tests/unit/test_frontend_workspace_ui.py`：构建产物与 CSP 模板守卫；
- `python scripts/probe_frontend_csp.py`：pywebview 严格 CSP 冒烟。
