import { test } from 'node:test';
import assert from 'node:assert/strict';
import { applyChallenges } from '../src/applyChallenges.js';
import { disposeFindings, acceptGroupingReply } from '../src/dispose.js';
import { selectDelivery } from '../src/reviewStages.js';
import { chooseGroupLead, groupDelivery } from '../src/groupRanking.js';
import { groupFindings, groupingPrompt, groupingSchema } from '../src/groupFindings.js';
import { makeFinding, makeCtx, assertValidSchema } from './helpers/pipelineMock.js';
import { validArgs } from './helpers/argsFixtures.js';
import { runWith } from '../src/run.js';
import { renderPublicSummary, renderReport } from '../src/renderReport.js';
import { slimPersistedCheckpoints } from '../src/checkpoints.js';

const members = () => [107, 114, 125, 141].map((line, i) => makeFinding(`F${line}`, {
  file: 'src/slots.ts', line_start: line, line_end: line, agent: i === 1 ? 'security-reviewer' : 'bug-detector',
}));
const reply = { groups: [{ finding_ids: members().map((f) => f.id) }] };

test('meaning groups distant sites and one-agent repeats after the challenge', async () => {
  const findings = [...members(), makeFinding('F107B', { file: 'src/slots.ts', line_start: 107, line_end: 107, agent: 'security-reviewer', confidence: 80 })];
  const modelReply = { groups: [{ finding_ids: findings.map((f) => f.id) }] };
  findings[3].verdict = { claim: 'confirmed', consequence: 'latent', latent_condition: 'A new caller', justification: 'No current caller' };
  findings[3] = applyChallenges([findings[3]], [{ id: findings[3].id, claim: 'confirmed', consequence: 'latent', latent_condition: 'A new caller', justification: 'No current caller' }]).findings[0];
  const grouping = await groupFindings({ agent: async (prompt, dispatch) => {
    assert.match(prompt, /^Findings:\n- F107 \| src\/slots.ts:107/);
    assert.equal(dispatch.agentType, 'code-gauntlet:grouper');
    assertValidSchema(dispatch.schema);
    assert.deepEqual(dispatch.schema.properties.groups.items.properties.finding_ids.items.enum, findings.map((f) => f.id));
    return modelReply;
  } }, findings);
  const out = disposeFindings({ findings, ...grouping });
  assert.equal(out.groups.length, 1);
  assert.equal(out.groups[0].issue_key, 'group:F107');
  assert.equal(out.groups[0].lead_id, 'F107');
  assert.equal(out.stats.grouped_members, 5);
  assert.equal(out.stats.grouping_placement_tradeoffs, 0);
  assert.equal(out.stats.grouping, 'model');
  assert.equal(out.stats.grouping_verdicts[0].same_challenge_verdict, false);
  assert.equal(out.findings.length, 5);
  assert.deepEqual(disposeFindings(out), out);
});

test('empty model groups keep different defects on identical lines separate', () => {
  const findings = members().slice(0, 2).map((f) => makeFinding(f.id, { ...f, line_start: 107, line_end: 107 }));
  const out = disposeFindings({ findings, grouping_reply: { groups: [] } });
  assert.equal(out.groups.length, 2);
  assert.equal(out.stats.grouping, 'model');
  assert.deepEqual(out.gaps, []);
});

