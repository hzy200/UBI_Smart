---
layer: L3
doc_id: frontend
title: 前端功能、交互与 UI 约束
owner: 项目作者
status: active
last_reviewed: 2026-11-04
review_cadence: 前端交互、事件契约、权限模型或桌面壳变动时
---

# 前端功能、交互与 UI 约束

本文描述 `web/` 前端的**当前实现**：功能边界、页面结构、消息呈现、
会话/工作区绑定、授权交互、视觉规范、性能与测试约束。桌面壳的启动、
端口、锁与后端接口见本文 §2；领域事件契约以
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
  DOMPurify、mammoth、SheetJS、pdf.js），运行时不依赖 CDN，也没有前端打包服务器。
  文档 viewer 库按需动态加载并带 SRI；PDF worker 从同源 `/static/vendor/` 加载。

### 构建链

```text
web/frontend/src/*.ts            --esbuild------>  web/static/app.js   (IIFE)
web/frontend/src/templates/*.html --build-ui.mjs--> web/static/index.html
web/frontend/src/styles/*.css     --build-ui.mjs--> web/static/style.css
web/frontend/src/admin/*.tsx      --esbuild------>  web/static/admin/app.js  (React IIFE)
web/frontend/src/admin/admin.css  --esbuild------>  web/static/admin/app.css
web/frontend/src/admin/index.html --build-ui.mjs--> web/static/admin/index.html
web/static/vendor/*                                (直接由 A2 提供)
```

管理端是**独立静态入口**：只从站点根 `config.json` 读取 `apiBaseUrl`，
数据全部经 `/api/admin/*`，不依赖服务端模板注入；同域反代与独立静态站
两种部署都可用。发布包会把 `admin/index.html` 放到发布根目录。

用户端与管理端的成本显示统一走运行时配置的 `cost` 段：
`{"currency":"CNY","usdToCny":7.2}`；上游事件仍携带 `cost_usd`，前端只做
展示换算，配额判断由服务端完成。

### 管理端

`src/admin/` 使用 React 19 + TSX，esbuild 直接打包；不引入 UI 库/路由库，
用简单状态路由。管理端与用户端共用 `src/i18n/zh-CN.ts` / `en-US.ts` 词表
（`admin` 分节），顶栏可切换 中文/EN，偏好写入同一个
`codingagent.settings.locale`。页面：

| 页面 | 功能 |
|---|---|
| 登录 | 企业账号密码；HttpOnly Session Cookie + CSRF |
| 用户 | 搜索/状态筛选、新建、禁用/启用、最近登录 |
| 用户详情 | 今日用量进度、每日 token/人民币额度、上游 Key 加密分配、登录安全事件、重置密码 |
| 用量 | 近 7/30/90 天、按用户/天汇总、token/人民币成本/运行次数；每日成本/tokens 柱状图、CSV 导出 |
| 审计 | 组织成员的审计索引；按用户/线程/类型/工具/结果/时间过滤，分页；最多导出 5000 条 CSV |
| 工作区 | 组织内工作区归属、状态与用量；只读共享的授予/撤销（不授予执行/写入） |
| 系统 | 控制面数据库连通性、活跃运行/SSE/待审批/认证/审计写失败等指标，10s 刷新 |
| Provider | 部署环境可用的 provider 列表与 Key 配置状态 |
| 审批 | 待审批队列 + 历史；SSE 推送（`/api/admin/approvals/stream`），不可用时回退 5s 轮询；支持批量批准/拒绝、历史筛选与 CSV 导出 |

约束：

- 不写内联 `<style>`/`<script>`，复用 `script-src 'self'` CSP；
- 管理员权限只在服务端判定；前端隐藏入口不构成授权；
- `admin/index.html`、`admin/app.js`、`admin/app.css` 都是生成物；
- 发布哈希需同时重写根 `index.html` 与 `admin/index.html`；
- Service Worker 对 `/admin` 强制网络，不回退用户端外壳。

