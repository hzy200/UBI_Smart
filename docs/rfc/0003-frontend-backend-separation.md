---
layer: L2
doc_id: rfc-0003
title: 前后端分离的运行时契约、错误协议与传输矩阵
owner: 项目作者
status: accepted
created: 2026-10-26
last_reviewed: 2026-10-26
review_cadence: 拓扑、基址、认证、CORS 或代理语义变动时
---

# RFC-0003：前后端分离的运行时契约、错误协议与传输矩阵

> 冻结"一份前端产物如何找到 API、如何认证、如何跨域、如何穿过代理"这四件事。
> 本文只定义设计与验收，不包含代码改动；实现顺序见第 12 节。

## 1. 目标

- **G1** 同一份前端构建产物能在桌面、同域反代、独立源站、BFF 四种拓扑下运行，
  差异只来自运行时配置；
- **G2** 基址、认证、版本、错误、请求 ID 成为稳定契约，前端不解析后端文案；
- **G3** CORS、SSE、上传在真实代理后有明确的允许列表、超时、缓冲与重试规则；
- **G4** 桌面桥只对本地页面开放，远程内容无法获得原生能力；
- **G5** 每条设计都有可执行的验收锚点，实施后能转成契约测试。

## 2. 非目标

- 不在本 RFC 内改动前端框架或重写 UI（ADR-0010 仍然有效）；
- 不把 A2 暴露到公网，不改变 A2/A3/A4 的 loopback 绑定；
- 不实现 BFF、OIDC 或对象存储直传，只定义接口与边界；
- 不改变审批、沙箱、路径守卫、密钥注入的既有语义；
- 不承诺 exactly-once 事件投递，仍按至少一次 + `seq` 去重。

## 3. 背景与证据

- 前端全部使用相对 `/api/...`，SPA 由 A2 同源托管；
- 服务端没有 CORS 中间件，CSP 为 `connect-src 'self'`；
- 认证是静态 Bearer 映射，Web 端 token 存在 `sessionStorage`，无过期/刷新/注销；
- 桌面 token 通过 URL fragment 传入，pywebview 桥直接注入页面；
- SSE 使用 fetch + Authorization 头（这点适合分离），但代理缓冲/压缩/超时规则未成文；
- 静态资源当前也被 `Cache-Control: no-store` 覆盖。

详细现状与耦合点见 [FRONTEND.md](../FRONTEND.md)、
[ARCHITECTURE.md](../ARCHITECTURE.md) 第 18 节与
[THREAT_MODEL.md](../THREAT_MODEL.md)。

## 4. 受影响面

| 项 | 内容 |
|---|---|
| 模块 | `web/frontend/src`、`web/static`、`web/middleware.py`、`web/auth.py`、`web/routes/*`、`desktop/*` |
| 契约 | 运行时配置、错误体、请求 ID、版本握手、CORS 头、SSE/上传代理规则 |
| 数据 | 无数据库迁移；认证会话存储随模式增加（内存/Redis） |
| 前端 | 基址注入、认证适配、错误解析、SSE/上传 URL 统一、桥能力降级 |
| 权限 | 认证模式、CSRF、CORS 允许列表、桌面桥 origin 绑定 |

## 5. 设计契约

### 5.1 拓扑

| 形态 | 前端 | API | 浏览器跨域 | 认证默认 | 桥 |
|---|---|---|---|---|---|
| D 桌面本地 | 本地静态 + loopback | 同源 | 否 | `desktop-token` | 有 |
| B 同域反代 | 静态站/边缘 | `/api` 反代 | 否 | `bearer` | 无 |
| A 独立源站 | 静态站/CDN | 独立源 | 是 | `bearer` | 无 |
| C BFF 公网 | BFF 同源 | BFF 持有 | 否 | `bff-cookie` | 无 |

迁移顺序固定为 **D 保持 → B → A → C**。B 阶段保留 API 托管旧首页作为回退入口。

### 5.2 运行时配置 schema

配置文件（`/config.json`）或桌面注入对象必须满足：

