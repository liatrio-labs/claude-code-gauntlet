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
