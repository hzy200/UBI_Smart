/* viewer 分派：按格式选择实现，inspector 只负责调用与状态。 */

import { detectViewerKind } from '../viewer-kind';
import {
  buildCodeView,
  buildImageView,
  buildMarkdownView,
  buildUnsupportedView,
} from './basic';
import { buildDocxView } from './docx';
import { buildDocumentView } from './document';
import { buildPdfView } from './pdf';
import { buildSheetView } from './sheet';
import type { FileViewBase, ViewerContext } from './types';

export function baseFileView(
  ctx: ViewerContext,
  path: string,
  data: any,
): FileViewBase {
  return {
    ...data,
    _size: ctx.formatBytes(data.size),
    _lang: ctx.fileLanguage(path),
    _text: '',
    _isImage: false,
    _isText: false,
    _imageNote: '',
    _html: '',
    _lineNumbers: '',
    _crumb: String(path || '').split('/').join(' › '),
    _viewKind: 'code',
    _rawUrl: '',
    _note: '',
    _pdfKey: '',
    _pdfPage: 0,
    _pdfPages: 0,
    _pdfImage: '',
    _pdfTextSvg: '',
    _pdfScale: 1.5,
    _pdfOutline: [],
    _pdfMeta: {},
    _pdfSearch: '',
    _pdfSearchHits: [],
    _pdfSearchIndex: 0,
    _pdfContinuous: false,
    _pdfContinuousPages: [],
    _slides: [],
    _docxExtras: '',
    _xlsxKey: '',
    _xlsxSheets: [],
    _activeSheet: 0,
    _xlsxFormula: null,
    blocks: [],
  };
}

export async function buildFileView(
  ctx: ViewerContext,
  path: string,
  data: any,
): Promise<FileViewBase> {
  const ext = String(path || '').split('.').pop().toLowerCase();
  const base = baseFileView(ctx, path, data);
  const kind = detectViewerKind(path, data);
  if (kind === 'image') {
    return buildImageView(base, data);
  }
  if (kind === 'unsupported') {
    return buildUnsupportedView(base, ext, data);
  }
  if (kind === 'pdf') {
    return buildPdfView(ctx, base, path);
  }
  if (kind === 'document' && ext === 'docx') {
    const view = await buildDocxView(ctx, base, path);
    if (view) return view;
  }
  if (
    kind === 'document'
    && ['xlsx', 'xlsm', 'xls'].includes(ext)
  ) {
    const view = await buildSheetView(ctx, base, path);
    if (view) return view;
  }
  if (kind === 'document') {
    return buildDocumentView(ctx, base, path, ext);
  }
  if (kind === 'markdown') {
    return buildMarkdownView(base, data);
  }
  return buildCodeView(ctx, base, path, data);
}
