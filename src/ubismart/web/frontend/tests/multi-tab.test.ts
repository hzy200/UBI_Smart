import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseSyncMessage } from '../src/ui/multi-tab';

test('parseSyncMessage accepts typed messages and rejects malformed ones', () => {
  assert.deepEqual(
    parseSyncMessage('{"type":"sessions","at":1}'),
    { type: 'sessions', at: 1 },
  );
  assert.deepEqual(
    parseSyncMessage({ type: 'settings', settings: {} }),
    { type: 'settings', settings: {} },
  );
  assert.equal(parseSyncMessage('{bad json'), null);
  assert.equal(parseSyncMessage('"sessions"'), null);
  assert.equal(parseSyncMessage({ type: '' }), null);
  assert.equal(parseSyncMessage(null), null);
});
