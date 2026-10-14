import { writeFileSync } from 'node:fs';
import { runWith } from '../../src/run.js';
import { validArgs } from '../helpers/argsFixtures.js';
import { makeCtx, makeFinding } from '../helpers/pipelineMock.js';

const outPath = process.argv[2];
if (!outPath) {
  console.error('usage: node emit_persisted_findings.mjs <out.json>');
  process.exit(2);
}

// Optional-field seeds must avoid injection patterns so filtering preserves them.
const args = validArgs({ delivery: { tier: 'all', prIdentity: {
  owner: 'o', repo: 'r', pr_number: 278,
  sha_full: '0123456789abcdef0123456789abcdef01234567',
  platform: 'github', web_origin: 'https://github.com',
} } });
let persisted = null;
const ctx = makeCtx(args, {
  findings: [
    makeFinding('F1', {
      suggestion:
        'Guard the member lookup before dereferencing it on the API-key path.',
      claude_md_rule:
        'Every auth path must null-check the member (CLAUDE.md section 4)',
    }),
    makeFinding('F2', {
      suggestion:
        'Add a test that raises PaymentGatewayError and asserts the rollback.',
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
  postReview: persisted.postReview,
}, null, 2));