```json
{
  "schemaVersion": 1,
  "apiBaseUrl": "",
  "apiVersion": "v1",
  "authMode": "bearer",
  "auth": {
    "loginUrl": "/auth/login",
    "refreshUrl": "/auth/refresh",
    "logoutUrl": "/auth/logout",
    "csrfHeader": "X-CSRF-Token"
  },
  "sse": {
    "heartbeatMs": 15000,
    "idleTimeoutMs": 45000,
    "reconnectBaseMs": 500,
    "reconnectMaxMs": 30000,
    "reconnectMaxAttempts": 3
  },
  "requestIdHeader": "X-Request-ID"
}
```

校验规则：

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `schemaVersion` | int | 是 | 当前只接受 `1`；不兼容时阻断 |
| `apiBaseUrl` | string | 是 | 空串 = 同源；或以 `/` 开头的根相对路径；或 `http(s)` 绝对 URL；禁止 query/fragment/userinfo；去尾部 `/` |
| `apiVersion` | string | 是 | `^v[0-9]+$`，与请求路径一致 |
| `authMode` | enum | 是 | `desktop-token` / `bearer` / `bff-cookie` |
| `auth.loginUrl` 等 | string | C 模式必填 | 同源相对路径或允许的绝对 URL |
| `auth.csrfHeader` | string | C 模式必填 | 仅 Cookie 模式使用；CSRF 值从 `csrf_token` 或 `XSRF-TOKEN` Cookie 读取 |
| `sse.heartbeatMs` | int | 否 | 5000–60000，默认 15000 |
| `sse.idleTimeoutMs` | int | 否 | 5000–300000，默认 45000；不得小于 heartbeat 两倍 |
| `sse.reconnect*` | int | 否 | 退避上下限；最大尝试次数可配 |
| `requestIdHeader` | string | 否 | 默认 `X-Request-ID` |

补充约束：

- 配置最大 8KB；未知字段忽略，未知枚举值失败关闭；
- 配置不得包含密钥、用户信息或业务开关；
- `apiBaseUrl` 必须落在当前页面 CSP 的 `connect-src` 允许范围内；
  配置不能扩大 CSP；
- 加载顺序：桌面注入 `window.__APP_CONFIG__` > `/config.json` > 构建默认值；
- 配置加载失败/非法 → 显示阻断页（`config.invalid`），**不能显示登录页**；
- 前端版本号由构建产物提供，部署配置不得伪造。

### 5.3 前端版本与 API 版本握手

