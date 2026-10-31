/* 纯展示格式化与 Markdown 渲染。 */

export function escapeHtml(text) {
  return String(text || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function formatDuration(ms) {
  const value = Number(ms || 0);
  if (!value) return '';
  if (value < 1000) return `${value}ms`;
  return `${(value / 1000).toFixed(1)}s`;
}

export function formatTokens(input, output) {
  const inTokens = Number(input || 0);
  const outTokens = Number(output || 0);
  if (!inTokens && !outTokens) return '';
  return `${inTokens} in / ${outTokens} out tokens`;
}

export function renderMarkdown(text) {
  const source = String(text || '');
  if (!source) return '';
  try {
    if (window.marked && window.DOMPurify) {
      return window.DOMPurify.sanitize(window.marked.parse(source), {
        USE_PROFILES: { html: true },
      });
    }
  } catch (e) { /* 降级为纯文本 */ }
  return escapeHtml(source);
}
