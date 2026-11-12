import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

test('citation scans bound model text and process long nonmatching words linearly', () => {
  const script = `
    import { coverageReferences } from './workflows/src/coverageLedger.js';
    const specs = [{agentType:'reviewer'}], noise = 'x'.repeat(24000);
    const blank = coverageReferences(specs,[{findings:Array.from({length:1000},()=>({evidence:noise}))}],'.');
    const bounded = coverageReferences(specs,[{findings:[{evidence:'src/main:12 '+noise.repeat(100)+' src/other:3'}]}],'.');
    console.log(JSON.stringify([blank,bounded]));
  `;
  const child=spawnSync(process.execPath,['--input-type=module','-e',script],{cwd:new URL('../..',import.meta.url),encoding:'utf8',timeout:5000});
  assert.equal(child.status,0,child.error?.message || child.stderr);
  assert.deepEqual(JSON.parse(child.stdout),[[],[['reviewer','src/main',12,12]]]);
});

test('only positive unique finding anchors cross the planner boundary', async()=>{
  const {coverageAnchors}=await import('../src/coverageLedger.js');
  assert.deepEqual(coverageAnchors([null,{id:'zero',file:'a',line_start:0},{id:'negative',file:'a',line_start:-1},{id:'same',file:'a',line_start:1},{id:'same',file:'b',line_start:2},{id:'valid',file:'b',line_start:2}],'/repo'),[['same','a',1],['valid','b',2]]);
});

test('a bounded ledger retains the post-hunter coverage count',async()=>{
  const {boundedCoverageLedger}=await import('../src/coverageLedger.js');
  const value=boundedCoverageLedger({version:2,status:'available',regions:[{},{}],uncovered:['a','b'],remaining_uncovered:['b'],omitted:[],hunters:[],budget_bytes:24000,assigned_bytes:20,omitted_fact_packets:0},1);
  assert.equal(value.status,'trimmed');assert.equal(value.uncovered_count,2);assert.equal(value.remaining_uncovered_count,1);
});
