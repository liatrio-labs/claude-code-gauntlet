// renderReport.js — deterministic report.md renderer (issues #36, #67).
// Keep every import on ONE line: workflows/build.js deliberately rejects imports its
// line-based bundle stripper cannot remove safely.
import { SEVERITY_ORDER } from './filterFindings.js';
import { rankFindings } from './applyChallenges.js';
import { AGENTS, AGENT_LABELS, DIMENSIONS, FINDING_PROP_TYPES, BRAND_MARK, BRAND_NAME, SEVERITY_EMOJI, SEVERITY_EMOJI_FALLBACK } from './registry.js';

// Fields the report path never sees. suggested_fix_code itself (no apply-check oracle
// exists at report time, see stripReportExcludedFields below) plus the two stamps
// filterFindings.js/filter_findings.py leave behind when IT stripped suggested_fix_code
// earlier in the pipeline (suggested_fix_code_removed_by / _removal_reason) — dangling
// metadata for a field the report renderer never sees either way (#220 review). A list,
// not a single field check, so adding a future report-excluded field is a one-line edit
// here rather than a second copy of stripReportExcludedFields's iteration.
export const REPORT_EXCLUDED_FIELDS = [
  'suggested_fix_code',
  'suggested_fix_code_removed_by',
  'suggested_fix_code_removal_reason',
];

// stripReportExcludedFields(findings) -> new array, same finding objects EXCEPT a
// shallow copy wherever any REPORT_EXCLUDED_FIELDS key was present. Used ONLY on the
// report path: the report renderer has no apply-check oracle at report time, so
// suggested_fix_code must never reach the report body, and its removal stamps are
// meaningless without it. selectDelivery / writerPayload read the SAME finding objects
// renderReport was called with, unstripped — delivery keeps every field for its own
// live-oracle apply-check (scripts/post_review.py).
export function stripReportExcludedFields(findings) {
  return (findings || []).map((f) => {
    if (!f || typeof f !== 'object') return f;
    if (!REPORT_EXCLUDED_FIELDS.some((key) => key in f)) return f;
    const copy = { ...f };
    for (const key of REPORT_EXCLUDED_FIELDS) delete copy[key];
    return copy;
  });
}

// dimensionsSummaryTable({ dispatched, degraded, findings, unverified }) -> markdown string
//
// Computes the Review Dimensions Summary table (report-format.md) in CODE, as a pure
// function of pipeline stats, instead of asking a Phase 8 model to classify each
// dimension itself (issue #89). Before this, the table was never rendered at all:
// reportPrompt never asked for it and reportInput never carried discoverOut.degraded /
// discoverOut.dispatched. One row per DISCOVERY AGENT (registry AGENTS order), not per
// dimension — a multi-dimension agent (conventions-and-intent) aggregates all of its
// dimensions' findings into one row. Output starts at the header row (no leading
// `## Review Dimensions Summary` heading) — heading placement is the caller's concern.
//
// Row classification (N = high-confidence finding count for the agent, M = unverified
// count, evaluated in this fixed priority order so at most one rule ever fires):
//   1. not dispatched (scope-skipped, e.g. light-scope agentFlags.deep=false) -> Skipped
//   2. degraded (one of its dimensions is in `degraded`) with N+M==0 -> agent never
//      returned usable coverage
//   3. degraded with N+M>0 -> partial coverage
//   4. dispatched, not degraded, N+M==0 -> clean run, genuinely zero findings
//   5. N>0, not degraded -> the normal case; Notes carries a severity breakdown
//   6. N==0, M>0, not degraded -> every finding this agent produced was routed to the
//      unverified/pipeline-degraded bucket
//
// SEVERITY_ORDER is imported from filterFindings.js (its single owner, per that file's
// own note — a second top-level declaration collides at bundle time).

// Findings with a missing/unknown `dimension` are silently excluded from every row's
// count: the discovery contracts pin `dimension` to one of the nine registry names, so
// an unmapped value here is belt-and-braces, never a live path.
function dimensionOwnerMap() {
  const owner = {};
  for (const d of DIMENSIONS) owner[d.dimension] = d.agentType;
  return owner;
}

