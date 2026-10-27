---
layer: L3
doc_id: frontend
title: 前端功能、交互与 UI 约束
owner: 项目作者
status: active
last_reviewed: 2026-10-26
review_cadence: 前端交互、事件契约、权限模型或桌面壳变动时
---

# 前端功能、交互与 UI 约束

本文描述 `web/` 前端的**当前实现**：功能边界、页面结构、消息呈现、
会话/工作区绑定、授权交互、视觉规范、性能与测试约束。桌面壳的启动、
端口、锁与后端接口另见 [GUI.md](GUI.md)；领域事件契约以
[docs/contracts/events.md](contracts/events.md) 为准。

---

## 1. 边界与事实源

前端是 **A1**，只做三件事：

1. 发 HTTP 请求（会话、工作区、审批、运行）；
2. 消费 SSE 领域事件；
3. 把状态渲染成可操作的界面。

部署与运行时契约（拓扑、基址、认证、CORS、代理、桌面桥）以
[RFC-0003](rfc/0003-frontend-backend-separation.md) 与
[ADR-0016](adr/0016-frontend-backend-topology-and-runtime-config.md)–
[ADR-0018](adr/0018-desktop-bridge-boundary.md) 为准；第 15 节是摘要。
运行时配置/基址注入（第 1 步）、版本握手（第 2 步）、认证适配（第 3 步）
与错误协议/request ID（第 4 步）已实现；CORS、代理与桌面桥改造尚未开始。

硬约束：

- **不得导入 `tools/`、`sandbox/` 或直接调用工具**。所有执行都必须经
  A2 → A3 → A4 与审批/授权链路；UI 隐藏按钮只是显示层，服务端仍会拒绝。
- **不得在浏览器里保存 API Key**。Provider 只返回 `has_key`；密钥写 OS
  密钥环（桌面/Web 单用户），前端只提交密钥。
- **模型输出不是 HTML**。所有 markdown 先经 `marked`，再经 `DOMPurify`
  清洗后才进入 `x-html`；不允许拼接未清洗的 HTML。
- **静态资源全部本地 vendored**（Alpine、highlight.js、marked、
  DOMPurify），运行时不依赖 CDN，也没有前端打包服务器。

### 构建链

```text
web/frontend/src/*.ts            --esbuild------>  web/static/app.js   (IIFE)
web/frontend/src/templates/*.html --build-ui.mjs--> web/static/index.html
web/frontend/src/styles/*.css     --build-ui.mjs--> web/static/style.css
web/static/vendor/*                                (直接由 A2 提供)
```

- `app.js`、`index.html`、`style.css` 都是生成物，**不要手改**；
  改 `frontend/src` 后必须重建（`node esbuild.mjs` 会同时构建三者）。
- `features/*.ts` 是按域拆分的 Alpine mixin；`app.ts` 只保留初始状态与组合。
  mixin 必须用 `Object.defineProperties` 组合，不能用对象展开——展开会在
  构造时求值 getter，导致 CSP 构建下失去响应性。
- `contracts/events.generated.ts` 由 `scripts/gen_contracts.py` 从
  `src/coding_agent/events.py` 生成，禁止手改。
- 守卫测试：`tests/unit/test_frontend_workspace_ui.py` 检查构建产物是否
  包含关键流程，防止忘记重建导致界面静默退回旧版本。
- 类型检查：`tsc --noEmit`；构建：`node esbuild.mjs`。
- 运行期没有 webpack/vite，也没有 Node 服务；只有静态文件。

---

## 2. 桌面壳与运行约束

桌面入口是 `desktop/launcher.py` 的 pywebview 窗口：

| 约束 | 值/行为 |
|---|---|
| 绑定 | 只连 `127.0.0.1` 的 A2；无公网监听 |
| 认证 | 随机 Bearer token，放在 URL fragment；JS 解析后存 `sessionStorage`，随后从地址栏移除 |
| 窗口 | `maximized=True`，最小 `980x620`，`text_select=True`，`zoomable=True` |
| 关闭 | 关闭事件立即返回；A2/A3/A2L **并行后台停止**，短等待后强制 kill，UI 不阻塞 |
| 单实例 | `WorkspaceLock` 阻止同一工作区重复启动 |
| 浏览器回退 | `agent web` 仍可用；桌面专属能力通过 `capabilities.desktop_mode`/`desktopAvailable` 隐藏 |

`--host-mode`（`desktop/bootstrap.py`）保留为回退入口，关闭时同样把
“取消运行”放到后台线程，避免 Windows 标记“未响应”。

---

## 3. 信息架构

