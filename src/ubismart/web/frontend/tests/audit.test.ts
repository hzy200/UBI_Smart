import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buildAuditCsv } from '../src/admin/lib/audit';

test('buildAuditCsv escapes fields and formats results', () => {
  const rows: any[] = [
    {
      ts: '2026-10-29T10:00:00+00:00',
      tenant: 'alice',
      thread_id: 't,1',
      kind: 'tool_call',
      tool: 'shell_exec',
      level: 'L0',
      decision: 'auto',
      ok: true,
      record_hash: 'h1',
    },
    {
      ts: '2026-10-29T11:00:00+00:00',
      tenant: 'bob',
      thread_id: 't2',
      kind: 'run_end',
      tool: '',
      level: '',
      decision: '',
      ok: false,
      record_hash: 'h2',
    },
    {
      ts: '2026-10-29T12:00:00+00:00',
      tenant: 'bob',
      thread_id: 't3',
      kind: 'plan',
      tool: '',
      level: '',
      decision: '',
      ok: null,
      record_hash: 'h3',
    },
  ];
  const lines = buildAuditCsv(rows).split('\r\n');
  assert.equal(lines[0], '时间,用户,线程,类型,工具,等级,决定,结果,记录哈希');
  assert.match(lines[1], /"t,1"/);
  assert.match(lines[1], /成功/);
  assert.match(lines[2], /失败/);
  assert.equal(lines[3].split(',')[7], '');
});