// "2 high, 1 low" — counted over the agent's HIGH-CONFIDENCE findings only, in a fixed
// severity order (critical, high, medium, low first; any other value the schema does not
// forbid — `severity` is declared `string`, not an enum — trails in first-seen order so
// no finding is silently dropped from the count). Empty string when no finding in the row
// carries a severity value at all. Severity is normalized with the same one-line rule as
// its report heading, so a row cannot be injected into this table.
function severityBreakdown(rowFindings) {
  const counts = new Map();
  for (const f of rowFindings) {
    if (!f || !f.severity) continue;
    const severity = oneLine(f.severity).toLowerCase();
    counts.set(severity, (counts.get(severity) || 0) + 1);
  }
  if (counts.size === 0) return '';
  const known = SEVERITY_ORDER.filter((s) => counts.has(s));
  const rest = [...counts.keys()].filter((s) => !SEVERITY_ORDER.includes(s));
  return [...known, ...rest].map((s) => `${counts.get(s)} ${s}`).join(', ');
}

export function dimensionsSummaryTable(input) {
  const inp = input || {};
  const dispatchedSet = new Set(inp.dispatched || []);
  const degradedSet = new Set(inp.degraded || []);
  const owner = dimensionOwnerMap();

  const byAgent = new Map(AGENTS.map((a) => [a, []]));
  const unverifiedByAgent = new Map(AGENTS.map((a) => [a, []]));
  for (const f of (inp.findings || [])) {
    const agentType = owner[f && f.dimension];
    if (agentType && byAgent.has(agentType)) byAgent.get(agentType).push(f);
  }
  for (const f of (inp.unverified || [])) {
    const agentType = owner[f && f.dimension];
    if (agentType && unverifiedByAgent.has(agentType)) unverifiedByAgent.get(agentType).push(f);
  }

  const rows = AGENTS.map((agentType) => {
    const rowFindings = byAgent.get(agentType);
    const rowUnverified = unverifiedByAgent.get(agentType);
    const n = rowFindings.length;
    const m = rowUnverified.length;
    const dims = DIMENSIONS.filter((d) => d.agentType === agentType).map((d) => d.dimension);
    const isDegraded = dims.some((d) => degradedSet.has(d));
    const isDispatched = dispatchedSet.has(agentType);
    const label = AGENT_LABELS[agentType] || agentType;
    const agentShort = agentType.split(':').pop();

    let findingsCell;
    let notes;
    if (!isDispatched) {
      findingsCell = '—';
      notes = 'Skipped — not dispatched in this run';
    } else if (isDegraded && n + m === 0) {
      findingsCell = '—';
      notes = 'No results — agent did not complete';
    } else if (isDegraded) {
      findingsCell = m > 0 ? `${n} (+${m} unverified)` : `${n}`;
      notes = 'Partial — agent may not have completed';
    } else if (n + m === 0) {
      findingsCell = '0';
      notes = 'Clean — no findings returned';
    } else if (n > 0) {
      findingsCell = m > 0 ? `${n} (+${m} unverified)` : `${n}`;
      notes = severityBreakdown(rowFindings);
    } else {
      findingsCell = `0 (+${m} unverified)`;
      notes = 'Unverified findings only — see secondary section';
    }

    return `| ${label} | ${agentShort} | ${findingsCell} | ${notes} |`;
  });

  return ['| Dimension | Agent | Findings | Notes |', '|-----------|-------|----------|-------|', ...rows].join('\n');
}

