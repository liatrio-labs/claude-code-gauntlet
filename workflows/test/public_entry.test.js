// public_entry.test.js — exercise the generated bundle's public entry, not only the
// importable source seam. The workflow host wraps the bundle body in an async function and
// injects args/agent/parallel/pipeline as globals; this test reproduces that boundary.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { makeCtx, validArgs } from './helpers/pipelineMock.js';
import { runWith } from '../src/stages.js';

const bundlePath = fileURLToPath(new URL('../pipeline.js', import.meta.url));
const bundleBody = readFileSync(bundlePath, 'utf8')
  .split('\n')
  .filter((line) => !/^\s*export\s+const\s+meta\b/.test(line))
  .join('\n');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runBundle = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', bundleBody);

test('public built entry injects the source pipeline version into persisted report.md', async () => {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
  const out = await runBundle(args, ctx.agent, ctx.parallel, undefined);
  assert.equal(out.ok, true);
  const source = readFileSync(new URL('../src/pipeline_entry.js', import.meta.url), 'utf8');
  const version = source.match(/const PIPELINE_VERSION = ['"]([^'"]+)['"]/)[1];
  assert.ok(persisted.report.includes(`pipeline_version=${version} (bundle)`));
});

test('source runWith default context pins unknown pipeline version as the receipt fallback', async () => {
  const args = validArgs();
  let persisted = null;
  const ctx = makeCtx(args, { onPersist: (payload) => { persisted = payload; } });
  const saved = { agent: globalThis.agent, parallel: globalThis.parallel, pipeline: globalThis.pipeline };
  globalThis.agent = ctx.agent;
  globalThis.parallel = ctx.parallel;
  delete globalThis.pipeline;
  try {
    const out = await runWith(undefined, args);
    assert.equal(out.ok, true);
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete globalThis[key];
      else globalThis[key] = value;
    }
  }
  assert.ok(persisted.report.includes('pipeline_version=unknown (bundle)'));
});
