/* 管理端用量聚合与 CSV 导出；不依赖 DOM，便于单测。 */

import type { UsageRow } from '../types';
import { csvText } from './csv';

export interface UsageDayPoint {
  day: string;
  tokens: number;
  cost_cny: number;
  runs: number;
}

export function aggregateUsageByDay(rows: UsageRow[]): UsageDayPoint[] {
  const map = new Map<string, UsageDayPoint>();
  for (const row of rows || []) {
    const day = String(row.day || '');
    if (!day) continue;
    const item = map.get(day) || { day, tokens: 0, cost_cny: 0, runs: 0 };
    item.tokens += Number(row.tokens || 0);
    item.cost_cny += Number(row.cost_cny || 0);
    item.runs += Number(row.runs || 0);
    map.set(day, item);
  }
  return [...map.values()].sort((left, right) =>
    left.day.localeCompare(right.day),
  );
}

export function buildUsageCsv(
  rows: UsageRow[],
  locale = 'zh-CN',
): string {
  const header = locale === 'en-US'
    ? [
        'Date',
        'User',
        'Input tokens',
        'Output tokens',
        'Worker tokens',
        'Cost (CNY)',
        'Runs',
        'Reserved tokens',
      ]
    : [
        '日期',
        '用户',
        '输入 tokens',
        '输出 tokens',
        'Worker tokens',
        '成本(CNY)',
        '运行次数',
        '预留 tokens',
      ];
  const body = (rows || []).map((row) => [
    row.day,
    row.user,
    row.input_tokens,
    row.output_tokens,
    Number(row.worker_input_tokens || 0)
      + Number(row.worker_output_tokens || 0),
    Number(row.cost_cny || 0).toFixed(4),
    row.runs,
    row.reserved_tokens,
  ]);
  return csvText(header, body);
}
