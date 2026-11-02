// Finding path normalization, validation, deduplication, and merge envelopes.
import { repoRelativeFindingPath, safeFindingLabel } from './paths.js';
import { AGENTS, DIMENSIONS, FINDING_PROP_TYPES, FINDING_PATH_DESCRIPTORS, CITATION_REQUEST_SCHEMA, CITATION_RESULT_SCHEMA, producerRow, findingPolicy } from './registry.js';
import { measurementDecisions, traceDecision } from './measurementTrace.js';
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

export function normalizeFindingPaths(findings, repoRoot, decisions = []) {
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
      if (finding.trace_id) decisions.push(traceDecision(finding, 'invalid_path', result.warning));
      continue;
    }
    if (result.rewritten) pathRewrites += 1;
    const droppedReference = (field, reason, action = 'reference dropped') => {
      const warning = invalidFindingPathWarning(finding, field, reason, action);
      warnings.push(warning);
      if (!Array.isArray(finding.reference_diagnostics)) finding.reference_diagnostics = [];
      finding.reference_diagnostics.push({ field: field.split('.')[0], reason, action });
      return undefined;
    };
    const confine = (value, schema, field) => {
      if (schema === CITATION_REQUEST_SCHEMA || schema === CITATION_RESULT_SCHEMA) {
        const request = schema === CITATION_RESULT_SCHEMA ? value?.request : value;
        const path = repoRelativeFindingPath('', request?.file);
        if ('reason' in path || path.file !== request?.file) {
          droppedReference(field, 'non-canonical citation path', 'retained for citation audit');
        }
        return value;
      }
      if (schema.$comment === 'repo-path') {
        const normalized = repoRelativeFindingPath(repoRoot, value);
        if ('reason' in normalized) {
          return droppedReference(field, normalized.reason);
        }
        if (normalized.file !== value) pathRewrites += 1;
        return normalized.file;
      }
      if (schema.type === 'array') {
        if (Array.isArray(value)) return value.flatMap((entry) => {
          const normalized = confine(entry, schema.items, field);
          return normalized === undefined ? [] : [normalized];
        });
      } else if (schema.type === 'object' && value && typeof value === 'object' && !Array.isArray(value)) {
        const out = { ...value };
        for (const [key, child] of Object.entries(schema.properties)) {
          if (!Object.hasOwn(value, key) && !schema.required.includes(key)) continue;
          const normalized = confine(value[key], child, `${field}.${key}`);
          if (normalized === undefined && schema.required.includes(key)) return undefined;
          out[key] = normalized;
        }
        return out;
      } else if (!['array', 'object'].includes(schema.type)
        && (schema.type === 'integer' ? Number.isInteger(value) : jsonTypeName(value) === schema.type)) return value;
      return droppedReference(field, schema.type === 'array' ? 'expected an array' : `expected ${schema.type}`, 'field dropped');
    };
    for (const [field, schema] of Object.entries(FINDING_PATH_DESCRIPTORS)) {
      if (!(field in finding)) continue;
      const refs = confine(finding[field], schema, field);
      if (refs === undefined) delete finding[field];
      else finding[field] = refs;
    }
    valid.push(finding);
  }
  return { valid, warnings, path_rewrites: pathRewrites };
}

export function validateFindings(findings, decisions = []) {
  const valid = [];
  const warnings = [];
  const rejected = [];

  for (const f of findings) {
    if (!f || typeof f !== 'object' || Array.isArray(f)) {
      warnings.push('Invalid finding shape: expected an object — finding rejected');
      rejected.push(f);
      continue;
    }
    delete f.origin_dimension;
    delete f.effective_test_kind;
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

    const row = producerRow(f);
    const kind = row?.admission?.kinds[f[row.admission.field]];
    const required = row ? [...row.requiredExtra, ...row.requiredWhenDimension, ...(kind?.required || [])] : [];
    if (!reject && row && !f.removed_test) for (const field of required) {
      const schema = row.schemaExtra[field] || FINDING_PROP_TYPES[field];
      const type = typeof schema === 'string' ? schema : schema.type;
      const value = f[field];
      if (jsonTypeName(value) !== type
        || (typeof value === 'string' && !value.trim()) || (schema.enum && !schema.enum.includes(value))) {
        warnings.push(`[${fid}] Invalid required field '${field}' for ${row.dimension} — finding rejected`);
        reject = true;
        break;
      }
    }
    if (!reject && kind?.anyOf && !kind.anyOf.some((field) => typeof f[field] === 'string' && f[field].trim())) {
      warnings.push(`[${fid}] Missing concrete fix (${kind.anyOf.join('/')}) — finding rejected`);
      reject = true;
    }
    if (reject) {
      rejected.push(deepClone(f));
      if (f.trace_id) decisions.push(traceDecision(f, 'invalid_shape', warnings[warnings.length - 1]));
      continue;
    }

    const dim = 'dimension' in f ? f.dimension : undefined;
    if (dim === null || dim === undefined) {
      warnings.push(`[${fid}] Missing 'dimension' field — finding kept with warning`);
    } else if (!KNOWN_DIMENSIONS.has(dim)) {
      warnings.push(`[${fid}] Unknown dimension '${dim}' — finding kept with warning`);
    }

    valid.push(f);
  }

  return { valid, warnings, rejected };
}

