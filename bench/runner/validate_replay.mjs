// Node owns product imports; Workflow scripts contain only sandbox-safe product code.
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { runInNewContext } from 'node:vm';
import { isDeepStrictEqual } from 'node:util';

const clone = (value) => JSON.parse(JSON.stringify(value));
const load = (path) => JSON.parse(readFileSync(path, 'utf8'));
const moduleAt = (tree, name) => import(pathToFileURL(join(tree, 'workflows', name)).href);

function sourceFunctions(record) {
  const marker = '// --- pipeline_entry.js ---';
  if (!record.script.includes(marker)) throw new Error('unsupported source bundle layout');
  const code = record.script.split(marker)[0].replace(/^export /gm, '');
  // The saved bundle supplies historical verification and settings semantics.
  return runInNewContext(`${code}\n;({ normalizeArgs, coarsenLimits, resolveReviewConfig,
    verifyStage, validateStage, filterStage, evaluateAdmission, traceFinding,
    FINDING_PROP_TYPES, DIMENSIONS, TRACE_FINDING_FIELDS });`, {}, { timeout: 10000 });
}

function snapshot(trace, stage) {
  const snapshots = trace.snapshots.filter((row) => row.stage === stage);
  if (snapshots.length !== 1 || snapshots[0].coverage !== 'complete') throw new Error(`incomplete ${stage} source trace`);
  const versions = new Map(trace.versions.map((v) => [v.version_id, v.finding]));
  return snapshots[0].finding_version_ids.map((id) => {
    if (!versions.has(id)) throw new Error(`missing trace version ${id}`);
    return clone(versions.get(id));
  });
}

export async function reconstruct(request) {
  const { record, trace, checkpoint, verifyOutputs, verifyInputs } = request;
  const source = sourceFunctions(record);
  const A = source.normalizeArgs(clone(record.args));
  const phase = checkpoint.phases.challenge;
  const held = [...phase.findings, ...phase.unverified, ...phase.eliminated];
  const survivingIds = new Set(snapshot(trace, 'verify').map((f) => f.id));
  const fields = new Set([...Object.keys(source.FINDING_PROP_TYPES),
    ...source.DIMENSIONS.flatMap((row) => Object.keys(row.schemaExtra)), 'removed_test']);
  const merge = snapshot(trace, 'merge').map((finding) => {
    const candidates = held.filter((f) => f.id === finding.id);
    if (candidates.length > 1 || (!candidates.length && survivingIds.has(finding.id))) throw new Error(`missing/ambiguous held finding ${finding.id}`);
    if (!candidates.length) {
      const original = verifyInputs.find((f) => f.id === finding.id);
      if (!original) throw new Error(`missing original verification input ${finding.id}`);
      return { ...Object.fromEntries(Object.entries(original).filter(([key]) => !source.TRACE_FINDING_FIELDS.includes(key))), ...finding };
    }
    return { ...Object.fromEntries(Object.entries(candidates[0]).filter(([key]) => fields.has(key) && !source.TRACE_FINDING_FIELDS.includes(key))), ...finding };
  });
  // When the larger delivery capture fits, it is the exact pre-verify record.
  const replay = record.result.measurement?.replay;
  const saved = replay?.status === 'available' ? JSON.parse(replay.text) : null;
  const merged = saved ? saved.state.phaseOutputs.merge.findings.map((f, i) => {
    if (f.id !== merge[i]?.id) throw new Error('saved merge identity mismatch');
    return { ...f, ...Object.fromEntries(['trace_id', 'discovery_agent', 'discovery_dimension'].map((key) => [key, merge[i][key]])) };
  }) : merge;
  let limits = source.coarsenLimits(A.limits, A.changedFiles.length, 0, A.baseBranch, A);
  limits = source.coarsenLimits(limits, A.changedFiles.length, merged, A.baseBranch, A);
  const seen = new Set();
  const verified = await source.verifyStage({ parallel: (thunks) => Promise.all(thunks.map((thunk) => thunk())), agent: async (_prompt, opts) => {
    const suffix = opts.label.replace(/^verify-slice-(\d+)/, 'slice$1').replace(/-retry-(\d+)$/, '.r2.$1').replace(/-retry$/, '.r1');
    const result = verifyOutputs[suffix];
    if (!result) throw new Error(`missing recorded verification ${opts.label}`);
    seen.add(suffix);
    return clone(result);
  } }, { findings: clone(merged), limits, policy: A.policy, nonce: A.nonce,
    headShaShort: A.headShaShort, python: A.python, repoRoot: A.repoRoot,
    outputDir: A.outputDir, pluginRoot: A.pluginRoot,
    verify: { ...A.verify, baseBranch: A.baseBranch, diffPath: A.diffPath } });
  const overlaps = new Set(trace.snapshots.find((row) => row.stage === 'verify').decisions
    .filter((row) => row.reason_code === 'audit_overlap' && row.decision === 'eliminated').map((row) => row.trace_id));
  verified.findings = verified.findings.filter((f) => !overlaps.has(f.trace_id));
  const expected = snapshot(trace, 'verify');
  const actual = clone(verified.findings.map(source.traceFinding));
  if (!isDeepStrictEqual(actual, expected)) {
    const index = actual.findIndex((f, i) => !isDeepStrictEqual(f, expected[i]));
    const keys = [...new Set([...Object.keys(actual[index] || {}), ...Object.keys(expected[index] || {})])];
    throw new Error('reconstructed verification differs from frozen trace: ' + JSON.stringify({ index, actualLength: actual.length, expectedLength: expected.length, changes: keys.filter((k) => !isDeepStrictEqual(actual[index]?.[k], expected[index]?.[k])).map((k) => [k, actual[index]?.[k], expected[index]?.[k]]) }));
  }
  return { args: A, findings: clone(verified.findings), limits: clone(limits),
    resolvedReview: clone(source.resolveReviewConfig(A)),
    recordedAudit: phase.validation_audit || saved?.state.phaseOutputs.validate.audit,
    recordedFilter: snapshot(trace, 'filter'), verificationDispatches: [...seen] };
}

