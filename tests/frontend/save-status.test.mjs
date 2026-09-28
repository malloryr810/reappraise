import assert from 'node:assert/strict';
import { test } from 'node:test';

import { saveStatus } from '../../frontend/save-status.js';

test('a saved appraisal offers the outcome field and no badge', () => {
  assert.deepEqual(saveStatus({ item_id: 3, estimate_id: 7 }), { isSaved: true, badge: null });
});

for (const [name, data] of [
  ['estimate_id is null (persistence off or the save failed)', { item_id: null, estimate_id: null }],
  ['estimate_id is missing', { item: 'lamp' }],
  ['estimate_id is not an integer', { estimate_id: '7' }],
  ['there is no response body', null],
]) {
  test(`shows "Not saved" and hides the outcome field when ${name}`, () => {
    assert.deepEqual(saveStatus(data), { isSaved: false, badge: 'Not saved' });
  });
}