// Groups findings by `consolidation_key` before they reach the report renderer — #22 D2,
// same grouping rule `consolidate_delivery` applies to the posted comment payload,
// applied here to the report's findings list instead: non-primary group members are
// folded into the primary's `corroborations` array rather than listed as separate
// top-level findings. A finding with no (falsy) `consolidation_key` passes through
// unchanged — older artifacts / pre-consolidation findings render exactly as before.
function consolidateForReport(findings) {
  const list = findings || [];
  const groups = [];
  const keyToGroup = new Map();
  for (const f of list) {
    const key = f && f.consolidation_key;
    if (!key) {
      groups.push({ primary: f, corroborators: [] });
      continue;
    }
    let group = keyToGroup.get(key);
    if (!group) {
      group = { primary: null, corroborators: [] };
      keyToGroup.set(key, group);
      groups.push(group);
    }
    if (f.consolidation_primary) {
      if (!group.primary) group.primary = f;
      else group.corroborators.push(f);
    } else group.corroborators.push(f);
  }
  return groups.map((group) => {
    // Reachable only for hand-assembled payloads: filterFindings.js always
    // stamps exactly one consolidation_primary per group. If a caller's data
    // has none, fall back to the first-seen member rather than surfacing `null`.
    const primary = group.primary || group.corroborators.shift();
    if (!group.corroborators.length) return primary;
    return {
      ...primary,
      corroborations: group.corroborators.map((c) => ({
        agent: c.agent,
        dimension: c.dimension,
        confidence: c.confidence,
        title: c.title,
        description: c.description,
      })),
    };
  });
}

// Fields with a dedicated placement below. Every other registry-declared field is
// rendered through reportExtraFields, so declared-but-unrendered cannot recur.
const REPORT_PLACED_FIELDS = new Set([
  'id', 'file', 'line_start', 'line_end', 'title', 'description', 'severity',
  'confidence', 'dimension', 'origin', 'evidence', 'suggestion',
  'claude_md_rule', 'spec_text', 'cross_file_refs',
]);

export function reportExtraFields() {
  const declared = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const dimension of DIMENSIONS) {
    for (const key of Object.keys(dimension.schemaExtra || {})) declared.add(key);
  }
  return [...declared]
    .filter((key) => !REPORT_PLACED_FIELDS.has(key) && !REPORT_EXCLUDED_FIELDS.includes(key))
    .sort();
}

