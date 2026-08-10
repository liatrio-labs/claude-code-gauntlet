#!/usr/bin/env node
// build.js — dependency-free bundler. Concatenates the workflows/src/*.js files named
// in ORDER (below) into the single self-contained workflows/pipeline.js — ORDER is a
// pinned dependency order, so a src file absent from it is silently left out of the
// bundle rather than appended. The bundle MUST begin with
// `export const meta` (hoisted from pipeline_entry.js — the ONLY `export` the
// workflow runtime permits in the bundle; any other `export`, including
// `export default`, is a runtime SyntaxError) followed by the plain
// `const PIPELINE_VERSION` declaration. All import lines are dropped; `export X`
// -> `X` for every other declaration. pipeline_entry.js is emitted LAST: its body
// ends with a top-level `return await run(...)`, which the runtime executes after
// every sibling definition above it, reading the runtime-injected `args` global.
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, 'src');
const OUT = join(HERE, 'pipeline.js');

// Pinned concat order. dedupCrossAgent (filterFindings) must precede applyChallenges.
// pipeline_entry.js is emitted LAST (its run() references everything above).
const ORDER = [
  'findingDedup.js', 'filterFindings.js', 'mergeFindings.js',
  'applyValidations.js', 'applyChallenges.js', 'registry.js', 'args.js',
  'stages.js', 'pipeline_entry.js',
];

// `meta` is the only declaration the runtime allows the `export` keyword on;
// `PIPELINE_VERSION` is a plain const (no `export`) hoisted alongside it.
const HOIST_META = /^\s*export\s+const\s+meta\b/;
const HOIST_VERSION = /^\s*const\s+PIPELINE_VERSION\b/;
const isHoisted = (line) => HOIST_META.test(line) || HOIST_VERSION.test(line);

// ORDER must name every workflows/src/*.js file exactly once. present() used to
// silently intersect ORDER with disk, so a new module left out of ORDER shipped
// as an incomplete bundle while unit tests importing ../src/<file>.js stayed green.
// Set equality alone cannot see a name listed TWICE — the repeat collapses into
// the set, present() then emits that module twice, and the only failure left is
// the collision detector naming duplicated identifiers instead of the repeated
// file. Count duplicates here so the guard enforces the "exactly once" it claims.
export function orderMismatches(order, onDisk) {
  const inOrder = new Set(order);
  const onDiskSet = new Set(onDisk);
  const missingFromOrder = [...onDiskSet].filter((f) => !inOrder.has(f)).sort();
  const missingFromDisk = [...inOrder].filter((f) => !onDiskSet.has(f)).sort();
  const seen = new Set();
  const duplicates = new Set();
  for (const file of order) {
    if (seen.has(file)) duplicates.add(file);
    seen.add(file);
  }
  return { missingFromOrder, missingFromDisk, duplicatedInOrder: [...duplicates].sort() };
}

function present() {
  const found = new Set(readdirSync(SRC).filter((f) => f.endsWith('.js')));
  const { missingFromOrder, missingFromDisk, duplicatedInOrder } = orderMismatches(ORDER, [...found]);
  if (missingFromOrder.length || missingFromDisk.length || duplicatedInOrder.length) {
    const lines = [];
    if (missingFromOrder.length) {
      lines.push(
        `on disk but not in ORDER: ${missingFromOrder.join(', ')} `
          + `(add each file to ORDER in dependency order, or remove the stray file)`,
      );
    }
    if (missingFromDisk.length) {
      lines.push(
        `in ORDER but not on disk: ${missingFromDisk.join(', ')} `
          + `(remove the name from ORDER, or restore the file)`,
      );
    }
    if (duplicatedInOrder.length) {
      lines.push(
        `listed more than once in ORDER: ${duplicatedInOrder.join(', ')} `
          + `(delete the repeated entry — a duplicate concatenates the module twice)`,
      );
    }
    throw new Error(
      `build.js: ORDER does not match workflows/src/*.js — every .js file in `
        + `src/ must appear in ORDER exactly once (and vice versa):\n`
        + lines.map((l) => `  ${l}`).join('\n'),
    );
  }
  return ORDER.filter((f) => found.has(f));
}

