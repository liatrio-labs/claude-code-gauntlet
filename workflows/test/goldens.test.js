import { test } from 'node:test';
import assert from 'node:assert/strict';
import { dedupById } from '../src/findingDedup.js';
import { merge } from '../src/mergeFindings.js';
import { applyValidations } from '../src/applyValidations.js';
import {
  normalizeFieldNames,
  parseReviewMd,
  buildReviewConfig,
  configForFile,
  applyFilterPipeline,
  applyThresholdFilter,
  applyReachabilityDemotion,
  applyInjectionFilter,
  applyReplayInjectionScan,
  loadExclusions,
  applyExclusions,
  detectDisagreement,
  routeByDimension,
  consolidateCrossAgent,
  tagFindings,
} from '../src/filterFindings.js';
import { applyChallenges } from '../src/applyChallenges.js';
import { loadCases, writeGolden } from './helpers/goldenCases.js';

const UPDATE_GOLDENS = process.env.UPDATE_GOLDENS === '1';

function mapByAgent(files) {
  const out = {};
  for (const [name, text] of Object.entries(files || {})) {
    const agent = name.replace(/^(?:code-gauntlet|deep-review)-(text-)?/, '').replace(/-[^-]+\.(ndjson|txt)$/, '');
    out[agent] = text;
  }
  return out;
}

function runFindingDedup(input) {
  const got = dedupById(input.ndjson_findings, input.text_findings);
  return {
    merged: got.merged,
    duplicates_resolved: got.duplicatesResolved,
    dropped_no_id: got.droppedNoId,
  };
}

function runMergeFindings(input) {
  return merge(
    mapByAgent(input.findings_dir_files),
    mapByAgent(input.text_dir_files),
    input.args,
  );
}

function runApplyValidations(input) {
  const findings = structuredClone(input.findings);
  const { adjustedCount, unmatchedIds } = applyValidations(findings, input.validations);
  return { findings, adjusted_count: adjustedCount, unmatched_ids: unmatchedIds };
}

function runFilterFindings(input) {
  if (input.fn === 'normalize_field_names') {
    normalizeFieldNames(input.findings);
    return { findings: input.findings };
  }
  if (input.fn === 'parse_review_md') return { config: parseReviewMd(input.markdown) };
  if (input.fn === 'build_review_config') return { config: buildReviewConfig(input.entries) };
  if (input.fn === 'config_for_file') return { config: configForFile(input.config, input.file) };
  if (input.fn === 'load_exclusions') return { patterns: loadExclusions(input.markdown) };
  if (input.fn === 'apply_threshold_filter') {
    const { kept, eliminated, contestedCount } = applyThresholdFilter(input.findings, input.config);
    return { kept, eliminated, contested_count: contestedCount };
  }
  if (input.fn === 'apply_reachability_demotion') {
    const { findings, demotedCount } = applyReachabilityDemotion(input.findings);
    return { findings, demoted_count: demotedCount };
  }
  if (input.fn === 'apply_injection_filter') return applyInjectionFilter(input.findings);
  if (input.fn === 'apply_replay_injection_scan') return applyReplayInjectionScan(input.findings);
  if (input.fn === 'apply_exclusions') {
    return applyExclusions(input.findings, input.exclusion_patterns, input.config ?? null);
  }
  if (input.fn === 'apply_filter_pipeline') {
    return applyFilterPipeline(input.findings, input.config, input.exclusion_patterns, input.generated_at);
  }
  if (input.fn === 'detect_disagreement') {
    const { active, suppressed, boostedCount } = detectDisagreement(input.findings);
    return { active, suppressed, boosted_count: boostedCount };
  }
  if (input.fn === '_route_by_dimension') return { route: routeByDimension(input.finding) };
  if (input.fn === 'consolidate_cross_agent') {
    const { findings, consolidatedCount } = consolidateCrossAgent(input.findings);
    return { findings, consolidated_count: consolidatedCount };
  }
  if (input.fn === 'tag_findings') {
    const { tagged, consolidatedCount, mainCount, suggestionCount } = tagFindings(input.findings);
    return {
      tagged,
      consolidated_count: consolidatedCount,
      main_count: mainCount,
      suggestion_count: suggestionCount,
    };
  }
  throw new Error(`unhandled fn: ${input.fn}`);
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
    const snapshot = c.name === 'deep_copy_no_mutation_of_input'
      ? structuredClone(c.input.findings)
      : null;
    const result = runApplyChallenges(c.input);
    if (snapshot) assert.deepEqual(c.input.findings, snapshot);
    assertOrWrite(c, result);
  });
}