export function merge(findingsByAgent, meta) {
  const M = typeof meta === 'string' ? JSON.parse(meta) : meta;
  const agents = M.agents;
  const allWarnings = [];
  const decisions = [];

  // Dictionary selection preserves repeated roster keys and integer-key enumeration.
  const selected = {};
  for (const agent of agents) {
    const group = (findingsByAgent || {})[agent] || [];
    const findings = [];
    for (let i = 0; i < group.length; i += 1) {
      let obj;
      try {
        obj = deepClone(group[i]);
      } catch {
        obj = undefined;
      }
      if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
        allWarnings.push(`[${agent}] finding ${i + 1}: expected object, got ${jsonTypeName(obj)}`);
      } else {
        findings.push(obj);
      }
    }
    selected[agent] = findings;
  }
  for (const [agent, findings] of Object.entries(selected)) injectAgentField(findings, agent);

  // Validate before deduplication so warnings cover every raw finding.
  const allFlat = Object.values(selected).flat();
  const { valid, warnings: preValWarnings, rejected } = validateFindings(allFlat, decisions);
  for (const finding of valid) {
    const row = producerRow(finding);
    if (row) {
      finding.origin_dimension = row.dimension;
      if (row.admission) finding.dimension = row.dimension;
    }
    const { kind } = findingPolicy(finding);
    if (kind?.dimension) finding.dimension = kind.dimension;
  }

  let droppedNoId = 0;
  for (const f of allFlat) {
    const fid = f.id;
    if (fid === undefined || fid === null || fid === '') droppedNoId += 1;
  }

  const { merged, duplicatesResolved } = dedupById(valid, decisions);
  const validationWarnings = allWarnings.concat(preValWarnings);

  return measurementDecisions({
    findings: merged,
    ...((M.rejected?.length || rejected.length) ? { rejected: [...(M.rejected || []), ...rejected] } : {}),
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
  }, decisions);
}

export function mergeStage(discoverOut, meta) {
  const out = discoverOut || { findings: [] };
  const M = typeof meta === 'string' ? JSON.parse(meta) : (meta || {});
  const findings = out.findings || [];

  const byAgent = {};
  for (const f of findings) {
    const a = f.agent || 'unknown';
    (byAgent[a] = byAgent[a] || []).push(f);
  }

  // Discovery uses short finding names and full dispatch types; merge needs a consistent roster to retain zero-finding agents in methodology.
  const shortAgentName = (a) => (typeof a === 'string' ? a.split(':').pop() : a);
  const agents = Array.isArray(out.dispatched)
    ? out.dispatched.map(shortAgentName)
    : (Object.keys(byAgent).length ? Object.keys(byAgent) : AGENTS.map(shortAgentName));
  return merge(byAgent, { ...M, agents, rejected: out.rejected });
}

export function dedupById(findings, decisions = []) {
  const seen = new Map();
  let duplicatesResolved = 0;
  let droppedNoId = 0;

  for (const finding of findings) {
    const fid = finding.id;
    if (fid === undefined || fid === null) { droppedNoId += 1; continue; }
    if (seen.has(fid)) {
      duplicatesResolved += 1;
      if (finding.trace_id) decisions.push(traceDecision(finding, 'duplicate_id', 'first id wins', [seen.get(fid).trace_id].filter(Boolean)));
    }
    else seen.set(fid, finding);
  }

  return { merged: [...seen.values()], duplicatesResolved, droppedNoId };
}