桌面视口下是 IDE 风格外壳：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ header：侧栏开关 / 品牌 / 命令面板 / 工作区 / 检查器 / 恢复 / 设置   │
├────┬───────────────┬───────────────────────────────┬─────────────────┤
│活动│ sidebar       │ main                          │ inspector       │
│栏  │ 会话/文件/树  │ 主区标签（对话）              │ 计划 / 工具     │
│    │ Git/任务/记忆 │ reasoning（可折叠）           │                 │
│    │ 审计          │ transcript（对话流）          │                 │
│    │               │ composer（输入区）            │                 │
├────┴───────────────┴───────────────────────────────┴─────────────────┤
│ statusbar：运行状态 / 连接 / 权限 / 模型 / 工作区 / 分支 / 费用       │
└──────────────────────────────────────────────────────────────────────┘
```

侧栏与检查器可拖拽调宽、可折叠，宽度与折叠状态存
`localStorage`（`codingagent.layout`）。窄屏下检查器改为浮层。

### 3.1 Header

显示：侧栏开关、品牌、命令面板入口、当前工作区，以及通知、检查器、
恢复中心、快捷键、设置按钮。运行状态、连接、权限、模型、token 用量、
费用移到状态栏，避免顶栏在部分 DPI 下被裁切。

### 3.2 Sidebar

活动栏切换侧栏视图；每个视图只渲染自己的内容：

- **会话**：项目/工作区、文件权限、模型、创建会话、会话搜索与列表；
  每个会话可“分屏”打开只读历史快照；
  支持置顶、归档、标签与“全部/置顶/归档”筛选。列表显示最后一条消息
  摘要、轮次/工具数与时间；未读会话有圆点与加粗标题，打开后自动已读，
  也可手动标记已读/未读。勾选多个会话后可批量归档、取消归档或删除。
  归档只影响列表，审计与 checkpoint 保留。
- **文件改动**：快照、diff、检查点、逐文件/整轮接受与回滚。
- **文件树**：host 工作区经 A2L 读取；sandbox 经 A3；按目录懒加载、
  可展开/折叠、刷新与收起全部；支持过滤已加载条目与复制相对路径；
  桌面 sandbox 模式可在文件标签中调用 VS Code 打开。加载时顺带读取
  `/api/git`，为文件显示 M/A/D/R/U 标记，目录显示聚合改动圆点；
  Agent 改动文件后先本地标 M，刷新时再与真实状态对齐。
- **Git**：只读分支/改动/提交/diff；提交仍由 Agent + 审批完成。
- **任务**：编排任务 DAG、worker、依赖、耗时与 token。
- **记忆 / 审计**：查询与维护操作；删除/清理需显式确认。
- **问题**：聚合验证失败/失败测试与审查发现，显示严重级别、来源与详情；
  按文件路径或来源（验证/审查/运行）分组，组头可折叠并统计错误/警告数；
  活动栏带未解决问题数量徽标。

会话列表按工作区分组（`web · 本机`、`WSL · /path`、`未绑定工作区`），
组内/组间都按最近活动排序；置顶会话优先，支持搜索（含标签）、改名、
导出 Markdown、删除、置顶、归档与标签编辑。
删除是 tombstone：审计与 checkpoint 保留，列表不再出现。

### 3.3 Main

自上而下：

1. **主区标签**：对话标签 + 多个文件/Diff 标签；支持切换、关闭当前、
   关闭其他、关闭全部。文件变化后标签显示“文件已变化”并可刷新。
2. **reasoning**：按节点分块，默认折叠；拿不到推理内容就不显示，不伪造。
3. **transcript**：用户/助手/系统/工具消息的时间线；可滚动，输入区固定在底部。
   长会话使用变量高度虚拟列表：只渲染视口附近消息，上下用 spacer 占位；
   搜索命中会滚动到目标并渲染对应消息。
   用户消息支持“编辑重发”，最终回答支持“重新生成”；`Ctrl/Cmd+Shift+F`
   搜索当前对话并可上下跳转命中；滚动离开底部时显示“跳到最新”。
4. **composer**：多行输入、发送/暂停/取消/继续、重试上一条、追加任务、
   重规划、上传文件、拖拽上传；上传列表与配额提示。

主区顶部有全局错误横幅（`role="alert"`）：会话列表/文件树/文件读取失败时
显示错误并提供重试；会话、文件树、文件预览在加载期间显示骨架屏。

文件预览使用 highlight.js 按扩展名高亮，带行号、面包屑与“跳转到行”；
常见图片（png/jpg/gif/webp/bmp/ico/svg）在 2MB 以内直接以 data URL 预览，
超过上限或二进制文件显示说明。host 工作区暂不支持图片预览。
主区可叠放一个只读分屏：从会话操作或命令面板打开另一个会话的历史快照，
支持刷新、在主区打开与关闭；不建立第二条 SSE，避免双流状态竞争。
diff 支持统一/并排视图、行号、增删着色、逐行语法高亮与词级高亮，
超过 200 行默认折叠。文件内容与 diff 都只在主区只读展示。

布局约束：`.main`/`.transcript` 必须允许收缩（`min-height: 0`），
滚动发生在 transcript 内部；composer 不能被长对话挤出视口。

### 3.4 Inspector

只保留 Agent 执行上下文：**计划**、**工具**、**时间线**。文件、树、Git、
任务、记忆、审计已移到侧栏活动栏，避免同一功能出现两套入口。

工具面板支持按运行中/成功/失败过滤、按名称或输出搜索；每个工具可展开
原始参数与结果 JSON 并复制。详情在事件到达时序列化一次，不在渲染时重复计算。

时间线记录当前运行的关键事件：开始、计划、步骤、工具、审批、验证、审查、
上下文裁剪、成本、完成/失败；每条带时间与摘要，不替代审计或 checkpoint。

### 3.5 全局搜索与通知

命令面板（`Ctrl/Cmd+Shift+P`）同时搜索命令、最近会话和工作区文件；
`Ctrl/Cmd+P` 直接进入 Quick Open（文件模式）：

- 支持 fuzzy 排序、分组标题、最近使用命令与快捷键提示；
- 前缀：`>` 仅命令、`@` 仅会话、`#` 仅文件；
- 输入至少 2 个字符后，前端防抖调用 `GET /api/search`（`#` 模式 1 个字符即可）；
- 后端只读检索路径与文本内容，跳过 `.git`、依赖目录、秘密文件与符号链接；
- sandbox 返回路径与内容命中，host 模式只返回路径命中；
- 单次最多 100 条结果、最多扫描 5000 个文件，并有 3 秒时间上限；
- 点击内容命中会打开文件并滚动到对应行。

