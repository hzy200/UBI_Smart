# Vendored frontend dependencies

这些文件按原样随应用分发，不经过 Node 构建链，也不从 CDN 加载。
`footer.html` 里的 `<script integrity>` 必须与下表 SHA-384 一致；升级时先更新
文件，再重算 SRI，最后跑 `scripts/build_frontend.py` 重建 `static/index.html`。

| 文件 | 版本 | 许可证 | SHA-256 | 来源 |
|---|---|---|---|---|
| `alpine-csp.min.js` | @alpinejs/csp 3.14.1 | MIT | `efb8d567b747c1a99eeea17b430b4a670518e7b3536ba3cbb5639cd6df2e777e` | npm `@alpinejs/csp@3.14.1` |
| `marked.min.js` | marked 18.1.2 | MIT | `d0d36428adbdc0ff293d0bbc79f11779b5c7a615f31acee9467b081fc7ce3a3d` | `marked@18.1.2/lib/marked.umd.js`（esbuild 压缩） |
| `purify.min.js` | DOMPurify 3.3.2 | Apache-2.0 OR MPL-2.0 | `d448d28fdc0e16a906823f0f4db4688288c159bf6b82dc37a48949db4231d380` | npm `dompurify@3.3.2` |
| `highlight.min.js` | highlight.js 11.11.1 | BSD-3-Clause | `f1e7fac1af8089b2281e623dbbd49e4dc307b51074e02a4d6473959688f556db` | npm `highlight.js@11.11.1` |

用途：alpine 提供无 `unsafe-eval` 的响应式绑定；marked 负责 Markdown 渲染，
DOMPurify 清洗模型输出防 XSS；highlight.js 负责代码块高亮（主题色由
style.css 提供）。

升级时请同步更新版本号、SHA-256 与 `footer.html` 的 SRI，并重新跑 GUI 的
手工检查（审批、流式输出、diff）。
