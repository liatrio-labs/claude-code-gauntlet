import { REPORT_FOLD_LIMITS } from './registry.js';
import { longestBacktickRun, deepFreeze } from './wire.js';

const OUTBOUND_NAMED_ENTITIES = deepFreeze({
  commat: '@',
  excl: '!',
  num: '#',
  percnt: '%',
  lbrack: '[',
  lsqb: '[',
  rsqb: ']',
  rbrack: ']',
  Hat: '^',
  colon: ':',
  lpar: '(',
  rpar: ')',
  bsol: '\\',
  nbsp: ' ',
  NonBreakingSpace: ' ',
});

// Rendering must accept replayed field types and hostile coercion without throwing; filtering keeps its stricter text policy.

export function reportAsText(value) {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  try {
    return String(value);
  } catch {
    return '';
  }
}

function codePointLength(value) {
  return Array.from(reportAsText(value)).length;
}

function codePointPrefix(value, limit) {
  return Array.from(reportAsText(value)).slice(0, limit).join('');
}

export function foldInline(text, limit, limits = REPORT_FOLD_LIMITS) {
  if (limit === undefined) limit = limits.inlineChars;
  const value = reportAsText(text);
  const length = codePointLength(value);
  if (length <= limit) return value;
  return `${codePointPrefix(value, limit)} [folded: ${length - limit} more characters]`;
}

export function openProseFence(text) {
  const value = reportAsText(text);
  let state = null;
  let lineStart = 0;

  function lineRun(line) {
    let indent = 0;
    while (indent < 3 && indent < line.length && line[indent] === ' ') indent += 1;
    if (indent >= line.length || !['`', '~'].includes(line[indent])) return null;
    const char = line[indent];
    let end = indent;
    while (end < line.length && line[end] === char) end += 1;
    return { char, length: end - indent, end, indent };
  }

  function visit(line, offset) {
    const run = lineRun(line);
    if (state !== null) {
      if (run !== null && run.char === state[0] && run.length >= state[1]
        && /^[ \t]*$/.test(line.slice(run.end))) state = null;
      return;
    }
    if (run !== null && run.length >= 3
      && !(run.char === '`' && line.slice(run.end).includes('`'))) {
      // Keep UTF-16 offsets because callers use them with String.slice, even when fold budgets count code points.
      state = [run.char, run.length, offset + run.indent];
    }
  }

  let index = 0;
  while (index < value.length) {
    if (value[index] === '\n' || value[index] === '\r') {
      visit(value.slice(lineStart, index), lineStart);
      if (value[index] === '\r' && value[index + 1] === '\n') index += 2;
      else index += 1;
      lineStart = index;
    } else index += 1;
  }
  visit(value.slice(lineStart), lineStart);
  return state;
}

export function proseFenceCloser(prefix) {
  const state = openProseFence(prefix);
  return state === null ? '' : state[0].repeat(state[1]);
}

export function foldProse(text, limit) {
  const value = reportAsText(text);
  const length = codePointLength(value);
  if (length <= limit) return value;

  let prefix = '';
  let prefixLength = 0;
  const lines = value.split('\n');
  for (let index = 0; index < lines.length; index += 1) {
    const next = `${index ? '\n' : ''}${lines[index]}`;
    const remaining = limit - prefixLength;
    const nextLength = codePointLength(next);
    if (nextLength <= remaining) {
      prefix += next;
      prefixLength += nextLength;
      continue;
    }
    if (codePointLength(lines[index]) > limit) {
      const partial = codePointPrefix(next, remaining);
      prefix += partial;
      prefixLength += codePointLength(partial);
    }
    break;
  }
  const omitted = length - prefixLength;
  const closer = proseFenceCloser(prefix);
  if (closer) prefix += `${prefix.endsWith('\n') || prefix.endsWith('\r') ? '' : '\n'}${closer}`;
  return `${prefix}\n\n_[folded: ${omitted} more characters]_`;
}

