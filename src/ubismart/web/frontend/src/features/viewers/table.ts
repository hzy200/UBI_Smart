/* 表格渲染与 XLSX 行窗口化。 */

import { XLSX_BOOKS } from './state';

export const XLSX_ROW_HEIGHT = 24;
export const XLSX_OVERSCAN = 12;
export const XLSX_DEFAULT_VIEWPORT = 480;

export interface RenderRow {
  _key: string;
  _index: number;
  cells: string[];
}

export interface RenderBlock extends Record<string, any> {
  _visibleRows: RenderRow[];
  _rowStart: number;
  _rowEnd: number;
  _colCount: number;
  _xlsxWindow: boolean;
}

function renderRows(slice: string[][], start: number): RenderRow[] {
  return slice.map((cells, offset) => ({
    _key: `r${start + offset}`,
    _index: start + offset,
    cells,
  }));
}

export function decorateTableBlock(
  block: Record<string, any>,
  xlsxWindow = false,
): RenderBlock {
  const rows: string[][] = Array.isArray(block.rows)
    ? block.rows.map((row: any) => (Array.isArray(row) ? row : []))
    : [];
  const colCount = rows.reduce(
    (max, row) => Math.max(max, row.length),
    0,
  );
  return {
    ...block,
    _isTable: true,
    _isImage: false,
    _isTextBlock: false,
    _visibleRows: renderRows(rows, 0),
    _rowStart: 0,
    _rowEnd: rows.length,
    _colCount: colCount || 1,
    _xlsxWindow: xlsxWindow,
  } as RenderBlock;
}

/**
 * 只保留可视窗口内的行，并用上下占位行维持滚动条高度。
 * 返回是否真的改变了窗口，避免无意义地重建 fileView。
 */
export function applySheetWindow(
  block: RenderBlock,
  scrollTop: number,
  viewportHeight: number,
): boolean {
  const rows: string[][] = Array.isArray(block.rows) ? block.rows : [];
  const total = rows.length;
  const height = Math.max(1, Number(viewportHeight) || XLSX_DEFAULT_VIEWPORT);
  const visible = Math.max(
    1,
    Math.ceil(height / XLSX_ROW_HEIGHT) + XLSX_OVERSCAN * 2,
  );
  const first = Math.max(
    0,
    Math.floor(Math.max(0, Number(scrollTop) || 0) / XLSX_ROW_HEIGHT)
      - XLSX_OVERSCAN,
  );
  const start = Math.min(Math.max(0, total - visible), first);
  const end = Math.min(total, start + visible);
  const changed = start !== block._rowStart || end !== block._rowEnd;
  block._rowStart = start;
  block._rowEnd = end;
  block._visibleRows = renderRows(rows.slice(start, end), start);
  return changed;
}

/**
 * CSP 下内联 style 属性会被浏览器丢弃（`style-src 'self'`），
 * 因此占位高度必须通过 CSSOM 命令式设置。
 */
export function syncSpacerHeights(
  block: RenderBlock,
  container: Element | null,
): void {
  if (!container) return;
  const top = container.querySelector(
    '.xlsx-spacer-top',
  ) as HTMLElement | null;
  const bottom = container.querySelector(
    '.xlsx-spacer-bottom',
  ) as HTMLElement | null;
  if (top) top.style.height = `${block._rowStart * XLSX_ROW_HEIGHT}px`;
  if (bottom) {
    const total = Array.isArray(block.rows) ? block.rows.length : 0;
    bottom.style.height =
      `${Math.max(0, total - block._rowEnd) * XLSX_ROW_HEIGHT}px`;
  }
}

export function selectXlsxCellData(
  view: any,
  row: number,
  col: number,
): { ref: string; formula: string; value: string } | null {
  if (!view) return null;
  const sheet = view._xlsxSheets
    && view._xlsxSheets[view._activeSheet || 0];
  const workbook = XLSX_BOOKS.get(view._xlsxKey);
  if (!sheet || !workbook) return null;
  const worksheet = workbook.Sheets[sheet.name];
  const ref = window.XLSX.utils.encode_cell({ r: row, c: col });
  const cell = (worksheet && worksheet[ref]) || {};
  return {
    ref,
    formula: cell.f ? `=${cell.f}` : '',
    value: String(cell.w ?? cell.v ?? ''),
  };
}
