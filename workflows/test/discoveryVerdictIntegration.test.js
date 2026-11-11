import { withPreparedPlan } from './helpers/diskDiscovery.js';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runWith } from '../src/run.js';
import { schemaAccepts } from '../src/schema.js';
import { validateStage } from '../src/reviewStages.js';
import { validArgs } from './helpers/argsFixtures.js';
import { makeCtx, makeFinding } from './helpers/pipelineMock.js';
import { decodeMeasurement } from './helpers/decodeMeasurement.js';

for (const gateway of [false, true]) for (const change_origin of ['introduced', 'moved_or_carried', 'pre_existing']) {
  for (const [consequence, route] of [['present_failure', 'posted'], ['latent', 'posted-latent'], ['artifact_defect', 'posted-artifact']]) {
    test(`hunter reaches ${route} with ${change_origin}, gateway=${gateway}, and an inconclusive challenger`, async () => {
      const relevance = change_origin === 'pre_existing' ? 'The changed caller now depends on this code for the new input.' : undefined;
      const args = validArgs({ measurementTrace:true, policy:{gateway}, persist:{returnPrimaries:true},
        contextReceipt:{contextLines:20,contextChars:400,contextMaxLineChars:20,contextReadPlanVersion:1,contextReadChunks:[20],
          discoveryPlan:{version:2,status:'available',fact_packets:[],regions:[
            {id:'focus',file:'FOCUS.js',line_start:1,line_end:2,reads:[{offset:5,limit:2,bytes:80}]},
          ]}} });
      const ctx = makeCtx(args, {findings:[]});
      const original = ctx.agent;
      let validatorReads = 0, challengerReads = 0;
      ctx.agent = async (prompt, options) => {
        let result = await original(prompt, options);
        if (options.agentType === 'code-gauntlet:gap-hunter') {
          const candidate = makeFinding('candidate', {file:'OUTSIDE.js',evidence:'FOCUS.js:1 supplies the triggering input.',change_origin,
            ...(relevance ? {change_relevance:relevance} : {})});
          const props = options.schema.properties.findings.items.properties;
          result = {...result,findings:[Object.fromEntries(Object.entries(candidate).filter(([key]) => Object.hasOwn(props,key)))],total_seen:1};
          assert.ok(schemaAccepts(result,options.schema));
          for (const key of ['oneOf','allOf','anyOf']) assert.equal(Object.hasOwn(options.schema,key),false);
        }
        if (options.agentType === 'code-gauntlet:validator') {
          validatorReads++;
          for (const verdict of result.validations) Object.assign(verdict,{consequence,
            ...(consequence === 'latent' ? {latent_condition:'Enable the other path'} : {})});
        }
        if (options.agentType === 'code-gauntlet:challenger') {
          challengerReads++;
          result = {verdict:{claim:'not_established',missing_fact:'Which runtime caller supplies the state?',justification:'The inspected code did not establish the caller.'}};
        }
        if (['code-gauntlet:validator','code-gauntlet:challenger'].includes(options.agentType)) {
          assert.ok(schemaAccepts(result,options.schema));
          const props = options.schema.properties.validations?.items.properties ?? options.schema.properties.verdict.properties;
          for (const key of ['change_origin','change_relevance']) assert.equal(Object.hasOwn(props,key),false);
        }
        return result;
      };
      const out = await withPreparedPlan(ctx,args,runWith);
      assert.equal(out.ok,true);
      assert.deepEqual(ctx.violations,[]);
      assert.equal(validatorReads,1);
      assert.equal(challengerReads,1);
      const trace = decodeMeasurement(out.measurement);
      assert.deepEqual(trace.coverage_ledger.hunters[0].outside_assignment,['gap-hunter-1']);
      assert.deepEqual(trace.coverage_ledger.remaining_uncovered,[]);
      const selected = trace.snapshots.find((s) => s.stage === 'select');
      assert.equal(selected.finding_version_ids.length,1);
      const finding = trace.versions.find((v) => v.version_id === selected.finding_version_ids[0]).finding;
      assert.equal(finding.verdict.claim,'confirmed');
      assert.equal(finding.challenge_verdict.claim,'not_established');
      assert.equal(finding.verdict_route,route);
      assert.equal(finding.admission.status,'admitted');
      assert.deepEqual(finding.admission.reasons,[]);
      assert.equal(finding.disposition,'deliver');
      for (const version of trace.versions) {
        assert.equal(version.discovery_dimension,'gap_hunt');
        assert.equal(version.finding.change_origin,change_origin);
        assert.equal(version.finding.change_relevance,relevance);
      }
      const persisted = JSON.parse(out.persistReturn.entries.find((entry) => entry.path.endsWith(`code-gauntlet-findings-${args.headShaShort}.json`)).text);
      assert.equal(persisted[0].change_origin,change_origin);
      assert.equal(persisted[0].change_relevance,relevance);
      assert.equal(persisted[0].verdict_route,route);
    });
  }
}

for (const field of ['change_origin','change_relevance']) test(`provenance remains finding data when a reader incorrectly echoes ${field}`, async () => {
  const finding = makeFinding('gap-hunter-1',{agent:'gap-hunter',change_origin:'pre_existing',change_relevance:'The changed caller now relies on this implementation.'});
  const ctx = {parallel:async (tasks) => Promise.all(tasks.map((task) => task())),
    agent:async () => ({validations:[{finding_id:finding.id,claim:'confirmed',consequence:'present_failure',justification:'Inspected the failing input.',[field]:finding[field]}]})};
  const out = await validateStage(ctx,{findings:[finding]});
  assert.equal(out.stats.skipped,1);
  assert.equal(out.findings[0].validation,'skipped');
  assert.equal(out.findings[0].change_origin,'pre_existing');
  assert.equal(out.findings[0].change_relevance,finding.change_relevance);
  assert.ok(out.gaps.some((gap) => gap.includes('schema_rejected')));
});
