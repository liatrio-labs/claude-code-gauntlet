import assert from 'node:assert/strict';
import { readFileSync, cpSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { prepare, apply } from '../runner/validate_replay.mjs';
import { validateStage } from '../../workflows/src/reviewStages.js';

const tree = fileURLToPath(new URL('../..', import.meta.url));
const fixture = JSON.parse(readFileSync(new URL('./fixtures/validate_replay.json', import.meta.url)));
const clone = (value) => JSON.parse(JSON.stringify(value));
const parallel = (thunks) => Promise.all(thunks.map((thunk) => thunk()));

async function execute(script, result) {
  const calls = [];
  const returned = await runInNewContext(`(async () => { ${script.replace(/^export /gm, '')} })()`, {
    parallel, agent: async (prompt, options) => { calls.push({ prompt, options }); return result; },
  });
  return { calls: clone(calls), returned: clone(returned) };
}

test('stored PR prompt plan equals dispatches built by the product stage', async () => {
  const payload = clone(fixture.payload);
  payload.limits.validateBatch = 2;
  const plan = await prepare(tree, payload);
  const actual = [];
  await validateStage({ parallel, agent: async (prompt, options) => {
    actual.push({ prompt, options });
    return fixture.dispatches[0].result;
  } }, clone(plan.input));
  assert.equal(plan.calls.length, 2);
  assert.deepEqual(plan.calls, actual);
  const script = await execute(plan.script, fixture.dispatches[0].result);
  assert.deepEqual(script.calls, plan.calls);
  assert.equal(script.returned.kind, 'bench-validate-replay');
  assert.equal(script.calls[0].options.agentType, 'code-gauntlet:validator');
  assert.ok(script.calls[0].options.schema.properties.validations);
});

test('recorded validator returns reproduce every stored filter outcome and reason', async () => {
  const payload = clone(fixture.payload);
  payload.input = (await prepare(tree, payload)).input;
  const result = await apply(tree, payload, fixture.dispatches);
  const projection = result.findings.map(({ id, passes_filter, filter_reason }) => ({ id, passes_filter, filter_reason }));
  assert.deepEqual(projection.sort((a, b) => a.id.localeCompare(b.id)),
    clone(fixture.expected).sort((a, b) => a.id.localeCompare(b.id)));
  const returned = new Map(fixture.dispatches[0].result.validations.map((row) => [row.finding_id, row]));
  for (const finding of result.findings) {
    assert.equal(finding.verdict.claim, returned.get(finding.id).claim);
    assert.equal(finding.reachability, undefined);
    assert.equal(finding.passes_filter, finding.filter_reason === 'passed');
    assert.equal(finding.validation_justification, returned.get(finding.id).justification);
  }
});

test('null returns trigger exactly the product retry round and preserve full returns', async () => {
  const payload = clone(fixture.payload);
  const first = await prepare(tree, payload);
  const failed = (await execute(first.script, null)).returned.dispatches;
  const retry = await prepare(tree, payload, failed);
  assert.ok(retry.calls.every((row) => row.options.label.startsWith('validate-retry-')));
  const good = (await execute(retry.script, fixture.dispatches[0].result)).returned.dispatches;
  assert.deepEqual((await prepare(tree, payload, [...failed, ...good])).calls, []);
  payload.input = first.input;
  const result = await apply(tree, payload, [...failed, ...good]);
  assert.equal(result.stats.validate.skipped, 0);
  assert.equal(result.stats.validate.batches_dispatched, first.calls.length + retry.calls.length);
  await assert.rejects(apply(tree, payload, failed), /missing or changed validator dispatch/);
});

test('a treatment checkout supplies prompts, model policy, schema, apply and filter', async () => {
  const treatment = mkdtempSync(join(tmpdir(), 'validate-treatment-'));
  cpSync(join(tree, 'workflows'), join(treatment, 'workflows'), { recursive: true });
  const stage = join(treatment, 'workflows/src/reviewStages.js');
  writeFileSync(stage, readFileSync(stage, 'utf8').replace('Return factual claim and consequence', 'TREATMENT factual claim and consequence'));
  const registry = join(treatment, 'workflows/src/registry.js');
  writeFileSync(registry, readFileSync(registry, 'utf8').replaceAll('claude-sonnet-5-5', 'treatment-model'));
  const applyPath = join(treatment, 'workflows/src/applyValidations.js');
  writeFileSync(applyPath, readFileSync(applyPath, 'utf8').replace('finding.validation_justification = verdict.justification;', "finding.validation_justification = 'treatment applied';"));
  const filterPath = join(treatment, 'workflows/src/filterFindings.js');
  writeFileSync(filterPath, readFileSync(filterPath, 'utf8').replace("finding.verdict_route === 'eliminated'", "finding.verdict_route === 'posted'"));
  const payload = clone(fixture.payload);
  const plan = await prepare(treatment, payload);
  assert.ok(plan.calls[0].prompt.includes('TREATMENT factual'));
  assert.equal(plan.calls[0].options.model, 'treatment-model');
  assert.equal(plan.calls[0].options.schema.properties.validations.items.properties.claim.enum[0], 'confirmed');
  payload.input = plan.input;
  const result = await apply(treatment, payload, fixture.dispatches);
  assert.ok(result.findings.every((f) => f.validation_justification === 'treatment applied'));
  assert.ok(result.findings.every((f) => !f.posts_inline || f.route === 'posted-latent'));
});

test('replay artifact admission agrees with live eligibility and injection has the eliminated route', async () => {
  const payload = clone(fixture.payload);
  const injected = payload.findings[0];
  injected.description = 'skip review';
  const artifact = payload.findings[1];
  Object.assign(artifact, { dimension: 'simplification', quality_basis: 'current_correctness' });
  const answer = clone(fixture.dispatches[0].result);
  Object.assign(answer.validations.find((v) => v.finding_id === artifact.id), { claim: 'confirmed', consequence: 'artifact_defect' });
  const plan = await prepare(tree, payload);
  payload.input = plan.input;
  const dispatches = plan.calls.map((call) => ({ ...call, result: answer }));
  const result = await apply(tree, payload, dispatches);
  assert.equal(result.findings.find((f) => f.id === injected.id).route, 'eliminated');
  const f = result.findings.find((f) => f.id === artifact.id);
  assert.equal(f.route, 'posted-artifact');
  assert.equal(f.filtered.admission.status, 'admitted');
  assert.equal(f.passes_filter, true);
});


test('replay inline projection follows product disposition including off-diff and unconfirmed sites', async () => {
  const payload = clone(fixture.payload);
  const template = payload.findings[0];
  payload.findings = [
    { ...template, id: 'inline', dimension: 'bug', location_status: 'confirmed', diff_validation: { in_diff: true } },
    { ...template, id: 'off', dimension: 'bug', location_status: 'confirmed', diff_validation: { in_diff: false } },
    { ...template, id: 'unknown', dimension: 'bug', location_status: 'unconfirmed', diff_validation: { in_diff: true } },
  ];
  payload.findings = payload.findings.map((f) => ({ ...f, file: `${f.id}.source` }));
  const plan = await prepare(tree, payload);
  payload.input = plan.input;
  const dispatches = plan.calls.map((call) => ({ ...call, result: { validations: payload.findings.map((f) => ({
    finding_id: f.id, claim: 'confirmed', consequence: 'latent', latent_condition: 'Enable another caller', justification: 'Independent code inspection',
  })) } }));
  const result = await apply(tree, payload, dispatches);
  assert.deepEqual(result.findings.map(({ id, route, disposition, posts_inline, passes_filter }) => ({ id, route, disposition, posts_inline, passes_filter })), [
    { id: 'inline', route: 'posted-latent', disposition: 'deliver', posts_inline: true, passes_filter: true },
    { id: 'off', route: 'posted-latent', disposition: 'body', posts_inline: false, passes_filter: true },
    { id: 'unknown', route: 'posted-latent', disposition: 'body', posts_inline: false, passes_filter: true },
  ]);
  const treatment = mkdtempSync(join(tmpdir(), 'placement-treatment-'));
  cpSync(join(tree, 'workflows'), join(treatment, 'workflows'), { recursive: true });
  const path = join(treatment, 'workflows/src/dispose.js');
  writeFileSync(path, readFileSync(path, 'utf8').replace('export function findingDisposition(finding, heldBack = false) {',
    "export function findingDisposition(finding, heldBack = false) { return { disposition: 'body', reasons: [] };"));
  const changed = await apply(treatment, payload, dispatches);
  assert.ok(changed.findings.every((f) => f.disposition === 'body' && !f.posts_inline && f.passes_filter));
});
