// parse_gate.mjs — bundle parse gate for tests/test_bundle_fresh.py.
//
// The workflow runtime executes the bundle BODY as a wrapped async function:
// `export const meta` is hoisted out by the host, and top-level await/return are
// legal inside the wrapper. Reproduce that here — strip the meta export line, then
// COMPILE the remainder with the AsyncFunction constructor. A top-level identifier
// collision across the concatenated modules (e.g. two `const SEVERITY_ORDER`)
// surfaces at compile time as a SyntaxError ("Identifier 'X' has already been
// declared") — exactly the class that shipped in the committed bundle and crashed
// the live smoke run. Compilation (not invocation) is where the collision throws,
// so we never run the body (which would need the runtime globals).
//
// Exit 0 = compiles cleanly. Exit 1 = SyntaxError (name printed to stderr).
import { readFileSync } from 'node:fs';

const path = process.argv[2];
if (!path) {
  process.stderr.write('usage: node parse_gate.mjs <bundle.js>\n');
  process.exit(2);
}

const src = readFileSync(path, 'utf8')
  .split('\n')
  .filter((line) => !/^\s*export\s+const\s+meta\b/.test(line))
  .join('\n');

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
try {
  new AsyncFunction(src); // eslint-disable-line no-new — compile only, never invoke
  process.exit(0);
} catch (e) {
  process.stderr.write(`${e.name}: ${e.message}\n`);
  process.exit(1);
}
