---
layer: L2
doc_id: releasing
title: 前端独立发版、兼容矩阵与回滚
owner: 项目作者
status: active
last_reviewed: 2026-10-27
review_cadence: 发版方式、兼容策略或部署拓扑变动时
---

# 前端独立发版、兼容矩阵与回滚

本文只描述**怎么做**：如何独立构建前端、如何判断前后端可一起上线、
以及出问题时如何只靠配置/路由回滚。设计契约见
[RFC-0003](rfc/0003-frontend-backend-separation.md)、
[ADR-0016](adr/0016-frontend-backend-topology-and-runtime-config.md)、
[ADR-0020](adr/0020-frontend-release-artifact.md)。

## 1. 产物

| 产物 | 来源 | 说明 |
|---|---|---|
| 前端发布包 | `dist/frontend/<version>/` | `index.html`、`admin/index.html`、`sw.js`、`manifest.webmanifest`、`static/`、`manifest.json`、`SHA256SUMS` |
| 控制面数据库 | 部署侧 MySQL | 发布前执行 `python -m coding_agent.control.migrate --backup`；生产关闭 auto_create |
| 运行时配置 | 部署侧 `/config.json` | 从 `config.example.json` 复制，填 `apiBaseUrl` / `authMode` / SSE 参数 |
| API | A2 服务 | `/api/v1/*`（当前）、`/api/*`（兼容窗口，带 `Deprecation`） |
| 回退入口 | A2 `/` + `/static` | 包内 `web/static` 快照，默认开启 |

发布包结构：

```text
dist/frontend/0.1.0/
  index.html
  admin/
    index.html
  sw.js
  manifest.webmanifest
  config.example.json
  manifest.json
  SHA256SUMS
  static/
    app.<hash>.js
    style.<hash>.css
    icon.<hash>.svg
    admin/
      app.<hash>.js
      app.<hash>.css
    vendor/*.<hash>.js
```

`manifest.json` 记录 `version`、`releaseId`、`apiVersion`、`minApiVersion`、
`minFrontendVersion`、`capabilitiesVersion` 与每个文件的 `sha256/size`。
CI 的 `frontend` job 生成并校验该目录，上传为 `frontend-release` artifact。

## 2. 版本规则

前后端**独立升版**：

- 前端：`frontend/src/version.ts` 与 `package.json` 必须一致；
- 后端：`pyproject.toml` 从 `coding_agent.__version__` 动态读取，只有一处；
- 不再要求前后端版本号相等；`scripts/check_version.py` 在 CI/Makefile 校验上述两组；
- 前端版本必须 `>= MIN_FRONTEND_VERSION`；
- API 版本由 `web/version.py` 的 `WEB_API_VERSION` 决定，当前 `v1`；
- `MIN_API_VERSION`（当前 `v0`）表示仍在兼容窗口内的最旧 API 版本。

**Windows 安装包也吃后端版本**：`build_installer.ps1` 从
`coding_agent.__version__` 读出来传给 ISCC（`/DAppVersion=`），`.iss` 里只有
`{#AppVersion}` 占位符。这条链路由 `scripts/check_installer.py` 守着 ——
安装包版本与 `__version__` 漂移会直接让 CI 红。

## 3. 发版前检查

```bash
conda activate agent

# 前端构建与类型
cd src/coding_agent/web/frontend
node node_modules/typescript/bin/tsc --noEmit
node esbuild.mjs
node test.mjs && node test-dom.mjs
cd ../../../..

# 生成并校验独立发布包
python scripts/build_frontend_release.py --out dist/frontend
python scripts/check_frontend_release.py --dir dist/frontend/0.1.0

# 契约与文档
python -m pytest -m "not llm" tests/unit/test_frontend_release_contract.py
python -m pytest -m "not llm" tests/unit/test_frontend_hosting.py
python scripts/check_docs.py --strict
python scripts/gen_contracts.py --check
```

必须确认：

- `manifest.json` 中每个文件的 sha256 与目录一致；
- `index.html` 只引用 hash 化资源；
- `sw.js` 的 `CACHE` 含本次 `releaseId`；
- `/healthz` 的 `api_version` 与 `/config.json` 的 `apiVersion` 一致；
- `FRONTEND_VERSION >= min_frontend_version`；
- 包内 `static/app.js` 是刚重建的（CI 用 `git diff --exit-code` 兜底）。

## 4. 独立发版步骤

1. **构建**：生成 `dist/frontend/<version>/`，保留上一版目录；
2. **迁移数据库**：先备份，再执行
   `python -m coding_agent.control.migrate --backup`；迁移失败不发布前端；
