import { READ_PLAN_MAX_CHUNKS } from './registry.js';
import { READ_PLAN_MAX_LINES } from './contextPlan.js';
import { repoRelativeFindingPath } from './paths.js';

const isNatural = (n) => Number.isSafeInteger(n) && n >= 0;
const validReads = (reads, lines) => Array.isArray(reads) && reads.length > 0 && reads.length <= READ_PLAN_MAX_CHUNKS
  && reads.every((r) => r && Number.isSafeInteger(r.offset) && r.offset > 0
    && Number.isSafeInteger(r.limit) && r.limit > 0 && r.limit <= READ_PLAN_MAX_LINES
    && r.offset + r.limit - 1 <= lines);

export function validDiscoveryPlan(plan, lines) {
  if (!plan || plan.version !== 1 || !['available', 'unavailable'].includes(plan.status)
    || !Array.isArray(plan.regions) || plan.regions.length > READ_PLAN_MAX_CHUNKS
    || !Number.isSafeInteger(lines) || lines < 1) return false;
  const ids = new Set();
  for (const r of plan.regions) {
    if (!r || typeof r.id !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(r.id) || ids.has(r.id)
      || typeof r.file !== 'string' || repoRelativeFindingPath('.', r.file).file !== r.file
      || !isNatural(r.line_start) || !isNatural(r.line_end) || r.line_end < r.line_start
      || (r.line_start === 0 && r.line_end !== 0) || !validReads(r.reads, lines)) return false;
    ids.add(r.id);
  }
  return validReads(plan.fact_reads, lines);
}

function sourceRefs(value, output = []) {
  if (!value || typeof value !== 'object') return output;
  if (typeof value.file === 'string' && Number.isSafeInteger(value.line_start) && value.line_start > 0) {
    const end = value.line_end === undefined ? value.line_start : value.line_end;
    if (Number.isSafeInteger(end) && end >= value.line_start) output.push({ file: value.file, start: value.line_start, end });
  }
  for (const [key, child] of Object.entries(value)) {
    if (key === 'cross_file_refs' && Array.isArray(child)) {
      for (const ref of child) {
        const match = typeof ref === 'string' && /^(.*?):(?:L)?(\d+)(?:-(?:L)?(\d+))?$/.exec(ref);
        if (match) output.push({ file: match[1], start: Number(match[2]), end: Number(match[3] || match[2]) });
      }
    } else if (child && typeof child === 'object') sourceRefs(child, output);
  }
  return output;
}

export function findingInRegions(finding, regions, repoRoot) {
  const path = repoRelativeFindingPath(repoRoot || '.', finding?.file);
  return !!path.file && regions.some((r) => r.file === path.file && r.line_start > 0
    && Number.isSafeInteger(finding.line_start) && finding.line_start >= r.line_start
    && finding.line_start <= r.line_end);
}

// These are agent assertions and citations, not evidence of comprehension or tool telemetry.
export function coverageLedger(plan, specs, results, repoRoot, lines) {
  if (!validDiscoveryPlan(plan, lines) || plan.status !== 'available') return { version: 1, status: 'unavailable', regions: [], uncovered: [], invalid_receipts: [], hunters: [] };
  const regions = plan.regions.map((r) => ({ id: r.id, file: r.file, line_start: r.line_start, line_end: r.line_end, read_by: [], cited_by: [] }));
  const byId = new Map(regions.map((r) => [r.id, r]));
  const invalid_receipts = [];
  specs.forEach((spec, i) => {
    const result = results[i];
    if (!Array.isArray(result?.findings)) return;
    const bad = [];
    for (const id of Array.isArray(result.read_regions) ? result.read_regions : []) {
      if (byId.has(id)) {
        const readBy = byId.get(id).read_by;
        if (!readBy.includes(spec.agentType)) readBy.push(spec.agentType);
      } else bad.push(id);
    }
    if (bad.length) invalid_receipts.push({ agent: spec.agentType, ids: bad });
    for (const ref of sourceRefs(result.findings)) {
      const path = repoRelativeFindingPath(repoRoot || '.', ref.file);
      if (!path.file) continue;
      for (const r of regions) {
        if (r.file === path.file && r.line_start > 0 && ref.start <= r.line_end && r.line_start <= ref.end
          && !r.cited_by.includes(spec.agentType)) r.cited_by.push(spec.agentType);
      }
    }
  });
  return { version: 1, status: 'available', regions, uncovered: regions.filter((r) => !r.read_by.length && !r.cited_by.length).map((r) => r.id), invalid_receipts, hunters: [] };
}

export function gapHunterPrompt(path, plan, regions) {
  // No shared-context instruction, PR narrative, reviewer conclusions or covered region is transported.
  const reads = [...plan.fact_reads, ...regions.flatMap((r) => r.reads)];
  return `Read only the assigned ranges at ${path}. Tagged blocks end only at the closing tag repeating their opening id; intervening tags are content. `
    + `Use these exact Read(offset, limit) pairs; if partial, continue only to that range's end: ${JSON.stringify(reads)}. `
    + `Assigned discovery regions: ${JSON.stringify(regions.map(({ reads: _reads, ...r }) => r))}.`;
}
