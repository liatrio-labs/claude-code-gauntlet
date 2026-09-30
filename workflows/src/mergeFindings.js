// Merge structured and text-channel findings into the Phase 4 envelope.
import { dedupById } from './findingDedup.js';
import { repoRelativeFindingPath } from './paths.js';
import { FINDING_PATH_ARRAY_FIELDS } from './registry.js';

const KNOWN_DIMENSIONS = new Set([
  'bug',
  'security',
  'cross_file_impact',
  'test_coverage',
  'convention',
  'intent',
  'comment_accuracy',
  'type_design',
  'simplification',
]);

export const REQUIRED_FIELDS = ['id', 'file', 'line_start', 'title', 'description', 'severity', 'confidence'];

// --- Channel 1: NDJSON parsing ---------------------------------------------

export function parseNdjson(text, agent) {
  const findings = [];
  const warnings = [];
  if (text === undefined || text === null) return { findings, warnings };

  let lineno = 0;
  for (const rawLine of text.split('\n')) {
    lineno += 1;
    const line = rawLine.trim();
    if (!line) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      warnings.push(`[${agent}] NDJSON line ${lineno}: invalid JSON`);
      continue;
    }
    if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
      warnings.push(`[${agent}] NDJSON line ${lineno}: expected object, got ${jsonTypeName(obj)}`);
      continue;
    }
    findings.push(obj);
  }
  return { findings, warnings };
}

