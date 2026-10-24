# Vendored frontend dependencies

这些文件按原样随应用分发，不经过 Node 构建链，也不从 CDN 加载。

| 文件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| `alpine-csp.min.js` | @alpinejs/csp 3.14.1 | MIT | 无 `unsafe-eval` 的响应式绑定 |
| `marked.min.js` | marked 12.0.2 | MIT | Markdown 渲染 |
| `purify.min.js` | DOMPurify 3.1.6 | Apache-2.0 OR MPL-2.0 | 清洗模型输出，防 XSS |
| `highlight.min.js` | highlight.js 11.11.1 | BSD-3-Clause | 代码块语法高亮（主题色由 style.css 提供） |

升级时请同步更新版本号并重新跑 GUI 的手工检查（审批、流式输出、diff）。
