import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { dedupById } from '../src/findingDedup.js';
import { merge } from '../src/mergeFindings.js';
import {
  normalizeFieldNames,
  parseReviewMd,
  applyThresholdFilter,
  applyInjectionFilter,
  loadExclusions,
  applyExclusions,
} from '../src/filterFindings.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, '..', '..', 'tests', 'fixtures', 'parity');

// Recursive walk (not a flat readdir): finding_dedup/merge_findings use a flat
// <script>/<case>/ layout, but filter_findings groups cases one level deeper
// (<script>/<group>/<case>/, e.g. filter_findings/threshold/<case>/). Both are
// found uniformly by descending until a directory holds input.json.
function findCaseDirs(dir) {
  if (existsSync(join(dir, 'input.json'))) return [dir];
  const out = [];
  for (const d of readdirSync(dir, { withFileTypes: true })) {
    if (d.isDirectory()) out.push(...findCaseDirs(join(dir, d.name)));
  }
  return out;
}

export function loadCases(script) {
  const base = join(FIXTURES, script);
  return findCaseDirs(base)
    .sort()
    .map((caseDir) => ({
      name: relative(base, caseDir),
      input: JSON.parse(readFileSync(join(caseDir, 'input.json'), 'utf8')),
      expected: JSON.parse(readFileSync(join(caseDir, 'expected.json'), 'utf8')),
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

// --- filterFindings part 1: normalize / review_md / threshold / injection / exclusions ---

const idsOf = (list) => list.map((f) => f.id);

for (const c of loadCases('filter_findings')) {
  const fn = c.input.fn;
  test(`filter_findings parity: ${c.name} (${fn})`, () => {
    if (fn === 'normalize_field_names') {
      const findings = c.input.findings;
      normalizeFieldNames(findings);
      assert.deepEqual({ findings }, c.expected);
      return;
    }
    if (fn === 'parse_review_md') {
      assert.deepEqual(parseReviewMd(c.input.markdown), c.expected.config);
      return;
    }
    if (fn === 'load_exclusions') {
      assert.deepEqual(loadExclusions(c.input.markdown), c.expected.patterns);
      return;
    }
    if (fn === 'apply_threshold_filter') {
      const { kept, eliminated, contestedCount } = applyThresholdFilter(c.input.findings, c.input.config);
      assert.deepEqual(idsOf(kept), idsOf(c.expected.kept));
      assert.deepEqual(idsOf(eliminated), idsOf(c.expected.eliminated));
      assert.equal(contestedCount, c.expected.contested_count);
      return;
    }
    if (fn === 'apply_injection_filter') {
      const { kept, eliminated } = applyInjectionFilter(c.input.findings);
      assert.deepEqual(idsOf(kept), idsOf(c.expected.kept));
      assert.deepEqual(idsOf(eliminated), idsOf(c.expected.eliminated));
      // Free-text join format is not load-bearing (per the brief) — only presence matters.
      for (const e of eliminated) assert.ok(e.elimination_reason && e.elimination_reason.length > 0);
      return;
    }
    if (fn === 'apply_exclusions') {
      const { kept, eliminated } = applyExclusions(c.input.findings, c.input.exclusion_patterns);
      assert.deepEqual(idsOf(kept), idsOf(c.expected.kept));
      assert.deepEqual(idsOf(eliminated), idsOf(c.expected.eliminated));
      return;
    }
    throw new Error(`unhandled fn: ${fn}`);
  });
}