- `app.js`、`index.html`、`style.css` 都是生成物，**不要手改**；
  改 `frontend/src` 后必须重建（`node esbuild.mjs` 会同时构建三者）。
- `features/*.ts` 是按域拆分的 Alpine mixin；`app.ts` 只保留初始状态与组合。
  mixin 必须用 `Object.defineProperties` 组合，不能用对象展开——展开会在
  构造时求值 getter，导致 CSP 构建下失去响应性。
- `contracts/events.generated.ts` 由 `scripts/gen_contracts.py` 从
  `src/ubismart/events.py` 生成，禁止手改。
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

### 2.1 安装、启动与桌面专属动作

```bash
conda activate agent
pip install -e ".[web,desktop]"          # 桌面窗口
pip install -e ".[web,desktop,secrets]"  # 需要在 GUI 里注入 API Key 时

agent gui                     # 首次运行打开 WSL 配置向导
agent gui --setup             # 重新配置
agent gui --write             # 允许会话选择审批写/自动写
agent gui --workspace D:\proj # 覆盖 WSL 工作区（转成 /mnt/d/proj）
agent gui --host-mode         # 旧 Windows 宿主模式
```

WSL 模式下端口由 server 自动分配并通过握手返回；`--port` 只影响 host mode。
只监听 `127.0.0.1`，每次启动生成随机 token；token 放在 URL fragment，不进入
服务端日志。`WorkspaceLock` 做单实例锁。

设置页“工作区与 Python”展示：WSL 原生或 `/mnt` 路径、项目 Python
（`.venv` → `venv` → `env` → 系统 `python3`）与 Agent Python、包管理器与锁文件、
建议测试命令与警告。桌面专属动作（浏览器模式自动隐藏）：Windows 原生文件夹
选择、`code --remote wsl+<distro>` 打开、`/mnt` → WSL 原生目录复制（拒绝覆盖）。
探测只读：不创建 venv、不安装依赖、不执行项目脚本。

会话栏支持搜索/改名/删除（tombstone）/导出 Markdown/置顶/归档/标签；实时分屏
各自接收 SSE；文件树带 Git M/A/D 标记、单文件最多 256 KB、图片 ≤2MB 预览；
Git 面板只读；文件快照可接受或拒绝（回滚）；上传区显示配额；恢复中心列出
待审批/暂停/运行中/异常中断会话。设置页可生成脱敏诊断包、检查更新（只提示，
不自动安装）；WSL server 意外退出时按 1s/3s/7s 退避最多重启 3 次。

### 2.2 桌面接口

```text
GET    /api/health
GET    /api/workspace
GET    /api/providers            POST /api/providers/{name}/key
POST   /api/providers/test       DELETE /api/providers/{name}/key
GET    /api/sessions             POST /api/sessions
POST   /api/sessions/{thread}/model
POST   /api/sessions/{thread}/uploads
GET    /api/capabilities?thread_id=
POST   /api/run                  POST /api/resume/{thread}
POST   /api/resume_steering/{thread}
POST   /api/steer/{thread}       GET  /api/approvals/{thread}
GET    /api/status/{thread}      GET  /api/stream/{thread}?after=<seq>
POST   /api/desktop/cancel_all（仅桌面模式）
GET    /api/thread/{thread}
GET    /api/snapshots?thread_id= GET  /api/diff?thread_id=
POST   /api/restore              GET  /api/checkpoints/{thread}
POST   /api/fork/{thread}        POST /api/rewind/{thread}
GET    /api/memory               POST /api/memory
GET    /api/library/items?limit=&offset=&status=&tag=&q=
POST   /api/library/items
GET    /api/library/items/{id}
GET    /api/library/items/{id}/detail
PATCH  /api/library/items/{id}
DELETE /api/library/items/{id}
POST   /api/library/items/{id}/reindex
POST   /api/library/reindex-all
GET    /api/library/jobs         GET  /api/library/jobs/{job_id}
POST   /api/library/jobs/{job_id}/cancel
GET    /api/library/search?q=&mode=&tags=
GET    /api/audit                POST /api/context/{thread}/compact
```