export async function nextCalls(validateStage, input, previous = []) {
  const calls = [];
  const stop = Symbol('planned');
  const returned = new Map(previous.map((row) => [row.options.label, row]));
  try {
    await validateStage({ agent: (prompt, options) => {
      const row = returned.get(options.label);
      if (row) {
        if (!isDeepStrictEqual({ prompt, options }, { prompt: row.prompt, options: row.options })) throw new Error('replay dispatch drift');
        return clone(row.result);
      }
      calls.push({ prompt, options });
      return null;
    }, parallel: async (thunks) => {
      const results = await Promise.all(thunks.map((thunk) => thunk()));
      if (calls.length) throw stop;
      return results;
    } }, clone(input));
  } catch (error) { if (error !== stop) throw error; }
  return calls;
}

export async function prepare(tree, payload, previous = []) {
  const { validateStage } = await moduleAt(tree, 'src/reviewStages.js');
  const { setupContext } = await moduleAt(tree, 'src/contextPlan.js');
  const { WORKFLOW_SCRIPT_CAP } = await moduleAt(tree, 'build.js');
  const input = { findings: payload.findings, limits: payload.limits, policy: payload.args.policy,
    contextLine: setupContext({ ...payload.args,
      contextPath: `${payload.args.outputDir}/code-gauntlet-context-${payload.args.headShaShort}.md` }).contextLine };
  const calls = await nextCalls(validateStage, input, previous);
  // Node resumes the real stage between rounds; no copied prompt, schema or retry algorithm.
  const script = `export const meta = { name: 'bench-validate-replay', description: 'Frozen validation replay' };
const replayCalls = ${JSON.stringify(calls)};
const replayResults = await parallel(replayCalls.map((call) => () => agent(call.prompt, call.options)));
return { kind: 'bench-validate-replay', dispatches: replayCalls.map((call, index) => ({
  ...call, result: replayResults[index] === undefined ? null : replayResults[index]
})) };
`;
  if (Buffer.byteLength(script, 'utf8') > WORKFLOW_SCRIPT_CAP) throw new Error('replay dispatch round exceeds Workflow script cap');
  return { input, calls, script };
}

export async function apply(tree, payload, dispatches) {
  const { validateStage, filterStage } = await moduleAt(tree, 'src/reviewStages.js');
  const { evaluateAdmission } = await moduleAt(tree, 'src/admission.js');
  const { SEVERITY_ORDER } = await moduleAt(tree, 'src/registry.js');
  const { applyThresholdFilter } = await moduleAt(tree, 'src/filterFindings.js');
  const byLabel = new Map(dispatches.map((row) => [row.options.label, row]));
  if (byLabel.size !== dispatches.length) throw new Error('duplicate validator dispatch label');
  const consumed = new Set();
  const stage = await validateStage({ parallel: (thunks) => Promise.all(thunks.map((thunk) => thunk())),
    agent: async (prompt, options) => {
      const row = byLabel.get(options.label);
      if (!row || (row.prompt !== undefined && row.prompt !== prompt)
          || (row.options.schema !== undefined && !isDeepStrictEqual(row.options, options))) {
        throw new Error(`missing or changed validator dispatch ${options.label}`);
      }
      consumed.add(options.label);
      return clone(row.result);
    } }, clone(payload.input));
  if (consumed.size !== dispatches.length) throw new Error('unexpected validator returns');
  const admitted = stage.findings.map((f) => evaluateAdmission(clone(f)));
  const review = payload.resolvedReview;
  const filtered = filterStage({ findings: clone(admitted), reviewConfig: review.reviewConfig,
    exclusionPatterns: review.exclusionPatterns, generatedAt: payload.args.generatedAt });
  const probes = stage.findings.map((finding) => {
    const probe = { ...clone(finding), severity: SEVERITY_ORDER[0] };
    delete probe.original_confidence;
    return probe;
  });
  const threshold = applyThresholdFilter(probes, review.reviewConfig);
  const floorPass = new Set(threshold.kept.map((f) => f.id));
  const outcomes = new Map([...filtered.filtered.map((f) => [f.id, { passes_filter: true, filter_reason: 'passed', filtered: f }]),
    ...filtered.eliminated.map((f) => [f.id, { passes_filter: false,
      filter_reason: f.elimination_reason || f.eliminated_by, filtered: f }])]);
  const findings = stage.findings.map((finding) => {
    const outcome = outcomes.get(finding.id);
    if (!outcome) throw new Error(`missing filter outcome ${finding.id}`);
    return { ...finding, ...outcome, passes_floor: floorPass.has(finding.id) };
  });
  return { findings, stats: { validate: stage.stats, filter: filtered.stats }, gaps: stage.gaps };
}

async function main() {
  const [operation, inputPath, outputPath] = process.argv.slice(2);
  const request = load(inputPath);
  let result;
  if (operation === 'reconstruct') result = await reconstruct(request);
  else if (operation === 'prepare') result = await prepare(resolve(request.tree), request.payload, request.previous);
  else if (operation === 'apply') result = await apply(resolve(request.tree), request.payload, request.dispatches);
  else throw new Error(`unknown operation ${operation}`);
  writeFileSync(outputPath, JSON.stringify(result));
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error) => { console.error(error.stack); process.exitCode = 1; });
}
