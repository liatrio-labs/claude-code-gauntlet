import { createHash } from 'node:crypto';
import { validArgs } from '../../../workflows/test/helpers/argsFixtures.js';
import { makeFinding } from '../../../workflows/test/helpers/pipelineMock.js';
import { persistCtx } from '../../../workflows/test/helpers/persistFixtures.js';
import { deriveFromPlan } from '../../../workflows/test/helpers/deriveFromPlan.js';
import { plannedArtifactPaths } from '../../../workflows/src/outputPaths.js';
import { normalizeArgs } from '../../../workflows/src/args.js';
import { parseWriterPayload } from '../../../workflows/src/persist.js';
import { runWith } from '../../../workflows/src/run.js';

// Shared IDs must remain distinct occurrences across holdback and elimination.
const finding = (occurrence, over = {}) => makeFinding(occurrence === 4 ? 'DUP' : `F${occurrence}`, {
  file: `${occurrence}.js`, trace_id: `nonce-xyz:0:${occurrence}`,
  agent: 'bug-detector', discovery_agent: 'bug-detector', discovery_dimension: 'bug', ...over,
  diff_validation: { in_diff: true, reason: 'fixture diff membership' },
  line: 10, end_line: 10,
});
const winner = finding(0, { id: 'DUP' });
const grouped = finding(1, { file: winner.file, title: 'corroborating defect',
  description: `${winner.description}. The intended return value is therefore broken.`,
  agent: 'conventions-and-intent', dimension: 'intent',
  discovery_agent: 'conventions-and-intent', discovery_dimension: 'intent' });
const held = finding(3, { validation: 'skipped' });
const eliminated = finding(4, { eliminated_by: 'challenge:removed', elimination_reason: 'disproved' });
const filtered = finding(5, { confidence: 10, eliminated_by: 'threshold', elimination_reason: 'below threshold' });
const capped = finding(2, { confidence: 60 });
const findings = [winner, grouped, held, eliminated, filtered, capped];
const phaseInputs = {
  summarize: { summary: 'Synthetic replay', gaps: [] },
  discover: { findings, dispatched: ['code-gauntlet:bug-detector'], degraded: [], removedTestAudit: {} },
  merge: { findings, methodology: { findings_per_channel: { ndjson: findings.length } } },
  verify: { findings, verified: true, inputProof: { trusted: findings.length } },
  validate: { findings, stats: { skipped: 1 } },
  filter: { filtered: findings.filter((f) => f !== filtered), eliminated: [filtered], stats: {} },
  challenge: { findings: [winner, grouped, capped], unverified: [{ ...held, challenge: 'capped' }],
    eliminated: [filtered, eliminated], stats: {} },
};
const rows = {};
for (const mode of ['return', 'derived', 'legacy', 'writer-failure', 'empty', 'candidate']) {
  // Clone each phase independently: real phase outputs do not share finding objects.
  const phases = Object.fromEntries(Object.entries(phaseInputs).map(([key, out]) => [key, structuredClone(out)]));
  if (mode === 'candidate') for (const output of Object.values(phases)) {
    for (const bucket of ['findings', 'unverified', 'eliminated', 'filtered']) {
      for (const finding of output[bucket] || []) if (finding.file === '4.js') finding.id = 'ELIM';
    }
  }
  if (mode === 'empty') for (const output of Object.values(phases)) {
    for (const bucket of ['findings', 'unverified', 'eliminated', 'filtered']) if (Array.isArray(output[bucket])) output[bucket] = [];
  }
  const args = normalizeArgs(validArgs({ contextLines: 1, contextChars: 1, measurementTrace: true,
    limits: { deliveryCap: 1 }, delivery: { tier: 'all', prIdentity: {
      owner: 'synthetic', repo: 'fixture', pr_number: 1, sha_full: 'a'.repeat(40), platform: 'github', web_origin: 'https://github.com',
    } }, checkpoints: phases,
    ...(mode === 'return' ? { persist: { returnPrimaries: true } } : mode === 'legacy' ? {} : { persist: {} }),
  }));
  const dispatches = [];
  const persistence = persistCtx();
  const ctx = { pipelineVersion: null, agent: async (prompt, dispatch) => {
    const row = { label: dispatch.label, agent_type: dispatch.agentType,
      prompt_sha256: createHash('sha256').update(prompt).digest('hex'), result: null, error: null };
    dispatches.push(row);
    try {
      if (mode === 'writer-failure') throw new Error('synthetic writer failure');
      row.result = await persistence.agent(prompt, dispatch);
      return row.result;
    } catch (error) { row.error = error.message; throw error; }
  } };
  // The public caller supplies the independent expected bytes and complete trace.
  const out = await runWith(ctx, structuredClone(args));
  if (out.measurement?.replay?.status !== 'available') throw new Error(JSON.stringify(out));
  const record = JSON.parse(out.measurement.replay.text);
  if (JSON.stringify(record.persistence) !== JSON.stringify(dispatches)) throw new Error('persistence capture differs');
  const paths = plannedArtifactPaths(args.outputDir, args.headShaShort);
  const writer = persistence.calls.find((call) => call.label === 'artifact-writer');
  const payload = writer ? parseWriterPayload(writer.prompt) : null;
  const entries = out.persistReturn?.entries || (Array.isArray(payload) ? payload : null);
  const artifacts = {};
  if (entries) {
    for (const entry of entries) artifacts[entry.path] = entry.text;
    const derived = deriveFromPlan(JSON.parse(entries.at(-1).text), entries[0].text);
    artifacts[paths.postReview] = JSON.stringify(derived.postReview, null, 2);
    artifacts[paths.checkpoints] = JSON.stringify(derived.checkpoints, null, 2);
  } else if (payload) {
    for (const key of Object.keys(paths)) artifacts[paths[key]] = key === 'report'
      ? payload.report : JSON.stringify(payload[key], null, 2);
  }
  rows[mode] = { record, out, artifacts, trace: JSON.parse(out.measurement.text) };
}
const duplicate = { file: 'existing', line_start: 1, line_end: 1, anchor: 'return process(value);' };
rows.admission = ['confirmed', 'rejected'].map((status) => finding(0, {
  dimension: 'simplification', origin_dimension: 'simplification', agent: 'code-simplifier',
  quality_basis: 'duplication', duplicate_of: duplicate,
  resolved_references: { duplicate_of: { ...duplicate, location_status: 'confirmed' } },
  admission_assessments: [
    { predicate: 'present_defect', status: 'rejected', references: ['primary'] },
    { predicate: 'confirmed_duplication', status, references: ['primary', 'duplicate_of'] },
  ],
}));
const legacy = structuredClone(rows.admission[0]);
for (const key of ['admission', 'body_fix_task_eligible', 'resolved_references', 'admission_assessments', 'sites']) delete legacy[key];
rows.admission.push(legacy);
console.log(JSON.stringify(rows));