桌面端所有 `/api/*` 都要求随机 Bearer token；Web 端按 `AuthConfig` 处理。
接口按会话能力与租户授权；前端不能通过请求体自行提升权限。

**安全边界**：GUI 只消费 `ApprovalRequested`，没有直接调用工具的能力；只读在
服务端强制；密钥只进密钥环，前端只看到 `has_key`；上传目录 `.agent/uploads`
不自动执行；模型输出经 `marked + DOMPurify` 清洗；本地服务只绑 loopback、
随机 token、`TrustedHostMiddleware` 限制 Host，无 CORS、无 `/docs`。

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
│    │ 资料库/审计   │                               │                 │
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
  每个会话可“分屏”打开实时视图，与主区并行运行；可对同一会话的两次运行
  做“对比”（并排用量与逐文件 diff），也可创建只读分享链接；
  支持置顶、归档、标签与“全部/置顶/归档”筛选。列表显示最后一条消息
  摘要、轮次/工具数与时间；未读会话有圆点与加粗标题，打开后自动已读，
  也可手动标记已读/未读。勾选多个会话后可批量归档、取消归档或删除。
  归档只影响列表，审计与 checkpoint 保留。活跃与归档分两次加载，归档不会
  占用活跃列表的名额；列表底部“加载更多”按 50 条递增。
- **文件改动**：快照、diff、检查点、逐文件/整轮接受与回滚；可下载本轮
  合并补丁（`GET /api/sessions/{thread_id}/patch`，`git apply` 可直接使用）。
- **文件树**：host 工作区经 A2L 读取；sandbox 经 A3；按目录懒加载、
  可展开/折叠、刷新与收起全部；支持过滤已加载条目与复制相对路径；
  桌面 sandbox 模式可在文件标签中调用 VS Code 打开。加载时顺带读取
  `/api/git`，为文件显示 M/A/D/R/U 标记，目录显示聚合改动圆点；
  Agent 改动文件后先本地标 M，刷新时再与真实状态对齐。
- **Git**：只读分支/改动/提交/diff；提交仍由 Agent + 审批完成。
- **任务**：编排任务 DAG、worker、依赖、耗时与 token。
- **记忆 / 审计**：查询与维护操作；删除/清理需显式确认。
- **资料库**：上传 PDF/DOCX/PPTX/Markdown/TXT/XLSX/HTML/EPUB/CSV，显示条目状态、大小与
  索引块数；上传/重建是异步任务，页面在有条目处于 queued/processing 时
  自动轮询，显示阶段与进度，可取消任务；支持单条/全部重建、删除、标签编辑、
  状态/标签/名称筛选与分页，并可展开条目详情（outline + chunk 片段）；提供
  lexical/vector/hybrid 检索自测框。资料库按用户跨工作区保存；模型侧仍只
  通过 `library_search` 主动检索。
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