`GET /healthz`（公开、最小信息）返回：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "api_version": "v1",
  "min_frontend_version": "0.1.0",
  "capabilities_version": 3
}
```

规则：

- 前端启动后、登录前读取；主版本低于 `min_frontend_version` 时显示阻断页，
  错误码 `version.frontend_too_old`，不得继续发业务请求；
- 每个 API 请求带 `X-Frontend-Version`；
- 服务端对携带非法或过旧版本的 `/api/*` 请求返回 426
  `version.frontend_too_old`；未携带该头的旧客户端在升级窗口内放行；
- 服务端可返回 `Deprecation` / `Sunset` 响应头，前端展示升级提示；
- `capabilities_version` 变化时重新拉取 `/api/capabilities`；
- `/healthz` 不返回模型、用户、工作区、数据库等敏感状态。

### 5.4 错误协议

所有 API 错误使用 `application/problem+json`：

```json
{
  "type": "urn:codingagent:error:auth.token_expired",
  "title": "Token expired",
  "status": 401,
  "code": "auth.token_expired",
  "detail": "访问令牌已过期，请刷新后重试。",
  "instance": "/api/v1/runs",
  "request_id": "01J...",
  "retryable": false,
  "details": {}
}
```

约束：

- **`code` 是唯一稳定的 UI 契约**；`title`/`detail` 可本地化、可改写；
- `request_id` 同时出现在响应头 `X-Request-ID`；
- `retryable` 只是提示，客户端仍需按状态码与幂等性判断；
- `details` 用于结构化补充（如 `stream_url`、`thread_id`、`idempotent`）；
- 429/503 返回 `Retry-After`；
- 兼容窗口内，旧 `detail` 响应由前端适配为 `legacy.unknown`，并保留 request ID。

状态码与错误码（最小集合）：

| HTTP | code | 客户端行为 |
|---|---|---|
| 400 | `request.invalid` | 展示字段错误，不重试 |
| 401 | `auth.required` | 进入登录 |
| 401 | `auth.token_expired` | 单飞刷新后重试一次 |
| 403 | `auth.forbidden` | 展示权限不足，不刷新 |
| 403 | `csrf.invalid` | 重新获取 CSRF token 后重试一次 |
| 404 | `run.not_found` / `workspace.not_found` | 刷新列表 |
| 409 | `run.in_progress` | 用 `stream_url` 重连或提示 |
| 413 | `request.too_large` | 不重试，提示限额 |
| 426 | `version.frontend_too_old` | 阻断并提示升级 |
| 429 | `rate.limited` | 按 `Retry-After` 退避 |
| 500 | `server.error` | 可重试一次，展示 request ID |
| 502/504 | `gateway.error` / `gateway.timeout` | 退避重试，SSE 走重连 |
| 503 | `server.unavailable` | 退避重试 |
| — | `transport.unreachable` | 客户端合成：网络或 CORS 失败 |
| — | `config.invalid` | 客户端合成：配置加载/校验失败 |

`transport.unreachable` 的 UI 必须展示：`apiBaseUrl`、当前 origin、request ID
（若有）、以及"检查 CORS/CSP/代理"的提示，避免只剩 `Failed to fetch`。

### 5.5 请求 ID

- 边缘或 A2 生成 `X-Request-ID`；格式 `[A-Za-z0-9._:-]{8,128}`；
- 只接受受信来源传入的 ID，其他来源一律重新生成，防日志注入；
- 透传到 A2→A3 内部调用与结构化日志；
- 错误体回显；CORS 暴露该头；
- SSE 在首个注释帧中带 request ID，便于把断流与请求日志对上；
- 审计/日志不得包含 token、Cookie、CSRF 值。

### 5.6 CORS 允许列表

只在形态 A 需要浏览器 CORS；B 同源、C 由 BFF 吸收。

- 允许列表为精确 `scheme + host + port`，来自 `AGENT_CORS_ALLOWED_ORIGINS`
  （逗号分隔）；默认空；非法条目启动失败；
- 禁止 `*`、禁止反射任意 Origin、禁止 `null`、禁止正则；
- `allow_credentials` 仅 `bff-cookie` 模式为 true；Bearer 模式为 false；
- 允许方法：GET/POST/PUT/PATCH/DELETE/OPTIONS；
- 允许请求头：`Authorization`、`Content-Type`、`Idempotency-Key`、
  `Last-Event-ID`、`X-Request-ID`、`X-Frontend-Version`、`X-CSRF-Token`；
- 暴露响应头：`X-Request-ID`、`X-Thread-Id`、`Retry-After`、
  `WWW-Authenticate`、`Deprecation`、`Sunset`；
- 预检：204、`Access-Control-Max-Age: 600`、不鉴权、不计业务限流；
- **CORS 必须是最外层**，保证 401/403/413/429/5xx 与中间件提前返回也带 CORS 头；
- 所有响应带 `Vary: Origin`；
- 公网前端不得直连 loopback/私网 API（避免 Private Network Access 预检）；
  需要时走 BFF；
- CORS 不是授权，服务端仍逐请求判定。

### 5.7 SSE 代理矩阵

| 关注点 | 要求 | 验收方式 |
|---|---|---|
| 连接 | 上游 HTTP/1.1、keep-alive；`Content-Type: text/event-stream` | 代理后首事件 < 2s |
| 缓冲 | `proxy_buffering off`、`proxy_cache off`、`X-Accel-Buffering: no` | 流式事件逐条到达，不批量延迟 |
| 压缩 | `text/event-stream` 永不 gzip/brotli；`Cache-Control: no-cache, no-transform` | 抓包无 Content-Encoding |
| 心跳 | 服务端每 15s 发 SSE 注释帧；代理读超时 ≥ 45s；A2 代理 A3 时同样注入 | 空闲 10 分钟不断流 |
| 客户端看门狗 | 45s 收不到任何字节则断开并按 `seq` 重连 | 模拟静默连接可恢复 |
| 重连 | 客户端用 `after` / `Last-Event-ID`；追平返回 204 | 断网后不重复、不丢事件 |
| 重试 | 一旦发送字节，代理不得自动重试 | 不产生重复 run |
| 错误 | 流开始前错误返回 problem+json；流开始后断流由客户端重连 | 502 不导致解析崩溃 |
| 请求 ID | 首个注释帧带 request ID | 日志可关联 |
| 多实例 | 共享事件传输或粘性会话；租约外置 | 跨实例重连成功 |
| 背压 | 有界缓冲；慢客户端断开而不是无限堆积 | 慢消费者不拖垮 A2 |
| CORS | 形态 A 的流式响应带允许头 | 跨域可读 |

### 5.8 上传代理矩阵

| 关注点 | 要求 |
|---|---|
| 预检 | 允许 `Authorization`、`Idempotency-Key`；返回 204 |
| 限额 | 边缘 body 限制 ≥ 后端单文件/总量限制；会话上传请求体按累计配额 + 1MiB 余量，单文件逐项校验；超限返回 413 + CORS，code 形如 `upload.file_too_large` |
| 缓冲 | 大文件关闭代理请求缓冲；或直接预签名上传到对象存储 |
| 超时 | 客户端/代理/后端上传超时一致；进度事件仍可用 |
| 幂等 | 会话上传与项目上传/导入都带 `Idempotency-Key`；重试复用同一 key；控制面 DB 不可用时只保证传输成功，不做跨请求去重 |
| 扫描 | 解压前扫描；失败关闭；前端明确显示"未扫描"档位 |
| 错误 | 413/429/5xx 都带 CORS 与 request ID |
| 凭证 | Bearer 模式不带 Cookie；Cookie 模式需要 CSRF |
| 大文件 | 超过约 200MB 的归档优先直传对象存储，由后端拉取/扫描 |

### 5.9 桌面桥交接

桌面形态继续加载本地页面，桥的细节由 [ADR-0018](../adr/0018-desktop-bridge-boundary.md)
定义。本 RFC 只固定三点：

- `authMode=desktop-token` 时 API 与页面同源，不需要 CORS；
- 远程页面不得获得 `window.pywebview.api`；
- 桥能力以服务端 `capabilities` 与本地 origin 双重判定；
- 当前实现：launcher 只加载 loopback `control.url`，并把其 origin 传给
  `DesktopApi`；桥每次调用校验 `get_current_url()`，无法证明来源时 fail closed；
  前端包装 api 拒绝非顶层窗口；浏览器模式桌面端点 403 `desktop.unavailable`。

## 6. 安全与隐私

- 凭证：Web 模式只放内存；Cookie 模式 httpOnly + CSRF；桌面 token 仅 loopback；
- token、Cookie、CSRF 值不得进入日志、错误详情、Referer、URL query；
- CORS 允许列表不是认证，认证失败仍返回 401；
- 配置不包含密钥；错误详情不回显请求头；
- 桌面桥拒绝非本地 origin/iframe，危险操作本地确认，更新验签；
- 请求 ID 不是秘密，可用于关联日志，但不能编码用户信息。

## 7. 兼容与回滚

- API 目标路径 `/api/v1`；实施时保留 `/api` 双挂载或边缘重写一个版本周期；
- 配置 schema 只做加法；破坏性变更提升 `schemaVersion` 并提高
  `min_frontend_version`；
- 拓扑切换通过边缘路由与配置完成，不要求重新构建前端；
- 回滚方式：配置回退 + 边缘切回 API 托管首页；保留旧入口一个版本周期；
- 桌面桥保持本地加载，回滚不影响原生能力。

## 8. 可观测性

- 结构化日志：`request_id`、路由、状态码、错误 `code`、耗时、origin、
  auth mode（不记录 token）、租户（可哈希）；
- 指标：401/403/429/5xx、预检失败、SSE 重连次数、上传失败、版本不匹配、
  桥拒绝次数；
- 审计：登录成功/失败、刷新、注销、权限拒绝、桥拒绝；
- 追踪：A2→A3 透传 request ID。

## 9. 验收标准

验收锚点写入
[`../acceptance/acceptance-criteria.yaml`](../acceptance/acceptance-criteria.yaml)：
`REQ-202`–`REQ-212`。实现阶段至少覆盖：

- 同一产物四种拓扑启动；
- 配置非法失败关闭；
- 版本不兼容阻断；
- Bearer 刷新/注销/单飞；
- Cookie 模式 CSRF；
- CORS 允许/拒绝/错误响应；
- 代理后 SSE 空闲与重连；
- 上传预检与限额；
- 桌面桥 origin/iframe 拒绝；
- request ID 端到端。

## 10. 替代方案与代价

| 方案 | 优点 | 代价 / 风险 | 为什么没选 |
|---|---|---|---|
| 构建时注入基址 | 实现简单 | 每个环境一份产物，易漂移 | 无法保证"同一产物多拓扑" |
| 通配 CORS | 配置省事 | 任意站点可读响应，凭证模式更危险 | 安全边界不可接受 |
| EventSource | 浏览器原生重连 | 不能带 Authorization 头 | 现有 fetch+SSE 更合适 |
| 远程前端 + 本地桥 | 前后端彻底独立 | 远程 XSS 直接获得本机权限 | 信任域混淆，明确禁止 |
| 服务端模板渲染 | 无 CORS/无前端构建 | 与静态 SPA 决策冲突，桌面离线与交互受限 | 不解决当前问题 |
| 只拆仓库不拆部署 | 改动小 | 契约/发版仍耦合 | 不满足独立发布目标 |

## 11. 决策与后果

- **最终选择**：采纳第 5 节全部契约；拓扑按 B→A→C 推进；
- **批准人 / 日期**：项目作者，2026-10-26；
- **实施状态**：九步均已实现（运行时配置/基址注入、配置失败关闭、版本握手、
  前端认证适配与单飞刷新、错误协议与 request ID、CORS 允许列表、
  SSE 心跳与代理规则、上传限额与幂等、桌面桥 origin 绑定、
  独立产物与发版契约）。发版/回滚见
  [RELEASING.md](../RELEASING.md)。
- **真实结果与预期差异**：待实现后补；
- **新增技术债**：无新增；原 `unsafe-eval`（TD-016）已通过 Alpine CSP 构建解决；
- **关联 ADR**：[0016](../adr/0016-frontend-backend-topology-and-runtime-config.md)、
  [0017](../adr/0017-auth-model-for-separated-deployment.md)、
  [0018](../adr/0018-desktop-bridge-boundary.md)。

## 12. 任务分解与验证计划

| 任务 | 负责模块 | 验证方式 |
|---|---|---|
| 1. 基址注入 + 配置校验 + 阻断页 | 前端构建/启动 | 四拓扑 smoke；`REQ-202`、`REQ-203` |
| 2. 版本握手 | 前端启动、`/healthz` | 不兼容阻断；`REQ-204` |
| 3. 认证适配（桌面/Bearer/BFF） | `auth.py`、前端请求层 | 401/刷新/注销/CSRF；`REQ-205`、`REQ-206` |
| 4. 错误协议 + request ID | A2/边缘/前端 | 错误码映射、日志关联；`REQ-211` |
| 5. CORS 允许列表 | 边缘/A2 | 预检/拒绝/错误响应矩阵；`REQ-207` |
| 6. SSE 心跳与代理规则 | A2/边缘/前端 | 空闲/重连/多实例；`REQ-208` |
| 7. 上传预检与限额 | 上传路由/边缘 | 预检/413/幂等；`REQ-209` |
| 8. 桥 origin 绑定 | `desktop/` | 远程 URL/iframe 拒绝；`REQ-210` |
| 9. 独立发版与契约测试 | CI | 前端独立构建、API 兼容矩阵；`REQ-212` |

实现必须以本顺序推进；在 1–2 完成前，不启用跨域或 Cookie 模式。
