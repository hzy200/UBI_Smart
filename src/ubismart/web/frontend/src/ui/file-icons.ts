/* 文件树图标与缩进引导：纯展示数据。 */

const ICONS: Record<string, { id: string; cls: string }> = {
  py: { id: 'i-file-code', cls: 'icon-py' },
  js: { id: 'i-file-code', cls: 'icon-js' },
  mjs: { id: 'i-file-code', cls: 'icon-js' },
  cjs: { id: 'i-file-code', cls: 'icon-js' },
  jsx: { id: 'i-file-code', cls: 'icon-js' },
  ts: { id: 'i-file-code', cls: 'icon-ts' },
  tsx: { id: 'i-file-code', cls: 'icon-ts' },
  json: { id: 'i-file-data', cls: 'icon-data' },
  yaml: { id: 'i-file-data', cls: 'icon-data' },
  yml: { id: 'i-file-data', cls: 'icon-data' },
  toml: { id: 'i-file-data', cls: 'icon-data' },
  ini: { id: 'i-file-data', cls: 'icon-data' },
  md: { id: 'i-file-text', cls: 'icon-text' },
  mdx: { id: 'i-file-text', cls: 'icon-text' },
  txt: { id: 'i-file-text', cls: 'icon-text' },
  rst: { id: 'i-file-text', cls: 'icon-text' },
  png: { id: 'i-file-image', cls: 'icon-image' },
  jpg: { id: 'i-file-image', cls: 'icon-image' },
  jpeg: { id: 'i-file-image', cls: 'icon-image' },
  gif: { id: 'i-file-image', cls: 'icon-image' },
  webp: { id: 'i-file-image', cls: 'icon-image' },
  bmp: { id: 'i-file-image', cls: 'icon-image' },
  ico: { id: 'i-file-image', cls: 'icon-image' },
  svg: { id: 'i-file-image', cls: 'icon-image' },
  css: { id: 'i-file-style', cls: 'icon-style' },
  scss: { id: 'i-file-style', cls: 'icon-style' },
  sass: { id: 'i-file-style', cls: 'icon-style' },
  html: { id: 'i-file-html', cls: 'icon-html' },
  htm: { id: 'i-file-html', cls: 'icon-html' },
  xml: { id: 'i-file-html', cls: 'icon-html' },
  sh: { id: 'i-file-shell', cls: 'icon-shell' },
  bash: { id: 'i-file-shell', cls: 'icon-shell' },
  ps1: { id: 'i-file-shell', cls: 'icon-shell' },
};

export function fileIcon(name: string, isDir: boolean, expanded = false) {
  if (isDir) {
    return {
      _iconId: expanded ? '#i-folder-open' : '#i-folder',
      _iconCls: 'tree-icon icon-folder',
    };
  }
  const base = String(name || '').split('/').pop() || '';
  const ext = base.includes('.')
    ? base.split('.').pop()!.toLowerCase()
    : '';
  const icon = ICONS[ext] || { id: 'i-file', cls: 'icon-default' };
  return {
    _iconId: `#${icon.id}`,
    _iconCls: `tree-icon ${icon.cls}`,
  };
}

export function treeGuides(depth: number) {
  const total = Math.max(0, Number(depth) || 0);
  return Array.from({ length: total }, (_, index) => ({
    _key: `guide-${index}`,
    _style: `left:${14 * (index + 1)}px`,
  }));
}
