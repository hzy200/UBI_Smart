---
layer: L2
doc_id: adr-0020
title: 前端发布产物与版本解耦
owner: 项目作者
status: accepted
created: 2026-10-27
last_reviewed: 2026-10-27
review_cadence: 发布产物结构、版本策略或静态托管方式变动时
---

# ADR-0020：前端发布产物与版本解耦

## 背景

ADR-0016 与 RFC-0003 已经冻结“一份产物 + 运行时配置”的拓扑，但第 9 步
只落地了 CI artifact 和版本一致性检查，发布链路仍与 Python 包绑在一起：

- `test_versions_are_in_sync` 要求 `pyproject == package.json == version.ts
  == __init__`，前端无法独立升版；
- `static/` 没有内容哈希和发布清单，服务端对静态资源统一 `no-store`，
  静态站/CDN 无法长缓存；
- A2 始终托管 `/` 与 `/static`，没有“只提供 API”的模式；
- API 仍是 `/api/*`，没有版本化路径。

约束：

- 桌面形态必须离线可用，包内 `static/` 仍是回退快照；
- 不搬迁源码目录，避免一次性改动 CI、spec、脚本与文档中的大量路径；
- 不引入新的运行时依赖或前端框架；
- 独立发版后仍要能按 RELEASING.md 的顺序回滚。

## 决策

1. **源码目录不动，发布链路单独产出。**
   `scripts/build_frontend_release.py` 从包内 `web/static/` 生成
   `dist/frontend/<version>/`，包含内容哈希资源、`manifest.json`、
   `SHA256SUMS`、`config.example.json`。包内 `static/` 继续作为桌面与
   API 托管回退，不参与发布包构建。

2. **前后端版本解耦，只保留兼容约束。**
   `package.json` 与 `version.ts` 必须一致；后端 `__version__` 与
   `pyproject.toml` 一致；前后端不再要求相等。兼容性由
   `min_frontend_version` / `min_api_version` 与 426 阻断保证。

3. **A2 托管可关闭，目录可外置。**
   新增 `AGENT_WEB_SERVE_FRONTEND`（默认 true）、`AGENT_WEB_FRONTEND_DIR`、
   `AGENT_WEB_STATIC_DIR`；桌面模式强制开启。显式配置的目录或发布包缺文件
   时启动失败，不静默退回旧产物。

4. **API 进入 v1，保留 v0 窗口。**
   `/api/v1/*` 由最内层中间件重写为 `/api/*`，路由只注册一份；
   `/api/*` 仍可用并返回 `Deprecation: true`。

5. **缓存按内容哈希分层。**
   `app.<hash>.js`、`style.<hash>.css`、hash 化的 vendor 资源
   `immutable`；`index.html`、`config.json`、`sw.js`、`manifest` 为
   `no-cache`；API 与 `/healthz` 为 `no-store`。Service Worker 的
   `CACHE` 名包含 `releaseId`，升级后旧壳不会被长期复用。

## 后果

### 好的

- 前端可独立构建、校验、部署、回滚，后端发布节奏不再被前端版本拖住；
- 发布包有清单与 SHA256，静态站/CDN 可以校验完整性并做长缓存；
- A2 可以只提供 API，边界更干净；
- 版本兼容从“人工对齐”变成可执行断言。

### 坏的 / 代价

- 发布包与包内 `static/` 各有一份产物，需要 CI 保证两者都来自同一次构建；
- 版本解耦后，兼容性依赖 `min_*_version` 的纪律，测试必须覆盖；
- `/api/v1` 重写中间件需要覆盖 SSE、上传与预检路径；
- 子路径/CDN 部署仍需部署侧处理 CSP、SW scope 与路径重写。

### 中性 / 需要留意

- 桌面与 PyInstaller 继续使用包内 `static/`，不读取 `dist/`；
- 发布包不入库（`dist/` 已忽略），CI artifact 才是交付物；
- `config.example.json` 只是模板，部署侧必须提供自己的 `config.json`。

## 复核

- **Verified by**：`scripts/check_frontend_release.py`、
  `tests/unit/test_frontend_release_contract.py`、
  `tests/unit/test_frontend_hosting.py` 与 REQ-212。
- **Revisit when**：引入 CDN 上传流水线、需要多前端版本并行灰度，
  或桌面改为从外部目录加载前端。
