// Finding path normalization, validation, deduplication, and merge envelopes.
import { repoRelativeFindingPath, safeFindingLabel } from './paths.js';
import { AGENTS, DIMENSIONS, FINDING_PATH_ARRAY_FIELDS } from './registry.js';
import { deepClone } from './wire.js';

const KNOWN_DIMENSIONS = new Set(DIMENSIONS.map((d) => d.dimension));

const REQUIRED_FIELDS = ['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence'];

function jsonTypeName(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

function injectAgentField(findings, agent) {
  for (const f of findings) f.agent = agent;
}

function invalidFindingPathWarning(finding, field, reason, action) {
  const label = finding.id == null
    ? '<no id>'
    : safeFindingLabel(finding.id, '<unsafe id>');
  return `[${label}] Invalid ${field} path: ${reason} - ${action}`;
}

function normalizeFindingPath(finding, repoRoot) {
  const original = finding.file;
  const result = repoRelativeFindingPath(repoRoot, original);
  if ('reason' in result) {
    return { valid: false, warning: invalidFindingPathWarning(finding, 'file', result.reason, 'finding rejected') };
  }
  if (result.file !== original) {
    finding.file = result.file;
    // Legacy keys embed the file path and survive replay without reconsolidation.
    if (typeof finding.consolidation_key === 'string' && finding.consolidation_key.startsWith(`${original}:`)) {
      finding.consolidation_key = `${result.file}${finding.consolidation_key.slice(original.length)}`;
    }
    return { valid: true, rewritten: true };
  }
  return { valid: true, rewritten: false };
}

export function normalizeFindingPaths(findings, repoRoot) {
  const valid = [];
  const warnings = [];
  let pathRewrites = 0;
  for (const finding of findings) {
    if (finding === null || typeof finding !== 'object' || Array.isArray(finding)) {
      warnings.push('Invalid finding shape: expected an object - finding rejected');
      continue;
    }
    const result = normalizeFindingPath(finding, repoRoot);
    if (!result.valid) {
      warnings.push(result.warning);
      continue;
    }
    if (result.rewritten) pathRewrites += 1;
    for (const field of FINDING_PATH_ARRAY_FIELDS) {
      if (!(field in finding)) continue;
      if (!Array.isArray(finding[field])) {
        delete finding[field];
        warnings.push(invalidFindingPathWarning(finding, field, 'expected an array', 'field dropped'));
        continue;
      }
      const refs = [];
      for (const ref of finding[field]) {
        const normalized = repoRelativeFindingPath(repoRoot, ref);
        if ('reason' in normalized) {
          warnings.push(invalidFindingPathWarning(finding, field, normalized.reason, 'reference dropped'));
        } else {
          refs.push(normalized.file);
          if (normalized.file !== ref) pathRewrites += 1;
        }
      }
      finding[field] = refs;
    }
    valid.push(finding);
  }
  return { valid, warnings, path_rewrites: pathRewrites };
}

export function validateFindings(findings) {
  const valid = [];
  const warnings = [];

  for (const f of findings) {
    const fid = 'id' in f ? f.id : '<no id>';
    let reject = false;

    for (const field of REQUIRED_FIELDS) {
      const v = f[field];
      if (!(field in f) || v === null || v === undefined || v === '') {
        warnings.push(`[${fid}] Missing required field '${field}' — finding rejected`);
        reject = true;
        break;
      }
    }

    if (reject) continue;

    const dim = 'dimension' in f ? f.dimension : undefined;
    if (dim === null || dim === undefined) {
      warnings.push(`[${fid}] Missing 'dimension' field — finding kept with warning`);
    } else if (!KNOWN_DIMENSIONS.has(dim)) {
      warnings.push(`[${fid}] Unknown dimension '${dim}' — finding kept with warning`);
    }

    valid.push(f);
  }

  return { valid, warnings };
}

export function merge(findingsByAgent, meta) {
  const M = typeof meta === 'string' ? JSON.parse(meta) : meta;
  const agents = M.agents;
  const allWarnings = [];

  // Clone all groups before roster selection so even unselected inputs retain JSON ownership effects.
  const clonedGroups = Object.fromEntries(Object.entries(findingsByAgent || {}).map(([agent, group]) => {
    const findings = [];
    const warnings = [];
    for (let i = 0; i < group.length; i += 1) {
      let obj;
      try {
        obj = deepClone(group[i]);
      } catch {
        obj = undefined;
      }
      if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
        warnings.push(`[${agent}] finding ${i + 1}: expected object, got ${jsonTypeName(obj)}`);
      } else {
        findings.push(obj);
      }
    }
    return [agent, { findings, warnings }];
  }));

  // Dictionary selection preserves repeated roster keys and integer-key enumeration.
  const selected = {};
  for (const agent of agents) {
    const { findings, warnings } = clonedGroups[agent] || { findings: [], warnings: [] };
    selected[agent] = findings;
    for (const w of warnings) allWarnings.push(w);
  }
  for (const [agent, findings] of Object.entries(selected)) injectAgentField(findings, agent);

  // Validate before deduplication so warnings cover every raw finding.
  const allFlat = Object.values(selected).flat();
  const { valid, warnings: preValWarnings } = validateFindings(allFlat);

  let droppedNoId = 0;
  for (const f of allFlat) {
    const fid = f.id;
    if (fid === undefined || fid === null || fid === '') droppedNoId += 1;
  }

  const { merged, duplicatesResolved } = dedupById(valid);
  const validationWarnings = allWarnings.concat(preValWarnings);

  return {
    findings: merged,
    base_branch: M.base_branch,
    head_sha: M.head_sha,
    pr_number: M.pr_number,
    owner: M.owner,
    repo: M.repo,
    methodology: {
      agents_dispatched: agents,
      findings_per_channel: {
        ndjson: merged.length,
        text_fallback: 0,
      },
      duplicates_resolved: duplicatesResolved,
      dropped_no_id: droppedNoId,
      truncation_warnings: [],
      validation_warnings: validationWarnings,
    },
  };
}

export function mergeStage(discoverOut, meta) {
  const out = discoverOut || { findings: [] };
  const M = typeof meta === 'string' ? JSON.parse(meta) : (meta || {});
  const findings = out.findings || [];

  const byAgent = {};
  for (const f of findings) {
    const a = f?.agent || 'unknown';
    (byAgent[a] = byAgent[a] || []).push(f);
  }

  // Discovery uses short finding names and full dispatch types; merge needs a consistent roster to retain zero-finding agents in methodology.
  const shortAgentName = (a) => (typeof a === 'string' ? a.split(':').pop() : a);
  const agents = Array.isArray(out.dispatched)
    ? out.dispatched.map(shortAgentName)
    : (Object.keys(byAgent).length ? Object.keys(byAgent) : AGENTS.map(shortAgentName));
  return merge(byAgent, { ...M, agents });
}

export function dedupById(findings) {
  const seen = new Map();
  let duplicatesResolved = 0;
  let droppedNoId = 0;

  for (const finding of findings) {
    const fid = finding.id;
    if (fid === undefined || fid === null) { droppedNoId += 1; continue; }
    if (seen.has(fid)) duplicatesResolved += 1;
    else seen.set(fid, finding);
  }

  return { merged: [...seen.values()], duplicatesResolved, droppedNoId };
}
