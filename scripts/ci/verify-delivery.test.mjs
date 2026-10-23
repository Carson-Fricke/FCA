import assert from 'node:assert/strict';
import test from 'node:test';
import { deliveryAllowed, pendingPaths } from './verify-delivery.mjs';

test('staged, unstaged, untracked and renamed files all count as pending delivery', () => {
  const paths = pendingPaths(' M sdk.rs\0M  staged.py\0?? new.py\0R  renamed.py\0old.py\0');
  assert.deepEqual(paths, ['sdk.rs', 'staged.py', 'new.py', 'renamed.py']);
  assert.equal(deliveryAllowed(paths), false);
  assert.equal(deliveryAllowed(paths, '  '), false);
  assert.equal(deliveryAllowed(paths, 'User explicitly requested keeping the UI changes local'), true);
  assert.equal(deliveryAllowed([]), true);
});