文档阅读器按类型分派：Markdown 经 marked + DOMPurify 渲染；PDF 用按需加载的
pdf.js 逐页渲染到 canvas，并叠加 SVG 文本层（可选中）、支持翻页、缩放、
全文搜索（命中文本标黄）与连续滚动（按批懒加载，不设固定页数上限），
同时展示 PDF 元数据与书签大纲（可跳页），失败时退回内置 viewer；DOCX 用 mammoth 转 HTML 后经 DOMPurify，
并额外展示服务端解析出的页眉/页脚/分节信息、脚注与批注（`.docx-extras`）；
XLSX/XLS 用 SheetJS 解析并支持 sheet 标签切换、合并单元格、大表截断提示，
点击单元格显示公式/值（`.xlsx-formula`）；大表按滚动窗口只渲染可视行，
上下占位高度通过 CSSOM 命令式设置（CSP `style-src 'self'` 会丢弃内联
`style` 属性）；
PPTX 经 `/api/file/document` 读取 python-pptx 提取的形状几何、文本、表格与内嵌图片，
生成绝对定位 SVG 版式（`.slide-svg`，不依赖 LibreOffice，也不还原像素级字体/阴影），
可展示内嵌图片。旧版 `.doc/.ppt` 不做转换，明确提示先另存为 docx/pptx。
实现按格式拆在 `features/viewers/`：`registry` 分派，`basic/pdf/docx/sheet/
document/table` 各自负责一种能力，`inspector.ts` 只保留 fileView 状态与调用。
vendored 库按需动态加载并带 SRI；raw 端点支持单段 `Range`，CSP 额外放行
`frame-src 'self' blob:` 与 `worker-src 'self'`。SVG 只按源码展示，不进入
`<img>`。注意 CSP 为 `style-src 'self'`，内联 `style` 属性会被浏览器丢弃；
需要动态高度/位置时用 `element.style`（CSSOM）而不是模板 `:style`。
主区可叠放一个实时分屏：从会话操作或命令面板打开另一个会话的独立
`SessionView`，两侧各自接收 SSE、显示状态并可独立发送任务；分屏收到审批时
直接展示内联审批卡，可在分屏内批准/拒绝/全部拒绝，无需切回主区。关闭分屏
不会停止后台流，会保留会话视图。
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
上下文裁剪、成本、预估卡、阶段完成、熔断/故障、完成/失败；每条带时间与摘要，
不替代审计或 checkpoint。当前卡片采用“时间线/提示”级接入；完整卡片视图
可按 `card_id` 升级为幂等 upsert。

### 3.5 全局搜索与通知

命令面板（`Ctrl/Cmd+Shift+P`）同时搜索命令、最近会话和工作区文件；
`Ctrl/Cmd+P` 直接进入 Quick Open（文件模式）：

- 支持 fuzzy 排序、分组标题、最近使用命令与快捷键提示；
- 前缀：`>` 仅命令、`@` 仅会话、`#` 仅文件；
- `@` 输入至少 2 个字符后防抖调用 `GET /api/sessions/search`，检索会话
  标题、最后消息、transcript 正文与审计索引（kind/tool/level/decision）；
  SQLite 正文走 FTS5 trigram，短查询/MySQL 回退 LIKE；结果带 `match`
  （title/transcript/audit）与片段；
- `#` 或全部模式输入至少 2 个字符后防抖调用 `GET /api/search`（`#` 模式 1 个字符即可）；
- 后端只读检索路径与文本内容，跳过 `.git`、依赖目录、秘密文件与符号链接；
- sandbox 返回路径与内容命中，host 模式只返回路径命中；
- 单次最多 100 条结果、最多扫描 5000 个文件，并有 3 秒时间上限；
- 点击内容命中会打开文件并滚动到对应行。

通知中心（顶栏铃铛）保存最近 100 条站内通知：审批、完成、失败等；
未读数在铃铛上显示红点，支持全部已读与清空；点击可回到对应会话或文件。
控制面数据库启用时，通知以 `control_notifications` 为事实源，通过
`GET /api/notifications/stream`（SSE）推送快照；`localStorage` 只做离线缓存。
没有控制面时退回纯本地通知。

配置了 VAPID 公/私钥并安装 `.[webpush]` 时，前端在用户已授权通知后
订阅 Web Push：`GET /api/push/config` 取公钥，`POST /api/push/subscriptions`
保存设备订阅；Service Worker 的 `push` 事件负责页面关闭时的审批待办、
运行完成/失败提醒，点击回到对应会话。同一账号的每台设备各自订阅，
410/404 的失效订阅由服务端清理。未配置时自动降级，不影响站内通知。

### 3.6 运行对比与只读分享

- **运行对比**：`GET /api/sessions/{id}/runs` 按审计 RUN_START/RUN_END
  切分运行；选择两次运行后，`/runs/compare` 返回用量（tokens/成本/工具
  调用）与逐文件 diff。diff 取该运行内每个文件最早的 `snapshot_id`，
  与“本轮补丁”的口径一致；单次最多 30 个文件。
