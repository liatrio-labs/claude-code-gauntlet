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
//
// Every suggestion/claude_md_rule/spec_text string below must stay injection-pattern-free
// (#213 scans all three against the seven _SUGGESTION_SETS pattern sets and strips a
// match): a strip deletes the field, and this seed set's whole job is proving these fields
// SURVIVE to persist. A pattern-matching seed would fail
// tests/test_boundary_parity.py::TestSchemaCarriesBoundaryFields::
// test_issue_47_fields_survive_the_whole_pipeline_to_persist (the field vanishes) and
// ::test_issue_47_field_values_are_not_hollowed_out (every surviving value is empty).
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

writeFileSync(outPath, JSON.stringify({
  findings: persisted.findings,
}, null, 2));
