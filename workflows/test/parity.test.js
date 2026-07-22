import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';
import { dedupById } from '../src/findingDedup.js';
import { merge } from '../src/mergeFindings.js';
import { applyValidations } from '../src/applyValidations.js';
import {
  normalizeFieldNames,
  parseReviewMd,
  applyThresholdFilter,
  applyInjectionFilter,
  loadExclusions,
  applyExclusions,
  detectDisagreement,
  routeByDimension,
  dedupCrossAgent,
  tagFindings,
} from '../src/filterFindings.js';
import { applyChallenges } from '../src/applyChallenges.js';

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

for (const c of loadCases('apply_validations')) {
  test(`apply_validations parity: ${c.name}`, () => {
    // applyValidations mutates findings in place -- clone so the fixture's
    // input.json (re-read by every test run) is never mutated across cases.
    const findings = structuredClone(c.input.findings);
    const { adjustedCount, unmatchedIds } = applyValidations(findings, c.input.validations);
    assert.deepEqual(
      { findings, adjusted_count: adjustedCount, unmatched_ids: unmatchedIds },
      c.expected,
    );
  });
}

function mapByAgent(files) {
  const out = {};
  for (const [name, text] of Object.entries(files || {})) {
    const agent = name.replace(/^(?:code-gauntlet|deep-review)-(text-)?/, '').replace(/-[^-]+\.(ndjson|txt)$/, '');
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
    // --- part 2: disagreement / dimension routing / cross-agent dedup / tag ---
    if (fn === 'detect_disagreement') {
      const { active, suppressed, boostedCount } = detectDisagreement(c.input.findings);
      // `active` carries no elimination_reason (free text) -- full structural
      // equality is meaningful and safe here, unlike the eliminated lists below.
      assert.deepEqual(active, c.expected.active);
      assert.deepEqual(idsOf(suppressed), idsOf(c.expected.suppressed));
      for (const s of suppressed) assert.ok(s.elimination_reason && s.elimination_reason.length > 0);
      assert.equal(boostedCount, c.expected.boosted_count);
      return;
    }
    if (fn === '_route_by_dimension') {
      assert.equal(routeByDimension(c.input.finding), c.expected.route);
      return;
    }
    if (fn === 'dedup_cross_agent') {
      const { kept, dropped } = dedupCrossAgent(c.input.findings);
      // `kept` has no elimination_reason field -- full equality. `dropped`'s
      // elimination_reason interpolates JSON.stringify (double quotes) where
      // Python interpolates !r (single quotes) -- cosmetic only, per the same
      // "free-text join format not load-bearing" rule as the injection filter.
      assert.deepEqual(kept, c.expected.kept);
      assert.deepEqual(idsOf(dropped), idsOf(c.expected.dropped));
      for (const d of dropped) assert.ok(d.elimination_reason && d.elimination_reason.length > 0);
      return;
    }
    if (fn === 'tag_findings') {
      const { tagged, dedupDropped, mainCount, suggestionCount } = tagFindings(c.input.findings);
      assert.deepEqual(tagged, c.expected.tagged);
      assert.deepEqual(idsOf(dedupDropped), idsOf(c.expected.dedup_dropped));
      for (const d of dedupDropped) assert.ok(d.elimination_reason && d.elimination_reason.length > 0);
      assert.equal(mainCount, c.expected.main_count);
      assert.equal(suggestionCount, c.expected.suggestion_count);
      return;
    }
    throw new Error(`unhandled fn: ${fn}`);
  });
}

// --- applyChallenges: composite comparator / deep-clone / dedup reuse ------

for (const c of loadCases('apply_challenges')) {
  test(`apply_challenges parity: ${c.name}`, () => {
    // deep_copy_no_mutation_of_input additionally asserts that calling
    // applyChallenges never mutates the caller's input findings array/objects
    // -- snapshot BEFORE the call, compare AFTER (applyChallenges itself is
    // called on c.input.findings directly, not cloned by the test, precisely
    // so a real aliasing bug would be caught here).
    const inputSnapshot = c.name === 'deep_copy_no_mutation_of_input' ? structuredClone(c.input.findings) : null;

    const { findings, eliminated, stats } = applyChallenges(c.input.findings, c.input.challenges);

    if (inputSnapshot) assert.deepEqual(c.input.findings, inputSnapshot);

    // `findings` (post-dedup, post-rank, ranked order matters) and `stats`
    // are fully structural -- no free-text fields. `eliminated` carries
    // elimination_reason (free text, e.g. dedup_cross_agent's JSON.stringify
    // vs Python's !r quoting) -- compared by id + eliminated_by only, same
    // convention as dedupCrossAgent's own parity test above.
    assert.deepEqual(findings, c.expected.findings);
    assert.deepEqual(
      eliminated.map((f) => ({ id: f.id, eliminated_by: f.eliminated_by })),
      c.expected.eliminated.map((f) => ({ id: f.id, eliminated_by: f.eliminated_by })),
    );
    for (const e of eliminated) assert.ok(e.elimination_reason && e.elimination_reason.length > 0);
    assert.deepEqual(stats, c.expected.stats);
  });
}
