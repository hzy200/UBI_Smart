/* 审计导出；只做格式化，不改变后端索引语义。 */

import type { AuditRecord } from '../types';
import { csvText } from './csv';

export function buildAuditCsv(
  rows: AuditRecord[],
  locale = 'zh-CN',
): string {
  const en = locale === 'en-US';
  const header = en
    ? ['Time', 'User', 'Thread', 'Kind', 'Tool', 'Level', 'Decision', 'Result', 'Hash']
    : ['时间', '用户', '线程', '类型', '工具', '等级', '决定', '结果', '记录哈希'];
  const body = (rows || []).map((row) => [
    row.ts,
    row.tenant,
    row.thread_id,
    row.kind,
    row.tool || '',
    row.level || '',
    row.decision || '',
    row.ok === null || row.ok === undefined
      ? ''
      : row.ok
        ? (en ? 'success' : '成功')
        : (en ? 'failure' : '失败'),
    row.record_hash,
  ]);
  return csvText(header, body);
}