通知中心（顶栏铃铛）保存最近 100 条站内通知：审批、完成、失败等；
未读数在铃铛上显示红点，支持全部已读与清空；通知持久化到
`localStorage`，点击可回到对应会话或文件。

### 3.6 设置

设置按“外观 / 模型 / 工作区 / 诊断 / 高级”分页，顶部搜索框按中英文关键词
过滤分节；搜索期间可同时看到多个匹配分节，无匹配时显示空态。切换分页会清空搜索。
外观支持主题、字体、字号、密度、对比度（标准/高）、强调色（蓝/紫/绿/琥珀/玫红）、
代码字号与代码行高；代码排版统一走 `--code-size` / `--code-line-height`。

设置面板使用左侧分类 + 右侧内容区，分为：

- **外观**：主题、字体、字号、密度、布局宽度重置、推理展示；
- **模型**：Provider 档案与模型密钥；
- **工作区**：项目探测、Python/环境信息、工作区注册表与 revisions；
- **诊断**：预算/通知、诊断包、更新检查；
- **高级**：认证方式、退出登录、租约与幂等键清理。

窄屏下分类导航变为横向标签条；内容区独立滚动。

---

## 4. 会话与工作区绑定

**一个会话永久绑定一个工作区**，这是产品级不变量。

绑定字段：

| 字段 | 含义 |
|---|---|
| `workspace_id` | 工作区注册表身份；host 会话必填 |
| `workspace_kind` | `host` / `sandbox` |
| `a2l_id` | host 工作区的 A2L 标识 |
| `root_handle` | host 绝对路径的客户端句柄；A2/A3 不持有路径 |
| `workspace` | sandbox 的运行时路径；host 会话留空 |

约束：

- 创建会话时写入 `control_sessions`（迁移 0006 持久化 kind/a2l/handle）；
  本地 catalog 丢失时从 `workspaces` 表恢复，**绝不把 host 会话退回 sandbox**。
- 在已打开会话里切换项目：前端清空 `thread_id` 并提示“发送时会新建会话”，
  后端也会拒绝改绑（409）。
- 没有 `workspace_id` 的旧 sandbox 会话：项目下拉清空，工作区标签显示原路径；
  不能沿用上一个会话的项目。
- host 项目在 UI 显示项目名（如 `web`），不显示 A2 侧的 sandbox 路径。
- 会话列表按工作区分组，方便把同一文件夹的历史集中在一起。

---

## 5. 事件与消息呈现

### 5.1 SSE 消费

`frontend/src/sse.ts` 负责：帧解析、`seq` 去重、`after`/重连参数。
前端只消费领域事件，不识别 LangGraph 内部结构。

流式渲染约束：

- 助手 token 先写入**非响应式缓冲**，每 **60ms** 节流刷新一次；
  不逐 token 触发 Alpine 更新，也不对整段文本反复做 Markdown 解析。
- 收到终止事件/流结束时 flush 缓冲；`run_finished` 前也会 flush，
  防止轨迹保存截断。
- 连接中断时按退避重连；`seq` 过滤避免重放重复内容。
- 同一轮里重复出现的完全相同的助手文本会在结束时去重；
  最终回答固定移动到 transcript 末尾，尾部的过程提示（如上下文裁剪）
  留在其上方。

### 5.2 消息类型与徽标

| 类型 | 徽标 | 视觉 |
|---|---|---|
| 用户提问 | 蓝色“你” | 右侧气泡 |
| Agent 过程 | 橙色虚线“工作中” | 弱化、虚线边框 |
| 最终回答 | 绿色“最终回答” | 绿色边框/浅绿背景，固定在最后 |
| 系统/步骤 | “过程” | 无大卡片，左侧细线 + 小字 |
| 工具调用 | 蓝色“工具” | 状态圆点、命令、输出、耗时、可折叠 |

其他呈现规则：

- 助手/用户消息超过约 **1200 字或 24 行**时折叠为 420px，提供
  “展开全文 / 收起全文”。
- 工具输出超过 **800 字**时折叠为 88px，提供“展开输出 / 收起输出”。
- 每条消息有 **复制** 按钮；窗口启用文本选择，`Ctrl+C` 可用；
  Clipboard API 失败时退回到选区复制。
- 工具卡片字段：名称、权限级别、状态（运行中/完成/失败/已拒绝/跳过/中断）、
  耗时或退出码、命令/参数摘要、输出预览。
- 代码块使用本地 highlight.js，只高亮一次，并提供复制按钮。

### 5.3 对话轨迹持久化

checkpoint 只保存 user/assistant 文本，不保存工具卡片与过程状态。为了让
“重新打开会话”仍能看到工作痕迹，桌面端在每轮结束时保存完整 UI 轨迹：

