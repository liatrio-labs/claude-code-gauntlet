import { fnv1a32, isPlainObject, normalizeReviewText } from '../../src/wire.js';
import { reviewMdSetChecksum } from '../../src/args.js';
import { KNOB_REGISTRY } from '../../src/knobs.js';

export function emptyRemovedTests() {
  const body = { status: 'available', items: [], omitted: 0 };
  return { ...body, checksum: fnv1a32(JSON.stringify(body, null, 2)) };
}

export function checkpointArgs(checkpoints) {
  if (!isPlainObject(checkpoints) || Object.hasOwn(checkpoints, 'checksum')) return { checkpoints };
  const phases = checkpoints.phases === undefined ? checkpoints : checkpoints.phases;
  if (!isPlainObject(phases) || !['summarize', 'discover', 'merge', 'verify', 'validate', 'filter', 'challenge', 'report']
    .some((name) => Object.hasOwn(phases, name))) return { checkpoints };
  const wrapper = checkpoints.phases === undefined ? { phases: checkpoints } : checkpoints;
  return { checkpoints: { ...wrapper, checksum: fnv1a32(JSON.stringify(wrapper, null, 2)) } };
}

export function reviewMdArgs(entries) {
  const reviewMd = entries.map((entry) => ({
    ...entry, checksum: Object.hasOwn(entry, 'checksum') ? entry.checksum : fnv1a32(normalizeReviewText(entry.text)),
  }));
  return { reviewMd,
    reviewMdSetChecksum: reviewMdSetChecksum(reviewMd) };
}

// Wired callers retain their dispatch defaults while focused rows use the smaller waist below.
export function validArgs(over = {}) {
  const changedFiles = over.changedFiles || ['a.js'];
  const base = {
    argsVersion: 1,
    removedTests: emptyRemovedTests(),
    mode: 'headless',
    repoRoot: '/repo',
    outputDir: '/repo/.code-gauntlet',
    headShaShort: 'abc1234',
    nonce: 'nonce-xyz',
    generatedAt: '2026-07-18T00:00:00Z',
    diffPath: '/repo/.code-gauntlet/diff.patch',
    changedFilesPath: '/repo/.code-gauntlet/changed.txt',
    changedFiles,
    changedLines: 1,
    riskTable: changedFiles.map((path) => ({ path, risk: 'medium' })),
    policy: {},
    limits: { validateBatch: 25, verifySliceSize: 100, challengeCap: 40, summarizeBucketSize: 20, deliveryCap: 25 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '25', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: 'full', source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
    pluginRoot: '/plugin',
    verify: { targetKind: 'branch' },
    reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
  };
  const args = { ...base, ...over, limits: { ...base.limits, ...(over.limits || {}) } };
  if (!Object.hasOwn(over, 'configEcho')) {
    const cap = args.limits.deliveryCap;
    args.configEcho = {
      ...base.configEcho,
      pr_comment_cap: { value: cap == null ? 'null' : String(cap), source: 'default' },
      delivery_tier: { value: args.delivery && args.delivery.tier ? args.delivery.tier : 'all', source: 'default' },
      trivial_scope: { value: args.scopeAnswer || 'full', source: 'default' },
    };
  }
  if (Array.isArray(over.reviewMd)) Object.assign(args, reviewMdArgs(over.reviewMd),
    Object.hasOwn(over, 'reviewMdSetChecksum') ? { reviewMdSetChecksum: over.reviewMdSetChecksum } : {});
  if (Object.hasOwn(over, 'checkpoints')) Object.assign(args, checkpointArgs(over.checkpoints));
  return structuredClone(args);
}

export function interactiveArgs(over = {}) {
  return structuredClone({
    argsVersion: 1, removedTests: emptyRemovedTests(), mode: 'interactive', repoRoot: '/r', outputDir: '/r/.code-gauntlet',
    headShaShort: 'abc123', nonce: 'n-1', generatedAt: '2026-07-18T00:00:00Z',
    diffPath: '/r/.code-gauntlet/d.patch', changedFilesPath: '/r/.code-gauntlet/f.json',
    changedFiles: ['a.js'], changedLines: 1, reviewConfigPath: null,
    riskTable: [{ path: 'a.js', risk: 'medium' }], policy: { tier: 'optimized', subagentModel: null },
    configEcho: {
      model_tier: { value: 'optimized', source: 'fixed' },
      delivery_tier: { value: 'all', source: 'default' },
      pr_comment_cap: { value: 'null', source: 'default' },
      review_md: { value: 'absent', source: 'discovery' },
    },
    pluginRoot: '/plugin',
    verify: { targetKind: 'branch' },
    reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
    limits: { summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 25, maxLineSpan: 100 },
    ...over,
    ...(Object.hasOwn(over, 'checkpoints') ? checkpointArgs(over.checkpoints) : {}),
    ...(Array.isArray(over.reviewMd) ? reviewMdArgs(over.reviewMd) : {}),
    ...(Object.hasOwn(over, 'reviewMdSetChecksum') ? { reviewMdSetChecksum: over.reviewMdSetChecksum } : {}),
  });
}

export function priorReviewDetector(over = {}) {
  return {
    previously_reviewed: true, sha_resolvable: true, head_advanced: true, sha_is_ancestor: true,
    incremental_safe: true, error: null, ...over,
  };
}

export function prIdentity(over = {}) {
  return {
    owner: 'o', repo: 'r', pr_number: 310, sha_full: 'dddddddddddddddddddddddddddddddddddddddd',
    platform: 'github', web_origin: 'https://github.com', ...over,
  };
}

export function headlessArgs(reviewedPolicy = 'full', scope = {}) {
  const args = interactiveArgs();
  return {
    ...args, mode: 'headless', delivery: {}, limits: { ...args.limits, deliveryCap: 6 },
    configEcho: {
      model_tier: { value: 'optimized', source: 'default' },
      delivery: { value: 'markdown', source: 'default' },
      post_mode: { value: 'dry-run', source: 'default' },
      pr_comment_cap: { value: '6', source: 'default' },
      delivery_tier: { value: 'all', source: 'default' },
      draft_policy: { value: 'review', source: 'default' },
      reviewed_policy: { value: reviewedPolicy, source: 'default' },
      pr_not_found_policy: { value: 'error', source: 'default' },
      trivial_scope: { value: 'full', source: 'default' },
    },
    reviewScope: structuredClone({
      requested: 'full', kind: 'full', since: null, commits: null, detector: priorReviewDetector(), ...scope,
    }),
  };
}

export function knobRows(patches = {}, extra = []) {
  return [...KNOB_REGISTRY.map((row) => ({ ...row, ...patches[row.key] })), ...extra];
}
