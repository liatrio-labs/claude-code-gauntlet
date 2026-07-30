// emit_persisted_findings.mjs — run the WIRED pipeline (runWith) with a mock ctx and
// write the REAL persisted findings (v2-aliased at the writeArtifacts boundary) to argv[2].
// Consumed by tests/test_boundary_parity.py so the boundary input is genuine pipeline
// output, not a hand-authored fixture: if a stage stops emitting a field the persisted
// schema loses it here and the boundary test catches it. (Only the high-confidence findings
// are persisted to disk — the pipeline-degraded `unverified` bucket lives in the report and
// the resume checkpoint, not in a persisted findings file — so only findings are emitted.)
import { writeFileSync } from 'node:fs';
import { runWith } from '../../src/stages.js';
import { validArgs, makeCtx, makeFinding } from '../helpers/pipelineMock.js';

const outPath = process.argv[2];
if (!outPath) {
  console.error('usage: node emit_persisted_findings.mjs <out.json>');
  process.exit(2);
}

// The seed set carries the fields issue #47 added to the schema, so the boundary test is
// checking real end-to-end carriage rather than a shape that never has them. F1 takes the
// canonical pair every dimension emits; F2 takes the per-dimension extras plus spec_text,
// which is also what exercises post_review.py's spec_text fallback on genuine pipeline
// output. (Both stay dimension 'bug' — the persist boundary carries fields, it does not
// police which dimension owns them, and changing a dimension here would change filter
// routing and with it what the rest of this recorder's consumers see.)
const args = validArgs();
let persisted = null;
const ctx = makeCtx(args, {
  findings: [
    makeFinding('F1', {
      suggestion: 'Guard the member lookup before dereferencing it on the API-key path.',
      claude_md_rule: 'Every auth path must null-check the member (CLAUDE.md section 4)',
    }),
    makeFinding('F2', {
      suggestion: 'Add a test that raises PaymentGatewayError and asserts the rollback.',
      spec_text: 'A failed payment MUST leave no partial transaction.',
      criticality: 9,
      failure_scenario: 'A regression dropping the rollback would go undetected.',
    }),
  ],
  onPersist: (payload) => { persisted = payload; },
});
const result = await runWith(ctx, args);

if (!result.ok || !persisted) {
  console.error(`pipeline did not persist findings (ok=${result.ok})`);
  process.exit(1);
}

// A SECOND run, deliberately DEGRADED and carrying a PR identity, so the recorder also
// emits the real post-review wrapper for a review that raises the health banner. That
// wrapper is the input tests/test_boundary_parity.py hands to post_review.py: the banner
// only reaches a GitHub reader if the field the pipeline WRITES is the field the poster
// READS, and nothing short of driving both halves proves that. `verifySliceFailIndex`
// leaves findings unclassified, which is what reviewHealth keys `degraded` on.
const degradedArgs = validArgs({
  delivery: { prIdentity: { owner: 'o', repo: 'r', pr_number: 5, sha_full: 'a'.repeat(40) } },
});
let degradedPersisted = null;
const degradedCtx = makeCtx(degradedArgs, {
  verifySliceFailIndex: 0,
  onPersist: (payload) => { degradedPersisted = payload; },
});
await runWith(degradedCtx, degradedArgs);

if (!degradedPersisted || !degradedPersisted.postReview) {
  console.error('degraded run persisted no post-review wrapper');
  process.exit(1);
}

writeFileSync(outPath, JSON.stringify({
  findings: persisted.findings,
  degradedPostReview: degradedPersisted.postReview,
}, null, 2));
