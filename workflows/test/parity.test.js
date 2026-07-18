import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { dedupById } from '../src/findingDedup.js';
import { merge } from '../src/mergeFindings.js';

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

const sortedIds = (env) => env.findings.map((f) => f.id).sort();

for (const c of loadCases('merge_findings')) {
  test(`merge_findings parity: ${c.name}`, () => {
    // Feed raw file strings keyed by agent; the merge twin accepts {agent: rawString}.
    const got = merge(mapByAgent(c.input.findings_dir_files), mapByAgent(c.input.text_dir_files), c.input.args);
    const m = got.methodology, em = c.expected.methodology;
    // Full finding set by id + every numeric methodology count (not just 3 scalars).
    assert.deepEqual(sortedIds(got), c.expected.findings.map((f) => f.id).sort());
    // agents_dispatched is an array; strict `equal` compares by reference, so deepEqual.
    assert.deepEqual(m.agents_dispatched, em.agents_dispatched);
    assert.deepEqual(m.findings_per_channel, em.findings_per_channel);
    assert.equal(m.duplicates_resolved, em.duplicates_resolved);
    assert.equal(m.dropped_no_id, em.dropped_no_id);
    // Warning ARRAY LENGTHS only — bodies are free-text (substring rule), so not byte-compared.
    assert.equal(m.truncation_warnings.length, em.truncation_warnings.length);
    assert.equal(m.validation_warnings.length, em.validation_warnings.length);
  });
}

function mapByAgent(files) {
  const out = {};
  for (const [name, text] of Object.entries(files || {})) {
    const agent = name.replace(/^deep-review-(text-)?/, '').replace(/-[^-]+\.(ndjson|txt)$/, '');
    out[agent] = text;
  }
  return out;
}
