import { readFileSync } from 'node:fs';
import { runWith } from '../../../workflows/src/run.js';
import { validArgs } from '../../../workflows/test/helpers/argsFixtures.js';
import { makeCtx, makeFinding } from '../../../workflows/test/helpers/pipelineMock.js';
import { dispatchedFindings } from '../../../workflows/test/helpers/verifyDelta.js';
import { deriveFromPlan } from '../../../workflows/test/helpers/deriveFromPlan.js';
const args = validArgs({ measurementTrace: true, persist: { returnPrimaries: true } });
const ctx = makeCtx(args, { findings: [makeFinding('source')] });
const original = ctx.agent, inputs = [], outputs = {};
ctx.agent = async (prompt, options) => {
  const result = await original(prompt, options);
  if (options.label.startsWith('verify-slice-')) {
    inputs.push(...dispatchedFindings(prompt));
    outputs[options.label.replace('verify-', '').replace('-', '')] = result;
  }
  return result;
};
const out = await runWith(ctx, args);
if (!out.ok) throw new Error(JSON.stringify(out));
const plan = JSON.parse(out.persistReturn.entries.at(-1).text);
const findings = out.persistReturn.entries[0].text;
const checkpoint = deriveFromPlan(plan, findings).checkpoints;
process.stdout.write(JSON.stringify({ record: { script: readFileSync('workflows/pipeline.js', 'utf8'), args, result: out },
  trace: JSON.parse(out.measurement.text), checkpoint, inputs, outputs }));
