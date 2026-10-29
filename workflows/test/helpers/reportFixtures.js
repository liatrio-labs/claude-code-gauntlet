import { readFileSync } from 'node:fs';
import { AGENTS } from '../../src/registry.js';
import { makeFinding } from './pipelineMock.js';

export function corpus(name) {
  return JSON.parse(readFileSync(new URL(`../../../tests/fixtures/${name}.json`, import.meta.url), 'utf8'));
}

export function finding(id, overrides = {}) {
  return makeFinding(id, { disposition: 'deliver', evidence: '', ...overrides });
}

export function reportInput(overrides = {}) {
  return {
    summary: 'A concise change summary.', findings: [], unverified: [],
    dimensions: { dispatched: AGENTS, degraded: [] },
    generatedAt: '2026-09-02T12:00:00Z', headShaShort: 'abcdef0',
    prIdentity: {
      owner: 'acme', repo: 'widget', pr_number: 36, sha_full: 'ffffffffffffffffffffffffffffffffffffffff',
      platform: 'github', web_origin: 'https://github.com', title: 'Repair widgets',
    },
    mode: 'interactive',
    configEcho: {
      model_tier: { value: 'optimized', source: 'fixed' }, delivery_tier: { value: 'all', source: 'default' },
      pr_comment_cap: { value: 'null', source: 'default' }, review_md: { value: 'absent', source: 'discovery' },
    },
    pluginRoot: '/absolute/plugin', pipelineVersion: '3.26.0',
    reviewScope: { requested: 'full', kind: 'full', since: null, commits: null, detector: null },
    policy: { tier: 'optimized', provider: 'firstParty', gateway: false },
    deliveryTier: 'all', deliveryCap: null, gapCount: 0,
    stats: { discovered: 0, validate: {}, filter: {}, challenge: {}, merge: {} },
    ...overrides,
  };
}