test('invalid grouping falls back without dropping members, including dispatch failure and old checkpoints', async () => {
  const findings = members().slice(0, 2).map((f) => makeFinding(f.id, { ...f, line_start: 107, line_end: 107 }));
  const expectedStamp = { issue_key: 'group:src/slots.ts:105', lead_id: 'F107', finding_ids: ['F107', 'F114'] };
  const assertPositional = (out) => {
    assert.equal(out.findings.length, 2);
    assert.equal(out.groups.length, 1);
    assert.equal(out.groups[0].issue_key, expectedStamp.issue_key);
    assert.deepEqual(out.groups[0].finding_ids, expectedStamp.finding_ids);
    assert.ok(out.findings.every((f) => f.consolidation_key === 'src/slots.ts:105'));
    assert.deepEqual(out.findings.map((f) => f.delivery_group), [expectedStamp, expectedStamp]);
  };
  for (const grouping_reply of [undefined, null, [], {}, { groups: null }, { groups: [], extra: 1 },
    { groups: [null] },
    { groups: [{ finding_ids: ['F107', 'unknown'] }] },
    { groups: [{ finding_ids: ['F107', 'F114'] }, { finding_ids: ['F114', 'F107'] }] },
    { groups: [{ finding_ids: ['F107', 'F107'] }] },
  ]) {
    const out = disposeFindings({ findings, grouping_reply });
    assertPositional(out);
    assert.equal(out.stats.grouping, 'positional');
    assert.match(out.gaps[0], /^grouping: .+; same-lines grouping used$/);
  }
  const failed = await groupFindings({ agent: async () => { throw Error('offline'); } }, findings);
  const out = disposeFindings({ findings, ...failed });
  assertPositional(out);
  assert.deepEqual(out.gaps, ['grouping: dispatch failed: offline; same-lines grouping used']);
});

test('replay filters removed ids after validating the whole grouping reply', () => {
  const findings = members();
  const grouping_ids = findings.map((f) => f.id);
  const out = disposeFindings({ findings: findings.slice(1), grouping_ids, grouping_reply: reply });
  assert.equal(out.stats.grouping, 'model');
  assert.equal(out.groups[0].finding_ids.length, 3);
  const singleton = disposeFindings({ findings: findings.slice(3), grouping_ids, grouping_reply: reply });
  assert.equal(singleton.groups[0].finding_ids.length, 1);
  assert.equal(singleton.stats.grouped_members, 0);
  assert.equal(singleton.stats.grouping, 'model');
  assert.equal(acceptGroupingReply(reply, grouping_ids, []).groups.length, 0);
});

test('lead ordering shares severity, security, origin, tier, placement and stable ties', () => {
  const [a, b] = members().slice(0, 2).map((f) => ({ ...f, disposition: 'deliver' }));
  for (const [left, right] of [
    [{ ...a, severity: 'medium', dimension: 'security' }, b],
    [a, { ...b, dimension: 'security', report_tag: 'suggestion' }],
    [{ ...a, report_tag: 'suggestion' }, b],
    [{ ...a, disposition: 'body' }, b],
    [{ ...a, origin: 'unknown' }, b],
  ]) assert.equal(chooseGroupLead([left, right]).id, b.id);
  assert.equal(chooseGroupLead([{ ...a, confidence: 0, dimension: 'comment_accuracy' },
    { ...b, confidence: 100, description: b.description + ' more' }]).id, a.id);
  const singleton = groupDelivery({ severity: 'high', admission: { tier: 'suggestion' } });
  assert.equal(singleton.report_tag, 'suggestion');
  assert.equal(singleton.report_destination, 'suggestion');
  assert.equal(singleton.severity, 'high');
});

test('prompt bounds folded text and a singleton skips dispatch', async () => {
  const findings = [makeFinding('one', { description: 'x\n'.repeat(700), suggestion: 'y\n'.repeat(300) })];
  const prompt = groupingPrompt(findings).split('\n');
  assert.equal(prompt.length, 2);
  const fields = prompt[1].split(' | ');
  assert.equal(fields[4].length, 600);
  assert.equal(fields[5].length, 205);
  assert.match(groupingPrompt([{ id: 'x', file: 'a', line_start: 1, line_end: 2 }]), /a:1-2/);
  assert.deepEqual(await groupFindings({}, findings), { grouping_ids: ['one'] });
  assert.deepEqual((await groupFindings({ agent: async () => null }, members())).grouping_reply, null);
  const schema = groupingSchema(['x', 'y']);
  assertValidSchema(schema);
  const ids = schema.properties.groups.items.properties.finding_ids;
  assert.equal(Object.hasOwn(ids, 'minItems'), false);
  assert.equal(Object.hasOwn(ids, 'uniqueItems'), false);
  assert.deepEqual(Object.keys(schema.properties.groups.items.properties), ['finding_ids']);
});

