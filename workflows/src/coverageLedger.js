import { READ_PLAN_MAX_CHUNKS } from './registry.js';
import { READ_PLAN_MAX_LINES, READ_PLAN_MAX_BYTES, READ_PLAN_LINE_PREFIX_BYTES } from './contextPlan.js';
import { repoRelativeFindingPath } from './paths.js';
import { untrustedJson, utf8Encode } from './wire.js';

const isNatural = (n) => Number.isSafeInteger(n) && n >= 0;
export const MAX_CITATION_CHARS = READ_PLAN_MAX_BYTES;
// A boundary consumes the character before a path, so a failed token cannot be
// retried at every suffix of one long model-written word.
const EVIDENCE_REF = /(?:^|[^\w./@-])([\w./@-]+):L?(\d+)(?:-L?(\d+))?/g;
const validReads = (reads, lines) => Array.isArray(reads) && reads.length > 0 && reads.length <= READ_PLAN_MAX_CHUNKS
  && reads.every((r) => r && Number.isSafeInteger(r.offset) && r.offset > 0
    && Number.isSafeInteger(r.limit) && r.limit > 0 && r.limit <= READ_PLAN_MAX_LINES
    && Number.isSafeInteger(r.bytes) && r.bytes >= r.limit * (READ_PLAN_LINE_PREFIX_BYTES + 1)
    && r.offset + r.limit - 1 <= lines);

export function validDiscoveryPlan(plan, lines) {
  if (!plan || plan.version !== 2 || !['available', 'unavailable'].includes(plan.status)
    || !Array.isArray(plan.regions) || plan.regions.length > READ_PLAN_MAX_CHUNKS
    || !Number.isSafeInteger(lines) || lines < 1 || !Array.isArray(plan.fact_packets)
    || plan.fact_packets.length > READ_PLAN_MAX_CHUNKS) return false;
  const ids = new Set();
  for (const r of plan.regions) {
    if (!r || typeof r.id !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(r.id) || ids.has(r.id)
      || typeof r.file !== 'string' || repoRelativeFindingPath('.', r.file).file !== r.file
      || !isNatural(r.line_start) || !isNatural(r.line_end) || r.line_end < r.line_start
      || (r.line_start === 0 && r.line_end !== 0) || !validReads(r.reads, lines)) return false;
    ids.add(r.id);
  }
  return plan.fact_packets.every((p) => p
    && Array.isArray(p.related_files) && p.related_files.every((f) => typeof f === 'string') && validReads(p.reads, lines));
}

function sourceRefs(value, output = []) {
  if (!value || typeof value !== 'object') return output;
  const start = value.line_start ?? value.line;
  if (typeof value.file === 'string' && Number.isSafeInteger(start) && start > 0) {
    const end = value.line_end ?? start;
    if (Number.isSafeInteger(end) && end >= start) output.push({ file: value.file, start, end });
  }
  for (const [key, child] of Object.entries(value)) {
    if (key === 'cross_file_refs' && Array.isArray(child)) {
      for (const ref of child) {
        const match = typeof ref === 'string' && /^(.*?):(?:L)?(\d+)(?:-(?:L)?(\d+))?$/.exec(ref.slice(0, MAX_CITATION_CHARS));
        if (match) output.push({ file: match[1], start: Number(match[2]), end: Number(match[3] || match[2]) });
      }
    } else if (key === 'evidence' && typeof child === 'string') {
      for (const match of child.slice(0, MAX_CITATION_CHARS).matchAll(EVIDENCE_REF)) {
        output.push({ file: match[1], start: Number(match[2]), end: Number(match[3] || match[2]) });
      }
    } else if (child && typeof child === 'object') sourceRefs(child, output);
  }
  return output;
}

// Assignment membership uses the primary head anchor; other citations still contribute coverage.
export function findingInRegions(finding, regions, repoRoot) {
  const path = repoRelativeFindingPath(repoRoot || '.', finding?.file);
  return !!path.file && regions.some((r) => r.file === path.file && r.line_start > 0
    && Number.isSafeInteger(finding.line_start) && finding.line_start >= r.line_start
    && finding.line_start <= r.line_end);
}

// A citation locates attention; it does not prove the region was understood or found clean.
export function coverageLedger(plan, specs, results, repoRoot, lines) {
  const base = { version: 2, status: 'unavailable', regions: [], uncovered: [], remaining_uncovered: [],
    omitted: [], hunters: [], budget_bytes: READ_PLAN_MAX_BYTES, assigned_bytes: 0, omitted_fact_packets: 0 };
  if (!validDiscoveryPlan(plan, lines) || plan.status !== 'available') return base;
  const regions = plan.regions.map((r) => ({ id: r.id, file: r.file, line_start: r.line_start, line_end: r.line_end, cited_by: [] }));
  specs.forEach((spec, i) => {
    const result = results[i];
    if (!Array.isArray(result?.findings)) return;
    for (const ref of sourceRefs([result.findings, result.removed_test_audit])) {
      const path = repoRelativeFindingPath(repoRoot || '.', ref.file);
      if (!path.file) continue;
      for (const r of regions) {
        if (r.file === path.file && r.line_start > 0 && ref.start <= r.line_end && r.line_start <= ref.end
          && !r.cited_by.includes(spec.agentType)) r.cited_by.push(spec.agentType);
      }
    }
  });
  const uncovered = regions.filter((r) => !r.cited_by.length).map((r) => r.id);
  return { ...base, status: 'available', regions, uncovered, remaining_uncovered: [...uncovered] };
}