- `POST /api/thread/{thread_id}/transcript`
- 保存内容：user/assistant/system/tool 消息、phase、工具摘要/输出/状态。
- 存储：租户审计目录下 `transcripts/<thread>.json`；跨重启保留。
- 打开会话：`GET /api/thread/{id}` 返回 `transcript`，前端优先恢复；
  没有轨迹的旧会话回退到 checkpoint 文本。
- 限制：最多 500 条消息 / 约 4MB 文本；工具预览单条最多 80KB。
- 轨迹是桌面 UI 增强，不替代审计、checkpoint 或附件。

---

## 6. 授权与审批

### 6.1 工具审批

- `ApprovalRequested` 进入队列，用应用内模态逐条展示；默认拒绝，Esc 拒绝。
- 全部答复后一次 `POST /api/resume/{thread}`；恢复流里可能出现新的审批。
- 运行中/挂起时创建会话、切换项目、回滚等操作被禁用或拒绝。

### 6.2 本机文件授权（host 工作区）

桌面端不再弹独立的本机授权窗口，而是在**对话输出区**显示授权卡片：

- A2L 通过内部 token 把 `grant.request` 转发到 A2 的 `/internal/consent`；
- 前端每 **1.2s** 轮询 `/api/consent/pending`，在 transcript 底部渲染：
  操作、原因、访问路径、**允许本次 / 拒绝**；
- 决定写回 A2，A2L 轮询到结果后继续或拒绝；
- A3 按 `workspace_id` 缓存 grant（约 1700s），同一工作区后续读取不再重复
  弹卡片；grant 过期/撤销时自动重新请求；
- 敏感根本仍按策略逐次确认，不能靠长 TTL 宽 scope 复用；
- 未配置授权桥（CLI/测试）时回退到 Tk/Stdin 本地确认。

卡片只负责“显示与提交决定”，真正的授权判定仍在 A2L/策略层；前端不能
伪造 grant 或代替本地确认。

---

## 7. 输入、快捷键与复制

- `Enter` 发送；`Shift+Enter` 换行。
- **输入法保护**：`compositionstart/end`、`isComposing`、`keyCode 229`、
  composition 结束后 80ms 内的 Enter 都不提交，避免中文选词误发。
- `Ctrl/Cmd+P` Quick Open；`Ctrl/Cmd+Shift+P` 打开命令面板；
  `Ctrl/Cmd+K` 聚焦会话搜索；`Ctrl/Cmd+Shift+F` 搜索当前对话；
  `Ctrl/Cmd+B` 切换侧栏；`Ctrl/Cmd+J` 切换检查器；
  `Ctrl/Cmd+Enter` 发送；`Esc` 关闭命令面板/设置/恢复中心/快捷键；
  `?` 打开快捷键帮助。
- 命令面板支持键盘上下选择、Enter 执行、Esc 关闭；命令覆盖视图切换、
  布局、主题、刷新、设置、恢复中心、通知中心、最近会话与工作区文件。
- 文本选择默认开启；每条消息的复制按钮复制原始文本（工具卡片复制
  命令 + 输出），不复制渲染后的 HTML。
- 上传：选择或拖拽文件进入 `.agent/uploads/<thread>`，显示大小与配额；
  上传是用户动作，不自动执行。

---

## 8. 视觉设计系统

### 8.1 令牌与主题

`style.css` 中的 CSS 变量是唯一视觉事实源：

- 深色 / 浅色 / 跟随系统（`theme-dark|light|auto`）；
- 字号（small/normal/large）、密度（compact/normal）、字体（system/serif/mono）；
- 这些偏好存 `localStorage`（`codingagent.settings`），不存敏感信息。
- 表面层级 `--surface-0..3`、文本层级 `--text-1..3`、边框/语义色、
  间距 4/8、圆角、阴影与动效令牌；阶段/工具/授权卡片的颜色都从这些派生。
- 布局宽度与折叠状态单独存 `codingagent.layout`。

### 8.2 可访问性

- transcript 使用 `role="log"`、`aria-live="off"`、运行中带 `aria-busy`；
  独立 `role="status"` 播报区只播报阶段变化（开始/计划/步骤/审批/完成/失败），
  不逐 token 打扰屏幕阅读器；按钮/输入有 `aria-label`；页面顶部提供
  “跳到主区”skip link。
- 文件树支持 roving tabindex：↑↓ 移动、→ 展开、← 折叠、Enter/Space 打开；
  主区标签支持 ←/→/Home/End 切换，Delete/Backspace 关闭当前编辑标签。
- 主区/检查器标签使用 `role="tab"` + `aria-selected`；活动栏使用 `aria-pressed`；
  侧栏/检查器开关使用 `aria-expanded` + `aria-controls`。
- 命令面板使用 `aria-activedescendant` 指向当前命令项；toast 使用
  `role="status"` + `aria-live="polite"`。
- 所有可交互元素有 `:focus-visible` 轮廓；支持 `prefers-reduced-motion`。
- 命令面板、设置、快捷键、恢复中心等模态打开时把焦点移入，`Tab`/`Shift+Tab`
  在模态内循环；`Esc` 关闭后把焦点还给触发元素。
