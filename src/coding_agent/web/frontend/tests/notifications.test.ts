import { test } from 'node:test';
import assert from 'node:assert/strict';

import { shouldSystemNotify } from '../src/ui/notifications';

const base = {
  enabled: true,
  hidden: true,
  supported: true,
  permission: 'granted',
};

test('system notification requires enabled, hidden, supported and granted', () => {
  assert.equal(shouldSystemNotify(base), true);
  assert.equal(shouldSystemNotify({ ...base, enabled: false }), false);
  assert.equal(shouldSystemNotify({ ...base, hidden: false }), false);
  assert.equal(shouldSystemNotify({ ...base, supported: false }), false);
  assert.equal(shouldSystemNotify({ ...base, permission: 'default' }), false);
  assert.equal(shouldSystemNotify({ ...base, permission: 'denied' }), false);
});