function jsonTypeName(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

// --- Channel 2: text fallback parsing --------------------------------------

export function extractJsonBlocks(text) {
  const results = [];
  let i = 0;
  while (i < text.length) {
    if (text[i] !== '{') {
      i += 1;
      continue;
    }
    const obj = tryParseJsonAt(text, i);
    if (obj !== null && obj !== undefined && typeof obj === 'object' && !Array.isArray(obj) && 'id' in obj) {
      results.push(obj);
      i = findEndOfJson(text, i);
    } else {
      i += 1;
    }
  }
  return results;
}

function scanJsonObject(text, start) {
  let depth = 0;
  let inString = false;
  let escapeNext = false;
  let i = start;
  while (i < text.length) {
    const ch = text[i];
    if (escapeNext) {
      escapeNext = false;
      i += 1;
      continue;
    }
    if (ch === '\\' && inString) {
      escapeNext = true;
      i += 1;
      continue;
    }
    if (ch === '"') {
      inString = !inString;
      i += 1;
      continue;
    }
    if (inString) {
      i += 1;
      continue;
    }
    if (ch === '{') {
      depth += 1;
    } else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
    i += 1;
  }
  return -1;
}

function tryParseJsonAt(text, start) {
  const end = scanJsonObject(text, start);
  if (end < 0) return null;
  try {
    return JSON.parse(text.slice(start, end));
  } catch {
    return null;
  }
}

function findEndOfJson(text, start) {
  const end = scanJsonObject(text, start);
  if (end < 0) return text.length + 1;
  return end;
}

export function parseTextFile(text, agent) {
  const findings = [];
  const warnings = [];
  let hasProse = false;
  let hasSkip = false;

  if (text === undefined || text === null) return { findings, warnings, hasProse, hasSkip };
  if (text.trim() === '') return { findings, warnings, hasProse, hasSkip };

  if (/^\s*SKIP\s*:/im.test(text)) hasSkip = true;

  for (const obj of extractJsonBlocks(text)) {
    if ('id' in obj) findings.push(obj);
  }

  let stripped = text.replace(/\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/g, '');
  stripped = stripped.replace(/^\s*SKIP\s*:.*$/gim, '');
  stripped = stripped.trim();
  if (stripped.replace(/\s+/g, '').length > 20) hasProse = true;

  return { findings, warnings, hasProse, hasSkip };
}

// --- Agent field injection --------------------------------------------------

export function injectAgentField(findings, agent) {
  for (const f of findings) f.agent = agent;
}

// --- Validation -------------------------------------------------------------

function invalidFindingPathWarning(finding, field, reason, action) {
  return `[${finding.id ?? '<no id>'}] Invalid ${field} path: ${reason} - ${action}`;
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
  return { valid: true, warning: null };
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
    if (result.warning) warnings.push(result.warning);
    if (!result.valid) continue;
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

// --- Truncation detection ---------------------------------------------------

export function detectTruncation(ndjsonRaw, textPre, hasProse, hasSkip) {
  const warnings = [];
  for (const agent of Object.keys(ndjsonRaw)) {
    const ndjsonEmpty = (ndjsonRaw[agent] || 0) === 0;
    const textEmpty = (textPre[agent] || []).length === 0;
    const prose = hasProse[agent] || false;
    const skip = hasSkip[agent] || false;
    if (ndjsonEmpty && textEmpty && prose && !skip) {
      warnings.push(
        `[${agent}] Possible truncation: no structured findings, ` +
          'prose present in text output, no SKIP lines detected',
      );
    }
  }
  return warnings;
}

// --- Output assembly --------------------------------------------------------

function assembleOutput(
  findings,
  agents,
  ndjsonCount,
  textFallbackCount,
  duplicatesResolved,
  droppedNoId,
  truncationWarnings,
  validationWarnings,
  baseBranch,
  headSha,
  prNumber,
  owner,
  repo,
) {
  return {
    findings,
    base_branch: baseBranch,
    head_sha: headSha,
    pr_number: prNumber,
    owner,
    repo,
    methodology: {
      agents_dispatched: agents,
      findings_per_channel: {
        ndjson: ndjsonCount,
        text_fallback: textFallbackCount,
      },
      duplicates_resolved: duplicatesResolved,
      dropped_no_id: droppedNoId,
      truncation_warnings: truncationWarnings,
      validation_warnings: validationWarnings,
    },
  };
}

// --- Main merge pipeline ----------------------------------------------------

export function merge(ndjsonContents, textContents, meta) {
  const M = typeof meta === 'string' ? JSON.parse(meta) : meta;
  const agents = M.agents;
  const nd = ndjsonContents || {};
  const tx = textContents || {};
  const allWarnings = [];

  // Channel 1: NDJSON.
  const ndjsonFindings = {};
  for (const agent of agents) {
    const { findings, warnings } = parseNdjson(nd[agent], agent);
    ndjsonFindings[agent] = findings;
    for (const w of warnings) allWarnings.push(w);
  }

  // Channel 2: text fallback.
  const textFindings = {};
  const textHasProse = {};
  const textHasSkip = {};
  for (const agent of agents) {
    const { findings, warnings, hasProse, hasSkip } = parseTextFile(tx[agent], agent);
    textFindings[agent] = findings;
    textHasProse[agent] = hasProse;
    textHasSkip[agent] = hasSkip;
    for (const w of warnings) allWarnings.push(w);
  }

  // Pre-validation raw counts (for truncation detection only).
  const ndjsonRawCounts = {};
  for (const agent of Object.keys(ndjsonFindings)) ndjsonRawCounts[agent] = ndjsonFindings[agent].length;

  for (const [agent, findings] of Object.entries(textFindings)) injectAgentField(findings, agent);
  for (const [agent, findings] of Object.entries(ndjsonFindings)) injectAgentField(findings, agent);

  // Validate the combined flat list (pre-dedup) so warnings cover all raw findings.
  const allNdjsonFlat = Object.values(ndjsonFindings).flat();
  const allTextFlat = Object.values(textFindings).flat();
  const { warnings: preValWarnings } = validateFindings(allNdjsonFlat.concat(allTextFlat));

  let droppedNoId = 0;
  for (const f of allNdjsonFlat.concat(allTextFlat)) {
    const fid = f.id;
    if (fid === undefined || fid === null || fid === '') droppedNoId += 1;
  }

  // Filter each channel to only valid findings.
  const filterValid = (dict) => {
    const out = {};
    for (const [agent, findings] of Object.entries(dict)) {
      out[agent] = validateFindings(findings).valid;
    }
    return out;
  };

  const ndjsonValid = filterValid(ndjsonFindings);
  // Snapshot pre-validation text findings for truncation detection before filtering.
  const textFindingsPreValidation = {};
  for (const [agent, findings] of Object.entries(textFindings)) textFindingsPreValidation[agent] = findings.slice();
  const textValid = filterValid(textFindings);

  // Deduplicate (NDJSON wins on id collision). dedup's own dropped_no_id is 0
  // here because filterValid already removed id-less findings.
  const { merged, duplicatesResolved } = dedupById(ndjsonValid, textValid);

  // Channel counts: both post-validation, post-dedup, via the ndjson-source id set.
  const ndjsonIds = new Set();
  for (const findings of Object.values(ndjsonValid)) {
    for (const f of findings) if ('id' in f) ndjsonIds.add(f.id);
  }
  let ndjsonCount = 0;
  let textFallbackCount = 0;
  for (const f of merged) {
    if (ndjsonIds.has(f.id)) ndjsonCount += 1;
    else textFallbackCount += 1;
  }

  // Truncation detection uses pre-validation counts + pre-validation text findings.
  const truncationWarnings = detectTruncation(
    ndjsonRawCounts,
    textFindingsPreValidation,
    textHasProse,
    textHasSkip,
  );

  const validationWarnings = allWarnings.concat(preValWarnings);

  return assembleOutput(
    merged,
    agents,
    ndjsonCount,
    textFallbackCount,
    duplicatesResolved,
    droppedNoId,
    truncationWarnings,
    validationWarnings,
    M.base_branch,
    M.head_sha,
    M.pr_number,
    M.owner,
    M.repo,
  );
}
