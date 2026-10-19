import { fnv1a32, isPlainObject } from './wire.js';
import { DISPATCH_RESULTS } from './registry.js';
import { repoRelativeFindingPath } from './paths.js';

const exactKeys = (value, keys) => isPlainObject(value)
  && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const nonempty = (value) => typeof value === 'string' && value.trim().length > 0;
const removedTestLine = (value) => Number.isSafeInteger(value) && value > 0;
const confinedPath = (value) => nonempty(value)
  && !value.includes('\\')
  && repoRelativeFindingPath(null, value).file === value;

export function validateRemovedTests(value) {
  const unavailable = (reason) => ({ status: 'unavailable', items: [], omitted: 0, reason });
  if (value === undefined || value === null) return unavailable('inventory not supplied');
  if (!exactKeys(value, ['status', 'items', 'omitted', 'checksum'])
    || !['available', 'unavailable'].includes(value.status)
    || !Array.isArray(value.items) || value.items.length > 60
    || !Number.isSafeInteger(value.omitted) || value.omitted < 0
    || (value.status === 'unavailable' && (value.items.length || value.omitted))) {
    return unavailable('invalid inventory shape');
  }
  const ids = new Set();
  for (const item of value.items) {
    if (!exactKeys(item, ['id', 'path', 'old_start', 'old_end', 'kind'])
      || typeof item.id !== 'string' || !/^[a-f0-9]{16}$/.test(item.id) || ids.has(item.id)
      || !confinedPath(item.path) || !removedTestLine(item.old_start)
      || !removedTestLine(item.old_end) || item.old_end < item.old_start
      || !['deleted_file', 'hunk'].includes(item.kind)) return unavailable('invalid inventory item');
    ids.add(item.id);
  }
  const body = { status: value.status, items: value.items, omitted: value.omitted };
  if (fnv1a32(JSON.stringify(body, null, 2)) !== value.checksum) return unavailable('inventory checksum mismatch');
  return value.status === 'unavailable' ? unavailable('saved diff structure unavailable') : body;
}

// Runtime intake must enforce the contract even when a host skips schema validation.
function matchesAuditSchema(value, schema) {
  if (schema.type === 'object') return exactKeys(value, Object.keys(schema.properties))
    && Object.entries(schema.properties).every(([key, child]) => matchesAuditSchema(value[key], child));
  if (schema.type === 'array') return Array.isArray(value) && value.every((entry) => matchesAuditSchema(entry, schema.items));
  if (schema.type === 'string') return nonempty(value) && (!schema.enum || schema.enum.includes(value));
  return typeof value === 'number' && Number.isFinite(value)
    && (schema.type !== 'integer' || Number.isSafeInteger(value))
    && (schema.minimum === undefined || value >= schema.minimum)
    && (schema.maximum === undefined || value <= schema.maximum);
}

export function checkRemovedTestAudit(inventory, rows) {
  const findings = [];
  const gap = (reason) => ({ findings: [], audit: { status: 'gap', reason } });
  const schema = DISPATCH_RESULTS['code-gauntlet:test-analyzer'].removed_test_audit;
  if (!matchesAuditSchema(rows, schema)) return gap('malformed audit rows');
  const items = new Map(inventory.items.map((item) => [item.id, item]));
  const seen = new Set();
  for (const row of rows) {
    if (!items.has(row.id) || seen.has(row.id)) return gap('unknown or duplicate inventory id');
    seen.add(row.id);
    if (row.status === 'covered_elsewhere' && (!row.covered_by.length
      || row.covered_by.some((cite) => !confinedPath(cite.file)))) return gap('incomplete surviving-test citation');
    if (row.status === 'lost' ? !row.losses.length : row.losses.length) return gap('losses disagree with audit status');
    const { id, path, old_start, old_end } = items.get(row.id);
    for (const [index, loss] of row.losses.entries()) {
      if (!confinedPath(loss.file)) return gap('invalid behavior anchor');
      if (items.get(row.id).kind === 'deleted_file' && loss.file === path) return gap('behavior anchor was deleted');
      if ([loss.title, loss.description, loss.failure_scenario].some((text) => text.includes(path))) {
        return gap('deleted test path belongs only in inventory metadata');
      }
      findings.push({
        id: `removed-test:${id}:${index + 1}`, agent: 'test-analyzer', dimension: 'test_coverage',
        file: loss.file, line_start: loss.line, title: loss.title, description: loss.description,
        severity: loss.severity, confidence: loss.confidence, criticality: loss.criticality,
        failure_scenario: loss.failure_scenario, removed_test: { id, path, old_start, old_end },
      });
    }
  }
  if (seen.size !== items.size) return gap('missing inventory ids');
  return { findings, audit: inventory.omitted > 0
    ? { status: 'overflow', omitted: inventory.omitted } : { status: 'ran-clean' } };
}