export function foldEvidence(text, limits = REPORT_FOLD_LIMITS) {
  const value = reportAsText(text);
  const length = codePointLength(value);
  const separators = [...value.matchAll(/\r\n|\r|\n/g)];
  const lineCount = separators.length + 1;
  const lineFolded = lineCount > limits.evidenceLines;
  const linePrefix = lineFolded
    ? value.slice(0, separators[limits.evidenceLines - 1].index)
    : value;
  const linePrefixLength = codePointLength(linePrefix);
  const charFolded = length > limits.evidenceChars;

  if (charFolded && (!lineFolded || limits.evidenceChars <= linePrefixLength)) {
    const prefix = codePointPrefix(value, limits.evidenceChars);
    return `${prefix}\n... [folded: ${length - limits.evidenceChars} more characters]`;
  }
  if (lineFolded) {
    return `${linePrefix}\n... [folded: ${lineCount - limits.evidenceLines} more lines]`;
  }
  return value;
}

export function oneLine(value) {
  return reportAsText(value).replace(/[\r\n]+/g, ' ').replace(/ +/g, ' ').trim();
}

const OUTBOUND_INVISIBLES =
  /[\u0000-\u0008\u000b-\u000d\u000e-\u001f\u007f-\u009f\u00ad\u200b-\u200d\ufeff\u2060\u202a-\u202e\u2066-\u2069]/;

function entityPrefixSet(entities) {
  return new Set(Object.keys(entities).flatMap((name) => (
    Array.from({ length: name.length }, (_, index) => name.slice(0, index + 1))
  )));
}

const OUTBOUND_NAMED_ENTITY_PREFIXES = deepFreeze(entityPrefixSet(OUTBOUND_NAMED_ENTITIES));

const entityAt = (start, kind, fields = {}) => ({ start, kind, value: 0, hasDigit: false, name: '', ...fields });

export function normalizeOutboundText(value, entities = OUTBOUND_NAMED_ENTITIES) {
  const namedPrefixes = entities === OUTBOUND_NAMED_ENTITIES ? OUTBOUND_NAMED_ENTITY_PREFIXES : entityPrefixSet(entities);
  let text = reportAsText(value);
  const output = [];
  const push = (character) => {
    if (character === '\u00A0') character = ' ';
    if (OUTBOUND_INVISIBLES.test(character)) return undefined;
    const previous = output.length ? output[output.length - 1] : null;
    let entity = previous ? previous.entity : null;
    let replacement;
    if (character === '&') {
      entity = entityAt(output.length, 'start');
    } else if (entity) {
      if (entity.kind === 'start') {
        if (character === '#') {
          entity = entityAt(entity.start, 'numeric');
        } else if (/[A-Za-z]/.test(character)) {
          const name = entity.name + character;
          entity = namedPrefixes.has(name)
            ? entityAt(entity.start, 'named', { name })
            : null;
        } else {
          entity = null;
        }
      } else if (entity.kind === 'numeric') {
        if (character === 'x' || character === 'X') {
          entity = entityAt(entity.start, 'hex');
        } else if (character >= '0' && character <= '9') {
          entity = entityAt(entity.start, 'decimal', {
            value: Math.min(127, character.charCodeAt(0) - 48),
            hasDigit: true,
          });
        } else {
          entity = null;
        }
      } else if (entity.kind === 'decimal' || entity.kind === 'hex') {
        const radix = entity.kind === 'decimal' ? 10 : 16;
        let digit = -1;
        if (character >= '0' && character <= '9') digit = character.charCodeAt(0) - 48;
        else if (radix === 16 && character.toLowerCase() >= 'a' && character.toLowerCase() <= 'f') {
          digit = character.toLowerCase().charCodeAt(0) - 87;
        }
        if (character === ';' && entity.hasDigit) {
          replacement = entity.value >= 32 && entity.value <= 126
            ? String.fromCharCode(entity.value)
            : '';
        } else if (digit >= 0 && digit < radix) {
          entity = entityAt(entity.start, entity.kind, {
            value: Math.min(127, entity.value * radix + digit),
            hasDigit: true,
          });
        } else {
          entity = null;
        }
      } else if (entity.kind === 'named') {
        if (character === ';') {
          if (Object.hasOwn(entities, entity.name)) {
            replacement = entities[entity.name];
          } else {
            entity = null;
          }
        } else if (/[A-Za-z]/.test(character)) {
          const name = entity.name + character;
          entity = namedPrefixes.has(name)
            ? entityAt(entity.start, 'named', { name })
            : null;
        } else {
          entity = null;
        }
      }
    }

    if (replacement !== undefined) {
      output.length = entity.start;
      return replacement || undefined;
    }

    const commentStart = previous ? previous.commentStart : null;
    const commentEnd = previous ? previous.commentEnd : null;
    output.push({ character, entity, commentStart, commentEnd });
    const length = output.length;
    if (commentStart === null && length >= 4
        && output[length - 4].character === '<'
        && output[length - 3].character === '!'
        && output[length - 2].character === '-'
        && output[length - 1].character === '-') {
      output[length - 1] = { character, entity, commentStart: length - 4, commentEnd: length };
      return undefined;
    }
    if (commentStart !== null && length >= 3
        && output[length - 3].character === '-'
        && output[length - 2].character === '-'
        && output[length - 1].character === '>'
        && length - 3 >= commentEnd) {
      output.length = commentStart;
    }
    return undefined;
  };

  for (let index = 0; index < text.length; index += 1) {
    let character = text[index];
    while (character !== undefined) character = push(character);
  }
  return output.map((entry) => entry.character).join('');
}

