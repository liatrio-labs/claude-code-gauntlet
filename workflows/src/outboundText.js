// Owns outbound line normalization and containment, prepared code spans, and report display folding.
import { REPORT_FOLD_LIMITS } from './registry.js';
import { longestBacktickRun, deepFreeze } from './wire.js';

const OUTBOUND_NAMED_ENTITIES = deepFreeze({
  commat: '@',
  excl: '!',
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

const OUTBOUND_NAMED_ENTITY_PREFIXES = Object.freeze(Object.keys(OUTBOUND_NAMED_ENTITIES).flatMap((name) => (
  Array.from({ length: name.length }, (_, index) => name.slice(0, index + 1))
)));

const entityAt = (start, kind, fields = {}) => ({ start, kind, value: 0, hasDigit: false, name: '', ...fields });

export function normalizeOutboundText(value, entities = OUTBOUND_NAMED_ENTITIES) {
  const prefixes = entities === OUTBOUND_NAMED_ENTITIES ? OUTBOUND_NAMED_ENTITY_PREFIXES : Object.keys(entities).flatMap((name) => (
    Array.from({ length: name.length }, (_, index) => name.slice(0, index + 1))
  ));
  const namedPrefixes = new Set(prefixes);
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

export function outboundVisible(text, code = false) {
  // Fullwidth brackets survive reparsing and keep footnote text visible.
  text = text.replace(/(?<=[!\[])\\*\[|\[(?=\\*\^)/g, (match) => `${match.slice(0, -1)}\uFF3B`);
  const escaped = text.replace(/<(?=[A-Za-z/!?])/g, code ? '\uFF1C' : '&lt;');
  return escaped.replace(/@/g, (match, index) => (
    index === 0 || !/[A-Za-z0-9]/.test(escaped[index - 1])
      ? '\uFF20' : match
  ));
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

function outboundCodeSpans(line, index = outboundTickRunIndex(line)) {
  const spans = [];
  let runIndex = 0;
  while (runIndex < index.runs.length) {
    const run = index.runs[runIndex];
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

export function outboundDefinitions(line, forceColon = false) {
  // Forced guarding covers interpolated labels that can become definitions when embedded in surrounding Markdown.
  const guardColon = forceColon || /\]\\?\(/.test(line);
  if (guardColon) {
    line = line.replace(/\](\\*):/g, (match) => `${match.slice(0, -1)}\uFF1A`);
  }
  return line.replace(/^[^A-Za-z\\\[\n]*\[((?:\\[^\n]|[^\\\[\]\n])+)\]:/u, (match, label) => (
    /\S/.test(label) ? `${match.slice(0, -1)}\\:` : match
  ));
}

function outboundContain(line, forceColon = false) {
  line = outboundDefinitions(line, forceColon);
  line = line.replace(/<(?=`+[A-Za-z/!?])/g, '\uFF1C');
  const index = outboundTickRunIndex(line);
  const spans = outboundCodeSpans(line, index);
  let spanIndex = 0;
  const output = [];
  let cursor = 0;
  let runIndex = 0;
  while (runIndex < index.runs.length) {
    const run = index.runs[runIndex];
    output.push(outboundVisible(line.slice(cursor, run.start)));
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
  output.push(outboundVisible(line.slice(cursor)));
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

export function preparedCodeSpan(value, substituteVisible = false, entities = OUTBOUND_NAMED_ENTITIES) {
  let display = oneLine(outboundBase(reportAsText(value).replace(/\r/g, ' '), entities));
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
