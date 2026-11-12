import { READ_PLAN_MAX_BYTES } from './contextPlan.js';
import { repoRelativeFindingPath } from './paths.js';

export const MAX_CITATION_CHARS = READ_PLAN_MAX_BYTES;
const EVIDENCE_REF = /(?:^|[^\w./@-])([\w./@-]+):L?(\d+)(?:-L?(\d+))?/g;

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

// The workflow can project citations without loading any changed-region metadata.
export function coverageReferences(specs, results, repoRoot) {
  return specs.flatMap((spec, i) => Array.isArray(results[i]?.findings)
    ? sourceRefs([results[i].findings, results[i].removed_test_audit]).flatMap((ref) => {
      const path = repoRelativeFindingPath(repoRoot || '.', ref.file);
      return path.file ? [[spec.agentType, path.file, ref.start, ref.end]] : [];
    }) : []);
}

export function coverageAnchors(findings, repoRoot) {
  const seen=new Set();
  return findings.flatMap((finding) => {
    const path = repoRelativeFindingPath(repoRoot || '.', finding?.file);
    if(!path.file || !Number.isSafeInteger(finding.line_start) || finding.line_start<1 || typeof finding.id!=='string' || !finding.id.trim() || seen.has(finding.id))return [];
    seen.add(finding.id);
    return [[finding.id,path.file,finding.line_start]];
  });
}

// Trace detail has its own allocation; it is independent of a reviewer Read limit.
export const COVERAGE_LEDGER_CHAR_BUDGET = 128000;
export function boundedCoverageLedger(ledger, budget = COVERAGE_LEDGER_CHAR_BUDGET) {
  if (JSON.stringify(ledger).length <= budget) return ledger;
  const summary = { version: 2, status: 'trimmed', region_count: ledger.region_count ?? ledger.regions.length,
    uncovered_count: ledger.uncovered_count ?? ledger.uncovered.length,
    ...((ledger.remaining_uncovered_count !== undefined || Array.isArray(ledger.remaining_uncovered))
      ? {remaining_uncovered_count:ledger.remaining_uncovered_count ?? ledger.remaining_uncovered.length} : {}),
    omitted_count: ledger.omitted_count ?? ledger.omitted.length, hunter_count: ledger.hunter_count ?? ledger.hunters.length,
    hunters: [...ledger.hunters], omitted: [...ledger.omitted], budget_bytes: ledger.budget_bytes,
    assigned_bytes: ledger.assigned_bytes, omitted_fact_packets: ledger.omitted_fact_packets };
  while (JSON.stringify(summary).length > budget && summary.omitted.length) summary.omitted.length = Math.floor(summary.omitted.length / 2);
  while (JSON.stringify(summary).length > budget && summary.hunters.length) summary.hunters.length = Math.floor(summary.hunters.length / 2);
  return summary;
}