export function outboundBase(value, entities = OUTBOUND_NAMED_ENTITIES) {
  const text = normalizeOutboundText(value, entities);
  if (text.trim() === '') return '';
  return text.replace(/(?:ghp_|gho_|ghs_|ghr_|ghu_|github_pat_)[A-Za-z0-9_]{20,}/g, '[REDACTED]')
    .replace(/(?:glpat-|glrt-)[A-Za-z0-9_-]{20,}/g, '[REDACTED]')
    .replace(/[\r\n]+/g, ' ');
}

// A destination stops at "(" so each "](" scans only to the next one.
const OUTBOUND_PROSE_LINK = /\]\(([^\s()]+)(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'))?\)|(?:https?:\/\/|ftp:\/\/|www\.)[A-Za-z0-9](?:(?!\]\()[^\s<>])*|(?<![A-Za-z0-9.-])github\.com\/(?:(?!\]\()[^\s<>])+/g;
const OUTBOUND_GITHUB_REFERENCE_PATH = /^\/[^/]+\/[^/]+\/(?:issues|pull|discussions)\/[0-9]/;
const OUTBOUND_GITLAB_REFERENCE_PATH = /\/-\/(?:issues|merge_requests|work_items|epics|milestones|snippets)\/[0-9]/;
const OUTBOUND_REFERENCE = /(?<!\\)(\\*)(#(?=[0-9])|!(?=[0-9])|(?<![A-Za-z0-9_])[Gg][Hh]-(?=[0-9])|(?<!~)(?<!~\\)~(?=[A-Za-z0-9_"])|%(?=[A-Za-z0-9_"])|&(?=[0-9]))/g;

function outboundProseLinks(text, lineStart = true, labelEnds = new Set(), offset = 0) {
  const links = [];
  const scanner = new RegExp(OUTBOUND_PROSE_LINK);
  let match;
  while ((match = scanner.exec(text)) !== null) {
    const destination = match[1] !== undefined;
    let value = destination ? match[1] : match[0];
    if (!destination) {
      let balance = 0;
      for (const character of value) {
        if (character === '(') balance += 1;
        if (character === ')') balance -= 1;
      }
      let end = value.length;
      while (end && (
        '.,:;!?\'"*_~'.includes(value[end - 1]) || (value[end - 1] === ')' && balance < 0)
      )) {
        if (value[end - 1] === ')') balance += 1;
        end -= 1;
      }
      value = value.slice(0, end);
    }
    const url = !destination || /^(?:https?:\/\/|ftp:\/\/|www\.)[A-Za-z0-9]|^github\.com\//.test(value);
    let path = value.split('?', 1)[0].split('#', 1)[0];
    const hostStart = path.includes('://') ? path.indexOf('://') + 3 : 0;
    const authorityEnd = path.indexOf('/', hostStart);
    const host = authorityEnd >= 0 ? path.slice(hostStart, authorityEnd).toLowerCase() : '';
    path = authorityEnd >= 0 ? path.slice(authorityEnd) : '';
    // Other trackers such as Sentry share GitHub's owner/repo/issues shape.
    const reference = url && (
      OUTBOUND_GITLAB_REFERENCE_PATH.test(path)
      || (['github.com', 'www.github.com'].includes(host) && OUTBOUND_GITHUB_REFERENCE_PATH.test(path))
    );
    // Even a rejected label must not leave a link to a backtick-wrapped URL.
    if (destination && !labelEnds.has(offset + match.index) && !reference) {
      scanner.lastIndex = match.index + 2;
      continue;
    }
    const start = match.index;
    if (!destination) {
      let slashStart = start;
      while (slashStart && text[slashStart - 1] === '\\') slashStart -= 1;
      const boundary = reference ? slashStart : start;
      const atBoundary = (boundary === 0 && (lineStart || reference)) || (boundary > 0 && (
        // Wrapping a reference URL is safe anywhere outside a code span.
        (reference && text[boundary - 1] !== '`') || /\s/.test(text[boundary - 1]) || '*_~(<'.includes(text[boundary - 1]) ||
        (text[boundary - 1] === '[' && labelEnds.has(offset + start + value.length)) ||
        text.slice(Math.max(0, boundary - 4), boundary) === '&lt;'
      ));
      if (!atBoundary) continue;
    }
    links.push({
      start: match.index,
      end: match.index + (destination ? 2 : 0) + value.length,
      destination,
      reference,
    });
  }
  return links;
}

function outboundReferences(text) {
  return text.replace(OUTBOUND_REFERENCE, (_match, slashes, sigil) => {
    const replacement = sigil.length === 3
      ? `${sigil.slice(0, -1)}\u2011`
      : { '#': '\uFF03', '!': '\uFF01', '~': '\uFF5E', '%': '\uFF05', '&': '\uFF06' }[sigil];
    return slashes.slice(0, slashes.length - slashes.length % 2) + replacement;
  });
}

function outboundVisible(text, code = false, lineStart = true, labelEnds = new Set(), offset = 0) {
  if (!code) {
    const parts = [];
    let cursor = 0;
    for (const link of outboundProseLinks(text, lineStart, labelEnds, offset)) {
      let before = text.slice(cursor, link.start);
      if (link.reference && !link.destination) {
        const slashes = before.length - before.replace(/\\+$/, '').length;
        before = before.slice(0, before.length - slashes % 2);
      }
      // Keep the URL's first character as lookahead for a preceding '<'.
      parts.push(outboundCharacters(outboundReferences(before) + text[link.start]).slice(0, -1));
      let value = text.slice(link.start, link.end);
      if (link.reference) {
        let prefix = link.destination ? ']\uFF08' : '';
        // A space keeps the new span from merging with the one just closed.
        if (!(link.destination || lineStart || cursor || before)) prefix = ' ';
        value = prefix + codeSpan(outboundCharacters(link.destination ? value.slice(2) : value, true));
      } else {
        value = outboundCharacters(value);
      }
      parts.push(value);
      cursor = link.end;
    }
    parts.push(outboundCharacters(outboundReferences(text.slice(cursor))));
    return parts.join('');
  }
  return outboundCharacters(text, code);
}

function outboundCharacters(text, code = false) {
  // Fullwidth brackets survive reparsing and keep footnote text visible.
  text = text.replace(/(?<=[!\[])\\*\[|\[(?=\\*\^)/g, (match) => `${match.slice(0, -1)}\uFF3B`);
  text = text.replace(/<(?=[A-Za-z/!?])/g, code ? '\uFF1C' : '&lt;');
  return text.replace(/(?<![A-Za-z0-9])@/g, '\uFF20');
}

function outboundTickRunIndex(line) {
  const runs = [];
  const startsByWidth = new Map();
  let slashParity = 0;
  let index = 0;
  while (index < line.length) {
    if (line[index] === '\\') {
      slashParity ^= 1;
      index += 1;
      continue;
    }
    if (line[index] === '`') {
      const start = index;
      while (index < line.length && line[index] === '`') index += 1;
      const width = index - start;
      runs.push({ start, end: index, width, escaped: Boolean(slashParity) });
      if (!startsByWidth.has(width)) startsByWidth.set(width, []);
      startsByWidth.get(width).push(start);
    } else {
      index += 1;
    }
    slashParity = 0;
  }
  return { runs, startsByWidth };
}

function outboundSpanClose(index, after, width) {
  const starts = index.startsByWidth.get(width);
  if (!starts) return -1;
  let low = 0;
  let high = starts.length;
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2);
    if (starts[middle] < after) low = middle + 1;
    else high = middle;
  }
  return low < starts.length ? starts[low] : -1;
}

function outboundCodeSpans(line, index = outboundTickRunIndex(line), skipOpeners = new Set()) {
  const spans = [];
  let runIndex = 0;
  while (runIndex < index.runs.length) {
    const run = index.runs[runIndex];
    if (skipOpeners.has(runIndex)) {
      runIndex += 1;
      continue;
    }
    let opener = run.start + Number(run.escaped);
    let width = run.width - Number(run.escaped);
    if (width <= 0) {
      runIndex += 1;
      continue;
    }
    let close = -1;
    while (width > 0) {
      close = outboundSpanClose(index, run.end, width);
      if (close >= 0) break;
      width -= 1;
      opener += 1;
    }
    if (close < 0) {
      runIndex += 1;
      continue;
    }
    const closeEnd = close + width;
    spans.push([opener, close, closeEnd]);
    runIndex += 1;
    while (runIndex < index.runs.length && index.runs[runIndex].start < closeEnd) runIndex += 1;
  }
  return spans;
}

function outboundDefinitions(line, forceColon = false) {
  // Forced guarding covers interpolated labels that can become definitions when embedded in surrounding Markdown.
  const guardColon = forceColon || /\]\\?\(/.test(line);
  if (guardColon) {
    line = line.replace(/\](\\*):/g, (match) => `${match.slice(0, -1)}\uFF1A`);
  }
  return line.replace(/^[^A-Za-z\\\[\n]*\[((?:\\[^\n]|[^\\\[\]\n])+)\]:/u, (match, label) => (
    /\S/.test(label) ? `${match.slice(0, -1)}\\:` : match
  ));
}

function outboundLinkLabelEnds(line, spans) {
  const labelEnds = new Set();
  let depth = 0;
  let escaped = false;
  let spanIndex = 0;
  // Substitutions preserve raw offsets and exclude brackets that will not survive.
  // Labels can cross code spans, whose brackets cannot change the label depth.
  const labelText = outboundCharacters(line, true);
  for (let position = 0; position < labelText.length; position += 1) {
    const character = labelText[position];
    while (spanIndex < spans.length && spans[spanIndex][2] <= position) spanIndex += 1;
    const inCode = spanIndex < spans.length && spans[spanIndex][0] <= position;
    if ('\r\n'.includes(character)) {
      depth = 0;
    } else if (!inCode && !escaped) {
      if (character === '[') depth += 1;
      else if (character === ']' && depth) {
        depth -= 1;
        if (line.startsWith('](', position)) {
          labelEnds.add(position);
          // A formed link deactivates every earlier opener, as in CommonMark.
          depth = 0;
        }
      }
    }
    escaped = !inCode && character === '\\' && !escaped;
  }
  return labelEnds;
}

function outboundContain(line, forceColon = false) {
  line = outboundDefinitions(line, forceColon);
  line = line.replace(/<(?=`+[A-Za-z/!?])/g, '\uFF1C');
  const index = outboundTickRunIndex(line);
  // URL backticks belong to the generated span, not to Markdown's input delimiters.
  const opaque = outboundProseLinks(line).filter((link) => link.reference);
  const skipOpeners = new Set();
  let opaqueIndex = 0;
  for (let runIndex = 0; runIndex < index.runs.length; runIndex += 1) {
    const run = index.runs[runIndex];
    while (opaqueIndex < opaque.length && opaque[opaqueIndex].end <= run.start) opaqueIndex += 1;
    if (opaqueIndex < opaque.length && opaque[opaqueIndex].start <= run.start) skipOpeners.add(runIndex);
  }
  // Opaque runs remain in the index because they can still close earlier spans.
  const spans = outboundCodeSpans(line, index, skipOpeners);
  const labelEnds = outboundLinkLabelEnds(line, spans);
  let spanIndex = 0;
  const output = [];
  let cursor = 0;
  let runIndex = 0;
  while (runIndex < index.runs.length) {
    const run = index.runs[runIndex];
    if (skipOpeners.has(runIndex)) {
      runIndex += 1;
      continue;
    }
    output.push(outboundVisible(line.slice(cursor, run.start), false, cursor === 0, labelEnds, cursor));
    let opener = run.start;
    if (run.escaped) {
      output.push('`');
      opener += 1;
    }
    if (spanIndex < spans.length && spans[spanIndex][0] < run.end) {
      const [selected, close, closeEnd] = spans[spanIndex];
      spanIndex += 1;
      output.push('\\`'.repeat(selected - opener));
      const openerEnd = selected + closeEnd - close;
      output.push(line.slice(selected, openerEnd));
      output.push(outboundVisible(line.slice(openerEnd, close), true));
      output.push(line.slice(close, closeEnd));
      cursor = closeEnd;
      runIndex += 1;
      while (runIndex < index.runs.length && index.runs[runIndex].start < cursor) runIndex += 1;
    } else {
      output.push('\\`'.repeat(run.width - Number(run.escaped)));
      cursor = run.end;
      runIndex += 1;
    }
  }
  output.push(outboundVisible(line.slice(cursor), false, cursor === 0, labelEnds, cursor));
  // Bracket replacements can complete definition-shaped labels.
  return outboundDefinitions(output.join(''), forceColon);
}

export function prepareLine(value, forceColon = false, entities = OUTBOUND_NAMED_ENTITIES) {
  const text = outboundBase(value, entities);
  return text.trim() === '' ? '' : outboundContain(text, forceColon === true);
}

export function prepareSummaryTitle(value, entities = OUTBOUND_NAMED_ENTITIES) {
  return prepareLine(foldInline(oneLine(outboundBase(value, entities))), true, entities);
}

export function preparedCodeSpan(value, substituteVisible = false, entities = OUTBOUND_NAMED_ENTITIES, foldLimit) {
  const display = oneLine(outboundBase(reportAsText(value).replace(/\r/g, ' '), entities));
  return codeSpan(foldLimit === undefined ? display : foldInline(display, foldLimit), substituteVisible);
}

export function codeSpan(display, substituteVisible = false) {
  // Joined Summary parts must not be normalized again because joining can form new comment or token delimiters.
  if (substituteVisible) {
    display = outboundVisible(display.replace(/<(?=`+[A-Za-z/!?])/g, '\uFF1C'), true);
    display = outboundDefinitions(display, true);
  }
  const delimiter = '`'.repeat(longestBacktickRun(display) + 1);
  if (/^[` ]|[` ]$/.test(display)) display = ` ${display} `;
  return `${delimiter}${display}${delimiter}`;
}

export function outboundTildeRuns(line) {
  const spans = outboundCodeSpans(line);
  let spanIndex = 0;
  const parts = [];
  let index = 0;
  let backslashes = 0;
  while (index < line.length) {
    if (spanIndex < spans.length && index === spans[spanIndex][0]) {
      // A tilde fence needs line start; a span's earlier tick prevents it.
      const end = spans[spanIndex][2];
      parts.push(line.slice(index, end));
      index = end;
      spanIndex += 1;
      backslashes = 0;
      continue;
    }
    if (line[index] === '~') {
      let end = index + 1;
      while (end < line.length && line[end] === '~') end += 1;
      if (end - index >= 3) {
        if (backslashes % 2 === 0) parts.push('\\');
        parts.push(`~${'\\~'.repeat(end - index - 1)}`);
        index = end;
        backslashes = 0;
        continue;
      }
    }
    const character = line[index];
    parts.push(character);
    backslashes = character === '\\' ? backslashes + 1 : 0;
    index += 1;
  }
  return parts.join('');
}