- **只读分享**：创建时从 transcript 生成**已脱敏、冻结**的快照：
  去掉 system 消息与工具原始输出，替换 workspace/常见绝对路径并脱敏秘密；
  只保留 user/assistant 文本与工具摘要。token 只存 sha256，默认 7 天
  （1–30 天），可随时撤销。匿名页面 `/share/{token}` 只渲染转义文本，
  带 `noindex` 与 `no-store`；不存在/过期/撤销统一 404。

### 3.7 设置

设置按“外观 / 模型 / 工作区 / 诊断 / 高级”分页，顶部搜索框按中英文关键词
过滤分节；搜索期间可同时看到多个匹配分节，无匹配时显示空态。切换分页会清空搜索。
外观支持主题、字体、字号、密度、对比度（标准/高）、强调色（蓝/紫/绿/琥珀/玫红）、
代码字号与代码行高；代码排版统一走 `--code-size` / `--code-line-height`。

设置面板使用左侧分类 + 右侧内容区，分为：

- **外观**：主题、字体、字号、密度、布局宽度重置、推理展示；
- **模型**：Provider 档案与模型密钥；
- **工作区**：项目探测、Python/环境信息、工作区注册表与 revisions；
- **诊断**：预算/通知、诊断包、更新检查；
- **高级**：认证方式、退出登录、个人访问令牌（read/run/audit scope，
  明文只显示一次）、出站 Webhook（HTTPS + 事件订阅，secret 只显示一次、
  HMAC-SHA256 签名）、租约与幂等键清理。

窄屏下分类导航变为横向标签条；内容区独立滚动。

### 3.8 窄屏三屏模式

≤760px 时隐藏活动栏/检查器/状态栏，底部提供三个固定入口：

- **会话**：侧栏会话列表全屏；
- **对话**：主区 transcript + composer 全屏，打开会话后自动切入；
- **审批**：有待审批时打开审批模态，否则提示“当前没有待审批请求”。

底部入口带未读会话数与待审批数徽标。桌面布局不受影响；PWA/Web Push
在页面关闭时仍可提醒审批与运行完成/失败，作为完整移动端的轻量替代。
已在移动网络/窄屏下使用单列布局，对话内长内容沿用折叠与虚拟列表。

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
- 通知面事件 `card` 与领域事件共用同一条 SSE 流；后端提供
  `card_id/event_id/plan_version`，前端按 `plan_version` 丢弃旧审批卡。
  当前卡片以时间线/提示级消费，完整 `card_id` upsert 待补（见 §13）。
  卡片先于终端事件发出，`RunFinished / RunFailed / ApprovalRequested / pause`
  仍是最后一帧。
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
- 保存轨迹时同步更新 `control_session_search`（正文最多 20k 字符），
  供命令面板 `@` 跨会话搜索；保存失败不影响轨迹本身。
- 轨迹是桌面 UI 增强，不替代审计、checkpoint 或附件。

---

## 6. 授权与审批

### 6.1 工具审批

- `ApprovalRequested` 进入队列，用应用内模态逐条展示；默认拒绝，Esc 拒绝。
- 事件带 `plan_version` / `ack_timeout_ms` / `default_action`；计划版本变化时
  前端丢弃旧审批卡，审批卡显示超时与默认动作提示。
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
- 字号（small/normal/large）、密度（compact/normal）、字体（system/serif/mono）、
  高对比度、强调色（5 个预设）、代码字号与代码行高；
- 这些偏好存 `localStorage`（`codingagent.settings`），不存敏感信息。
- 表面层级 `--surface-0..3`、文本层级 `--text-1..3`、代码字号/行高、边框/语义色、
  间距 4/8、圆角、阴影与动效令牌；阶段/工具/授权卡片的颜色都从这些派生。
