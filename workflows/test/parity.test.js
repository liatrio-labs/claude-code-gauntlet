import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { dedupById } from '../src/findingDedup.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, '..', '..', 'tests', 'fixtures', 'parity');

export function loadCases(script) {
  const base = join(FIXTURES, script);
  return readdirSync(base, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => ({
      name: d.name,
      input: JSON.parse(readFileSync(join(base, d.name, 'input.json'), 'utf8')),
      expected: JSON.parse(readFileSync(join(base, d.name, 'expected.json'), 'utf8')),
    }));
}

for (const c of loadCases('finding_dedup')) {
  test(`finding_dedup parity: ${c.name}`, () => {
    const got = dedupById(c.input.ndjson_findings, c.input.text_findings);
    assert.deepEqual(
      { merged: got.merged, duplicates_resolved: got.duplicatesResolved, dropped_no_id: got.droppedNoId },
      c.expected,
    );
  });
}