test('challenge checkpoint persists grouping and resume never dispatches it again', async () => {
  const args = validArgs({ persist: { returnPrimaries: true } });
  const ctx = makeCtx(args, { findings: members() });
  const original = ctx.agent;
  ctx.agent = (prompt, opts) => opts.label === 'group-findings' ? reply : original(prompt, opts);
  const fresh = await runWith(ctx, args);
  assert.equal(fresh.ok, true, JSON.stringify(fresh));
  const entry = fresh.persistReturn.entries.find((e) => e.path.endsWith('.json') && e.text.includes('"grouping_reply"'));
  assert.ok(entry);
  const checkpoint = slimPersistedCheckpoints({ challenge: { findings: members(), unverified: [], eliminated: [],
    grouping_reply: reply, grouping_ids: members().map((f) => f.id) } }, ['challenge'], 'challenge');
  const resumedArgs = validArgs({ checkpoints: checkpoint, persist: { returnPrimaries: true } });
  const resumedCtx = makeCtx(resumedArgs);
  const resumed = await runWith(resumedCtx, resumedArgs);
  assert.equal(resumed.ok, true);
  assert.equal(resumed.stats.challenge.grouping, 'model');
  assert.equal(resumedCtx.calls.filter((c) => c.label === 'group-findings').length, 0);
});


test('group verdict statistics use actual challenge outcomes rather than the pass-through stamp', () => {
  const findings = members();
  findings[0].verdict = { claim: 'confirmed', consequence: 'latent', latent_condition: 'New caller', justification: 'Inspected caller' };
  const out = applyChallenges(findings, findings.map((f, i) => ({ id: f.id, claim: 'confirmed', consequence: i === 0 ? 'latent' : 'present_failure',
    justification: 'Inspected caller', latent_condition: 'New caller' })));
  assert.ok(out.findings.every((f) => f.challenge === 'survived'));
  const disposed = disposeFindings({ ...out, grouping_reply: reply, grouping_ids: findings.map((f) => f.id) });
  assert.equal(disposed.stats.grouping_verdicts[0].same_challenge_verdict, false);
});


