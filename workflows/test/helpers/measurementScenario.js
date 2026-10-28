import { validArgs } from './argsFixtures.js';
import { makeCtx, makeFinding } from './pipelineMock.js';
import { dispatchedFindings, deltaEnvelope, sliceInputRecorder } from './verifyDelta.js';
import { parseWriterPayload } from '../../src/persist.js';
import { persistCtx } from './persistFixtures.js';
import { deriveFromPlan } from './deriveFromPlan.js';
import { fnv1a32 } from '../../src/wire.js';

export function measurementScenario(mode = 'return', overrides = {}, options = {}) {
  const id = { owner: 'synthetic', repo: 'fixture', pr_number: 1, sha_full: 'a'.repeat(40), platform: 'github', web_origin: 'https://github.com' };
  const args = validArgs({ delivery: { tier: 'all', prIdentity: id },
    limits: { challengeCap: 6, deliveryCap: 2 },
    ...(mode === 'return' ? { persist: { returnPrimaries: true } } : mode === 'derived' ? { persist: {} } : {}),
    ...overrides,
  });
  if (mode === 'legacy') delete args.persist;
  const removed = { status: 'available', items: [{ id: '0123456789abcdef', path: 'tests/removed', old_start: 3, old_end: 7, kind: 'deleted_file' }], omitted: 0 };
  if (options.auditOverlap) args.removedTests = { ...removed, checksum: fnv1a32(JSON.stringify(removed, null, 2)) };
  const seed = [makeFinding('DUP', { description: 'The winner claim survives for a distinct benign defect that breaks the ordinary public call and returns an incorrect value.', file: 'winner.js' }),
    makeFinding('DUP', { description: 'merge loss describes a different concrete defect', file: 'merge.js' }),
    makeFinding('VERIFY'), makeFinding('FILTER', { confidence: 65 }), makeFinding('REMOVE'),
    makeFinding('DELIVERY', { confidence: 80 }),
    makeFinding('SKIP', { severity: 'critical' }), makeFinding('CAP', { severity: 'low' }),
    makeFinding('GROUP', { file: 'group.js', severity: 'critical', title: 'shared group defect', description: 'The shared group defect breaks the ordinary value when this public function is called.' }),
  ];
  const convention = makeFinding('CORROBORATOR', { file: 'group.js', dimension: 'intent', severity: 'critical', title: 'corroborating shared defect', description: `${seed.at(-1).description} The intended return value is therefore broken.` });
  if (options.extraBytes) seed[1].diagnostic = 'x'.repeat(options.extraBytes);
  const ctx = makeCtx(args, { findings: seed });
  const baseAgent = ctx.agent;
  const persistence = persistCtx();
  const state = { payload: null, entries: null };
  ctx.agent = async (prompt, dispatch) => {
    const label = dispatch.label;
    if (options.auditOverlap && dispatch.agentType === 'code-gauntlet:test-analyzer') {
      ctx.calls.push({ prompt, ...dispatch });
      const finding = makeFinding('AUDIT', { file: 'audit.js', dimension: 'test_coverage' });
      return { findings: [finding], complete: true, total_seen: 1, removed_test_audit: [{ id: removed.items[0].id, status: 'lost', covered_by: [],
        losses: [{ anchor: finding.anchor, file: finding.file, line: finding.line_start, title: finding.title, description: finding.description,
          severity: 'high', confidence: 90, criticality: 8, failure_scenario: 'Override precedence silently chooses the wrong value.' }] }] };
    }
    if (dispatch.agentType === 'code-gauntlet:conventions-and-intent') {
      ctx.calls.push({ prompt, ...dispatch });
      return { findings: [structuredClone(convention)], complete: true, total_seen: 1 };
    }
    if (label.startsWith('verify-slice-')) {
      ctx.calls.push({ prompt, ...dispatch });
      const slice = dispatchedFindings(prompt);
      const index = Number(/verify-slice-(\d+)/.exec(label)[1]);
      return sliceInputRecorder(deltaEnvelope(slice, { sha: args.headShaShort, nonce: `${args.nonce}.${index}`,
        overrides: Object.fromEntries(slice.map((f) => [f.id, f.id === 'VERIFY'
          ? { verified: false, elimination_reason: 'trusted executor disproved the claim' } : { origin: 'new' }])) }), prompt);
    }
    if (options.failValidation && label.startsWith('validate-batch-')) throw new Error('synthetic validation dispatch failure');
    if (label.startsWith('validate-batch-')) {
      ctx.calls.push({ prompt, ...dispatch });
      const ids = [...prompt.matchAll(/^- (\S+) \[/gm)].map((m) => m[1]);
      return { validations: ids.filter((id) => !['SKIP', 'CAP'].includes(id)).map((finding_id) => ({ finding_id,
        confidence: finding_id === 'FILTER' ? 45 : seed.find((f) => f.id === finding_id)?.confidence ?? 90 })) };
    }
    if (label.startsWith('challenge-')) {
      ctx.calls.push({ prompt, ...dispatch });
      return { confidence_claim_is_correct: prompt.includes('finding REMOVE') ? 0 : 90, justification: 'fixed independent check' };
    }
    if (label === 'artifact-writer' || label === 'assemble-artifacts') {
      ctx.calls.push({ prompt, ...dispatch });
      if (label === 'artifact-writer') {
        const value = parseWriterPayload(prompt);
        if (Array.isArray(value)) {
          state.entries = value;
          const plan = JSON.parse(value.at(-1).text);
          state.payload = { findings: JSON.parse(value[0].text), ...deriveFromPlan(plan, value[0].text) };
        } else state.payload = value;
      }
      return persistence.agent(prompt, dispatch);
    }
    return baseAgent(prompt, dispatch);
  };
  return { args, ctx, state };
}

export function persistedScenario(out, state) {
  if (state.payload) return state.payload;
  const entries = out.persistReturn.entries;
  const plan = JSON.parse(entries.find((e) => e.path.endsWith('persist-plan-abc1234.json')).text);
  const findingsJson = entries.find((e) => e.path.endsWith('findings-abc1234.json')).text;
  return { findings: JSON.parse(findingsJson), ...deriveFromPlan(plan, findingsJson) };
}