3. **发布前端**：先上 hash 资源与 `admin/` 静态文件，再切
   `index.html` / `config.json`；
4. **灰度**：先让一个内部来源访问新前端与 `/admin`，观察
   401/403/426/5xx、SSE 重连与上传错误码；
5. **扩大**：确认 `/healthz` 版本匹配后再切全量；
6. **保留回退**：至少保留上一个发布目录与 A2 托管入口一个版本周期。

部署侧缓存规则：

| 文件 | Cache-Control |
|---|---|
| `static/*.<hash>.*` | `public, max-age=31536000, immutable` |
| `index.html` / `admin/index.html` / `config.json` / `sw.js` / `manifest.webmanifest` | `no-cache` |
| `/api/*` / `/healthz` | `no-store` |

A2 托管发布包（回退入口）：

```bash
AGENT_WEB_FRONTEND_DIR=dist/frontend/0.1.0 agent web --port 8123
```

A2 会自动把 `<dir>/static` 挂到 `/static`；也可显式设置
`AGENT_WEB_STATIC_DIR`。显式目录或发布包缺文件时启动失败，不会静默
退回包内旧产物。

纯 API 模式（独立静态站/CDN）：

```bash
AGENT_WEB_SERVE_FRONTEND=0 agent web --port 8123
# 或
agent web --no-frontend
```

此时 `/`、`/static`、`/sw.js`、`/manifest.webmanifest` 返回 404；
`/healthz`、`/config.json` 与 `/api/*` 仍可用。

## 5. 安装包（Windows 桌面）

CI 只做**编译演练**，不产出可发布包：真包要 WSL rootfs（`build_wsl_distro.py`
需要跑 apt）和签名证书，只能在装有 WSL2 的 Windows 机器上构建。发布前在真机执行：

```powershell
conda activate agent
python scripts/build_wsl_distro.py
python scripts/build_portable.py
powershell -ExecutionPolicy Bypass -File scripts/build_installer.ps1

# 可选签名（也支持 $env:CODESIGN_CERT / $env:CODESIGN_PASSWORD）
```

发版前先过静态校验（CI 里同一条命令）：

```bash
python scripts/check_installer.py
```

它检查：版本单一源（`AppVersion={#AppVersion}` + 兜底值等于 `__version__`）、
构建脚本没有写死版本、`PrivilegesRequired=lowest`、卸载段不删
`%LOCALAPPDATA%`、`.iss` 引用的文件与图标存在。

产物是 `dist/CodingAgent-Setup.exe`。**没有 CI 签名链路**，只有本地
`signtool`（在 PATH 上时才生效）；没有签名证书时明确当作"内部构建"发布，
不要对外宣称已签名。

## 6. 兼容矩阵

| 项 | 规则 | 失败表现 |
|---|---|---|
| 前端 ↔ 最低版本 | `FRONTEND_VERSION >= MIN_FRONTEND_VERSION` | 前端阻断页 / 服务端 426 |
| 前端 ↔ API 版本 | `config.apiVersion == /healthz.api_version` | `version.api_mismatch` |
| API 兼容窗口 | `/api/*` 仍可用并带 `Deprecation: true` | 旧前端可继续工作 |
| 能力 | `capabilities_version` 变化后重拉 | 旧字段可能缺失 |
| 认证 | 凭证模式与配置一致 | 401 / 403 / 登录跳转 |
| 错误体 | 只依赖 `code`，不解析 `detail` | 文案变化不应影响 UI |

## 7. 回滚

优先顺序（从最轻到最重）：

1. **只回滚配置**：把 `apiBaseUrl`/`authMode` 切回旧值，刷新页面；
2. **只回滚静态**：把静态根切回上一版目录，API 不动；
3. **切回 API 托管**：把 `/` 与 `/static` 指回 A2 的兼容入口；
4. **回滚 API**：最后才回滚后端；前端需满足新 API 的
   `min_frontend_version`，否则会被 426 阻断。

回滚后验证：

- `/healthz` 与 `/config.json` 版本一致；
- 登录/刷新/注销正常；
- SSE 能收到心跳并重连；
- 上传返回的 413/429 带 CORS 与 request_id；
- 桌面桥仍只对本地 loopback 页面可用。

## 8. 已知限制

- BFF 与对象存储直传由部署侧实现，本仓库只冻结前端契约；
- 真实反向代理、跨域 SSE、上传扫描需要部署环境 E2E；
- 子路径部署需要部署侧同时处理 CSP、Service Worker scope 与路径前缀；
- 桌面更新只打开下载页，不执行安装包，因此没有应用内签名安装链；
- A2 仍只监听 loopback，公网入口必须由边缘/BFF 提供。
