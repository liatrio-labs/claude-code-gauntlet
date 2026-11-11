"""An independently established claim recovers eligibility after validator failure."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_persisted_delivery_recovers_from_missing_validation():
    script = r"""
import assert from 'node:assert/strict';
import { runWith } from './workflows/src/run.js';
import { makeCtx } from './workflows/test/helpers/pipelineMock.js';
import { validArgs } from './workflows/test/helpers/argsFixtures.js';
const args = validArgs();
let payload;
const ctx = makeCtx(args, {onPersist: value => {payload = value;}});
const agent = ctx.agent;
ctx.agent = (prompt, dispatch) => dispatch.label.startsWith('validate-') ? null : agent(prompt, dispatch);
const out = await runWith(ctx, args);
assert.equal(out.ok, true);
assert.equal(out.stats.validate.skipped, 2);
assert.deepEqual(payload.postReview.map(f => f.id), ['F1','F2']);
for (const f of payload.postReview) {
  assert.equal(f.verdict.claim, 'confirmed');
  assert.equal(f.verdict_route, 'posted');
  assert.equal(f.validation, 'validated');
  assert.equal(f.disposition, 'deliver');
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