- 颜色不是唯一信息通道：状态同时有文字徽标/圆点/边框样式。
- 高对比度模式提高边框/次要文字对比度；强调色通过 body 内联 `--accent`
  覆盖主题默认值，代码字号与行高使用统一令牌。
- 窄屏（≤1100px）检查器改为右侧浮层；≤760px 活动栏横向、侧栏限高。

### 8.3 响应式与滚动

- 桌面窗口最大化；活动栏固定，侧栏与检查器可拖拽调宽/折叠。
- ≤1100px 检查器浮层化；≤760px 活动栏横向、侧栏限制高度。
- transcript 内部滚动；composer 固定在底部；长消息/工具输出可折叠。
- 设置面板、恢复中心、快捷键等使用模态层，内容可滚动，
  避免长内容把操作按钮挤出视口。

---

## 9. 状态、持久化与边界

| 状态 | 位置 | 说明 |
|---|---|---|
| 认证凭证 | 桌面 `sessionStorage`；目标 Web 模式仅内存，公网用 httpOnly Cookie | 当前全部是 Bearer；分离后按 `authMode` 分档，见 ADR-0017 |
| 运行时配置 | `/config.json` 或桌面注入 `window.__APP_CONFIG__` | 已实现；非法配置显示阻断页，默认同源相对基址 |
| 主题/字号/密度/最近工作区 | `localStorage` | 只放 UI 偏好，不放密钥/路径授权 |
| 会话配置 | `control_sessions`（DB）；`gui-sessions.json` 兜底 | 含不可变工作区绑定 |
| 完整 UI 轨迹 | `transcripts/<thread>.json` | 工具卡片/阶段/最终回答；仅桌面 UI 运行产生 |
| 运行事件 | A2/A3 内存缓冲 | 用于重连 `after`；不替代 checkpoint/审计 |

运行状态：`ready`、`busy`、`paused`、`approvals`、`connectionState`、
`lastSeq`。这些是 UI 状态，不构成权限事实。

---

## 10. 安全约束

- **CSP**：`default-src 'self'`、`script-src 'self'`、`connect-src 'self'`、
  `frame-ancestors 'none'`、`form-action 'self'`、`base-uri 'none'`。
  前端使用 Alpine CSP 构建，不需要 `unsafe-eval`；模板必须保持
  “属性访问/无参方法/委托事件”的表达式子集，守卫测试会拦截回归。
- **跨域**：`AGENT_CORS_ALLOWED_ORIGINS` 配置精确允许列表后启用；
  禁止 `*`/`null`/路径；A2 侧 `allow_credentials=False`（Bearer 形态），
  预检 204，401/426/413 等错误响应也带 CORS 头与 `Vary: Origin`。
- **认证**：Web 目标模式只把 access token 放内存；公网用 BFF + httpOnly
  Cookie + CSRF；桌面一次性 token 只对 loopback 有效，见 ADR-0017。
- 无 `/docs`；桌面只绑 loopback；`TrustedHostMiddleware` 限制 Host。
- **请求边界**：无 Content-Length 的 chunked 写请求 411；超过 body 上限 413；
  每用户限流 429；删除/改绑等操作服务端二次校验；错误体带稳定 `code` 与
  `request_id`。
- **不信任前端**：审批/授权/只读/工作区路径都由服务端或 A2L 重新判定。
- **XSS**：markdown 清洗后再 `x-html`；工具预览/路径作为文本渲染，
  不拼接 HTML；静态资源本地化。
- **敏感信息**：前端不读取 API Key、不展示密钥明文；复制只复制用户可见文本。

---

## 11. 性能约束

- 流式刷新 60ms 节流；token 缓冲不进入 Alpine 响应式系统。
- 流式 Markdown 做块级增量渲染：缓存空行分隔的稳定块，只重渲染尾部；
  未闭合代码围栏、列表/引用/表格等跨空行语法保守留在尾部，保证正确性。
- 长消息/工具输出默认折叠；`content-visibility: auto` 降低长列表成本。
- 工具输出按行分页：首次只渲染前 40 行，点“加载更多”每次增加 200 行，
  工具面板与 transcript 都不会把超大输出一次性塞进 DOM。
- `filteredTools()` 与 `virtualMessages()` 使用单槽缓存，只在版本/查询/窗口
  变化时重算；切换会话时清空缓存，避免逐 token 重复过滤。
- 会话列表最多加载 50 条；会话按工作区分组，避免一次渲染过多。
- 代码高亮只做一次；复制按钮/代码按钮不重复装饰。
- 长会话使用变量高度虚拟列表（实测高度 + overscan），只渲染视口附近消息；
  搜索命中会计算目标偏移并滚动到对应消息。
- 对话内搜索只在已加载消息上同步执行，不发后端请求；工具详情 JSON 在
  事件到达时生成一次。
- 流式输出默认跟随底部，但用户主动上滚后不再强制滚动，可用“跳到最新”恢复。
- 事件缓冲/重放有上限（A2 侧 8MB/5000 条/5 分钟，按实现为准）。
- 分离后静态产物按 hash 长缓存，`index.html`/配置 `no-cache`，API/SSE
  `no-store`；SSE 经过反代时禁缓冲、禁压缩并保留心跳。
- 已有 jsdom 交互测试覆盖 DOM 行为；尚无 Playwright 真实浏览器 E2E
  （pywebview 探针作为手工冒烟补充）。

