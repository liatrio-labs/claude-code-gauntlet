import { readFileSync, readdirSync, existsSync } from 'node:fs';
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
      input: JSON.parse(readFileSync(join(caseDir, 'input.json'), 'utf8')),
      expected: JSON.parse(readFileSync(join(caseDir, 'expected.json'), 'utf8')),
    }));
}
