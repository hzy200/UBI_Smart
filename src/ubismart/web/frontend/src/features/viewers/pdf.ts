/* PDF viewer：pdf.js canvas + SVG 文本层，失败时退回浏览器内置 viewer。 */

import { loadVendor } from '../vendor-loader';
import { PDF_DOCS } from './state';
import type { FileViewBase, ViewerContext } from './types';

export async function flattenPdfOutline(
  pdf: any,
  items: any[],
  depth = 0,
  out: any[] = [],
): Promise<any[]> {
  for (const item of items || []) {
    let page = 0;
    try {
      const dest = typeof item.dest === 'string'
        ? await pdf.getDestination(item.dest)
        : item.dest;
      if (Array.isArray(dest) && dest[0]) {
        page = (await pdf.getPageIndex(dest[0])) + 1;
      }
    } catch (err) {
      page = 0;
    }
    const title = String(item.title || '').trim();
    if (title) {
      out.push({
        title,
        page,
        depth,
        _label: `${'  '.repeat(depth)}${title}${page ? `（第 ${page} 页）` : ''}`,
      });
    }
    if (item.items && item.items.length) {
      await flattenPdfOutline(pdf, item.items, depth + 1, out);
    }
  }
  return out;
}

export async function buildPdfView(
  ctx: ViewerContext,
  base: FileViewBase,
  path: string,
): Promise<FileViewBase> {
  let rawUrl = '';
  try {
    rawUrl = await ctx.fetchRawUrl(path);
  } catch (err) {
    return {
      ...base,
      _viewKind: 'unsupported',
      _note: `PDF 预览失败：${err}`,
    };
  }
  try {
    const buffer = await ctx.fetchRawBuffer(path);
    await loadVendor('pdfjs');
    const pdf = await window.pdfjsLib.getDocument({
      data: new Uint8Array(buffer),
      isEvalSupported: false,
    }).promise;
    const oldDoc = PDF_DOCS.get(path);
    if (oldDoc && oldDoc !== pdf) {
      try {
        oldDoc.destroy?.();
      } catch (err) {
        // 忽略旧文档释放失败。
      }
    }
    PDF_DOCS.set(path, pdf);
    let outline: any[] = [];
    let meta: Record<string, string> = {};
    try {
      outline = await flattenPdfOutline(pdf, await pdf.getOutline());
    } catch (err) {
      outline = [];
    }
    try {
      const info = (await pdf.getMetadata()).info || {};
      for (const [key, name] of [
        ['Title', 'title'],
        ['Author', 'author'],
        ['Subject', 'subject'],
        ['Keywords', 'keywords'],
        ['Creator', 'creator'],
        ['Producer', 'producer'],
      ]) {
        const value = String(info[key] || '').trim();
        if (value) meta[name] = value;
      }
    } catch (err) {
      meta = {};
    }
    const view = {
      ...base,
      _viewKind: 'pdf',
      _rawUrl: rawUrl,
      _pdfKey: path,
      _pdfPage: 1,
      _pdfPages: Number(pdf.numPages || 1),
      _pdfImage: '',
      _pdfOutline: outline,
      _pdfMeta: meta,
    };
    await ctx.renderPdfPage(view, 1);
    return view;
  } catch (err) {
    // pdf.js 不可用时退回浏览器内置 viewer。
    return { ...base, _viewKind: 'pdf', _rawUrl: rawUrl, _pdfImage: '' };
  }
}
