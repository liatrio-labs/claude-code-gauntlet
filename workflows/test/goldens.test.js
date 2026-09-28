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

const FILTER_HANDLERS = {
  normalize_field_names: (input) => {
    normalizeFieldNames(input.findings);
    return { findings: input.findings };
  },
  parse_review_md: (input) => ({ config: parseReviewMd(input.markdown) }),
  build_review_config: (input) => ({ config: buildReviewConfig(input.entries) }),
  config_for_file: (input) => ({ config: configForFile(input.config, input.file) }),
  load_exclusions: (input) => ({ patterns: loadExclusions(input.markdown) }),
  apply_threshold_filter: (input) => {
    const { kept, eliminated, contestedCount } = applyThresholdFilter(input.findings, input.config);
    return { kept, eliminated, contested_count: contestedCount };
  },
  apply_reachability_demotion: (input) => {
    const { findings, demotedCount } = applyReachabilityDemotion(input.findings);
    return { findings, demoted_count: demotedCount };
  },
  apply_injection_filter: (input) => applyInjectionFilter(input.findings),
  apply_replay_injection_scan: (input) => applyReplayInjectionScan(input.findings),
  apply_exclusions: (input) => applyExclusions(input.findings, input.exclusion_patterns, input.config ?? null),
  apply_filter_pipeline: (input) => applyFilterPipeline(input.findings, input.config, input.exclusion_patterns, input.generated_at),
  detect_disagreement: (input) => {
    const { active, suppressed, boostedCount } = detectDisagreement(input.findings);
    return { active, suppressed, boosted_count: boostedCount };
  },
  _route_by_dimension: (input) => ({ route: routeByDimension(input.finding) }),
  consolidate_cross_agent: (input) => {
    const { findings, consolidatedCount } = consolidateCrossAgent(input.findings);
    return { findings, consolidated_count: consolidatedCount };
  },
  tag_findings: (input) => {
    const { tagged, consolidatedCount, mainCount, suggestionCount } = tagFindings(input.findings);
    return {
      tagged,
      consolidated_count: consolidatedCount,
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
    const snapshot = c.name === 'deep_copy_no_mutation_of_input'
      ? structuredClone(c.input.findings)
      : null;
    const result = runApplyChallenges(c.input);
    if (snapshot) assert.deepEqual(c.input.findings, snapshot);
    assertOrWrite(c, result);
  });
}