- 对话 Markdown 有独立 prose 层级；代码块带语言/行数标题栏；文件树使用
  类型图标与层级引导线；Toast 堆叠并按语义着色。
- 侧栏、检查器与分屏宽度都存 `codingagent.layout`；分屏可拖拽，活跃 pane
  使用 accent 边框。

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

会话运行态（消息、计划、工具、审批、用量、流式游标等）归属 `SessionView`
（`state/session-view.ts`）。`sessionViews[threadId]` 复用视图：重复打开会话不会
重置消息、计划、审批或草稿，只刷新工作区相关 UI；最近最多保留 6 个视图，正在跑流
的视图不会被 LRU 淘汰，删除会话会 abort 其请求并释放视图。

每个会话的 SSE 在发起时绑定自己的 view：`consume(response, view)` 的事件只落到
该视图，即使用户切走也继续接收。切换会话不再被 busy/审批阻挡；后台完成、失败或
等待审批会通过通知中心与未读标记提示。`applyEvent` 用 `withView` 临时切换上下文，
后台事件不会滚动/播报/切标签或改动编辑器标签；token 定时器捕获所属 view。

| 状态 | 位置 | 说明 |
|---|---|---|
| 认证凭证 | 桌面 `sessionStorage`；目标 Web 模式仅内存，公网用 httpOnly Cookie | 当前全部是 Bearer；分离后按 `authMode` 分档，见 ADR-0017 |
| 运行时配置 | `/config.json` 或桌面注入 `window.__APP_CONFIG__` | 已实现；非法配置显示阻断页，默认同源相对基址 |
| 主题/字号/密度/对比度/强调色/代码排版/最近工作区 | `localStorage` | 只放 UI 偏好，不放密钥/路径授权 |
| 会话视图 | `sessionViews[threadId]`（内存） | 消息/计划/工具/审批/用量/流式游标；最多 6 个，LRU 跳过运行中与分屏视图 |
| 会话配置 | `control_sessions`（DB）；`gui-sessions.json` 兜底 | 含不可变工作区绑定 |
| 完整 UI 轨迹 | `transcripts/<thread>.json` | 工具卡片/阶段/最终回答；仅桌面 UI 运行产生 |
| 运行事件 | A2/A3 内存缓冲 | 用于重连 `after`；不替代 checkpoint/审计 |

运行状态（`busy`、`paused`、`approvals`、`connectionState`、`lastSeq`）属于各自
`SessionView`，不是全局单份；主区与分屏可同时运行。这些仍是 UI 状态，不构成权限事实。

---

## 10. 安全约束

- **CSP**：`default-src 'self'`、`script-src 'self'`、`connect-src 'self'`、
  `frame-ancestors 'none'`、`form-action 'self'`、`base-uri 'none'`；
  `report-uri /api/client-errors` 与 `report-to csp-endpoint` 接收违规摘要。
  该端点对 CSP 形状的请求允许匿名（Bearer 模式不会给报告带 Authorization），
  按来源 IP 限流，并去掉 URL query/fragment、截断所有字段。
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
- **PAT**：Bearer `ca_pat_*` 只允许 read/run/audit 三类最小路径；管理端、
  令牌/Webhook 管理与其他写路径一律 403，不能靠隐藏 UI 绕过。
- **Webhook**：只允许 https + 公网 IP（阻止回环/私网/保留地址），
  固定 443 端口；出站 JSON 带 `X-CodingAgent-Signature: sha256=...`。签名
  secret 需可逆保存，数据库访问本身就是信任边界。
- **分享链接**：匿名可访问，但快照在创建时已脱敏并冻结；token 只存哈希，
  默认 7 天、最长 30 天，可撤销；页面不加载脚本、不渲染模型 HTML，
  并返回 `noindex`/`no-store`。

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
- 会话列表默认加载 50 条，可“加载更多”按 50 条递增；会话按工作区分组，
  活跃与归档分别计数（`total`），避免一次渲染过多。
