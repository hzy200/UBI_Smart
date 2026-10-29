import { test } from 'node:test';
import assert from 'node:assert/strict';

import { aggregateUsageByDay, buildUsageCsv } from '../src/admin/lib/usage';

test('aggregateUsageByDay sums users into one point per day', () => {
  const rows: any[] = [
    { day: '2026-10-02', user: 'b', tokens: 30, cost_cny: 3, runs: 1 },
    { day: '2026-10-01', user: 'a', tokens: 10, cost_cny: 1, runs: 2 },
    { day: '2026-10-01', user: 'b', tokens: 20, cost_cny: 2, runs: 1 },
  ];
  assert.deepEqual(aggregateUsageByDay(rows), [
    { day: '2026-10-01', tokens: 30, cost_cny: 3, runs: 3 },
    { day: '2026-10-02', tokens: 30, cost_cny: 3, runs: 1 },
  ]);
  assert.deepEqual(aggregateUsageByDay([]), []);
});

test('buildUsageCsv escapes commas, quotes and newlines', () => {
  const csv = buildUsageCsv([
    {
      day: '2026-10-01',
      user: 'a,"b"',
      input_tokens: 1,
      output_tokens: 2,
      worker_input_tokens: 3,
      worker_output_tokens: 4,
      cost_cny: 0.5,
      runs: 2,
      reserved_tokens: 0,
    },
  ] as any);
  const lines = csv.split('\r\n');
  assert.equal(lines[0].startsWith('日期,用户'), true);
  assert.match(lines[1], /"a,""b"""/);
  assert.match(lines[1], /,7,/);
  assert.match(lines[1], /,0\.5000,/);
});
