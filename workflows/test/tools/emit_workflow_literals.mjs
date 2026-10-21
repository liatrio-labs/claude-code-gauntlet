// Use Node's bundled parser so regexes, comments and interpolation identifiers never become prompt text.
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { parse } = require('internal/deps/acorn/acorn/dist/acorn');
let input = '';
for await (const chunk of process.stdin) input += chunk;
const surfaces = [];
for (const { path, text } of JSON.parse(input)) {
  const tree = parse(text, { ecmaVersion: 'latest', sourceType: 'module', locations: true, allowReturnOutsideFunction: true });
  let index = 0;
  function visit(node) {
    if (!node || typeof node !== 'object') return;
    const literal = node.type === 'Literal' && typeof node.value === 'string';
    const template = node.type === 'TemplateElement';
    if (literal || template) {
      surfaces.push({ surface_id: `workflow-literals/${path}/${index++}`, source_path: path,
        producer_path: path, scenario: 'literal', text: literal ? node.value : node.value.cooked ?? node.value.raw,
        source_line: node.loc.start.line, source_column: node.loc.start.column + (literal ? 2 : 1) });
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(visit);
      else if (value && typeof value === 'object') visit(value);
    }
  }
  visit(tree);
}
process.stdout.write(JSON.stringify(surfaces));
