// Owns local report rendering: multiline prose keeps Markdown, while evidence and receipts keep their fence grammars.
// Line fields use the outbound line contract, and repository spans preserve copyable normalized text in dynamic delimiters.
import { incompleteVetting, UNVERIFIED_REASONS } from './vetting.js';
import { rankFindings } from './applyChallenges.js';
import { PERMALINK_TEMPLATES, identityCheck, CODE_OWNED_HEADINGS, FINDING_PROP_TYPES } from './registry.js';
import { DIMENSIONS, AGENTS, AGENT_LABELS, BRAND_MARK, BRAND_NAME, SEVERITY_EMOJI } from './registry.js';
import { RULE_SOURCE_LABELS, RULE_SOURCE_LABEL_FALLBACK, conditionalSchemaActive, pinsModelIds, resolvePolicy } from './registry.js';
import { SEVERITY_ORDER, REPORT_FOLD_LIMITS } from './registry.js';
import { REPORT_PLACED_FIELDS, STAGE_DEFAULTS, FINDING_PATH_ARRAY_FIELDS } from './registry.js';
import { KNOB_REGISTRY } from './knobs.js';
import { longestBacktickRun, deepFreeze } from './wire.js';
import { reportAsText, foldProse, foldEvidence, oneLine, outboundBase, prepareLine, foldInline } from './outboundText.js';
import { prepareSummaryTitle, preparedCodeSpan, codeSpan, outboundTildeRuns } from './outboundText.js';

// Delivery owns committable fix fences; the report must not expose them as unvalidated prose extras.
const REPORT_EXCLUDED_FIELDS = deepFreeze([
  'suggested_fix_code',
  'suggested_fix_code_removed_by',
  'suggested_fix_code_removal_reason',
]);

export const REVIEW_SCOPE_FALLBACK_RULES = deepFreeze([
  { error: true },
  { facts: { previously_reviewed: false }, reason: 'no prior review recorded' },
  { facts: { previously_reviewed: true, sha_resolvable: false }, reason: 'recorded SHA not resolvable' },
  { facts: { previously_reviewed: true, sha_resolvable: true, sha_is_ancestor: true, head_advanced: false },
    reason: 'head has not advanced' },
  { facts: { previously_reviewed: true, sha_resolvable: true, sha_is_ancestor: false },
    reason: 'history rewritten (recorded SHA is not an ancestor)' },
]);

function dimensionOwnerMap(dimensions) {
  const owner = {};
  for (const d of dimensions) owner[d.dimension] = d.agentType;
  return owner;
}

// Replayed values may expose getters; a failed read is an absent field.
function safeReportField(value, key) {
  try {
    return value?.[key];
  } catch {
    return undefined;
  }
}

// Severity is an open wire string, so display unknown values as low rather than dropping the finding.
function severityBreakdown(rowFindings) {
  const counts = new Map();
  for (const f of rowFindings) {
    const raw = safeReportField(f, 'severity');
    if (raw === undefined || raw === null || raw === '') continue;
    const severity = normalizeReportSeverity(raw);
    counts.set(severity, (counts.get(severity) || 0) + 1);
  }
  if (counts.size === 0) return '';
  const known = SEVERITY_ORDER.filter((s) => counts.has(s));
  return known.map((s) => `${counts.get(s)} ${s}`).join(', ');
}

