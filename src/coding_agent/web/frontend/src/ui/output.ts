/* 大工具输出的分页窗口，避免一次性把整段输出塞进 DOM。 */

export interface OutputPage {
  text: string;
  total: number;
  shown: number;
  hidden: number;
  truncated: boolean;
  hint: string;
}

export function outputPage(text: unknown, limitLines = 40): OutputPage {
  const source = String(text ?? '');
  const lines = source.split('\n');
  const limit = Math.max(1, Number(limitLines) || 40);
  if (lines.length <= limit) {
    return {
      text: source,
      total: lines.length,
      shown: lines.length,
      hidden: 0,
      truncated: false,
      hint: '',
    };
  }
  return {
    text: lines.slice(0, limit).join('\n'),
    total: lines.length,
    shown: limit,
    hidden: lines.length - limit,
    truncated: true,
    hint: `显示 ${limit}/${lines.length} 行`,
  };
}