---

## 12. 测试与验收

自动：

```bash
cd src/coding_agent/web/frontend
node node_modules/typescript/bin/tsc --noEmit
node esbuild.mjs
node test.mjs
node test-dom.mjs   # jsdom：加载 static 产物 + Alpine CSP，桩掉 API

pytest -m "not llm" tests/unit/test_frontend_workspace_ui.py \
       tests/unit/test_web_app.py \
       tests/unit/test_workspace_routes.py
```

手工 CSP 冒烟（需要 pywebview）：

```bash
python scripts/probe_frontend_csp.py
```

脚本启动桩 API、以严格 CSP 头加载实时静态产物，并验证初始化、活动栏切换、
设置分类、命令面板与主题命令。

覆盖点：构建产物包含项目/工作区流程；页面无构建链；静态资源可访问；
安全头/CSP；SSE 事件流；审批/steering/恢复端点；工作区绑定与
transcript 保存。`node test.mjs` 覆盖 diff 解析/split/词级标记、查询匹配/
fuzzy/前缀解析、虚拟列表区间、Git 状态映射、Problems 聚合、增量 Markdown、
会话摘要与焦点循环等纯函数。`node test-dom.mjs` 用 jsdom 真实加载
`static/index.html`、vendored Alpine CSP 与 `app.js`，桩掉后端 API 后验证
会话未读/摘要/批量选择、文件树 Git 标记与键盘导航、Problems 折叠、流式渲染、
大工具输出分页与派生缓存、阶段播报、主题设置、模态焦点陷阱、审批批准/全部拒绝、
侧栏/检查器拖拽与宽度持久化、图片预览与只读会话分屏，并把 Alpine 表达式错误
作为失败。

分离改造实施后新增契约测试（设计冻结，锚点见验收 `REQ-202`–`REQ-212`）：

- 同一产物在桌面/同域反代/独立源站/BFF 四种拓扑启动；
- 运行时配置非法时阻断，版本不匹配时阻断；
- Bearer 刷新/注销/单飞，Cookie 模式 CSRF；
- CORS 允许/拒绝/错误响应矩阵；
- 真实反代后的 SSE 空闲、心跳与重连；
- 上传预检、限额与幂等；
- 桌面桥 origin/iframe 拒绝与浏览器模式 403；
- request ID 端到端关联。

手工桌面清单（每次改 UI 或桌面壳后）：

1. 选择项目/添加本机工作区；确认文件夹选择窗口出现在最前。
2. 新建会话、发问、第一次文件读取出现对话内授权卡片；允许后继续。
3. 同一工作区再读文件不重复弹卡片。
4. 过程中显示“工作中”、工具卡片；结束后最终回答在最后。
5. 切换会话/项目，确认工作区绑定跟随会话且不可改绑。
6. 长回答/长工具输出可展开；每条消息可复制。
7. 关闭窗口：窗口立即消失，无“未响应”；后台进程清理完成。

---

## 13. 已知限制与技术债

| 限制 | 影响 | 备注 |
|---|---|---|
| 顶栏右侧按钮在部分 DPI 下不绘制/被裁切 | 设置入口不可见 | 侧栏已提供设置/+ 本机兜底 |
| 无浏览器自动化/JS 单测 | 交互回归靠手测 | 构建产物守卫只能防漂移 |
| 轨迹只在桌面 UI 运行时保存 | 旧会话/CLI 运行无工具卡片 | 不替代 checkpoint/审计 |
| 轨迹优先于 checkpoint | CLI 追加的运行可能被旧轨迹遮住 | 桌面单用户场景可接受 |
| 无虚拟列表 | 超长会话可能变慢 | 折叠/`content-visibility` 缓解 |
| 授权桥仅桌面模式 | Web/CLI 仍走本地确认或拒绝 | `AGENT_CONSENT_TOKEN` 门控 |
| 前后端分离九步已落地 | 独立静态产物、版本契约与回滚步骤已就绪；真实部署 E2E 待补 | 见 RFC-0003 与 RELEASING.md；公网部署前仍需完成部署侧验证 |
| pywebview 目录对话框需强制前台 | 个别环境可能被遮挡 | 已用原生 shell32 + topmost |

---

## 14. 文件地图

