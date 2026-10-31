/* 图片、Markdown、纯文本与不支持格式的 viewer。 */

import { renderMarkdown } from '../../ui/format';
import type { FileViewBase, ViewerContext } from './types';

export function buildImageView(base: FileViewBase, data: any): FileViewBase {
  return {
    ...base,
    _viewKind: 'image',
    _isImage: true,
    _text: `图片：${data.mime || ''}`,
    _imageNote: data.image_too_large ? '图片超过 2MB，暂不预览' : '',
  };
}

export function buildUnsupportedView(
  base: FileViewBase,
  ext: string,
  data: any,
): FileViewBase {
  return {
    ...base,
    _viewKind: 'unsupported',
    _note: ['doc', 'ppt'].includes(ext)
      ? '暂不支持直接预览旧版 .doc/.ppt，请先另存为 .docx/.pptx。'
      : data.image_too_large
        ? '图片超过 2MB，暂不预览。'
        : '二进制文件，暂不展示内容。',
  };
}

export function buildMarkdownView(
  base: FileViewBase,
  data: any,
): FileViewBase {
  const text = String(data.content || '')
    + (data.truncated ? '\n…（已截断）' : '');
  return {
    ...base,
    _viewKind: 'markdown',
    _text: text,
    _html: renderMarkdown(text),
  };
}

export function buildCodeView(
  ctx: ViewerContext,
  base: FileViewBase,
  path: string,
  data: any,
): FileViewBase {
  const text = String(data.content || '')
    + (data.truncated ? '\n…（已截断）' : '');
  const lines = text.split('\n').length;
  return {
    ...base,
    _viewKind: 'code',
    _isText: true,
    _text: text,
    _html: ctx.highlightCode(path, text, data.binary),
    _lineNumbers: Array.from(
      { length: lines },
      (_, index) => String(index + 1),
    ).join('\n'),
  };
}
