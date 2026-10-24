---
layer: L3
doc_id: phase5
title: Phase 5：质量与可选能力
owner: 项目作者
status: active
last_reviewed: 2026-10-24
review_cadence: 诊断/更新/重启策略或可选能力变动时
---

# Phase 5：质量与可选能力

Phase 5 聚焦可运维性与低风险增强：

- 诊断包（脱敏日志 + 版本 + WSL 概况）；
- WSL server 崩溃看门狗与自动重启；
- 更新检查（只提示与打开下载页，不自动安装）；
- 大 diff 折叠、文件树懒加载、Git diff 截断等性能细节；
- 本地 OpenAI 兼容端点预设（Ollama / LM Studio）。

## 1. 诊断包

桌面设置页“诊断与更新”→“生成诊断包”，调用
`DesktopApi.collect_diagnostics()`，产物：

```text
%LOCALAPPDATA%/CodingAgent/diagnostics/CodingAgent-diagnostics-YYYYMMDD-HHMMSS.zip
  ├─ summary.json        # 版本、Python、平台、配置（无密钥）
  ├─ launcher.json
  ├─ logs/launcher.log   # 尾部最多 2 MB
  ├─ logs/server.log
  ├─ wsl.txt             # 发行版列表、wsl --version、HOME、数据目录探针
  └─ README.txt
```

- 日志与 summary 经 `sanitize()`：`sk-...`、`Bearer ...`、
  `api_key/token/secret=...` 一律替换为 `[redacted]`；
- 不读取工作区源码，不包含 API Key；
- 诊断包位置可“打开所在目录”，且只允许 app 数据目录内的文件。

## 2. 崩溃看门狗

`desktop/watchdog.py` 在窗口打开期间每 2 秒检查 `server.proc.poll()`：

1. 进程意外退出 → 按 `1s / 3s / 7s` 退避，最多重启 3 次；
2. 成功 → 用新端口/新 token 调用 `window.load_url()` 重载页面；
3. 全部失败 → 记录日志并停止自动恢复；
4. 用户关窗 → `stop_event` 置位，不再拉起新进程。

重启不改变配置与数据目录；运行中的会话会因进程退出丢失内存态，
checkpoint/审计保留，可在恢复中心继续。

## 3. 更新检查

设置 `AGENT_UPDATE_MANIFEST_URL` 指向 JSON 清单：

```json
{
  "version": "0.2.0",
  "download_url": "https://example.com/CodingAgent-Setup.exe",
  "sha256": "...",
  "notes": "修复 ..."
}
```

- 只比较点分版本号并返回 `available`；
- “下载更新”只打开系统浏览器，不自动下载/安装；
- 清单地址或下载地址必须为 http(s)；
- 未配置清单时明确提示“未配置”。

**刻意不做自动安装/回滚**：在没有代码签名与可信更新源时，自动替换 exe
风险高于收益。建议流程仍是：下载 → 校验 sha256 → 手动运行安装包；
安装包升级替换 `{app}`，`%LOCALAPPDATA%\CodingAgent` 与 WSL 数据保留。

## 4. 性能细节

- 快照 diff 超过 200 行默认折叠，可“展开全部 diff”；
- 文件树逐层懒加载，目录上限 400 项；
- Git diff 截断 120 K 字符，`/mnt` 超时只降级 diff；
- 消息流式 token 仍按 80 ms 批量渲染 Markdown。

## 5. 本地模型

Provider 档案表单提供“本地端点预设”：

```text
name=local
base_url=http://127.0.0.1:11434/v1
model=qwen2.5-coder
```

适用于 Ollama、LM Studio 等 OpenAI-compatible 服务；保存后仍需在
“模型密钥”里写入一个占位/真实 key（本地服务通常接受任意非空值）。
桌面版仍只通过专用 WSL 内的 Agent Server 访问该端点。

## 6. 验收

- `collect_diagnostics` 生成的 zip 包含 summary/日志/WSL 信息，
  且测试密钥不会出现在任何成员中；
- 看门狗在崩溃后重试并回调新 server，耗尽次数后返回 `failed`；
- 更新检查正确比较版本、拒绝非法清单与 ftp 地址；
- 大 diff 折叠与本地预设为前端行为，随 `node --check` 与端到端冒烟验证。

本机验证（2026-10-24）：

- 单测覆盖诊断脱敏/zip 结构、看门狗重试与停止、更新清单解析；
- Portable 冷启动后 `kill -9` WSL 内 server：约 13 秒后启动器记录
  “WSL server 已重启，端口 49310”，新进程在另一个端口监听；
- 未配置更新清单时界面显示“未配置”，不发起网络请求；
- Portable 冷启动与安装包流程沿用 Phase 4 验证。

## 7. 尚未完成（可选能力）

- 自动下载、签名校验与版本回滚流水线；
- MCP server 客户端与插件市场；
- 多窗口/多工作区并行窗口；
- VS Code/JetBrains 扩展；
- 原生 Windows 沙箱。
