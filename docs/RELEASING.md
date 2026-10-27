---
layer: L2
doc_id: releasing
title: 前端独立发版、兼容矩阵与回滚
owner: 项目作者
status: active
last_reviewed: 2026-10-26
review_cadence: 发版方式、兼容策略或部署拓扑变动时
---

# 前端独立发版、兼容矩阵与回滚

本文只描述**怎么做**：如何独立构建前端、如何判断前后端可一起上线、
以及出问题时如何只靠配置/路由回滚。设计契约见
[RFC-0003](rfc/0003-frontend-backend-separation.md)、
[ADR-0016](adr/0016-frontend-backend-topology-and-runtime-config.md)–
[ADR-0018](adr/0018-desktop-bridge-boundary.md)。

## 1. 产物

| 产物 | 来源 | 说明 |
|---|---|---|
| 前端静态包 | `src/coding_agent/web/static/` | `app.js` / `index.html` / `style.css` / `vendor/` |
| 运行时配置 | 部署侧 `/config.json` | `apiBaseUrl`、`authMode`、SSE 参数 |
| API | A2 服务 | `/api/*`、`/healthz`、CORS、SSE、上传 |
| 回退入口 | A2 `/` + `/static` | 独立发版后至少保留一个版本 |

CI 的 `CI` workflow 会构建前端并上传 `frontend-static` artifact；
独立静态站可直接消费该 artifact，不需要重新构建后端。

## 2. 发版前检查

```bash
# 前端构建与类型
cd src/coding_agent/web/frontend
node node_modules/typescript/bin/tsc --noEmit
node esbuild.mjs

# 契约与验收
cd ../../../..
python -m pytest -m "not llm" tests/unit/test_frontend_release_contract.py
python scripts/check_docs.py --strict
python scripts/gen_contracts.py --check
```

必须确认：

- `pyproject.toml`、`package.json`、`frontend/src/version.ts`、
  `coding_agent.__version__` 版本一致；
- `/healthz` 的 `api_version` 与 `/config.json` 的 `apiVersion` 一致；
- `FRONTEND_VERSION` 不低于 `min_frontend_version`；
- `capabilities_version` 变化时，前端重新拉取能力；
- 静态包中的 `app.js` 是刚重建的，不是旧产物。

## 3. 独立发版步骤

1. **构建**：生成静态包，保留上一版目录；
2. **发布前端**：先上静态资源，再切 `/config.json`（或反代路由）；
3. **灰度**：先让一个内部来源访问新前端，观察 401/403/426/5xx、
   SSE 重连与上传错误码；
4. **扩大**：确认 `/healthz` 版本匹配后再切全量；
5. **保留回退**：A2 托管首页与上一个静态目录至少保留一个版本周期。

同域反代（形态 B）只需切 `/` 的静态根；独立源站（形态 A）同时确认
CORS 允许列表包含新 origin；BFF（形态 C）只切 BFF 的前端资源，
浏览器不直连 A2。

## 4. 兼容矩阵

| 项 | 规则 | 失败表现 |
|---|---|---|
| 前端 ↔ 最低版本 | `FRONTEND_VERSION >= MIN_FRONTEND_VERSION` | 前端阻断页 / 服务端 426 |
| 前端 ↔ API 版本 | `config.apiVersion == /healthz.api_version` | `version.api_mismatch` |
| 能力 | `capabilities_version` 变化后重拉 | 旧字段可能缺失 |
| 认证 | 凭证模式与配置一致 | 401 / 403 / 登录跳转 |
| 错误体 | 只依赖 `code`，不解析 `detail` | 文案变化不应影响 UI |

## 5. 回滚

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

## 6. 已知限制

- BFF 与对象存储直传由部署侧实现，本仓库只冻结前端契约；
- 真实反向代理、跨域 SSE、上传扫描需要部署环境 E2E；
- 桌面更新只打开下载页，不执行安装包，因此没有应用内签名安装链；
- A2 仍只监听 loopback，公网入口必须由边缘/BFF 提供。
