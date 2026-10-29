/* 审批历史导出。 */

import type { ApprovalRow } from '../types';
import { csvText } from './csv';

const STATUS_LABELS: Record<string, Record<string, string>> = {
  'zh-CN': {
    pending: '待审批',
    approved: '已批准',
    denied: '已拒绝',
    expired: '已过期',
  },
  'en-US': {
    pending: 'Pending',
    approved: 'Approved',
    denied: 'Denied',
    expired: 'Expired',
  },
};

export function approvalStatusLabel(
  status: string,
  locale = 'zh-CN',
): string {
  const table = STATUS_LABELS[locale] || STATUS_LABELS['zh-CN'];
  return table[String(status || '')] || String(status || '');
}

export function buildApprovalCsv(
  rows: ApprovalRow[],
  locale = 'zh-CN',
): string {
  const en = locale === 'en-US';
  const header = en
    ? ['Created', 'Requester', 'Thread', 'Level', 'Tool', 'Command', 'Status', 'Approver', 'Decided']
    : ['创建时间', '发起人', '线程', '等级', '工具', '命令', '状态', '审批人', '决定时间'];
  const body = (rows || []).map((row) => [
    row.created_at_iso || row.created_at,
    row.requested_by,
    row.thread_id,
    row.level || `L${row.level_int}`,
    row.tool,
    row.command,
    approvalStatusLabel(row.status, locale),
    row.decided_by || '',
    row.decided_at_iso || row.decided_at || '',
  ]);
  return csvText(header, body);
}