for (const grouping of ['model', 'positional']) for (const [name, overrides] of [
  ['severity', { severity: 'critical' }],
  ['tier', { report_tag: 'main' }],
]) test(`${grouping} group lead preserves ${name} when the stronger member is outside the diff`, () => {
  const strong = makeFinding('STRONG', { file: 'priority.js', agent: 'security-reviewer', severity: 'low', diff_validation: { in_diff: false }, ...overrides });
  const inline = makeFinding('INLINE', { file: 'priority.js', agent: 'bug-detector', severity: 'low', report_tag: name === 'tier' ? 'suggestion' : 'main' });
  const out = disposeFindings({ findings: [inline, strong], grouping_reply: grouping === 'model' ? {
    groups: [{ finding_ids: ['INLINE', 'STRONG'] }],
  } : null });
  assert.equal(out.groups.length, 1);
  assert.equal(out.groups[0].lead_id, 'STRONG');
  assert.equal(out.groups[0].disposition, 'body');
  assert.equal(out.groups[0].tier, 'main');
  assert.equal(out.stats.grouping_placement_tradeoffs, 1);
  const summary = renderPublicSummary(out).body;
  assert.equal(summary, name === 'severity' ? 'Found 1 issue in this change: 1 critical.' : 'Found 1 issue in this change: 1 low.');
  const local = renderReport(out);
  assert.match(local, name === 'severity' ? /### 🔴 Critical[\s\S]*#### finding STRONG/ : /#### finding STRONG/);
  assert.doesNotMatch(local, /#### finding INLINE/);
});


test('short groups are harmless and legacy lead hints are ignored', () => {
  const findings = members();
  const out = disposeFindings({ findings, grouping_reply: { groups: [
    { finding_ids: [] }, { finding_ids: ['F107'] },
    { finding_ids: ['F125', 'F141'], lead_id: 'ignored' },
  ] } });
  assert.equal(out.stats.grouping, 'model');
  assert.equal(out.groups.length, 3);
  assert.equal(out.stats.grouped_members, 2);
});

for (const path of ['model', 'positional']) for (const [name, left, right, expectedLead] of [
  ['security text under maximum severity', { severity: 'critical' }, { severity: 'medium', dimension: 'security' }, 'LEAD'],
  ['main tier with critical suggestion', { severity: 'low', report_tag: 'main' }, { severity: 'critical', report_tag: 'suggestion' }, 'MEMBER'],
  ['planted non-security lead cannot hide or understate security', { severity: 'critical' }, { severity: 'medium', dimension: 'security', report_tag: 'suggestion' }, 'LEAD'],
]) test(`${path}: indivisible group preserves ${name}`, () => {
  const findings = [makeFinding('LEAD', { confidence: 99, file: 'shared.js', description: 'Ordinary defect text', agent: 'bug-detector', ...left }),
    makeFinding('MEMBER', { file: 'shared.js', description: 'Security or suggestion text', agent: 'security-reviewer', ...right })];
  const assessed = findings.map((f) => [f.id, f.severity, f.report_tag]);
  const out = disposeFindings({ findings, grouping_reply: path === 'model' ? {
    groups: [{ finding_ids: ['LEAD', 'MEMBER'] }],
  } : null });
  assert.equal(out.groups.length, 1);
  assert.equal(out.groups[0].lead_id, expectedLead);
  assert.equal(out.groups[0].severity, 'critical');
  assert.equal(out.groups[0].tier, 'main');
  assert.deepEqual(out.findings.map((f) => [f.id, f.severity, f.report_tag]), assessed);
  assert.deepEqual(findings.map((f) => [f.id, f.severity, f.report_tag]), assessed);
  const selected = selectDelivery([...out.findings, makeFinding('OTHER', { severity: 'high' })], 1, 'main_only');
  assert.deepEqual(selected.map((f) => f.id).sort(), ['LEAD', 'MEMBER']);
  const summary = renderPublicSummary({ ...out, delivered: selected }).body;
  assert.match(summary, /^Found 1 issue in this change: 1 critical\./);
  assert.equal(summary.includes('improvement suggestion'), false);
  assert.match(summary, expectedLead === 'MEMBER' ? /Security or suggestion text|finding MEMBER/ : /finding LEAD/);
  const local = renderReport(out);
  const primary = findings.find((f) => f.id === expectedLead);
  assert.match(local, new RegExp(`### .*${primary.severity[0].toUpperCase()}${primary.severity.slice(1)}[\\s\\S]*#### finding ${expectedLead}`));
  assert.match(local, /Ordinary defect text/);
  assert.match(local, /Security or suggestion text/);
});


for (const [mode, reason] of [['invalid', 'missing or non-object reply'], ['throwing', 'dispatch failed: offline']]) {
  test(`run reports ${mode} grouper fallback without losing findings`, async () => {
    const findings = members();
    const args = validArgs({ persist: { returnPrimaries: true } });
    const ctx = makeCtx(args, { findings });
    const original = ctx.agent;
    ctx.agent = (prompt, opts) => {
      if (opts.label !== 'group-findings') return original(prompt, opts);
      if (mode === 'throwing') throw Error('offline');
      return null;
    };
    const out = await runWith(ctx, args);
    assert.equal(out.ok, true, JSON.stringify(out));
    assert.equal(out.stats.challenge.grouping, 'positional');
    assert.deepEqual(out.gaps.filter((gap) => gap.startsWith('grouping: ')), [
      `grouping: ${reason}; same-lines grouping used`,
    ]);
    const entry = out.persistReturn.entries.find((entry) => entry.path === out.artifactPaths.findings);
    assert.deepEqual(JSON.parse(entry.text).map((finding) => finding.id).sort(), findings.map((finding) => finding.id).sort());
  });
}


for (const count of [0, 1]) test(`grouping is not needed for ${count} candidates`, async () => {
  const findings = members().slice(0, count);
  let calls = 0;
  const grouping = await groupFindings({ agent: async () => { calls++; return { groups: [] }; } }, findings);
  const out = disposeFindings({ findings, ...grouping });
  assert.equal(calls, 0);
  assert.equal(out.stats.grouping, 'not_needed');
  assert.deepEqual(out.gaps, []);
  assert.equal(out.findings.length, count);
  assert.equal(Object.hasOwn(out, 'grouping_reply'), false);
  assert.deepEqual(disposeFindings(out), out);
});
