/* 服务端结构化文档 / PPTX 版式 viewer。 */

import { decorateTableBlock } from './table';
import type { FileViewBase, ViewerContext } from './types';

function decorateBlock(block: any, index: number): Record<string, any> {
  const key = `${index}:${block.anchor || block.kind}`;
  if (block.kind === 'table' || block.kind === 'sheet') {
    return decorateTableBlock({ ...block, _key: key });
  }
  const isImage = block.kind === 'image'
    && Boolean(block.meta && block.meta.data_url);
  return {
    ...block,
    _key: key,
    _isTable: false,
    _isImage: isImage,
    _dataUrl: isImage ? block.meta.data_url : '',
    _isTextBlock: !isImage,
  };
}

export async function buildDocumentView(
  ctx: ViewerContext,
  base: FileViewBase,
  path: string,
  ext: string,
): Promise<FileViewBase> {
  try {
    const model = await ctx.fetchDocument(
      path,
      ext === 'pptx',
      ext === 'pptx',
    );
    if (ext === 'pptx' && (model.slide_layouts || []).length) {
      return {
        ...base,
        _viewKind: 'slides',
        _slides: model.slide_layouts.map((slide: any) => ({
          ...slide,
          _key: `slide-${slide.page}`,
        })),
        _note: (model.warnings || []).join('\n'),
      };
    }
    const blocks = (model.blocks || []).map(decorateBlock);
    return {
      ...base,
      ...model,
      _viewKind: 'document',
      blocks,
      _note: (model.warnings || []).join('\n'),
    };
  } catch (err) {
    return {
      ...base,
      _viewKind: 'unsupported',
      _note: `文档解析失败：${err}`,
    };
  }
}
