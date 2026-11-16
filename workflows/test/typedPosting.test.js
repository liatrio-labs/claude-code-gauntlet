import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateStage } from '../src/reviewStages.js';
import { deliveryGroups } from '../src/dispose.js';
import { runWith } from '../src/run.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';
import { validArgs } from './helpers/argsFixtures.js';
import { stageCtx } from './helpers/stageFixtures.js';
import { replayProducedCheckpoint } from './helpers/persistFixtures.js';

const verdict = (consequence) => ({ claim: 'confirmed', consequence, justification: 'Inspected the complete claim',
  ...(consequence === 'latent' ? { latent_condition: 'A caller supplies the other keyword' } : {}) });

for (const dimension of ['type_design', 'simplification']) for (const consequence of ['present_failure', 'latent', 'artifact_defect']) {
  for (const grouping of ['semantic', 'fallback']) test(`typed ${dimension}/other ${consequence} reaches challenge and folds repeats (${grouping})`, async () => {
    const agent = dimension === 'type_design' ? 'type-design-analyzer' : 'code-simplifier';
    const at = (id, line_start, fields = {}) => makeFinding(id, { file: 'mixins.scss', line_start, line_end: line_start, ...fields });
    const other = { dimension, agent, quality_basis: 'other' };
    const findings = [
      at('EXISTING', 117, { agent: 'bug-detector', title: 'Legacy alignment keywords break', description: 'Modern alignment keywords are passed through to a legacy property that accepts different values.' }),
      at('NEW', 120, { ...other, title: 'Same alignment defect', description: 'The legacy alignment declaration receives the incompatible modern alignment value.' }),
      at('DISTINCT', 125, { ...other, title: 'Independent selector defect', description: 'This selector targets the wrong container, so the independent footer loses its layout.' }),
      at('PREFERENCE-A', 117, { ...other, title: 'Prefer a different variable name' }),
      at('PREFERENCE-B', 117, { dimension: 'simplification', agent: 'code-simplifier', quality_basis: 'other', title: 'Prefer another variable name' }),
    ];
    const answers = new Map(findings.map((f) => [f.id, verdict(f.id.startsWith('PREFERENCE') ? 'no_practical_effect' : f.id === 'EXISTING' ? 'present_failure' : consequence)]));
    const validated = await validateStage(stageCtx({ reply: { validations: findings.map((f) => ({ finding_id: f.id, ...answers.get(f.id) })) } }), { findings });
    const args = validArgs({ checkpoints: { validate: validated } });
    let payload, grouped;
    const challenged = [];
    const ctx = makeCtx(args, { onPersist: (value) => { payload = value; } });
    const original = ctx.agent;
    ctx.agent = async (prompt, dispatch) => {
      const response = await original(prompt, dispatch);
      if (dispatch.label.startsWith('challenge-')) {
        const finding = findings.find((f) => prompt.includes(`Claim: ${f.title}\n`));
        challenged.push(finding.id);
        return { verdict: answers.get(finding.id) };
      }
      if (dispatch.label === 'group-findings') {
        grouped = dispatch.schema.properties.groups.items.properties.finding_ids.items.enum;
        return grouping === 'fallback' ? null : { groups: [{ finding_ids: ['EXISTING', 'NEW'] }] };
      }
      return response;
    };
    const out = await runWith(ctx, args);
    assert.equal(out.ok, true, JSON.stringify(out));
    assert.deepEqual(challenged.sort(), findings.map((f) => f.id).sort());
    assert.deepEqual([...grouped].sort(), ['DISTINCT', 'EXISTING', 'NEW']);
    const posted = payload.postReview;
    const added = posted.find((f) => f.id === 'NEW');
    assert.equal(added.dimension, dimension);
    assert.equal(added.admission.kind, 'other');
    assert.equal(added.admission.status, 'admitted');
    assert.deepEqual(added.admission.reasons, []);
    assert.equal(added.verdict.consequence, consequence);
    assert.equal(added.severity, consequence === 'present_failure' ? 'high' : 'low');
    const groups = deliveryGroups(posted);
    assert.equal(groups.length, 2);
    assert.deepEqual(groups.find((g) => g.primary.id === 'EXISTING').corroborators.map((f) => f.id), ['NEW']);
    assert.deepEqual(groups.find((g) => g.primary.id === 'DISTINCT').corroborators, []);
    assert.ok(posted.every((f) => f.disposition === 'deliver'));
    for (const id of ['PREFERENCE-A', 'PREFERENCE-B']) {
      const preference = payload.findings.find((f) => f.id === id);
      assert.equal(preference.disposition, 'report_only');
      assert.deepEqual(preference.admission.reasons, [{ predicate: 'verdict', status: 'report_only' }]);
    }
    const resumed = await replayProducedCheckpoint(args, payload.checkpoints);
    assert.deepEqual(resumed.payload.postReview, posted);
  });
}
