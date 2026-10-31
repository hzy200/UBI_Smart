/* XLSX viewer：SheetJS 解析 + sheet 标签 + 窗口化行渲染。 */

import { loadVendor } from '../vendor-loader';
import { XLSX_BOOKS } from './state';
import {
  applySheetWindow,
  decorateTableBlock,
  XLSX_DEFAULT_VIEWPORT,
} from './table';
import type { FileViewBase, ViewerContext } from './types';

export async function buildSheetView(
  ctx: ViewerContext,
  base: FileViewBase,
  path: string,
): Promise<FileViewBase | null> {
  try {
    const buffer = await ctx.fetchRawBuffer(path);
    await loadVendor('xlsx');
    const workbook = window.XLSX.read(buffer, {
      type: 'array',
      cellFormula: true,
      cellNF: true,
    });
    XLSX_BOOKS.set(path, workbook);
    let truncated = '';
    const sheets = workbook.SheetNames.map((name, index) => {
      const worksheet = workbook.Sheets[name] as Record<string, any>;
      const rows = window.XLSX.utils.sheet_to_json(
        worksheet,
        { header: 1, raw: false, defval: '' },
      ).slice(0, 2000).map((row) => (
        (row || []).slice(0, 200).map((cell) => String(cell ?? ''))
      ));
      const range = window.XLSX.utils.decode_range(
        String(worksheet['!ref'] || 'A1'),
      );
      if (range.e.r + 1 > 2000 || range.e.c + 1 > 200) {
        truncated = '（大表仅显示前 2000 行 × 200 列）';
      }
      for (const merge of worksheet['!merges'] || []) {
        const row = rows[merge.s.r];
        if (!row) continue;
        const value = row[merge.s.c] ?? '';
        for (let r = merge.s.r; r <= merge.e.r; r += 1) {
          if (!rows[r]) continue;
          for (let c = merge.s.c; c <= merge.e.c; c += 1) {
            rows[r][c] = value;
          }
        }
      }
      const block = decorateTableBlock({
        kind: 'sheet',
        anchor: `sheet=${name}!A1`,
        text: `工作表 ${name}`,
        rows,
        _key: `${index}:sheet:${name}`,
      }, true);
      applySheetWindow(block, 0, XLSX_DEFAULT_VIEWPORT);
      return { name, blocks: [block] };
    });
    return {
      ...base,
      _viewKind: 'document',
      _xlsxKey: path,
      _xlsxSheets: sheets,
      _activeSheet: 0,
      _xlsxFormula: null,
      blocks: sheets[0] ? sheets[0].blocks : [],
      _note: truncated,
    };
  } catch (err) {
    // 退回服务端结构化解析。
    return null;
  }
}