// Only a RELATIVE import is safe to drop: the target is a sibling src module whose
// body ORDER inlines into the bundle, so the stripped binding still resolves. A
// `node:*` or bare specifier inlines nothing — stripping it ships an undefined
// reference that lint cannot see (the binding IS declared in the src file), that the
// bundle-fresh check calls clean (committed bundle and rebuild are wrong together),
// and that only throws on a live dispatch, since the sandbox provides no Node
// builtins. Same crash class as the `structuredClone` live-smoke failure. Detect it
// at BUILD time instead. The OTHER unsafe shape is any import line strip() does not
// match at all — its regex wants a single-line `import … from …`, so a side-effect
// (`import './x.js';`) or multi-line import survives into the bundle verbatim, which
// the runtime cannot parse. Both are the same defect (the line ships as written), so
// they carry one reason: it is the missing single-line `from` clause, not the
// specifier, that makes them unsafe.
const IMPORT_LINE = /^\s*import(?:\s+|\s*['"])/;
const IMPORT_SPECIFIER = /\bfrom\s*['"]([^'"]*)['"]/;

export function unsafeImports(source) {
  const bad = [];
  source.split('\n').forEach((line, i) => {
    if (!IMPORT_LINE.test(line)) return;
    const match = IMPORT_SPECIFIER.exec(line);
    const specifier = match ? match[1] : null;
    if (specifier === null) {
      bad.push({
        line: i + 1, text: line.trim(), specifier: null,
        reason: 'no single-line `from` clause — strip() matches only `import … from …` on one line, so a side-effect or multi-line import ships into the bundle verbatim; ORDER already concatenates every module body, so no src module needs one',
      });
    } else if (!specifier.startsWith('./')) {
      bad.push({
        line: i + 1, text: line.trim(), specifier,
        reason: `specifier '${specifier}' is not relative to src/ — nothing is inlined for it, so stripping the line ships an undefined reference; inline the value into src/ instead`,
      });
    }
  });
  return bad;
}

// Drop import lines and the hoisted consts (emitted at the top instead); rewrite
// `export X` -> `X` for every other declaration.
function strip(source) {
  const out = [];
  for (const line of source.split('\n')) {
    if (/^\s*import\s.+from\s.+;?\s*$/.test(line)) continue;
    if (isHoisted(line)) continue;
    out.push(line.replace(/^(\s*)export\s+(async function|function|const|let|class|{)/, '$1$2'));
  }
  return out.join('\n');
}

// Every top-level binding in the concatenated bundle shares ONE lexical scope
// (the runtime wraps the whole body in a single async function). Two modules
// declaring the same top-level name — after `export` is stripped — is therefore a
// runtime `Identifier 'X' has already been declared` SyntaxError, invisible to this
// bundler's text concat but fatal on the first live dispatch (the SEVERITY_ORDER
// collision the live smoke run hit). Detect it at BUILD time and fail loudly with the
// duplicate name instead. A top-level declaration is one at column 0 (module bodies
// concatenate flat); const/let/var/function/class, with optional `export`/`async`.
const TOP_LEVEL_DECL = /^(?:export\s+)?(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)/;

export function detectTopLevelCollisions(bundleText) {
  const seen = new Map(); // name -> [lineNumbers]
  bundleText.split('\n').forEach((line, i) => {
    const m = TOP_LEVEL_DECL.exec(line);
    if (m) seen.set(m[1], (seen.get(m[1]) || []).concat(i + 1));
  });
  return [...seen.entries()]
    .filter(([, lines]) => lines.length > 1)
    .map(([name, lines]) => ({ name, lines }));
}

export function build() {
  const files = present();
  const sources = new Map(files.map((f) => [f, readFileSync(join(SRC, f), 'utf8')]));

  // 0) Fail on any import strip() cannot safely drop (see unsafeImports).
  const unsafe = files.flatMap((file) =>
    unsafeImports(sources.get(file)).map((v) => ({ ...v, file })));
  if (unsafe.length) {
    throw new Error(
      `build.js: unsafe import(s) in workflows/src — only single-line './sibling.js' imports may be stripped:\n`
        + unsafe.map((v) => `  src/${v.file}:${v.line}: ${v.text}\n    ${v.reason}`).join('\n'),
    );
  }

  // 1) Hoist the public surface so the bundle's first line is `export const meta`.
  const hoisted = [];
  for (const file of files) {
    for (const line of sources.get(file).split('\n')) {
      if (isHoisted(line)) hoisted.push(line);
    }
  }
  const parts = [...hoisted, '// GENERATED by workflows/build.js — do not edit by hand.'];
  // 2) Emit every module body (public consts already hoisted, imports dropped).
  for (const file of files) {
    parts.push(`// --- ${file} ---`);
    parts.push(strip(sources.get(file)));
  }
  const bundle = parts.join('\n').replace(/\n+$/, '') + '\n';

  // 3) Fail the build on any top-level identifier collision (see above).
  const collisions = detectTopLevelCollisions(bundle);
  if (collisions.length) {
    const detail = collisions
      .map((c) => `  '${c.name}' declared at lines ${c.lines.join(', ')}`)
      .join('\n');
    throw new Error(
      `build.js: top-level identifier collision(s) in the bundle — each name must have a single owner (export from one module, import into the others):\n${detail}`,
    );
  }
  return bundle;
}

// main-guard: only write when run as `node workflows/build.js`; importing this module
// (the collision-detector unit test) must not trigger a write.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    writeFileSync(OUT, build());
    console.log(`built ${OUT}`);
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
}
