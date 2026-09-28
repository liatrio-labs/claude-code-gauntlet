import { readFileSync, readdirSync, existsSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, '..', '..', '..', 'tests', 'fixtures', 'parity');

// Recursively find fixture cases: filter_findings groups cases one level deeper
// than finding_dedup and merge_findings, so descend until input.json is found.
function findCaseDirs(dir) {
  if (existsSync(join(dir, 'input.json'))) return [dir];
  const out = [];
  for (const d of readdirSync(dir, { withFileTypes: true })) {
    if (d.isDirectory()) out.push(...findCaseDirs(join(dir, d.name)));
  }
  return out;
}

export function loadCases(script) {
  const base = join(FIXTURES, script);
  return findCaseDirs(base)
    .sort()
    .map((caseDir) => ({
      name: relative(base, caseDir),
      expectedPath: join(caseDir, 'expected.json'),
      input: JSON.parse(readFileSync(join(caseDir, 'input.json'), 'utf8')),
      expected: JSON.parse(readFileSync(join(caseDir, 'expected.json'), 'utf8')),
    }));
}

function pythonString(value) {
  let out = '"';
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    if (code === 0x22) out += '\\"';
    else if (code === 0x5c) out += '\\\\';
    else if (code === 0x08) out += '\\b';
    else if (code === 0x09) out += '\\t';
    else if (code === 0x0a) out += '\\n';
    else if (code === 0x0c) out += '\\f';
    else if (code === 0x0d) out += '\\r';
    else if (code >= 0x20 && code <= 0x7f) out += value[i];
    else if (code >= 0xd800 && code <= 0xdbff && i + 1 < value.length) {
      const low = value.charCodeAt(i + 1);
      if (low >= 0xdc00 && low <= 0xdfff) {
        out += `\\u${code.toString(16).padStart(4, '0')}\\u${low.toString(16).padStart(4, '0')}`;
        i += 1;
      } else {
        out += `\\u${code.toString(16).padStart(4, '0')}`;
      }
    } else if (code >= 0xd800 && code <= 0xdfff) {
      out += `\\u${code.toString(16).padStart(4, '0')}`;
    } else {
      out += `\\u${code.toString(16).padStart(4, '0')}`;
    }
  }
  return `${out}\"`;
}

function compareCodePoints(left, right) {
  const leftPoints = Array.from(left, (character) => character.codePointAt(0));
  const rightPoints = Array.from(right, (character) => character.codePointAt(0));
  const length = Math.min(leftPoints.length, rightPoints.length);
  for (let i = 0; i < length; i += 1) {
    if (leftPoints[i] !== rightPoints[i]) return leftPoints[i] - rightPoints[i];
  }
  return leftPoints.length - rightPoints.length;
}

function pythonJson(value, depth = 0) {
  if (value === null) return 'null';
  if (typeof value === 'string') return pythonString(value);
  if (typeof value === 'number') return JSON.stringify(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';

  const indent = '  '.repeat(depth);
  const childIndent = '  '.repeat(depth + 1);
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    const items = value.map((item) => `${childIndent}${pythonJson(item, depth + 1)}`);
    return `[\n${items.join(',\n')}\n${indent}]`;
  }

  const keys = Object.keys(value).sort(compareCodePoints);
  if (keys.length === 0) return '{}';
  const items = keys.map((key) => `${childIndent}${pythonString(key)}: ${pythonJson(value[key], depth + 1)}`);
  return `{\n${items.join(',\n')}\n${indent}}`;
}

export function serializeGolden(value) {
  return `${pythonJson(value)}\n`;
}

export function writeGolden(testCase, value) {
  writeFileSync(testCase.expectedPath, serializeGolden(value), 'utf8');
}
