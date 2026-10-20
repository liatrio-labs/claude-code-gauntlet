import { asText } from './wire.js';

// Python str.strip includes control separators and NEL, but excludes BOM.
export const stripPythonWhitespace = (value) => value.replace(/^[\t-\r\x1C-\x20\x85\xA0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+|[\t-\r\x1C-\x20\x85\xA0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+$/g, '');

const VERIFY_SKIP_SYMBOLS = new Set(('the this that with from import class def for not and its but are was were '
  + 'can should would could also will has have been when then else elif True '
  + 'False None self return raise pass break continue lambda yield async await '
  + 'print isinstance len str int list dict set tuple type super object '
  + 'Exception ValueError TypeError KeyError AttributeError IndexError '
  + 'RuntimeError StopIteration OSError IOError FileNotFoundError '
  + 'NotImplementedError AssertionError OverflowError ZeroDivisionError').split(' '));
const VERIFY_IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

export function extractSymbols(description, evidence) {
  const text = `${asText(description)}\n${asText(evidence)}`;
  const symbols = new Set();
  for (const block of text.matchAll(/```[^\n]*\n(.*?)```/gs)) {
    for (const identifier of block[1].matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)) symbols.add(identifier[0]);
  }
  // Spans also consume fences, concatenating their language and body identifiers.
  for (const span of text.matchAll(/`([^`]+)`/g)) {
    for (const part of stripPythonWhitespace(span[1]).replace(/^[._]+|[._]+$/g, '').split('.')) {
      const clean = part.replace(/[^A-Za-z0-9_]/g, '');
      if (VERIFY_IDENT_RE.test(clean)) symbols.add(clean);
    }
  }
  // JS \b is ASCII; Python boundaries include Unicode letters and numbers.
  for (const match of text.matchAll(/(?<![\p{L}\p{N}_])([A-Za-z_][A-Za-z0-9_]*(?:[.()\[\]#]|::|->)[A-Za-z0-9_.()#\[\]:>-]*)/gu)) {
    for (const part of match[1].split(/[.()\[\]#:>-]+/)) {
      if (VERIFY_IDENT_RE.test(part)) symbols.add(part);
    }
  }
  for (const match of text.matchAll(/(?<![\p{L}\p{N}_])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\p{L}\p{N}_])/gu)) symbols.add(match[1]);
  // Bare CamelCase can be prose; only definite code tokens justify a search.
  return new Set([...symbols].filter((symbol) => symbol.length > 2 && !VERIFY_SKIP_SYMBOLS.has(symbol)));
}
