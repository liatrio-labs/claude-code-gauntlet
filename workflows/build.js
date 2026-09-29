#!/usr/bin/env node
// Emit modules in depth-first post-order from pipeline_entry.js, following imports
// in source order, so its top-level return runs after its dependencies. The runtime
// requires exported `meta` and plain `PIPELINE_VERSION` at the bundle's start.
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, 'src');
const OUT = join(HERE, 'pipeline.js');

// `meta` is the only declaration the runtime allows the `export` keyword on;
// `PIPELINE_VERSION` is a plain const (no `export`) hoisted alongside it.
const HOIST_META = /^\s*export\s+const\s+meta\b/;
const HOIST_VERSION = /^\s*const\s+PIPELINE_VERSION\b/;
const isHoisted = (line) => HOIST_META.test(line) || HOIST_VERSION.test(line);

export const WORKFLOW_SCRIPT_CAP = 524_288;
export const BUNDLE_HEADROOM = 65_536;
export const BUNDLE_MAX_BYTES = WORKFLOW_SCRIPT_CAP - BUNDLE_HEADROOM;
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

// Only a single-line './sibling.js' import is safe to strip: its target is inlined.
// A node: or bare specifier inlines nothing, and the sandbox has no Node builtins, so
// the stripped binding would stay undefined until a live dispatch threw. Any other
// import shape survives strip() verbatim and the runtime cannot parse it.
const IMPORT_LINE = /^\s*import(?:\s+|\s*['"])/;
const IMPORT_SPECIFIER = /\bfrom\s*['"]([^'"]*)['"]/;
const IMPORT_FROM_CLAUSES = /\bfrom\s*['"]([^'"]*)['"]/g;
const isStrippableImportLine = (line) => /^\s*import\s.+from\s.+;?\s*$/.test(line);

export function moduleOrder(sources) {
  const files = [...sources.keys()].sort();
  checkUnsafeImports(files, sources);

  const dependencies = new Map();
  for (const file of files) {
    const imports = [];
    sources.get(file).split('\n').forEach((line, index) => {
      if (!isStrippableImportLine(line)) return;
      const specifier = IMPORT_SPECIFIER.exec(line)[1];
      const dependency = specifier.slice(2);
      if (!sources.has(dependency)) {
        throw new Error(
          `build.js: src/${file}:${index + 1} imports missing sibling '${specifier}'`,
        );
      }
      imports.push(dependency);
    });
    dependencies.set(file, imports);
  }

  if (!sources.has('pipeline_entry.js')) {
    throw new Error('build.js: workflows/src/pipeline_entry.js is required');
  }

  const states = new Map();
  const active = [];
  const ordered = [];
  const reachable = new Set();
  function visit(file, emit) {
    const state = states.get(file);
    if (state === 2) return;
    if (state === 1) {
      const start = active.indexOf(file);
      const cycle = [...active.slice(start), file].join(' -> ');
      throw new Error(`build.js: import cycle in workflows/src: ${cycle}`);
    }

    states.set(file, 1);
    active.push(file);
    if (emit) reachable.add(file);
    for (const dependency of dependencies.get(file)) visit(dependency, emit);
    active.pop();
    states.set(file, 2);
    if (emit) ordered.push(file);
  }

  visit('pipeline_entry.js', true);
  for (const file of files) visit(file, false);

  const unreachable = files.filter((file) => !reachable.has(file));
  if (unreachable.length) {
    throw new Error(
      `build.js: src files unreachable from pipeline_entry.js: ${unreachable.join(', ')}`,
    );
  }
  return ordered;
}

export function unsafeImports(source) {
  const bad = [];
  source.split('\n').forEach((line, i) => {
    if (!IMPORT_LINE.test(line)) return;
    const clauses = [...line.matchAll(IMPORT_FROM_CLAUSES)];
    if (clauses.length > 1) {
      bad.push({
        line: i + 1, text: line.trim(), specifier: null,
        reason: 'multiple `from` clauses on one line — use one import per line',
      });
      return;
    }
    const specifier = clauses.length === 1 ? clauses[0][1] : null;
    if (specifier === null || !isStrippableImportLine(line)) {
      bad.push({
        line: i + 1, text: line.trim(), specifier,
        reason: 'no single-line `from` clause — strip() only removes single-line imports',
      });
    } else if (!specifier.startsWith('./')) {
      bad.push({
        line: i + 1, text: line.trim(), specifier,
        reason: `specifier '${specifier}' is not relative to src/ — stripping it ships an undefined reference; inline the value into src/ instead`,
      });
    }
  });
  return bad;
}

function strip(source) {
  const out = [];
  for (const line of source.split('\n')) {
    if (isStrippableImportLine(line)) continue;
    if (isHoisted(line)) continue;
    out.push(line.replace(/^(\s*)export\s+(async function|function|const|let|class|{)/, '$1$2'));
  }
  return out.join('\n');
}

// Concatenated modules share one async-function scope, so duplicate top-level names
// would make the generated workflow fail to parse.
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

export function checkUnsafeImports(files, sources) {
  const unsafe = files.flatMap((file) =>
    unsafeImports(sources.get(file)).map((v) => ({ ...v, file })));
  if (unsafe.length) {
    throw new Error(
      `build.js: unsafe import(s) in workflows/src — only single-line './sibling.js' imports may be stripped:\n`
        + unsafe.map((v) => `  src/${v.file}:${v.line}: ${v.text}\n    ${v.reason}`).join('\n'),
    );
  }
}

// Raw line terminators would evade the LF-based import and comment scans.
export function checkRawLineTerminators(files, sources) {
  const violations = [];
  for (const file of files) {
    const source = sources.get(file);
    for (const match of source.matchAll(/[\r\u2028\u2029]/g)) {
      const line = source.slice(0, match.index).split('\n').length;
      const codePoint = `U+${match[0].codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}`;
      violations.push(`  src/${file}:${line}: raw ${codePoint}`);
    }
  }
  if (violations.length) {
    throw new Error(
      `build.js: raw line terminator(s) are not allowed in workflows/src; use LF only:\n${violations.join('\n')}`,
    );
  }
}

function canCompile(body) {
  try {
    new AsyncFunction(body);
    return true;
  } catch {
    return false;
  }
}

// Probe candidates with NULs and keep them only when V8 still parses the body; a failed probe proves the line inert.
export function stripInertLines(body, moduleName = 'module') {
  try {
    new AsyncFunction(body);
  } catch (error) {
    throw new Error(
      `build.js: ${moduleName} is not parseable as an async function body: ${error.message}`,
    );
  }

  const hasTrailingNewline = body.endsWith('\n');
  const lines = body.split('\n');
  if (hasTrailingNewline) lines.pop();
  const output = [];
  const dropped = [];
  const kept = [];

  for (const [index, line] of lines.entries()) {
    const comment = /^(\s*)\/\//.exec(line);
    const blank = /^\s*$/.test(line);
    if (!comment && !blank) {
      output.push(line);
      continue;
    }

    let probe;
    if (blank) {
      probe = '\0';
    } else {
      const at = comment[1].length;
      probe = line.slice(0, at) + '\0\0' + line.slice(at + 2);
    }
    const probed = lines.map((candidate, candidateIndex) =>
      candidateIndex === index ? probe : candidate).join('\n');
    if (canCompile(probed)) {
      output.push(line);
      kept.push(line);
    } else {
      dropped.push(line);
    }
  }

  return {
    text: output.join('\n') + (hasTrailingNewline ? '\n' : ''),
    dropped,
    kept,
  };
}

// Keep headroom below the Workflow tool's script cap.
export function checkBundleSize(bundle, maxBytes = BUNDLE_MAX_BYTES) {
  const bytes = Buffer.byteLength(bundle, 'utf8');
  if (bytes > maxBytes) {
    throw new Error(
      `build.js: bundle is ${bytes} bytes; limit is ${maxBytes} bytes `
        + `(Workflow script cap ${WORKFLOW_SCRIPT_CAP} bytes with ${BUNDLE_HEADROOM} bytes of headroom); `
        + 'the bundle must shrink',
    );
  }
}

export function buildFromSources(
  sources,
  { dropComments = true, maxBytes = BUNDLE_MAX_BYTES } = {},
) {
  const files = [...sources.keys()].sort();
  checkRawLineTerminators(files, sources);
  const order = moduleOrder(sources);

  const hoisted = [];
  for (const file of order) {
    for (const line of sources.get(file).split('\n')) {
      if (isHoisted(line)) hoisted.push(line);
    }
  }
  const parts = [...hoisted, '// GENERATED by workflows/build.js — do not edit by hand.'];
  for (const file of order) {
    parts.push(`// --- ${file} ---`);
    const body = strip(sources.get(file));
    const emitted = dropComments ? stripInertLines(body, `src/${file}`).text : body;
    parts.push(emitted.replace(/\n+$/, ''));
  }
  const bundle = parts.join('\n').replace(/\n+$/, '') + '\n';

  const collisions = detectTopLevelCollisions(bundle);
  if (collisions.length) {
    const detail = collisions
      .map((c) => `  '${c.name}' declared at lines ${c.lines.join(', ')}`)
      .join('\n');
    throw new Error(
      `build.js: top-level identifier collision(s) in the bundle — each name must have a single owner (export from one module, import into the others):\n${detail}`,
    );
  }
  checkBundleSize(bundle, maxBytes);
  return bundle;
}

// dropComments and maxBytes are build test controls.
export function build(options = {}) {
  const files = readdirSync(SRC).filter((f) => f.endsWith('.js')).sort();
  const sources = new Map(files.map((f) => [f, readFileSync(join(SRC, f), 'utf8')]));
  return buildFromSources(sources, options);
}

// Importing the module for tests must not write the generated bundle.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const bundle = build();
    writeFileSync(OUT, bundle);
    const bytes = Buffer.byteLength(bundle, 'utf8');
    console.log(`built ${OUT}: ${bytes} bytes (${(bytes / WORKFLOW_SCRIPT_CAP * 100).toFixed(1)}% of cap)`);
  } catch (e) {
    console.error(e.message);
    process.exit(1);
  }
}
