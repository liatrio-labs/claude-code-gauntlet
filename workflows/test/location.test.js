import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeLocation } from '../src/location.js';

for (const status of ['confirmed', 'relocated']) {
  test(`location aliases follow the canonical ${status} range`, () => {
    assert.deepEqual(normalizeLocation({ location_status: status, line_start: 7, line_end: 8 }), {
      location_status: status, line_start: 7, line_end: 8, line: 7, end_line: 8,
    });
  });
}

test('unconfirmed location clears delivery aliases', () => {
  assert.deepEqual(normalizeLocation({ location_status: 'unconfirmed', line: 99, end_line: 100 }), {
    location_status: 'unconfirmed',
  });
});
