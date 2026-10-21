import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { runWith } from '../../src/run.js';
import { measurementScenario, persistedScenario } from '../helpers/measurementScenario.js';

const [outPath, mode = 'return', outputDir] = process.argv.slice(2);
const { args, ctx, state } = measurementScenario(mode, { measurementTrace: true, ...(outputDir ? { outputDir } : {}) });
const out = await runWith(ctx, args);
if (!out.ok || out.measurement?.status !== 'available') throw new Error(JSON.stringify(out));
const entry = { text: out.measurement.text };
entry.sha256 = createHash('sha256').update(entry.text).digest('hex');
writeFileSync(outPath, entry.text);
writeFileSync(`${outPath}.receipt.json`, JSON.stringify({ sha256: entry.sha256, out, payload: persistedScenario(out, state),
  calls: ctx.calls.map(({ label, agentType }) => ({ label, agentType })) }));
