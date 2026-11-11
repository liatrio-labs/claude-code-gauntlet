import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runWith} from '../src/run.js';
import {fnv1a32} from '../src/wire.js';
import {validArgs} from './helpers/argsFixtures.js';
import {makeCtx} from './helpers/pipelineMock.js';

// Retired inputs deliberately target real finding titles so restoring the old mechanism loses findings.
test('retired settings cannot gate findings and the notice is local only', async () => {
  const text = '```yaml code-gauntlet\nconfidence_threshold: 100\nseverity_threshold: critical\nignore:\n  - finding\n```';
  const checksum = fnv1a32(text);
  const args = validArgs({ unsupportedReviewConfig: true,
    reviewMd: [{path:'REVIEW.md',text,checksum}],
    reviewMdSetChecksum: fnv1a32(`REVIEW.md\0${checksum}`), reviewConfigPath:'/repo/REVIEW.md',
    reviewConfig:{severity_threshold:'critical',ignore:['finding']}, exclusionPatterns:['finding'] });
  // The retired raw/parsed conflict is intentionally present; it no longer has an interpreter.
  let payload;
  const ctx = makeCtx(args,{onPersist:value=>{payload=value;}});
  const out = await runWith(ctx,args);
  assert.equal(out.ok,true);
  assert.deepEqual(payload.postReview.map(f=>f.id),['F1','F2']);
  const notice='REVIEW.md settings are no longer supported; use CODE_GAUNTLET_DELIVERY for delivery; its prose is read as project rules.';
  assert.equal(payload.report.split(notice).length-1,1);
  assert.equal(JSON.stringify(payload.postReview).includes(notice),false);
  assert.equal(out.gaps.some(g=>g.includes('REVIEW.md')),false);
});
