---
layer: L2
doc_id: adr-0017
title: 认证模型按部署形态分三档：桌面一次性 token、内网短期 Bearer、公网 BFF Cookie
owner: 项目作者
status: accepted
last_reviewed: 2026-10-26
review_cadence: 认证方式、凭证生命周期、跨域策略或公网部署形态变动时
---

# ADR-0017：认证模型按部署形态分三档

## 背景

当前 Web 认证是静态 Bearer Token 映射：token 配置在
`AGENT_WEB_AUTH_TOKENS`，客户端把明文放在 `Authorization` 头；未配置
token 时只允许 loopback 匿名。桌面壳每次启动生成随机 token，通过 URL
fragment 传给页面，前端存入 `sessionStorage`。

分离部署后，这套模型暴露出四个问题：

- **无生命周期**：token 不过期、不轮换、不能注销，泄漏后长期有效；
- **存储面过大**：Web 模式下 `sessionStorage` 可被 XSS 读取；
- **跨域传递未定义**：独立源站需要 CORS，Cookie 模式还需要 CSRF；
- **身份与授权混在一起**：前端容易把"有 token"误当成"有权限"。

约束：

- A2 只监听 loopback，公网入口在边缘/BFF；
- 服务端始终是认证与授权的事实源，前端只提交凭证；
- 桌面形态必须离线可用，且不能把远程页面加载进带原生桥的 webview；
- 不把 API Key、Bearer token 写入 localStorage、日志或 URL query；
- 现有的 `AuthUser → WebTenant` 租户隔离与审批/租约判定不变。

可选方案：

1. 继续使用长期静态 Bearer + `sessionStorage`；
2. 改为 localStorage 持久化；
3. 短期 Bearer（内存）+ refresh Cookie；
4. BFF 同源 httpOnly Cookie + CSRF；
5. 直接接 OIDC，浏览器持有 ID/access token。

## 决策

**按部署形态分三档，凭证传输与授权判断彻底分开。**

### 1. 桌面：一次性本地 token（`desktop-token`）

- 每次启动生成随机 token，只对本次进程与 loopback 有效；
- 通过 URL fragment 传入并立即 `history.replaceState` 清除；
- 优先只放内存；若 reload 需要恢复，可退回 `sessionStorage`，但必须在
  威胁模型中登记这是桌面本地风险；
- 服务端同时校验 loopback 来源与 Host 白名单；配置 token 时不允许匿名；
- token 不落盘、不进日志、不随诊断包导出；进程重启即失效。

### 2. 内网/受控跨域：短期 Bearer（`bearer`）

- access token 短 TTL，**只放内存**，禁止 localStorage；
- refresh 使用 httpOnly + Secure + SameSite 的 Cookie，作用域限定认证路径；
  没有 refresh 条件时，401 直接回登录；
- 401 使用稳定错误码 `auth.token_expired` / `auth.required`；
  前端单飞刷新，成功后重放一次原请求；
- 非幂等请求在刷新后重试必须复用原 `Idempotency-Key`，避免重复运行/上传；
- 注销调用服务端吊销并清空内存凭证，同时关闭 SSE；
- 不把 token 放进 URL query、Referer 或错误详情。

### 3. 公网多用户：BFF 同源 Cookie（`bff-cookie`）

- 浏览器只访问 BFF 同源；BFF 持有上游凭证，API 不直接对浏览器暴露；
- 会话 Cookie 使用 `HttpOnly; Secure; SameSite=Lax`，空闲与绝对超时并存；
- CSRF 采用双提交 Token 或严格 `Origin` / `Sec-Fetch-Site` 校验；
  BFF 通过 `csrf_token` 或 `XSRF-TOKEN` Cookie 下发，前端按
  `auth.csrfHeader`（默认 `X-CSRF-Token`）回填；
- Bearer refresh 响应返回 `{access_token}`；BFF refresh 返回 204/200 并更新
  Cookie，前端不解析 body；
- 登录、注销、权限变更后轮换会话；注销与禁用需要在服务端可撤销；
- 多实例会话存共享存储（Redis 等），不能只放单进程内存；
- 公网推荐 OIDC（Authorization Code + PKCE），由 BFF 完成换取与续期；
  静态 token 映射只用于受控内网。

### 4. 错误、授权与审计

- 401 与 403 必须分开：401 可刷新/登录，403 是权限问题，不触发刷新；
- `WWW-Authenticate` 只在 Bearer 模式返回；
- token → `AuthUser` → `WebTenant` 的映射不变；工作区、审批、租约与读写
  权限仍由服务端判定，前端隐藏按钮不构成授权；
- 登录成功/失败、刷新、注销、权限拒绝写入审计，日志中凭证一律脱敏；
- SSE 中途断流不等于 401：保留 `lastSeq`，重新认证后按 `after` 续传，
  不能丢弃已渲染的部分答案。

**否掉的替代**：继续使用长期静态 token（无法吊销）；localStorage
（XSS 可持久窃取）；把跨站 Cookie 当公网主方案（受浏览器第三方 Cookie
限制）；把 token 放 query（会进入日志与 Referer）。

## 后果

### 好的

- 凭证生命周期与吊销路径明确，泄漏窗口显著缩短；
- Web 模式不再把 token 放在可持久读取的存储里；
- CSRF、CORS、Cookie 限制在方案层面被显式处理；
- 桌面离线体验不变，且远程页面拿不到本地凭证；
- 授权判断仍集中在服务端，前端只负责传输。

### 坏的 / 代价

- 需要 refresh/会话存储与注销接口，公网还要 BFF 与 OIDC 集成；
- Cookie 模式增加 CSRF 防护与跨站策略配置；
- 桌面 reload 的凭证恢复需要取舍并写进威胁模型；
- 401 刷新与重试要单飞，前端状态机复杂度上升；
- 静态 token 映射继续存在，需要明确"仅内网"并逐步淘汰。

### 中性 / 需要留意

- Bearer 模式仍无法抵御 XSS 读取内存，只能靠 CSP、输入清洗与短 TTL 降低影响；
- 第三方 Cookie 策略变化可能使跨域 Cookie 不可用，因此公网优先 BFF 同源；
- 匿名 loopback 仅限本地单用户，不能作为任何远程部署的默认值；
- 认证失败诊断要带 `request_id`，但不能回显 token 片段。

## 复核

- **Verified by**：设计契约见
  [RFC-0003](../rfc/0003-frontend-backend-separation.md)；实现后由
  `REQ-205`–`REQ-206` 的验收项覆盖登录、刷新、注销、CSRF 与凭证存储检查。
- **Revisit when**：引入 OIDC/企业 SSO、需要跨设备会话、移动端接入，
  或浏览器对第三方 Cookie 的策略使现有模式不可用。