- 代码高亮只做一次；复制按钮/代码按钮不重复装饰。
- 长会话使用变量高度虚拟列表（实测高度 + overscan），只渲染视口附近消息；
  搜索命中会计算目标偏移并滚动到对应消息。
- 对话内搜索只在已加载消息上同步执行，不发后端请求；工具详情 JSON 在
  事件到达时生成一次。
- 流式输出默认跟随底部，但用户主动上滚后不再强制滚动，可用“跳到最新”恢复。
- 事件缓冲/重放有上限（A2 侧 8MB/5000 条/5 分钟，按实现为准）。
- 分离后静态产物按 hash 长缓存，`index.html`/配置 `no-cache`，API/SSE
  `no-store`；SSE 经过反代时禁缓冲、禁压缩并保留心跳。
- 已有 40 个 jsdom 交互用例与 6 个 Playwright E2E（用户端 smoke + `/admin`）
  （pywebview 探针作为手工冒烟补充）。

---

## 12. 测试与验收

自动：

```bash
cd src/ubismart/web/frontend
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
transcript 保存。当前 74 个纯函数用例 + 40 个 jsdom 交互用例。
`node test.mjs` 覆盖 diff 解析/split/词级标记、查询匹配/
fuzzy/前缀解析、虚拟列表区间、Git 状态映射、Problems 聚合、增量 Markdown、
会话摘要与焦点循环等纯函数。`node test-dom.mjs` 用 jsdom 真实加载
`static/index.html`、vendored Alpine CSP 与 `app.js`，桩掉后端 API 后验证
会话未读/摘要/批量选择、文件树 Git 标记与键盘导航、Problems 折叠、流式渲染、
大工具输出分页与派生缓存、阶段播报、主题设置、Markdown 代码块标题、文件树
图标/引导线、Toast 堆叠、可拖拽分屏、模态焦点陷阱、审批批准/全部拒绝、
侧栏/检查器拖拽与宽度持久化、图片预览与实时会话分屏、两个 SessionView 的
事件状态隔离、SessionStore 视图复用/LRU/重复打开不重置、后台 SSE 事件隔离与
后台完成未读、实时分屏双流/独立发送，并把 Alpine 表达式错误作为失败。

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
| 真实浏览器覆盖 | jsdom + 6 个 Playwright smoke | 已覆盖壳层、草稿、语言切换与 `/admin` |
| 键盘焦点仍是浏览器全局 | 各会话保留自己的编辑器标签，但焦点不按会话保存 | 后续可做每视图焦点隔离 |
| 分屏审批不自动抢焦点 | 分屏出现审批卡时不转移键盘焦点 | 避免打断主区输入；可点卡片或切主区处理 |
| 轨迹只在桌面 UI 运行时保存 | 旧会话/CLI 运行无工具卡片 | 不替代 checkpoint/审计 |
| 轨迹优先于 checkpoint | CLI 追加的运行可能被旧轨迹遮住 | 桌面单用户场景可接受 |
| 单条超大工具输出按行分页 | 首次只渲染 40 行，需手动加载更多 | 避免一次性塞满 DOM |
| 授权桥仅桌面模式 | Web/CLI 仍走本地确认或拒绝 | `AGENT_CONSENT_TOKEN` 门控 |
| 前后端分离九步已落地 | 独立静态产物、版本契约与回滚步骤已就绪；真实部署 E2E 待补 | 见 RFC-0003 与 RELEASING.md；公网部署前仍需完成部署侧验证 |
| pywebview 目录对话框需强制前台 | 个别环境可能被遮挡 | 已用原生 shell32 + topmost |
| 卡片只做时间线级接入 | 预估/进度/阶段/故障卡没有独立卡片视图 | 后端已提供 `card_id/event_id`，可按需升级为幂等 upsert |

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
| `web/frontend/src/ui/file-icons.ts` | 文件树类型图标与层级引导线 |
| `web/frontend/src/ui/problems.ts` | Problems 按文件/来源聚合 |
| `web/frontend/src/ui/markdown.ts` | 流式 Markdown 稳定块切分与增量渲染 |
| `web/frontend/src/ui/focus.ts` | 模态 Tab 循环计算 |
| `web/frontend/src/ui/sessions.ts` | 会话列表摘要与元信息格式化 |
| `web/frontend/src/ui/library.ts` | 资料库条目状态、用量与检索结果格式化 |
| `web/frontend/src/features/library.ts` | 资料库上传/列表/删除/重建/检索自测 mixin |
| `web/frontend/src/ui/output.ts` | 大工具输出按行分页 |
| `web/frontend/src/ui/memo.ts` | 派生列表单槽缓存 |
| `web/frontend/src/ui/settings.ts` | 设置分节关键词搜索 |
| `web/frontend/src/ui/theme.ts` | 强调色预设与主题 body class |
| `web/frontend/src/features/errors.ts` | 全局错误横幅与重试动作 |
| `web/frontend/src/features/focus.ts` | 模态焦点进入/陷阱/恢复 mixin |
| `web/frontend/tests/*.test.ts` | 纯函数单元测试（`node test.mjs`） |
| `web/frontend/tests/dom/*` | jsdom 交互测试（`node test-dom.mjs`） |
| `web/frontend/src/state/runtime.ts` | 非响应式 Map 缓冲 |
| `web/frontend/src/state/session-view.ts` | 会话视图状态工厂（消息/计划/工具/审批/流式游标） |
| `web/frontend/src/features/session-view.ts` | SessionStore、activeView facade、withView/applyEvent |
| `web/frontend/src/contracts/events.generated.ts` | 由契约生成的事件 union（禁止手改） |
| `web/frontend/src/templates/*.html` | 页面模板分片（构建期拼成 `static/index.html`） |
| `web/frontend/src/styles/*.css` | 设计令牌与样式分片（构建期拼成 `static/style.css`） |
| `web/frontend/build-ui.mjs` | 模板/样式拼装脚本 |
| `web/frontend/src/runtime-config.ts` | 运行时配置加载/校验、API 基址拼接 |
| `web/errors.py` | problem+json 错误协议与 request id |
| `platform/request_context.py` | request id ContextVar（A2→A3 透传） |
| `protocol/sse_stream.py` | SSE 心跳、注释帧与响应头（A2/A3 共用） |
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
- fetch/SSE/XHR 上传统一走同一 URL 拼接函数，禁止裸 `/api` 字面量；
- 调用方仍写 `/api/...`，`apiUrl()` 按运行时 `apiVersion` 插入
  `/api/v1/`；`apiBaseUrl` 不得以 `/api` 结尾（版本段由前端统一加）。

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
- `/healthz` 返回 `api_version`、`min_api_version` 与
  `min_frontend_version`；不兼容时阻断；请求带 `X-Frontend-Version`。
- 当前 API 为 `v1`；`/api/v1/*` 由最内层中间件重写为 `/api/*`，
  `/api/*` 在兼容窗口内继续可用并返回 `Deprecation: true`。
- 服务端对携带非法或过旧 `X-Frontend-Version` 的 `/api/*` 请求返回
  426 `version.frontend_too_old`；未携带该头的旧客户端在升级窗口内放行。
- 前端版本常量在 `frontend/src/version.ts`；服务端在 `web/version.py`
  维护 `api_version`、`min_api_version`、`min_frontend_version` 与
  `capabilities_version`。前后端版本独立递增，只保留最小兼容约束。

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
9. 独立发版与契约测试：`scripts/build_frontend_release.py` 生成版本化
   产物与 `manifest.json`，`scripts/check_frontend_release.py` 校验哈希与
   版本契约；A2 可用 `AGENT_WEB_SERVE_FRONTEND=0` 切为纯 API。
   详见 [RELEASING.md](RELEASING.md) 与 [ADR-0020](adr/0020-frontend-release-artifact.md)。

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