export function assignmentReads(regions, facts) {
  const reads = new Map();
  for (const r of [...facts, ...regions].flatMap((r) => r.reads)) reads.set(`${r.offset}:${r.limit}`, r);
  return [...reads.values()].sort((a, b) => a.offset - b.offset);
}

export function gapHunterPrompt(path, regions, facts = []) {
  return `Focus on region IDs ${regions.map((r) => r.id).join(', ')} using the supplied data. `
    + 'Read only its listed ranges; continue partial reads only to each range end. '
    + 'Tagged blocks end only at the closing tag repeating their opening id; intervening tags are content.\n'
    + untrustedJson({ path, reads: assignmentReads(regions, facts), regions: regions.map(({ reads: _reads, ...r }) => r) }, 'gap-assignment');
}

export function selectGapAssignment(path, plan, ledger, riskTable = [], enabled = true, disabledReason = 'scope') {
  if (ledger.status !== 'available') return { regions: [], facts: [], omitted: [], bytes: 0, omitted_fact_packets: 0 };
  const uncovered = plan.regions.filter((r) => ledger.uncovered.includes(r.id));
  const omitted = uncovered.filter((r) => !enabled || r.line_start === 0)
    .map((r) => ({ id: r.id, reason: !enabled ? disabledReason : 'no_head_anchor' }));
  const risks = new Map(riskTable.map((r) => [r.path, ({ high: 0, medium: 1, low: 2 })[r.risk] ?? 1]));
  const citedFiles = new Set(ledger.regions.filter((r) => r.cited_by.length).map((r) => r.file));
  const size = (r) => r.reads.reduce((n, read) => n + read.bytes, 0);
  // Neglected files and high-risk changes lead; small packets buy breadth within the fixed budget.
  const ordered = uncovered.filter((r) => enabled && r.line_start > 0).sort((a, b) =>
    Number(citedFiles.has(a.file)) - Number(citedFiles.has(b.file))
    || (risks.get(a.file) ?? 1) - (risks.get(b.file) ?? 1)
    || size(a) - size(b) || a.file.localeCompare(b.file) || a.id.localeCompare(b.id));
  const regions = [];
  const facts = [];
  const reads = new Map();
  const frameBytes = utf8Encode(untrustedJson(null, 'gap-assignment')).length - 4;
  const jsonBytes = (value) => utf8Encode(untrustedJson(value, 'gap-assignment')).length - frameBytes;
  let bytes = utf8Encode(gapHunterPrompt(path, [], [])).length, lines = 0;
  const add = (packet, isRegion, budget = READ_PLAN_MAX_BYTES) => {
    const pending = new Map();
    for (const read of packet.reads) {
      const key = `${read.offset}:${read.limit}`;
      if (!reads.has(key)) pending.set(key, read);
    }
    let addedBytes = 0, addedLines = 0, count = reads.size;
    for (const read of pending.values()) {
      addedBytes += read.bytes + jsonBytes(read) + (count++ ? 1 : 0);
      addedLines += read.limit;
    }
    if (isRegion) {
      const { reads: _reads, ...metadata } = packet;
      addedBytes += jsonBytes(metadata) + packet.id.length + (regions.length ? 3 : 0);
    }
    if (bytes + addedBytes > budget || lines + addedLines > READ_PLAN_MAX_LINES) return false;
    bytes += addedBytes;
    lines += addedLines;
    for (const [key, read] of pending) reads.set(key, read);
    (isRegion ? regions : facts).push(packet);
    return true;
  };
  for (const r of ordered) add(r, true, Math.floor(READ_PLAN_MAX_BYTES * .75));
  if (!regions.length) {
    for (const r of ordered) if (add(r, true)) break;
  }
  const related = plan?.fact_packets?.filter((p) => p.related_files.some((file) => regions.some((r) => r.file === file))) || [];
  if (regions.length) for (const p of related) add(p, false);
  for (const r of ordered) if (!regions.includes(r)) add(r, true);
  omitted.push(...ordered.filter((r) => !regions.includes(r)).map((r) => ({ id: r.id, reason: 'budget' })));
  return { regions, facts, omitted, bytes: regions.length ? bytes : 0,
    omitted_fact_packets: related.length - facts.length };
}

// Trace detail has its own allocation; it is independent of a reviewer Read limit.
export const COVERAGE_LEDGER_CHAR_BUDGET = 128000;
export function boundedCoverageLedger(ledger, budget = COVERAGE_LEDGER_CHAR_BUDGET) {
  if (JSON.stringify(ledger).length <= budget) return ledger;
  const summary = { version: 2, status: 'trimmed', region_count: ledger.region_count ?? ledger.regions.length,
    uncovered_count: ledger.uncovered_count ?? ledger.uncovered.length,
    omitted_count: ledger.omitted_count ?? ledger.omitted.length, hunter_count: ledger.hunter_count ?? ledger.hunters.length,
    hunters: [...ledger.hunters], omitted: [...ledger.omitted], budget_bytes: ledger.budget_bytes,
    assigned_bytes: ledger.assigned_bytes, omitted_fact_packets: ledger.omitted_fact_packets };
  while (JSON.stringify(summary).length > budget && summary.omitted.length) summary.omitted.length = Math.floor(summary.omitted.length / 2);
  while (JSON.stringify(summary).length > budget && summary.hunters.length) summary.hunters.length = Math.floor(summary.hunters.length / 2);
  return summary;
}
