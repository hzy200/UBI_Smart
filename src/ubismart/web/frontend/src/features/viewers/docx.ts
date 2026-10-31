/* DOCX viewer：mammoth 转 HTML + DOMPurify，并附加服务端结构信息。 */

import { escapeHtml } from '../../ui/format';
import { loadVendor } from '../vendor-loader';
import type { FileViewBase, ViewerContext } from './types';

export async function buildDocxView(
  ctx: ViewerContext,
  base: FileViewBase,
  path: string,
): Promise<FileViewBase | null> {
  try {
    const buffer = await ctx.fetchRawBuffer(path);
    await loadVendor('mammoth');
    const result = await window.mammoth.convertToHtml({
      arrayBuffer: buffer,
    });
    const html = window.DOMPurify
      ? window.DOMPurify.sanitize(result.value, {
        USE_PROFILES: { html: true },
      })
      : escapeHtml(result.value);
    let extras = '';
    try {
      const model = await ctx.fetchDocument(path);
      extras = (model.blocks || [])
        .filter((block) => (
          ['header', 'footer', 'section', 'note'].includes(block.kind)
        ))
        .map((block) => `[${block.anchor}] ${block.text}`)
        .join('\n');
    } catch (err) {
      extras = '';
    }
    return {
      ...base,
      _viewKind: 'html',
      _text: 'Word 文档',
      _html: html,
      _docxExtras: extras,
    };
  } catch (err) {
    // 退回服务端结构化解析。
    return null;
  }
}
