import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  approvalStatusLabel,
  buildApprovalCsv,
} from '../src/admin/lib/approvals';

test('approvalStatusLabel maps known states', () => {
  assert.equal(approvalStatusLabel('approved'), '已批准');
  assert.equal(approvalStatusLabel('denied'), '已拒绝');
  assert.equal(approvalStatusLabel('other'), 'other');
});

test('buildApprovalCsv escapes command and formats times', () => {
  const rows: any[] = [
    {
      id: 'a1',
      tenant: 'default',
      thread_id: 't1',
      call_id: 'c1',
      tool: 'shell_exec',
      command: 'rm -rf "build,dist"',
      level: 'L3',
      level_int: 3,
      requested_by: 'bob',
      status: 'approved',
      decided_by: 'admin',
      decided_at: '1790000000',
      created_at: '1789990000',
      created_at_iso: '2026-10-29T10:00:00+00:00',
      decided_at_iso: '2026-10-29T11:00:00+00:00',
    },
  ];
  const lines = buildApprovalCsv(rows).split('\r\n');
  assert.equal(
    lines[0],
    '创建时间,发起人,线程,等级,工具,命令,状态,审批人,决定时间',
  );
  assert.match(lines[1], /"rm -rf ""build,dist"""/);
  assert.match(lines[1], /已批准/);
  assert.match(lines[1], /2026-10-29T11:00:00\+00:00$/);
});