// 'failure_scenario' -> 'Failure scenario'
function fieldLabel(key) {
  const words = String(key).replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// A fence at least one backtick longer than the longest run inside text, minimum 3.
function fenceFor(text) {
  let longest = 0;
  for (const match of String(text).matchAll(/`+/g)) longest = Math.max(longest, match[0].length);
  return '`'.repeat(Math.max(3, longest + 1));
}

// Bullet values, heading interpolations and the identity line are single-line positions.
function oneLine(value) {
  return String(value == null ? '' : value).replace(/\r?\n+/g, ' ').replace(/ +/g, ' ').trim();
}

function isPresent(value) {
  if (value == null) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'string') return value.trim() !== '';
  return true;
}

const severityMark = (severity) => SEVERITY_EMOJI[String(severity || '').toLowerCase()] || SEVERITY_EMOJI_FALLBACK;

function normalizeFindings(value) {
  if (!Array.isArray(value)) return [];
  return value.map((finding) => (finding && typeof finding === 'object' && !Array.isArray(finding) ? finding : {}));
}

// Collect protected evidence offsets while composing, then neutralize comment openers
// in one final pass everywhere else. Evidence must preserve source bytes verbatim.
function reportBuilder() {
  let text = '';
  const evidenceRanges = [];
  const add = (line = '') => {
    if (text.length) text += '\n';
    text += String(line);
  };
  const addEvidence = (evidence) => {
    const fence = fenceFor(evidence);
    const start = text.length + (text.length ? 1 : 0);
    add(fence);
    add(String(evidence));
    add(fence);
    evidenceRanges.push([start, text.length]);
  };
  const finish = () => {
    let out = '';
    let cursor = 0;
    while (true) {
      const index = text.indexOf('<!--', cursor);
      if (index < 0) break;
      out += text.slice(cursor, index);
      const protectedEvidence = evidenceRanges.some(([start, end]) => index >= start && index < end);
      out += protectedEvidence ? '<!--' : '&lt;!--';
      cursor = index + 4;
    }
    return out + text.slice(cursor);
  };
  return { add, addEvidence, finish };
}

function location(finding) {
  if (!isPresent(finding.file)) return '';
  const file = oneLine(finding.file);
  if (!isPresent(finding.line_start)) return file;
  const start = oneLine(finding.line_start);
  if (!isPresent(finding.line_end) || String(finding.line_end) === String(finding.line_start)) {
    return `${file}:${start}`;
  }
  return `${file}:${start}-${oneLine(finding.line_end)}`;
}

function unverifiedReason(finding) {
  const reasons = [];
  if (finding.origin === 'unknown') {
    reasons.push('the verify slice could not be proven against the dispatched document');
  }
  if (finding.challenge === 'skipped') {
    reasons.push('the challenge cap was reached, so this finding was not challenge-verified');
  }
  return reasons.length ? reasons.join('; ') : 'a pipeline stage was skipped or failed';
}

function renderFinding(builder, finding, unverified) {
  const blocks = [];
  blocks.push(() => builder.add(`#### ${oneLine(finding.title)}`));

  const bullets = [];
  const where = location(finding);
  if (where) bullets.push(`- **Location:** \`${where}\``);
  const classification = [];
  if (isPresent(finding.dimension)) classification.push(`**Dimension:** ${oneLine(finding.dimension)}`);
  if (isPresent(finding.confidence)) classification.push(`**Confidence:** ${oneLine(finding.confidence)}%`);
  if (classification.length) bullets.push(`- ${classification.join(' · ')}`);
  if (finding.origin === 'surfaced') {
    bullets.push('- **Origin:** surfaced — pre-existing, surfaced by this change');
  }
  if (unverified) bullets.push(`- **Unverified because:** ${unverifiedReason(finding)}`);
  if ((finding.report_tag ?? finding.report_destination) === 'suggestion') {
    bullets.push('- **Routing:** improvement suggestion');
  }
  if (finding.challenge_contested === true) {
    bullets.push('- **Contested:** the challenger could not confirm the cited location');
  }
  if (bullets.length) blocks.push(() => bullets.forEach(builder.add));

  if (isPresent(finding.description)) blocks.push(() => builder.add(String(finding.description)));
  if (isPresent(finding.evidence)) {
    blocks.push(() => {
      builder.add('**Evidence:**');
      builder.add();
      builder.addEvidence(finding.evidence);
    });
  }

  const extras = reportExtraFields()
    .filter((key) => isPresent(finding[key]))
    .map((key) => `- **${fieldLabel(key)}:** ${oneLine(finding[key])}`);
  if (extras.length) blocks.push(() => extras.forEach(builder.add));

  if (isPresent(finding.suggestion)) {
    blocks.push(() => {
      builder.add('**Suggested fix:**');
      builder.add();
      builder.add(String(finding.suggestion));
    });
  }

  const citedRule = isPresent(finding.claude_md_rule) ? finding.claude_md_rule : finding.spec_text;
  if (isPresent(citedRule)) {
    blocks.push(() => {
      builder.add('**Cited rule:**');
      builder.add();
      builder.add(String(citedRule).split(/\r?\n/).map((line) => `> ${line}`).join('\n'));
    });
  }

  if (isPresent(finding.cross_file_refs)) {
    blocks.push(() => builder.add(`- **Cross-file refs:** ${oneLine(finding.cross_file_refs)}`));
  }

  if (Array.isArray(finding.corroborations) && finding.corroborations.length) {
    blocks.push(() => {
      for (const corroboration of finding.corroborations) {
        const agent = oneLine(corroboration.agent);
        const dimension = oneLine(corroboration.dimension);
        const confidence = oneLine(corroboration.confidence);
        const title = oneLine(corroboration.title);
        builder.add(`- **Corroborated by** \`${agent}\` (\`${dimension}\`, confidence ${confidence}) — ${title}`);
        if (isPresent(corroboration.description)) {
          builder.add(String(corroboration.description).split(/\r?\n/).map((line) => `  ${line}`).join('\n'));
        }
      }
    });
  }

  blocks.forEach((block, index) => {
    if (index) builder.add();
    block();
  });
}

function severityKey(finding) {
  return (oneLine(finding.severity) || 'unknown').toLowerCase();
}

function renderSeverityBuckets(builder, findings, unverified) {
  const ranked = rankFindings(findings);
  const buckets = new Map();
  for (const finding of ranked) {
    const key = severityKey(finding);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(finding);
  }
  const known = SEVERITY_ORDER.filter((severity) => buckets.has(severity));
  const rest = [...buckets.keys()].filter((severity) => !SEVERITY_ORDER.includes(severity));
  for (const severity of [...known, ...rest]) {
    builder.add();
    builder.add(`### ${severityMark(severity)} ${fieldLabel(severity)}`);
    for (const finding of buckets.get(severity)) {
      builder.add();
      renderFinding(builder, finding, unverified);
    }
  }
}

function countsSentence(findings, rawCount, unverified) {
  const count = findings.length;
  let sentence = count === rawCount
    ? `${count} finding(s) after the gauntlet`
    : `${count} reported issue(s) from ${rawCount} finding(s) after the gauntlet`;
  const severityCounts = new Map(SEVERITY_ORDER.map((severity) => [severity, 0]));
  for (const finding of findings) {
    const severity = severityKey(finding);
    if (severityCounts.has(severity)) severityCounts.set(severity, severityCounts.get(severity) + 1);
  }
  const breakdown = SEVERITY_ORDER
    .filter((severity) => severityCounts.get(severity) > 0)
    .map((severity) => `${severityCounts.get(severity)} ${severity}`);
  sentence += count > 0 && breakdown.length ? ` — ${breakdown.join(', ')}.` : '.';
  const suggestions = findings.filter((finding) => (finding.report_tag ?? finding.report_destination) === 'suggestion').length;
  if (suggestions) sentence += ` ${suggestions} routed as improvement suggestion(s).`;
  if (unverified.length) sentence += ` ${unverified.length} unverified / pipeline-degraded.`;
  return sentence;
}

// Complete report.md with no trailing newline. Pure and total for absent/empty input:
// no clock, dispatch, prompt, schema, segmentation or fallback participates.
export function renderReport(input) {
  const inp = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
  const rawFindings = normalizeFindings(stripReportExcludedFields(normalizeFindings(inp.findings)));
  const rawUnverified = normalizeFindings(stripReportExcludedFields(normalizeFindings(inp.unverified)));
  const findings = consolidateForReport(rawFindings);
  const unverified = consolidateForReport(rawUnverified);
  const identity = inp.prIdentity && typeof inp.prIdentity === 'object' && !Array.isArray(inp.prIdentity)
    ? inp.prIdentity
    : null;

  let subject = 'local changes';
  if (identity && typeof identity.title === 'string' && identity.title.trim()) {
    subject = oneLine(identity.title);
  } else if (
    identity
    && isPresent(identity.owner)
    && isPresent(identity.repo)
    && isPresent(identity.pr_number)
  ) {
    subject = `\`${oneLine(identity.owner)}/${oneLine(identity.repo)}#${oneLine(identity.pr_number)}\``;
  }

  const identityParts = ['Reviewed'];
  if (isPresent(inp.headShaShort)) identityParts.push(`head \`${oneLine(inp.headShaShort)}\``);
  if (isPresent(inp.generatedAt)) identityParts.push(`at ${oneLine(inp.generatedAt)}`);
  identityParts.push(`by ${BRAND_NAME}.`);

  const builder = reportBuilder();
  builder.add(`# ${BRAND_MARK} ${BRAND_NAME}: ${subject}`);
  builder.add();
  builder.add(identityParts.join(' '));
  builder.add();
  builder.add('## Summary');
  builder.add();
  if (isPresent(inp.summary)) {
    builder.add(String(inp.summary));
    builder.add();
  }
  builder.add(countsSentence(findings, rawFindings.length, unverified));

  if (findings.length) {
    builder.add();
    builder.add('## Findings');
    renderSeverityBuckets(builder, findings, false);
  }

  if (unverified.length) {
    builder.add();
    builder.add('## Unverified / pipeline-degraded findings');
    builder.add();
    builder.add('These did not clear the full pipeline (a stage was skipped or failed) and carry lower confidence. They are not confirmed findings.');
    renderSeverityBuckets(builder, unverified, true);
  }

  const dimensionsTable = dimensionsSummaryTable({
    ...(inp.dimensions && typeof inp.dimensions === 'object' ? inp.dimensions : {}),
    findings: rawFindings,
    unverified: rawUnverified,
  });
  builder.add();
  builder.add('## Review Dimensions Summary');
  builder.add();
  builder.add(dimensionsTable);
  return builder.finish();
}
