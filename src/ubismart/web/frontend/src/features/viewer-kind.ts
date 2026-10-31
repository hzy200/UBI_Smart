/* 文件预览的类型判定。纯函数，便于单测与 CSP 构建复用。 */

export type ViewerKind =
  | 'image'
  | 'pdf'
  | 'document'
  | 'unsupported'
  | 'markdown'
  | 'code';

const DOCUMENT_EXTS = ['docx', 'xlsx', 'xlsm', 'xls', 'pptx'];
const LEGACY_EXTS = ['doc', 'ppt'];
const MARKDOWN_EXTS = ['md', 'markdown'];

export function detectViewerKind(
  path: string,
  data?: { image?: boolean; data_url?: string; binary?: boolean; image_too_large?: boolean },
): ViewerKind {
  const ext = String(path || '').split('.').pop().toLowerCase();
  // SVG 可能内嵌脚本；按源码展示，不交给 <img> 渲染。
  if (ext === 'svg') return 'code';
  if (data && data.image && data.data_url) return 'image';
  if (ext === 'pdf') return 'pdf';
  if (DOCUMENT_EXTS.includes(ext)) return 'document';
  if (LEGACY_EXTS.includes(ext)) return 'unsupported';
  if (MARKDOWN_EXTS.includes(ext)) return 'markdown';
  if (data && (data.image_too_large || data.binary)) return 'unsupported';
  return 'code';
}
