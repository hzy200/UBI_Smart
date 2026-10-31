/* CSV 生成与下载；转义按 RFC 4180。 */

export function csvCell(value: unknown): string {
  const text = String(value ?? '');
  return /[",\r\n]/.test(text)
    ? `"${text.replace(/"/g, '""')}"`
    : text;
}

export function csvText(
  header: readonly string[],
  rows: readonly unknown[][],
): string {
  return [Array.from(header), ...rows]
    .map((row) => row.map(csvCell).join(','))
    .join('\r\n');
}

export function downloadCsv(filename: string, text: string): void {
  // BOM 让 Excel 正确识别 UTF-8 中文表头。
  const blob = new Blob(['\ufeff' + text], {
    type: 'text/csv;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