export function dimensionsSummaryTable(input, dimensions = DIMENSIONS) {
  const inp = input || {};
  const dispatchedSet = new Set((Array.isArray(inp.dispatched) ? inp.dispatched : []).map(reportAsText));
  const degradedSet = new Set((Array.isArray(inp.degraded) ? inp.degraded : []).map(reportAsText));
  const owner = dimensionOwnerMap(dimensions);
  const agents = [...new Set(dimensions.map((dimension) => dimension.agentType))];

  const byAgent = new Map(agents.map((agent) => [agent, []]));
  const unverifiedByAgent = new Map(agents.map((agent) => [agent, []]));
  const bucketFinding = (buckets, finding) => {
    const dimension = safeReportField(finding, 'dimension');
    const agentType = owner[dimension];
    if (agentType && buckets.has(agentType)) buckets.get(agentType).push(finding);
  };
  for (const finding of (inp.findings || [])) bucketFinding(byAgent, finding);
  for (const finding of (inp.unverified || [])) bucketFinding(unverifiedByAgent, finding);

  const rows = agents.map((agentType) => {
    const rowFindings = byAgent.get(agentType);
    const rowUnverified = unverifiedByAgent.get(agentType);
    const n = rowFindings.length;
    const m = rowUnverified.length;
    const dims = dimensions.filter((d) => d.agentType === agentType).map((d) => d.dimension);
    const isDegraded = dims.some((d) => degradedSet.has(d));
    const isDispatched = dispatchedSet.has(agentType);
    const label = AGENT_LABELS[agentType] || agentType;
    const agentShort = agentType.split(':').pop();
    const audit = agentShort === 'test-analyzer' ? inp.removedTestAudit : null;

    let findingsCell;
    let notes;
    if (audit && audit.status !== 'ran-clean') {
      findingsCell = `${n}${m ? ` (+${m} unverified)` : ''}`;
      notes = `Degraded - removed-test audit: ${inline(audit.status)}${audit.reason ? ` (${inline(audit.reason)})` : ''}${audit.omitted ? ` (${audit.omitted} omitted)` : ''}`;
    } else if (!isDispatched) {
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

    if (audit && audit.duplicates_dropped) notes += `; ${audit.duplicates_dropped} repeated an audit loss and were dropped`;
    return `| ${label} | ${agentShort} | ${findingsCell} | ${notes} |`;
  });

  return ['| Dimension | Agent | Findings | Notes |', '|-----------|-------|----------|-------|', ...rows].join('\n');
}

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
    // A delivered subset may omit the stamped primary; use its first remaining member instead of surfacing null.
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

// Derive extras from the schema so a newly declared field cannot silently lack a report placement.

export function reportExtraFields(dimensions = DIMENSIONS) {
  const declared = new Set(Object.keys(FINDING_PROP_TYPES));
  for (const dimension of dimensions) {
    for (const key of Object.keys(dimension.schemaExtra || {})) declared.add(key);
  }
  return [...declared]
    .filter((key) => !REPORT_PLACED_FIELDS.includes(key) && !REPORT_EXCLUDED_FIELDS.includes(key))
    .sort();
}

function reportOptionalText(value) {
  return value === undefined || value === null ? undefined : reportAsText(value);
}

function reportAsConfidence(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function reportRiskLevel(value) {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'number' || typeof value === 'string') return value;
  const text = reportAsText(value);
  return text || undefined;
}

function reportCorroboration(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out = {};
  for (const key of ['agent', 'dimension', 'title', 'description']) {
    const text = reportOptionalText(safeReportField(value, key));
    if (text !== undefined) out[key] = text;
  }
  const confidence = reportAsConfidence(safeReportField(value, 'confidence'));
  if (confidence !== undefined) out.confidence = confidence;
  return out;
}

function coerceReportFinding(finding, severities) {
  const out = {};
  let keys;
  try {
    keys = Object.keys(finding);
  } catch {
    return out;
  }
  for (const key of keys) {
    const raw = safeReportField(finding, key);
    let value;
    if (key === 'severity') {
      value = raw === undefined || raw === null || raw === '' ? undefined : normalizeReportSeverity(raw, severities);
    } else if (key === 'confidence') {
      value = reportAsConfidence(raw);
    } else if (key === 'corroborations') {
      value = Array.isArray(raw) ? raw.map(reportCorroboration) : undefined;
    } else if (key === 'consolidation_primary' || key === 'challenge_contested') {
      value = raw;
    } else if (key === 'risk_level') {
      value = reportRiskLevel(raw);
    } else if (FINDING_PATH_ARRAY_FIELDS.includes(key) && Array.isArray(raw)) {
      value = raw.map(reportAsText);
    } else {
      value = reportOptionalText(raw);
    }
    if (value !== undefined) out[key] = value;
  }
  return out;
}

// Project replayed field types before ranking so malformed values and throwing getters cannot reach raw ranking code.
export function coerceReportFindings(value, severities = SEVERITY_ORDER) {
  if (!Array.isArray(value)) return [];
  return value.map((finding) => (
    finding && typeof finding === 'object' && !Array.isArray(finding)
      ? coerceReportFinding(finding, severities)
      : {}
  ));
}

export function normalizeReportSeverity(raw, severities = SEVERITY_ORDER) {
  if (typeof raw !== 'string') return 'low';
  const normalized = raw.trim().toLowerCase();
  return severities.includes(normalized) ? normalized : 'low';
}

function fieldLabel(key) {
  const words = String(key).replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Size the evidence fence beyond every embedded backtick run so payload text cannot close it.
function fenceFor(text) {
  return '`'.repeat(Math.max(3, longestBacktickRun(reportAsText(text)) + 1));
}

function inline(value, prefixLimit) {
  const text = reportAsText(value).replace(/\r/g, ' ');
  if (prefixLimit === undefined) return prepareSummaryTitle(text);
  const sliced = oneLine(outboundBase(text)).slice(0, prefixLimit);
  return prepareLine(foldInline(sliced), true);
}

// Multiline model prose must not forge code-owned section headings.
// Evidence bypasses this rename because its fence contains the literal payload.
function safeProse(value) {
  const parts = reportAsText(value).split(/(\r\n|\r|\n)/);
  let output = '';
  for (let index = 0; index < parts.length; index += 2) {
    const line = parts[index];
    const boundary = parts[index + 1] || '';
    const comparable = line.replace(/[ \t]+$/, '');
    if (CODE_OWNED_HEADINGS.includes(comparable)) {
      // Preserve prose delimiters unless renaming a heading; keep a line boundary so adjacent model lines cannot join.
      const renamedBoundary = boundary === '\r\n' || boundary === '\r' ? '\n' : boundary;
      output += `${comparable} (finding text)${renamedBoundary}`;
    } else {
      output += line + boundary;
    }
  }
  return output;
}

function foldedProse(value, limit = REPORT_FOLD_LIMITS.proseChars) {
  return safeProse(foldProse(value, limit));
}

function findingFieldText(key, value) {
  return FINDING_PATH_ARRAY_FIELDS.includes(key) && Array.isArray(value)
    ? foldedProse(value.map((path) => inline(path)).join(',')) : foldedProse(value);
}

function tableCell(value) {
  return reportAsText(value).replaceAll('|', '\\|');
}

function isPresent(value) {
  if (value == null) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'string') return value.trim() !== '';
  return true;
}

const severityMark = (severity) => SEVERITY_EMOJI[normalizeReportSeverity(severity)];

// Preserve evidence and prepared spans; neutralize comment openers elsewhere so prose cannot hide later sections.
function reportBuilder() {
  let text = '';
  const protectedRanges = [];
  const add = (line = '') => {
    if (text.length) text += '\n';
    text += reportAsText(line);
  };
  const addSpans = (line, spans) => {
    const start = text.length + (text.length ? 1 : 0);
    add(line);
    let cursor = 0;
    for (const span of spans) {
      if (!span) continue;
      const index = line.indexOf(span, cursor);
      if (index < 0) continue;
      protectedRanges.push([start + index, start + index + span.length]);
      cursor = index + span.length;
    }
  };
  const addEvidence = (evidence) => {
    const folded = foldEvidence(evidence);
    const fence = fenceFor(folded);
    const start = text.length + (text.length ? 1 : 0);
    add(fence);
    add(folded);
    add(fence);
    protectedRanges.push([start, text.length]);
  };
  const finish = () => {
    let out = '';
    let cursor = 0;
    while (true) {
      const index = text.indexOf('<!--', cursor);
      if (index < 0) break;
      out += text.slice(cursor, index);
      const protectedLiteral = protectedRanges.some(([start, end]) => index >= start && index < end);
      out += protectedLiteral ? '<!--' : '&lt;!--';
      cursor = index + 4;
    }
    return out + text.slice(cursor);
  };
  return { add, addSpans, addEvidence, finish };
}

function joinLocation(finding, preparePart, file = preparePart(finding.file)) {
  if (!isPresent(finding.line_start)) return file;
  const start = preparePart(finding.line_start);
  if (!isPresent(finding.line_end) || String(finding.line_end) === String(finding.line_start)) {
    return `${file}:${start}`;
  }
  return `${file}:${start}-${preparePart(finding.line_end)}`;
}

function location(finding) {
  if (!isPresent(finding.file)) return '';
  const file = oneLine(outboundBase(oneLine(finding.file)));
  const foldedFile = foldInline(file);
  // Keep joint normalization when the file fits; the line suffix must stay outside the file's fold budget.
  if (foldedFile === file) return preparedCodeSpan(joinLocation(finding, oneLine));
  return codeSpan(joinLocation(finding, (part) => oneLine(outboundBase(oneLine(part))), foldedFile));
}

function quotedSummaryLocation(finding) {
  if (!isPresent(finding.file)) return '';
  const display = joinLocation(finding, (part) => oneLine(outboundBase(part)));
  return codeSpan(display, true);
}

function encodeUrlSegment(segment) {
  const repaired = reportAsText(segment)
    .replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/g, '\uFFFD')
    .replace(/(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g, '\uFFFD');
  return encodeURIComponent(repaired).replace(/[!'()*]/g, (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
}

function templateValue(template, values) {
  let output = template;
  for (const [name, value] of Object.entries(values)) output = output.replaceAll(`{${name}}`, value);
  return output;
}

function permalinkContext(identity) {
  if (!identity) return { repositoryBase: null, blobBase: null, template: null };
  const repositoryReady = ['platform', 'web_origin', 'owner', 'repo']
    .every((name) => identityCheck(identity, name));
  if (!repositoryReady) return { repositoryBase: null, blobBase: null, template: null };
  const template = PERMALINK_TEMPLATES[identity.platform];
  const owner = reportAsText(identity.owner).split('/').map(encodeUrlSegment).join('/');
  const repo = reportAsText(identity.repo).split('/').map(encodeUrlSegment).join('/');
  const values = { origin: identity.web_origin, owner, repo };
  const repositoryBase = `${identity.web_origin}/${owner}/${repo}`;
  const blobBase = identityCheck(identity, 'sha_full')
    ? templateValue(template.blob, { ...values, sha: identity.sha_full, path: '' })
    : null;
  return { repositoryBase, blobBase, template, values };
}

function positiveLine(value) {
  if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0 ? value : null;
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

function locationUrl(finding, permalinks, displayedLocation) {
  if (!permalinks.blobBase || !isPresent(finding.file)) return null;
  const rawFile = reportAsText(finding.file);
  // Refuse a link when normalization changes its path so the displayed location cannot disagree with the target.
  if (outboundBase(rawFile) !== rawFile) return null;
  if (oneLine(rawFile) !== rawFile) return null;
  if (displayedLocation !== undefined && displayedLocation !== preparedCodeSpan(joinLocation(finding, oneLine))) return null;
  const segments = rawFile.split('/');
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) return null;
  const path = segments.map(encodeUrlSegment).join('/');
  let anchor = '';
  const start = positiveLine(finding.line_start);
  const endPresent = isPresent(finding.line_end);
  const end = positiveLine(finding.line_end);
  if (start !== null && (!endPresent || end === start)) {
    anchor = templateValue(permalinks.template.line, { start: String(start) });
  } else if (start !== null && end !== null && end > start) {
    anchor = templateValue(permalinks.template.range, { start: String(start), end: String(end) });
  }
  return `${permalinks.blobBase}${path}${anchor}`;
}

function unverifiedReason(finding) {
  const reasons = incompleteVetting(finding).map((check) => UNVERIFIED_REASONS[check]);
  if (Object.hasOwn(UNVERIFIED_REASONS.challenge, finding.challenge)) {
    reasons.push(UNVERIFIED_REASONS.challenge[finding.challenge]);
  }
  return reasons.length ? reasons.join('; ') : 'a pipeline stage was skipped or failed';
}

function renderFinding(builder, finding, unverified, permalinks) {
  const blocks = [];
  blocks.push(() => builder.add(`#### ${inline(finding.title)}`));

  const bullets = [];
  const where = location(finding);
  const url = locationUrl(finding, permalinks, where);
  if (where) bullets.push(url ? `- **Location:** [${where}](${url})` : `- **Location:** ${where}`);
  const classification = [];
  if (isPresent(finding.dimension)) classification.push(`**Dimension:** ${inline(finding.dimension)}`);
  if (isPresent(finding.confidence)) classification.push(`**Confidence:** ${inline(finding.confidence)}%`);
  if (classification.length) bullets.push(`- ${classification.join(' · ')}`);
  if (finding.origin === 'surfaced') {
    bullets.push('- **Origin:** surfaced — pre-existing, surfaced by this change');
  }
  if (unverified) bullets.push(`- **Unverified because:** ${unverifiedReason(finding)}`);
  if ((finding.report_tag ?? finding.report_destination) === 'suggestion') {
    bullets.push('- **Routing:** improvement suggestion');
  }
  if (finding.demoted_by === 'reachability') {
    bullets.push('- **Reachability:** only under a future change (severity demoted to low)');
  }
  if (finding.challenge_contested === true) {
    bullets.push('- **Contested:** the challenger could not confirm the cited location');
  }
  if (bullets.length) blocks.push(() => bullets.forEach((line) => builder.addSpans(line, [where])));

  if (isPresent(finding.description)) blocks.push(() => builder.add(foldedProse(finding.description)));
  if (isPresent(finding.evidence)) {
    blocks.push(() => {
      builder.add('**Evidence:**');
      builder.add();
      builder.addEvidence(finding.evidence);
    });
  }

  const extras = reportExtraFields()
    .map((key) => ({ key, text: findingFieldText(key, finding[key]) }))
    .filter(({ text }) => isPresent(text))
    .map(({ key, text }) => `- **${fieldLabel(key)}:** ${text}`);
  if (extras.length) blocks.push(() => extras.forEach(builder.add));

  if (isPresent(finding.suggestion)) {
    blocks.push(() => {
      builder.add('**Suggested fix:**');
      builder.add();
      builder.add(foldedProse(finding.suggestion));
    });
  }

  const hasClaudeMdRule = isPresent(finding.claude_md_rule);
  const citedRule = hasClaudeMdRule ? finding.claude_md_rule : finding.spec_text;
  if (isPresent(citedRule)) {
    const source = finding.rule_source;
    const ruleLabel = hasClaudeMdRule && typeof source === 'string' && Object.hasOwn(RULE_SOURCE_LABELS, source)
      ? RULE_SOURCE_LABELS[source]
      : RULE_SOURCE_LABEL_FALLBACK;
    blocks.push(() => {
      builder.add(`**${ruleLabel}:**`);
      builder.add();
      builder.add(foldedProse(citedRule).split(/\r?\n/).map((line) => `> ${line}`).join('\n'));
    });
  }

  const crossFileRefs = findingFieldText('cross_file_refs', finding.cross_file_refs);
  if (isPresent(crossFileRefs)) {
    blocks.push(() => builder.add(`- **Cross-file refs:** ${crossFileRefs}`));
  }

  if (Array.isArray(finding.corroborations) && finding.corroborations.length) {
    blocks.push(() => {
      for (const corroboration of finding.corroborations) {
        const agent = preparedCodeSpan(corroboration.agent, false, undefined, REPORT_FOLD_LIMITS.inlineChars);
        const dimension = preparedCodeSpan(corroboration.dimension, false, undefined, REPORT_FOLD_LIMITS.inlineChars);
        const confidence = inline(corroboration.confidence);
        const title = inline(corroboration.title);
        builder.addSpans(`- **Corroborated by** ${agent} (${dimension}, confidence ${confidence}) — ${title}`, [agent, dimension]);
        if (isPresent(corroboration.description)) {
          builder.add(foldedProse(corroboration.description).split(/\r?\n/).map((line) => `  ${line}`).join('\n'));
        }
      }
    });
  }

  blocks.forEach((block, index) => {
    if (index) builder.add();
    block();
  });
}

function severityView(findings) {
  const ranked = rankFindings(findings);
  const buckets = new Map();
  for (const finding of ranked) {
    const key = normalizeReportSeverity(finding.severity);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(finding);
  }
  const known = SEVERITY_ORDER.filter((severity) => buckets.has(severity));
  return { buckets, order: known };
}

function renderSeverityBuckets(builder, view, unverified, permalinks) {
  for (const severity of view.order) {
    builder.add();
    builder.add(`### ${severityMark(severity)} ${fieldLabel(severity)}`);
    for (const finding of view.buckets.get(severity)) {
      builder.add();
      renderFinding(builder, finding, unverified, permalinks);
    }
  }
}

export function plural(count, noun) {
  return count === 1 ? noun : `${noun}s`;
}

function countsSentence(findings, rawCount, unverified, view) {
  const count = findings.length;
  let sentence = count === rawCount
    ? `${count} ${plural(count, 'finding')} after the gauntlet`
    : `${count} reported ${plural(count, 'issue')} from ${rawCount} ${plural(rawCount, 'finding')} after the gauntlet`;
  const breakdown = view.order.map((severity) => `${view.buckets.get(severity).length} ${severity}`);
  sentence += count > 0 && breakdown.length ? ` — ${breakdown.join(', ')}.` : '.';
  const suggestions = findings.filter((finding) => (finding.report_tag ?? finding.report_destination) === 'suggestion').length;
  if (suggestions) sentence += ` ${suggestions} routed as improvement ${plural(suggestions, 'suggestion')}.`;
  if (unverified.length) sentence += ` ${unverified.length} unverified / pipeline-degraded.`;
  return sentence;
}

function reportUnitKey(finding, original) {
  return finding.consolidation_key
    ? finding.consolidation_key
    : (original && typeof original === 'object' ? original : finding);
}

function plainReportObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  } catch {
    return false;
  }
}

function summaryIndex(rawFindings, findingsView, input) {
  const reported = findingsView.order.flatMap((severity) => findingsView.buckets.get(severity));
  if (!Array.isArray(input.delivered)) return { indexed: reported, reasons: [] };
  const originals = Array.isArray(input.findings) ? input.findings : [];
  const projected = rawFindings;
  const delivered = input.delivered.filter(plainReportObject);
  const deliveredOriginals = new Set(delivered);
  const deliveryOrder = new Map();
  delivered.forEach((finding, index) => {
    if (!deliveryOrder.has(finding)) deliveryOrder.set(finding, index);
  });
  const originalByProjected = new WeakMap();
  const pairs = [];
  const unitMembers = new Map();
  for (let index = 0; index < originals.length; index += 1) {
    const original = originals[index];
    const finding = projected[index] || {};
    if (finding && typeof finding === 'object') originalByProjected.set(finding, original);
    const key = reportUnitKey(finding, original);
    if (!unitMembers.has(key)) unitMembers.set(key, []);
    unitMembers.get(key).push(finding);
    if (plainReportObject(original) && deliveredOriginals.has(original)) {
      pairs.push({ original, finding });
    }
  }
  pairs.sort((left, right) => deliveryOrder.get(left.original) - deliveryOrder.get(right.original));
  const deliveredUnits = new Map(consolidateForReport(pairs.map((pair) => pair.finding)).map((finding) => (
    [reportUnitKey(finding, originalByProjected.get(finding)), finding]
  )));
  // Keep report group order but select the delivered subset's primary so the index agrees with posted issues.
  const indexed = reported.map((finding) => deliveredUnits.get(
    reportUnitKey(finding, originalByProjected.get(finding)),
  )).filter(Boolean);
  const omittedUnits = [...unitMembers].filter(([key]) => !deliveredUnits.has(key)).map(([, members]) => members);
  const reasons = [];
  const isMain = (finding) => (finding.report_tag ?? finding.report_destination) === 'main';
  if (typeof input.deliveryCap === 'number' && omittedUnits.some((members) => (
    input.deliveryTier !== 'main_only' || members.some(isMain)
  ))) {
    reasons.push(`over the delivery cap of ${input.deliveryCap} ${plural(input.deliveryCap, 'finding')}`);
  }
  if (input.deliveryTier === 'main_only' && omittedUnits.some((members) => (
    members.some((finding) => (finding.report_tag ?? finding.report_destination) === 'suggestion')
  ))) {
    reasons.push('improvement suggestions held back by delivery tier main_only');
  }
  if (!reasons.length && omittedUnits.length) reasons.push('not selected for delivery');
  return { indexed, reasons };
}

function summaryBlock(builder, input) {
  const inp = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
  const rawFindings = coerceReportFindings(inp.findings);
  const rawUnverified = coerceReportFindings(inp.unverified);
  const findings = consolidateForReport(rawFindings);
  const unverified = consolidateForReport(rawUnverified);
  const findingsView = severityView(findings);
  builder.add(countsSentence(findings, rawFindings.length, unverified, findingsView));
  const { indexed, reasons } = summaryIndex(rawFindings, findingsView, inp);
  const permalinks = permalinkContext(inp.prIdentity);
  const bullets = [];
  let indexChars = 0;
  let cutForLength = false;
  for (const finding of indexed) {
    const severity = normalizeReportSeverity(finding.severity);
    const where = quotedSummaryLocation(finding) || '`location unavailable`';
    const url = locationUrl(finding, permalinks);
    const link = url ? `[${where}](${url})` : where;
    const suggestion = (finding.report_tag ?? finding.report_destination) === 'suggestion' ? ' (improvement suggestion)' : '';
    const bullet = outboundTildeRuns(
      `- ${severityMark(severity)} [${severity.toUpperCase()}] ${link}${suggestion}: ${prepareSummaryTitle(finding.title)}`,
    );
    const nextChars = [...bullet].length + (bullets.length ? 1 : 0);
    if (indexChars + nextChars > REPORT_FOLD_LIMITS.summaryIndexChars) {
      cutForLength = true;
      break;
    }
    bullets.push(bullet);
    indexChars += nextChars;
  }
  if (bullets.length) builder.add();
  for (const bullet of bullets) builder.add(bullet);
  const remainder = findings.length - bullets.length;
  if (cutForLength) reasons.push('over the summary length limit');
  const remainderNoun = findings.length === rawFindings.length
    ? plural(remainder, 'finding')
    : `reported ${plural(remainder, 'issue')}`;
  if (remainder) {
    builder.add();
    builder.add(`${remainder} more ${remainderNoun} not listed here (${reasons.join('; ')}).`);
  }
}

export function renderSummaryBody(input) {
  const builder = reportBuilder();
  summaryBlock(builder, input);
  return builder.finish();
}

function receiptSafe(value) {
  if (value === undefined || value === null) return 'unknown';
  const cleaned = oneLine(value).replaceAll('`', '');
  return cleaned || 'unknown';
}

function receiptLines(input, knobs) {
  const mode = input.mode === 'headless' ? 'headless' : 'interactive';
  const echo = input.configEcho && typeof input.configEcho === 'object' && !Array.isArray(input.configEcho)
    ? input.configEcho
    : {};
  const lines = [mode === 'headless' ? 'Headless config:' : 'Resolved config:'];
  for (const descriptor of knobs) {
    if (!descriptor.modes.includes(mode)) continue;
    const entry = echo[descriptor.key];
    const validEntry = entry && typeof entry === 'object' && !Array.isArray(entry)
      && typeof entry.value === 'string' && typeof entry.source === 'string';
    const value = validEntry ? entry.value : 'unknown';
    const source = validEntry ? entry.source : 'unknown';
    lines.push(`  ${descriptor.key}=${receiptSafe(value)} (${receiptSafe(source)})`);
  }
  lines.push(`  pipeline_version=${receiptSafe(input.pipelineVersion)} (bundle)`);
  lines.push(`  plugin_root=${receiptSafe(input.pluginRoot)} (resolved)`);
  return lines;
}

function countSummary(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'none';
  const entries = Object.entries(value).filter(([key, item]) => key !== 'replay_path_rejected' || item > 0).map(([key, item]) => {
    const count = Array.isArray(item) ? item.length : item;
    return `${inline(key)}=${inline(count)}`;
  });
  return entries.length ? entries.join(', ') : 'none';
}

// Detector facts own fallback explanations; precedence distinguishes unresolved SHAs from rewritten history
// even when both report that the head has not advanced.
export function reviewScopeFallbackReason(scope, rules = REVIEW_SCOPE_FALLBACK_RULES) {
  if (!scope || typeof scope !== 'object' || Array.isArray(scope)
    || scope.kind !== 'full' || scope.requested !== 'incremental'
    || !scope.detector || typeof scope.detector !== 'object' || Array.isArray(scope.detector)) {
    return null;
  }
  for (const rule of rules) {
    const detector = scope.detector;
    if (rule.error) {
      if (typeof detector.error === 'string' && detector.error !== '') {
        return `detection failed: ${inline(detector.error, 120)}`;
      }
    } else if (Object.entries(rule.facts).every(([key, value]) => detector[key] === value)) {
      return rule.reason;
    }
  }
  return 'detection failed: unavailable';
}

function methodologyRows(input, rawFindings) {
  const policy = input.policy && typeof input.policy === 'object' && !Array.isArray(input.policy)
    ? input.policy
    : {};
  const provider = policy.provider || 'firstParty';
  const gateway = policy.gateway === true;
  const conditional = conditionalSchemaActive({ ...policy, provider });
  const rows = [];
  const version = inline(input.pipelineVersion) || 'unknown';
  rows.push(['Version', `${version} (plugin and pipeline)`]);
  rows.push(['Provider', `provider=${inline(provider)}; gateway=${gateway}; conditional schema active=${conditional}`]);

  if (policy.subagentModel) {
    rows.push(['Subagent model override', `EVERY stage uses ${inline(policy.subagentModel)}`]);
  }
  if (pinsModelIds(provider) && policy.mappedAliases?.length) {
    rows.push(['User tier mapping', policy.mappedAliases.map((alias) => inline(alias)).join(', ')]);
  }
  const stageTypes = [
    'change-summarizer', ...AGENTS.map((agentType) => agentType.split(':').pop()),
    ...Object.keys(STAGE_DEFAULTS),
  ];
  const models = stageTypes.map((agentType) => {
    const model = resolvePolicy(`code-gauntlet:${agentType}`, {
      subagentModelEnv: policy.subagentModel,
      provider,
      mappedAliases: policy.mappedAliases,
    }).model;
    return `${agentType}=${inline(model)}`;
  }).join(', ');
  rows.push(['Per-stage models', models]);

  const tier = input.deliveryTier ?? 'all';
  const cap = typeof input.deliveryCap === 'number' ? String(input.deliveryCap) : 'uncapped';
  rows.push(['Delivery selection', `selection policy: tier=${inline(tier)}, cap=${inline(cap)}`]);

  const scope = input.reviewScope && typeof input.reviewScope === 'object' ? input.reviewScope : { kind: 'full' };
  if (scope.kind === 'incremental') {
    const commits = scope.commits === null || scope.commits === undefined ? 'commits unknown' : `${inline(scope.commits)} commits`;
    rows.push(['Review scope', `Incremental since ${inline(scope.since || 'unknown')} (${commits})`]);
  } else {
    const reason = reviewScopeFallbackReason(scope);
    rows.push(['Review scope', reason ? `Full (incremental requested; ${reason})` : 'Full']);
  }

  const stats = input.stats && typeof input.stats === 'object' ? input.stats : {};
  const merge = stats.merge && typeof stats.merge === 'object' ? stats.merge : {};
  const channels = merge.findings_per_channel && typeof merge.findings_per_channel === 'object'
    ? `ndjson=${inline(merge.findings_per_channel.ndjson ?? 0)}, text_fallback=${inline(merge.findings_per_channel.text_fallback ?? 0)}`
    : 'ndjson=0, text_fallback=0';
  const mergeCounts = `per-channel: ${channels}; duplicates resolved=${inline(merge.duplicates_resolved ?? 0)}; `
    + `dropped-no-id=${inline(merge.dropped_no_id ?? 0)}; truncation warnings=${inline(merge.truncation_warnings ?? 0)}; `
    + `validation warnings=${inline(merge.validation_warnings ?? 0)}`;
  const pathCounts = Object.entries(stats.pathNormalization || {})
    .map(([phase, counts]) => `; ${inline(phase)} paths: ${countSummary(counts)}`).join('');
  rows.push([
    'Findings pipeline',
    `discovered=${inline(stats.discovered ?? rawFindings.length)}; validate: ${countSummary(stats.validate)}; `
      + `filter: ${countSummary(stats.filter)}; challenge: ${countSummary(stats.challenge)}; merge: ${mergeCounts}${pathCounts}`,
  ]);

  const gapCount = Number.isInteger(input.gapCount) ? input.gapCount : 0;
  rows.push(['Gaps', String(gapCount)]);
  const dispatched = Array.isArray(input.dimensions && input.dimensions.dispatched) ? input.dimensions.dispatched.length : 0;
  const degraded = Array.isArray(input.dimensions && input.dimensions.degraded) ? input.dimensions.degraded.length : 0;
  rows.push(['Dimensions', `dispatched=${dispatched}; degraded=${degraded}`]);
  return rows;
}

function renderMethodology(builder, input, rawFindings, knobs) {
  builder.add();
  builder.add('## Review Methodology');
  builder.add();
  const receipt = receiptLines(input, knobs);
  const receiptText = receipt.join('\n');
  const fence = fenceFor(receiptText);
  builder.add(`${fence}text`);
  builder.add(receiptText);
  builder.add(fence);
  builder.add();
  builder.add('| Aspect | Details |');
  builder.add('|--------|---------|');
  for (const [aspect, details] of methodologyRows(input, rawFindings)) {
    builder.add(`| ${tableCell(aspect)} | ${tableCell(details)} |`);
  }
  builder.add();
  builder.add('This section covers the pipeline up to the report stage; persistence, delivery, and duration are reported '
    + 'by the orchestrator at delivery.');
}

export function renderReport(input, knobs = KNOB_REGISTRY) {
  const inp = input && typeof input === 'object' && !Array.isArray(input) ? input : {};
  const rawFindings = coerceReportFindings(inp.findings);
  const rawUnverified = coerceReportFindings(inp.unverified);
  const findings = consolidateForReport(rawFindings);
  const unverified = consolidateForReport(rawUnverified);
  const findingsView = severityView(findings);
  const unverifiedView = severityView(unverified);
  const identity = inp.prIdentity && typeof inp.prIdentity === 'object' && !Array.isArray(inp.prIdentity)
    ? inp.prIdentity
    : null;
  const permalinks = permalinkContext(identity);

  const reference = identity && isPresent(identity.owner) && isPresent(identity.repo) && isPresent(identity.pr_number)
    ? `${oneLine(identity.owner)}/${oneLine(identity.repo)}${identityCheck(identity, 'platform')
      ? templateValue(PERMALINK_TEMPLATES[identity.platform].ref, { number: oneLine(identity.pr_number) })
      : `#${oneLine(identity.pr_number)}`}`
    : null;
  const referenceSpan = reference ? preparedCodeSpan(reference) : '';

  let subject = 'local changes';
  const subjectSpans = [];
  if (identity && typeof identity.title === 'string' && identity.title.trim()) {
    subject = inline(identity.title);
  } else if (reference) {
    subject = referenceSpan;
    subjectSpans.push(referenceSpan);
  }

  const identityParts = ['Reviewed'];
  const identitySpans = [];
  if (isPresent(inp.headShaShort)) {
    const head = preparedCodeSpan(inp.headShaShort);
    identityParts.push(`head ${head}`);
    identitySpans.push(head);
  }
  if (isPresent(inp.generatedAt)) identityParts.push(`at ${inline(inp.generatedAt)}`);
  if (permalinks.repositoryBase && identityCheck(identity, 'pr_number')) {
    const refUrl = templateValue(permalinks.template.refUrl, {
      ...permalinks.values,
      number: String(identity.pr_number),
    });
    identityParts.push(oneLine(outboundBase(reference)) === reference
      ? `for [${referenceSpan}](${refUrl})` : `for ${referenceSpan}`);
    identitySpans.push(referenceSpan);
  }
  identityParts.push(`by ${BRAND_NAME}.`);

  const builder = reportBuilder();
  builder.addSpans(`# ${BRAND_MARK} ${BRAND_NAME}: ${subject}`, subjectSpans);
  builder.add();
  builder.addSpans(identityParts.join(' '), identitySpans);
  builder.add();
  builder.add('## Summary');
  builder.add();
  summaryBlock(builder, inp);

  builder.add();
  builder.add('## Change Context');
  builder.add();
  if (isPresent(inp.summary)) {
    builder.add('This is the change summary the review agents shared as context.');
    builder.add();
    builder.add(foldedProse(inp.summary, REPORT_FOLD_LIMITS.summaryChars));
  } else {
    builder.add('No change summary was produced for this run.');
  }

  if (findings.length) {
    builder.add();
    builder.add('## Findings');
    renderSeverityBuckets(builder, findingsView, false, permalinks);
  }

  if (unverified.length) {
    builder.add();
    builder.add('## Unverified / pipeline-degraded findings');
    builder.add();
    builder.add('These did not clear the full pipeline (a stage was skipped or failed) and carry lower confidence. '
      + 'They are not confirmed findings.');
    renderSeverityBuckets(builder, unverifiedView, true, permalinks);
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
  renderMethodology(builder, { ...inp, mode: inp.mode || 'interactive' }, rawFindings, knobs);
  return builder.finish();
}
