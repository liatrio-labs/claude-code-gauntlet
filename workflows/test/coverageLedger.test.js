import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { coverageLedger, selectGapAssignment, gapHunterPrompt, assignmentReads } from '../src/coverageLedger.js';

const plan = { version: 2, status: 'available', fact_packets: [], regions: [
  { id: 'a', file: 'src/main', line_start: 10, line_end: 14, reads: [{ offset: 1, limit: 5, bytes: 200 }] },
  { id: 'b', file: 'src/other', line_start: 2, line_end: 5, reads: [{ offset: 6, limit: 4, bytes: 160 }] },
] };

test('citation scans bound model text and process long nonmatching words linearly', () => {
  const script = `
    import { coverageLedger } from './workflows/src/coverageLedger.js';
    const plan = ${JSON.stringify(plan)}, specs = [{agentType:'reviewer'}];
    const noise = 'x'.repeat(24000);
    const blank = coverageLedger(plan, specs, [{findings:Array.from({length:1000},()=>({evidence:noise}))}], '.', 10);
    const bounded = coverageLedger(plan, specs, [{findings:[{evidence:'src/main:12 '+noise.repeat(100)+' src/other:3'}]}], '.', 10);
    console.log(JSON.stringify([blank.uncovered,bounded.uncovered]));
  `;
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', script], { cwd: new URL('../..', import.meta.url), encoding: 'utf8', timeout: 5000 });
  assert.equal(child.status, 0, child.error?.message || child.stderr);
  assert.deepEqual(JSON.parse(child.stdout), [['a', 'b'], ['b']]);
});

test('incremental assignment cost matches escaped prompt bytes and unique framed reads', () => {
  const input = structuredClone(plan);
  input.regions[1].file = 'src/other<&界';
  input.regions[0].reads.push({offset:10,limit:1,bytes:80});
  input.regions[1].reads.push({offset:10,limit:1,bytes:80});
  input.fact_packets = [{related_files:['src/main'],reads:[{offset:10,limit:1,bytes:80},{offset:11,limit:2,bytes:100}]}];
  const ledger = coverageLedger(input, [], [], '.', 20);
  const path = '/prepared/界<&>';
  const assignment = selectGapAssignment(path, input, ledger);
  assert.equal(assignment.regions.length, 2);
  assert.equal(assignment.facts.length, 1);
  const prompt = gapHunterPrompt(path, assignment.regions, assignment.facts);
  assert.equal(assignment.bytes, Buffer.byteLength(prompt, 'utf8') + assignmentReads(assignment.regions, assignment.facts).reduce((sum, read) => sum + read.bytes, 0));
});

test('omitted fact count includes only packets considered for the chosen regions', () => {
  const input = {...plan, regions:[plan.regions[0]], fact_packets:[
    {related_files:['src/main'],reads:[{offset:15,limit:1,bytes:25000}]},
    {related_files:['elsewhere'],reads:[{offset:16,limit:1,bytes:25000}]},
  ]};
  const assignment = selectGapAssignment('/prepared', input, coverageLedger(input, [], [], '.', 20));
  assert.equal(assignment.regions.length, 1);
  assert.equal(assignment.facts.length, 0);
  assert.equal(assignment.omitted_fact_packets, 1);
});
