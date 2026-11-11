import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dedupById } from '../src/mergeFindings.js';
import { merge } from '../src/mergeFindings.js';
import { applyValidations } from '../src/applyValidations.js';
import { normalizeFieldNames, applyFilterPipeline, consolidateCrossAgent, tagFindings } from '../src/filterFindings.js';
import { applyInjectionFilter } from '../src/injection.js';
import { applyChallenges } from '../src/applyChallenges.js';
import { loadCases, writeGolden } from './helpers/goldenCases.js';

const UPDATE_GOLDENS = process.env.UPDATE_GOLDENS === '1';

function runFindingDedup(input) {
  const got = dedupById(input.findings);
  return {
    merged: got.merged,
    duplicates_resolved: got.duplicatesResolved,
    dropped_no_id: got.droppedNoId,
  };
}

function runMergeFindings(input) {
  return merge(input.findings_by_agent, input.args);
}

function runApplyValidations(input) {
  const findings = structuredClone(input.findings);
  const { adjustedCount, unmatchedIds } = applyValidations(findings, input.validations);
  return { findings, adjusted_count: adjustedCount, unmatched_ids: unmatchedIds };
}

const FILTER_HANDLERS = {
  normalize_field_names: (input) => {
    normalizeFieldNames(input.findings);
    return { findings: input.findings };
  },
  apply_injection_filter: (input) => applyInjectionFilter(input.findings),
  apply_replay_injection_scan: (input) => applyInjectionFilter(input.findings),
  apply_filter_pipeline: (input) => applyFilterPipeline(input.findings, input.generated_at),
  consolidate_cross_agent: (input) => {
    const { findings, consolidatedCount } = consolidateCrossAgent(input.findings);
    return { findings, consolidated_count: consolidatedCount };
  },
  tag_findings: (input) => {
    const { tagged, mainCount, suggestionCount } = tagFindings(input.findings);
    return {
      tagged,
      main_count: mainCount,
      suggestion_count: suggestionCount,
    };
  },
};

function runFilterFindings(input) {
  if (!Object.hasOwn(FILTER_HANDLERS, input.fn)) throw new Error(`unhandled fn: ${input.fn}`);
  return FILTER_HANDLERS[input.fn](input);
}

function runApplyChallenges(input) {
  return applyChallenges(input.findings, input.challenges);
}

function assertOrWrite(testCase, result) {
  if (UPDATE_GOLDENS) writeGolden(testCase, result);
  else if (testCase.expected === undefined) assert.fail(`missing golden: ${testCase.expectedPath}`);
  else assert.deepEqual(result, testCase.expected);
}

for (const c of loadCases('finding_dedup')) {
  test(`finding_dedup golden: ${c.name}`, () => {
    assertOrWrite(c, runFindingDedup(c.input));
  });
}

for (const c of loadCases('merge_findings')) {
  test(`merge_findings golden: ${c.name}`, () => {
    assertOrWrite(c, runMergeFindings(c.input));
  });
}

for (const c of loadCases('apply_validations')) {
  test(`apply_validations golden: ${c.name}`, () => {
    assertOrWrite(c, runApplyValidations(c.input));
  });
}

for (const c of loadCases('filter_findings')) {
  test(`filter_findings golden: ${c.name} (${c.input.fn})`, () => {
    assertOrWrite(c, runFilterFindings(c.input));
  });
}

for (const c of loadCases('apply_challenges')) {
  test(`apply_challenges golden: ${c.name}`, () => {
    const result = runApplyChallenges(c.input);
    assertOrWrite(c, result);
  });
}
