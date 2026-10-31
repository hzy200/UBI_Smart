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
| `mammoth.browser.min.js` | mammoth 1.8.0 | BSD-2-Clause | `deb07bf230d1cb3e190bc5adc6743f35c6531b6571d1e5469b24f452a7f0f4ab` | `mammoth@1.8.0/mammoth.browser.min.js` |
| `xlsx.full.min.js` | SheetJS 0.18.5 | Apache-2.0 | `c9506197caf809a075b6dee1da0d36fb19da7158ffe8a88e7b0c96c5d8623c99` | `xlsx@0.18.5/dist/xlsx.full.min.js` |
| `pdf.min.js` | pdfjs-dist 3.11.174 | Apache-2.0 | `5b5799e6f8c680663207ac5b42ee14eed2a406fa7af48f50c154f0c0b1566946` | `pdfjs-dist@3.11.174/build/pdf.min.js` |
| `pdf.worker.min.js` | pdfjs-dist 3.11.174 | Apache-2.0 | `feabdf309770ed24bba31a5467836cdc8cf639c705af27d52b585b041bb8527b` | `pdfjs-dist@3.11.174/build/pdf.worker.min.js` |

用途：alpine 提供无 `unsafe-eval` 的响应式绑定；marked 负责 Markdown 渲染，
DOMPurify 清洗模型输出防 XSS；highlight.js 负责代码块高亮（主题色由
style.css 提供）。mammoth 把 DOCX 转 HTML，SheetJS 解析 XLS/XLSX，
pdf.js 渲染 PDF 页面；三者按需动态加载，SRI 写在
`frontend/src/features/vendor-loader.ts`。

升级时请同步更新版本号、SHA-256 与 `footer.html` 的 SRI，并重新跑 GUI 的
手工检查（审批、流式输出、diff）。