| 路径 | 职责 |
|---|---|
| `web/frontend/src/app.ts` | 初始状态与各域 mixin 的组合入口 |
| `web/frontend/src/features/*.ts` | 按域拆分的 Alpine mixin（会话/工作区/运行/检查器等） |
| `web/frontend/src/features/csp.ts` | CSP 兼容层：delegated actions 与无参 getter |
| `web/frontend/src/api/http.ts` | 认证、fetch、401 刷新与 problem+json 解析 |
| `web/frontend/src/ui/format.ts` | 展示格式化与 Markdown 清洗 |
| `web/frontend/src/ui/diff.ts` | diff 解析、并排视图配对与词级标记 |
| `web/frontend/src/ui/search.ts` | 查询匹配、fuzzy 排序与命令前缀解析 |
| `web/frontend/src/ui/virtual.ts` | 变量高度虚拟列表区间计算 |
| `web/frontend/src/ui/git.ts` | Git porcelain 状态归一与文件树标记 |
| `web/frontend/src/ui/problems.ts` | Problems 按文件/来源聚合 |
| `web/frontend/src/ui/markdown.ts` | 流式 Markdown 稳定块切分与增量渲染 |
| `web/frontend/src/ui/focus.ts` | 模态 Tab 循环计算 |
| `web/frontend/src/ui/sessions.ts` | 会话列表摘要与元信息格式化 |
| `web/frontend/src/ui/output.ts` | 大工具输出按行分页 |
| `web/frontend/src/ui/memo.ts` | 派生列表单槽缓存 |
| `web/frontend/src/ui/settings.ts` | 设置分节关键词搜索 |
| `web/frontend/src/ui/theme.ts` | 强调色预设与主题 body class |
| `web/frontend/src/features/errors.ts` | 全局错误横幅与重试动作 |
| `web/frontend/src/features/focus.ts` | 模态焦点进入/陷阱/恢复 mixin |
| `web/frontend/tests/*.test.ts` | 纯函数单元测试（`node test.mjs`） |
| `web/frontend/tests/dom/*` | jsdom 交互测试（`node test-dom.mjs`） |
| `web/frontend/src/state/runtime.ts` | 非响应式 Map 缓冲 |
| `web/frontend/src/contracts/events.generated.ts` | 由契约生成的事件 union（禁止手改） |
| `web/frontend/src/templates/*.html` | 页面模板分片（构建期拼成 `static/index.html`） |
| `web/frontend/src/styles/*.css` | 设计令牌与样式分片（构建期拼成 `static/style.css`） |
| `web/frontend/build-ui.mjs` | 模板/样式拼装脚本 |
| `web/frontend/src/runtime-config.ts` | 运行时配置加载/校验、API 基址拼接 |
| `web/errors.py` | problem+json 错误协议与 request id |
| `request_context.py` | request id ContextVar（A2→A3 透传） |
| `sse_stream.py` | SSE 心跳、注释帧与响应头（A2/A3 共用） |
| `web/frontend/src/sse.ts` | SSE 解析、seq 去重 |
| `web/frontend/src/main.ts` | 入口与 Alpine 启动 |
| `web/static/index.html` | 页面结构与 Alpine 指令 |
| `web/static/style.css` | 主题令牌、布局、消息/工具/授权卡片 |
| `web/static/app.js` | esbuild 生成物（不要手改） |
| `web/routes/runs.py` | run/SSE/resume/history/transcript 接口 |
| `web/consent_bridge.py` | 桌面对话内授权桥 |
| `host_broker/consent.py` | A2L 本地/远程授权提供者 |
| `host_broker/workspace_registry.py` | 本机目录选择与持久化 |
| `desktop/launcher.py` | pywebview 窗口、服务生命周期、关闭清理 |
| `desktop/api.py` | 桌面桥能力、origin/顶层窗口校验与拒绝日志 |
| `desktop/supervisor.py` | A2/A3/A2L 启停与超时 |
| `tests/unit/test_frontend_workspace_ui.py` | 构建产物/页面守卫 |
| `tests/unit/test_web_app.py` | Web 端点与流式契约 |

---

## 15. 前后端分离形态（设计冻结，九步已实现）

本节是 [RFC-0003](rfc/0003-frontend-backend-separation.md) 的前端摘要；
契约细节与验收矩阵以 RFC 为准。当前代码仍是同源相对基址、Bearer +
`sessionStorage`、无 CORS。

### 15.1 拓扑

| 形态 | 前端托管 | 认证默认 | 浏览器跨域 | 桌面桥 |
|---|---|---|---|---|
| D 桌面本地 | 本地静态 + loopback | `desktop-token` | 否 | 有 |
| B 同域反代 | 静态站/边缘，`/api` 反代 | `bearer` | 否 | 无 |
| A 独立源站 | 静态站/CDN，独立 API 源 | `bearer` | 是 | 无 |
| C 公网 BFF | BFF 同源 | `bff-cookie` | 否（浏览器只到 BFF） | 无 |

迁移顺序：D 不变 → B → A → C。

### 15.2 运行时配置

- 启动时加载 `/config.json`（或桌面注入 `window.__APP_CONFIG__`），
  校验 `schemaVersion`、`apiBaseUrl`、`apiVersion`、`authMode`、SSE 参数；
- 默认空基址 = 同源，桌面与同域反代零配置；独立源站只改配置；
- 配置不含密钥和业务开关；能力仍以 `/api/capabilities` 为准；
- 配置非法/加载失败显示阻断页，不显示登录页；
- fetch/SSE/XHR 上传统一走同一 URL 拼接函数，禁止裸 `/api` 字面量。

### 15.3 认证

- 桌面：每次启动随机 token，URL fragment 传入后立即清除；内存优先，
  reload 时用 `sessionStorage` 兜底，只对 loopback 有效；
- 内网 Bearer：access token 只放内存，不写任何持久存储；401 单飞
  `POST auth.refreshUrl`，响应 `{access_token}` 后重试一次；
  重试非幂等请求复用原 `Idempotency-Key`；
- 公网 BFF：不发送 `Authorization`，请求带 `credentials: include`；
  写请求若存在 `csrf_token` 或 `XSRF-TOKEN` Cookie，则按
  `auth.csrfHeader`（默认 `X-CSRF-Token`）回填；登录/刷新/注销 URL
  由运行时配置提供，BFF 本体由部署侧实现；
