// emit_persisted_findings.mjs — run the WIRED pipeline (runWith) with a mock ctx and
// write the REAL persisted findings (v2-aliased at the writeArtifacts boundary) to argv[2].
// Consumed by tests/test_boundary_parity.py so the boundary input is genuine pipeline
// output, not a hand-authored fixture: if a stage stops emitting a field the persisted
// schema loses it here and the boundary test catches it. (Only the high-confidence findings
// are persisted to disk — the pipeline-degraded `unverified` bucket lives in the report and
// the resume checkpoint, not in a persisted findings file — so only findings are emitted.)
import { writeFileSync } from 'node:fs';
import { runWith } from '../../src/stages.js';
import { validArgs, makeCtx } from '../helpers/pipelineMock.js';

const outPath = process.argv[2];
if (!outPath) {
  console.error('usage: node emit_persisted_findings.mjs <out.json>');
  process.exit(2);
}

const args = validArgs();
let persisted = null;
const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
const result = await runWith(ctx, args);

if (!result.ok || !persisted) {
  console.error(`pipeline did not persist findings (ok=${result.ok})`);
  process.exit(1);
}

writeFileSync(outPath, JSON.stringify({
  findings: persisted.findings,
}, null, 2));
