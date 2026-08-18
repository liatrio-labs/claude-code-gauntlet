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
  consolidateCrossAgent,
  tagFindings,
} from '../src/filterFindings.js';
import { applyChallenges } from '../src/applyChallenges.js';
import { joinVerifyDeltas, deltaContentProof, fnv1a32 } from '../src/stages.js';

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
      // suggestion_removal_reason is MOSTLY free text (Python !r vs JS
      // pattern-source quoting differ), but the "suggestion <noun phrase>: "
      // prefix up to and including the ": " separator is identical across
      // runtimes by construction (both read it from the same SUGGESTION_SETS
      // phrase strings) -- compare that prefix byte-exactly so renaming any of
      // the 7 set labels goes red here, and leave only the pattern-spelling
      // tail (after the ": ") presence-only. The non-string-suggestion reason
      // ("suggestion is not a string") carries no pattern tail and no colon
      // separator, so it gets its own byte-exact branch instead.
      kept.forEach((got, i) => {
        const exp = c.expected.kept[i];
        if ('suggestion_removal_reason' in exp) {
          const expReason = exp.suggestion_removal_reason;
          const gotReason = got.suggestion_removal_reason;
          assert.ok(gotReason && gotReason.length > 0);
          if (expReason === 'suggestion is not a string') {
            assert.equal(gotReason, expReason);
          } else {
            const sepIdx = expReason.indexOf(': ');
            assert.ok(sepIdx !== -1, `golden reason missing ': ' separator: ${expReason}`);
            const expPrefix = expReason.slice(0, sepIdx + 2);
            assert.equal(gotReason.slice(0, expPrefix.length), expPrefix);
          }
          const { suggestion_removal_reason: _g, ...gotRest } = got;
          const { suggestion_removal_reason: _e, ...expRest } = exp;
          assert.deepEqual(gotRest, expRest);
        } else {
          assert.deepEqual(got, exp);
        }
      });
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
    if (fn === 'consolidate_cross_agent') {
      const { findings, consolidatedCount } = consolidateCrossAgent(c.input.findings);
      // Nothing is dropped (#22 D1) -- full structural equality, including the
      // stamped consolidation_key/consolidation_primary fields.
      assert.deepEqual(findings, c.expected.findings);
      assert.equal(consolidatedCount, c.expected.consolidated_count);
      return;
    }
    if (fn === 'tag_findings') {
      const { tagged, consolidatedCount, mainCount, suggestionCount } = tagFindings(c.input.findings);
      assert.deepEqual(tagged, c.expected.tagged);
      assert.equal(consolidatedCount, c.expected.consolidated_count);
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

    // `findings` (post-consolidation, post-rank, ranked order and the
    // stamped consolidation fields matter) and `stats` are fully structural
    // -- no free-text fields. `eliminated` carries elimination_reason
    // (free text, e.g. injection/threshold reasons) -- compared by
    // id + eliminated_by only.
    assert.deepEqual(findings, c.expected.findings);
    assert.deepEqual(
      eliminated.map((f) => ({ id: f.id, eliminated_by: f.eliminated_by })),
      c.expected.eliminated.map((f) => ({ id: f.id, eliminated_by: f.eliminated_by })),
    );
    for (const e of eliminated) assert.ok(e.elimination_reason && e.elimination_reason.length > 0);
    assert.deepEqual(stats, c.expected.stats);
  });
}

// --- verify_deltas: issue #25 requirement 1's equivalence claim -----------
//
// Python (verify_findings.py's build_deltas/deltas_checksum, run by the recorder) owns
// the producing half; this block owns the reconstructing half (joinVerifyDeltas/
// deltaContentProof, the same functions verifyStage actually calls). One golden fixture
// sits between them, so a change to either side that breaks the join is caught here
// rather than only by the two runtimes agreeing with themselves.
for (const c of loadCases('verify_deltas')) {
  test(`verify_deltas parity: ${c.name}`, () => {
    // (1) THE join reproduces, for every field any downstream stage consumes, what
    // verify_findings.py itself left on the finding (minus its own audit trail -- the
    // recorder's project() drops exactly those three; `agent` is no longer among them,
    // #22). This is the equivalence claim itself, not a proxy for it.
    const joined = joinVerifyDeltas(c.input.dispatched, c.expected.deltas);
    assert.deepEqual(joined, c.expected.joined);

    // (2) The two runtimes compute the SAME content proof over the SAME deltas --
    // Python's deltas_checksum(deltas) (over the deltas in dispatch order) against JS's
    // deltaContentProof (which re-keys by id before stringifying), so an order-dependent
    // divergence between the two canonicalisations would fail here even though each
    // runtime's own deltas array already carries the checksum that produced it.
    const ids = c.input.dispatched.map((f) => f.id);
    assert.equal(deltaContentProof(ids, c.expected.deltas), c.expected.checksum);

    // (3) #22 re-lands deterministic `agent` on the trusted path: every dispatched
    // finding that carried an `agent` still carries the SAME `agent` after the join.
    const agentById = new Map(c.input.dispatched.map((f) => [f.id, f.agent]));
    for (const f of joined) {
      if (agentById.get(f.id) !== undefined) assert.equal(f.agent, agentById.get(f.id));
    }
  });
}

// --- slice_input_proof: the slice-input content proof's cross-runtime agreement ----
//
// The workflow computes the EXPECTED checksum over the content it dispatched
// (materializeVerifySlices) and verify_findings.py computes the ACTUAL one over the
// document it parsed off disk. Those two numbers are compared by trustSlice, so a
// serializer divergence between the runtimes would not read as a bug — it would read as
// a corrupt slice input, and degrade every slice of every run. Python (record_parity.py,
// via verify_findings._input_checksum) owns one half; this block owns the other.
for (const c of loadCases('slice_input_proof')) {
  test(`slice_input_proof parity: ${c.name}`, () => {
    assert.equal(fnv1a32(JSON.stringify(c.input.doc, null, 2)), c.expected.checksum);
  });
}