- 401 触发统一认证失效处理：Bearer/桌面回到 token 登录，
  BFF 跳转 `auth.loginUrl`；403 不触发刷新；
- SSE 与普通请求共用认证层；断流不等于 401，保留 `lastSeq` 后续传；
- 注销清空内存/桌面 token、重置会话；BFF 注销后跳登录页；
- 前端隐藏按钮不构成授权，服务端仍判定工作区/审批/租约。

### 15.4 错误、请求 ID 与版本

- 错误体使用 `application/problem+json`，稳定 `code` + `request_id`；
  已覆盖 HTTPException、参数校验、BodyLimit 提前返回与未处理异常；
- 客户端只依赖 `code`，不解析 `detail` 文案；错误提示会带
  `code · request_id`，便于对照服务端日志；
- 全链路透传 `X-Request-ID`：A2 生成/校验后回写响应头与错误体，
  内部调用通过 context var 传给 A3；SSE 首个注释帧也带；
- `/healthz` 返回 `api_version` 与 `min_frontend_version`；
  不兼容时阻断；请求带 `X-Frontend-Version`。
- 服务端对携带非法或过旧 `X-Frontend-Version` 的 `/api/*` 请求返回
  426 `version.frontend_too_old`；未携带该头的旧客户端在升级窗口内放行。
- 前端版本常量在 `frontend/src/version.ts`；服务端在
  `web/version.py` 维护 `api_version/min_frontend_version/capabilities_version`，
  发布时需同步。

### 15.5 CORS、SSE 与上传

- CORS 只用于形态 A：由 `AGENT_CORS_ALLOWED_ORIGINS` 配置精确来源，
  非法条目启动失败；禁止 `*`/`null`/路径；A2 侧不带凭证；
  预检 204；CORS 位于 RequestId 内层、业务外层，
  401/403/413/426/429/5xx 也带头；允许并暴露 `X-Request-ID`。
- SSE：fetch + Authorization（不换 EventSource）；服务端每 15s 发
  `: keepalive` 注释帧，A2→A3 代理流也由 A2 注入心跳；响应头固定
  `Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`、
  `Connection: keep-alive`；前端 45s 收不到任何字节会主动断开并按 `seq` 重连；
  反代必须禁缓冲、禁压缩，读超时大于心跳；代理不重试已发送的流；
  多实例用共享传输或粘性。
- 上传：会话上传带 `Idempotency-Key`；控制面数据库可用时，同 key 重试
  返回同一结果而不重复落盘（未配置 DB 时幂等不生效）；
  单文件、文件数与工作区累计限额分别校验，413 使用
  `upload.file_too_large` / `upload.too_many_files` / `upload.total_too_large`
  并带 `limit/received/filename/current` 等 details；
  请求体上限按累计配额 + 1MiB 余量，单文件仍逐项校验；
  大归档优先预签名直传；扫描在解压前完成；413/429 带 CORS 与 request_id。

### 15.6 桌面桥

- 桌面只加载本地 loopback 页面；`launcher` 把 `control.url` 的 origin
  传给 `DesktopApi`，启动前再次校验必须为 loopback；
- `DesktopApi` 每个公开方法调用前用 `get_current_url()` 校验当前 origin
  精确匹配；无法证明来源（无 window、URL 非法、远程 origin）时 fail closed；
- 前端包装 `window.pywebview.api`，`window.top !== window` 的 iframe 调用
  直接拒绝；桥对象不可改写时由 Python origin 校验兜底；
- 浏览器模式桌面专属端点返回 403 `desktop.unavailable`。不靠前端隐藏按钮；
- 桥保持窄能力，路径规范化 + 允许根；更新只打开下载页、不自动安装，
  诊断包脱敏；
- 远程前端若确有需求，只能用无桥 webview + 本地 A2L，不能复用页面桥。

### 15.7 实施顺序

1. 基址注入 + 配置校验 + 阻断页；
2. 版本握手；
3. 认证适配（桌面/Bearer/BFF）；
4. 错误协议 + request ID；
5. CORS 允许列表；
6. SSE 心跳与代理规则；
7. 上传预检与限额；
8. 桌面桥 origin 绑定；
9. 独立发版与契约测试。

前两步完成前，不启用跨域或 Cookie 模式。

### 15.8 SSE 反代样例

Nginx：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8780;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 75s;
    proxy_next_upstream off;
    gzip off;
}
```

Caddy：

```caddy
reverse_proxy 127.0.0.1:8780 {
    flush_interval -1
}
```

注意：

- `text/event-stream` 永不压缩；否则代理会攒批，前端看起来"卡住"；
- 读超时必须大于 15s 心跳（示例 75s）；
- `Connection ""` 让上游保持 keep-alive；
- 不要对非幂等请求做代理层自动重试；
- 边缘如生成 `X-Request-ID`，必须是 `[A-Za-z0-9._:-]{8,128}`。

当前状态：九步已实现（运行时配置/基址注入、配置失败阻断、
`/healthz` 版本握手、认证适配/单飞刷新、错误协议与 request ID、
CORS 允许列表、SSE 心跳与代理规则、上传限额与幂等、桌面桥 origin 绑定、
独立静态产物与发版契约测试）。发版与回滚步骤见
[RELEASING.md](RELEASING.md)。
