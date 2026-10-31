/* 流式 Markdown 的增量渲染。
 *
 * 只缓存「稳定块」（空行分隔、且不在未闭合围栏里的完整块），
 * 每次 token 追加只重渲染尾部；块边界遇到列表/引用/表格等
 * 可能跨空行延续的语法时保守地留在尾部，保证输出正确。
 */

export interface MarkdownCache {
  head: string;
  html: string;
}

/** 空行后的这一行是否仍属于上一个块（列表、引用、表格、缩进代码、HTML）。 */
export function isMarkdownContinuation(line: string): boolean {
  return /^\s*([-*+]\s|\d+[.)]\s|>|\|| {4}|\t|<)/.test(String(line || ''));
}

export function splitMarkdownTail(text: string): { head: string; tail: string } {
  const source = String(text ?? '');
  if (!source) return { head: '', tail: '' };
  const lines = source.split('\n');
  let fence = '';
  let offset = 0;
  let headEnd = 0;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const marker = line.match(/^\s*(`{3,}|~{3,})/);
    if (marker) {
      const kind = marker[1][0];
      if (!fence) fence = kind;
      else if (fence === kind) fence = '';
    }
    if (!fence && line.trim() === '') {
      let j = i + 1;
      while (j < lines.length && lines[j].trim() === '') j += 1;
      if (j >= lines.length) {
        headEnd = source.length;
      } else if (!isMarkdownContinuation(lines[j])) {
        // 空行本身属于稳定头，尾部从下一块开始。
        headEnd = offset + line.length + 1;
      }
    }
    offset += line.length + 1;
  }
  const end = Math.min(headEnd, source.length);
  return { head: source.slice(0, end), tail: source.slice(end) };
}

/**
 * 增量渲染：head 只渲染新增部分并追加到缓存，tail 每次重渲染。
 * `render` 由调用方注入（通常是经过 DOMPurify 的 renderMarkdown）。
 */
export function renderMarkdownIncremental(
  text: string,
  cache: MarkdownCache,
  render: (value: string) => string,
): string {
  const source = String(text ?? '');
  const renderFn = typeof render === 'function'
    ? render
    : (value: string) => String(value ?? '');
  const { head, tail } = splitMarkdownTail(source);
  if (!cache || typeof cache !== 'object') return renderFn(source);
  let html = cache.html || '';
  if (head !== cache.head) {
    if (!head) {
      html = '';
    } else if (cache.head && head.startsWith(cache.head) && html) {
      html += renderFn(head.slice(cache.head.length));
    } else {
      html = renderFn(head);
    }
    cache.head = head;
    cache.html = html;
  }
  return tail ? html + renderFn(tail) : html;
}
