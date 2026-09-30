import { deepClone } from './applyChallenges.js';

function collapseDotSlash(path) {
  let out = path;
  for (;;) {
    const next = out.replace(/\/\.\//g, '/');
    if (next === out) return out;
    out = next;
  }
}

function stripTrailingSlashes(path) {
  let out = path;
  while (out.length > 1 && out.endsWith('/')) out = out.slice(0, -1);
  return out;
}

function normalizePathString(path) {
  let out = path;
  for (;;) {
    // POSIX treats repeated slashes as one path separator.
    let next = stripTrailingSlashes(collapseDotSlash(out.replace(/\/{2,}/g, '/')));
    if (next.endsWith('/.')) next = next.length === 2 ? '/' : next.slice(0, -2);
    next = stripTrailingSlashes(next);
    if (next === out) return next;
    out = next;
  }
}

function hasBackslash(path) {
  return path.includes('\\');
}

function hasDotDotSegment(path) {
  return path.split('/').includes('..');
}

export function normalizeAbsoluteRoot(root) {
  if (typeof root !== 'string' || root === '') return null;
  const normalized = normalizePathString(root);
  if (!normalized.startsWith('/') || hasBackslash(normalized) || hasDotDotSegment(normalized)) return null;
  return normalized;
}

export function pathUnderRoot(root, path) {
  const normalizedRoot = normalizeAbsoluteRoot(root);
  if (normalizedRoot === null || typeof path !== 'string' || path === '') return false;
  const normalizedPath = normalizePathString(path);
  if (!normalizedPath.startsWith('/') || hasBackslash(normalizedPath) || hasDotDotSegment(normalizedPath)) return false;
  const prefix = normalizedRoot === '/' ? '/' : `${normalizedRoot}/`;
  return normalizedPath === normalizedRoot || normalizedPath.startsWith(prefix);
}

export function repoRelativeFindingPath(repoRoot, file) {
  if (typeof file !== 'string' || file === '') return { reason: 'file must be a non-empty string' };
  // Reject traversal lexically because resolving it across symlinks could escape the root.
  if (file.startsWith('/') && hasDotDotSegment(file)) return { reason: 'file path contains a .. segment' };

  let relative;
  if (!file.startsWith('/')) {
    relative = file;
    while (relative.startsWith('./')) relative = relative.slice(2);
    relative = normalizePathString(relative);
    const root = normalizeAbsoluteRoot(repoRoot);
    if (root !== null && pathUnderRoot(root, `/${relative}`)) {
      relative = `/${relative}`.slice(root === '/' ? 1 : root.length + 1);
    }
  } else {
    const root = normalizeAbsoluteRoot(repoRoot);
    if (root === null) return { reason: 'repoRoot must be an absolute path without .. segments or backslashes' };
    const absolute = normalizePathString(file);
    if (absolute === root) return { reason: 'absolute file path resolves to repoRoot' };
    if (!pathUnderRoot(root, absolute)) return { reason: 'absolute file path is outside repoRoot' };
    relative = absolute.slice(root === '/' ? 1 : root.length + 1).replace(/^\/+/, '');
  }

  if (relative === '' || relative === '.' || relative.startsWith('/')) return { reason: 'file path does not name a repository file' };
  if (hasDotDotSegment(relative)) return { reason: 'file path contains a .. segment' };
  if (hasBackslash(relative)) return { reason: 'file path contains a backslash' };
  const segments = relative.split('/');
  const segmentPattern = /^[\p{L}\p{N}._+@=,~-][\p{L}\p{N}._+@=,~ -]*$/u;
  const laterSegmentPattern = /^[\p{L}\p{N}._+@=,~-][\p{L}\p{N}._+@=,~ :-]*$/u;
  if (segments[0].startsWith('~')) return { reason: 'file path starts with a home-directory marker' };
  if (segments.some((segment, index) => !(index === 0 ? segmentPattern : laterSegmentPattern).test(segment))) {
    return { reason: 'file path contains a disallowed character' };
  }
  return { file: relative };
}

// The skill reads these top-level fields as real host paths (Phase 8 opens artifactPaths and
// materializes persistReturn.entries[].path), so the fence must leave them intact. Their artifact
// bytes are fenced separately before persistence.
const HOST_PATH_KEYS = new Set(['artifactPaths', 'persistReturn']);

export function redactHostPaths(value, repoRoot, priorCount = 0, recordGap = false) {
  const copy = deepClone(value);
  const root = normalizeAbsoluteRoot(repoRoot);
  if (root === null) return { value: copy, count: 0 };
  let count = 0;
  const walk = (node) => {
    if (typeof node === 'string') {
      const occurrences = node.split(root).length - 1;
      count += occurrences;
      return occurrences ? node.split(root).join('<repo>') : node;
    }
    if (Array.isArray(node)) return node.map(walk);
    if (node !== null && typeof node === 'object') {
      for (const key of Object.keys(node)) node[key] = walk(node[key]);
    }
    return node;
  };
  const redacted =
    copy !== null && typeof copy === 'object' && !Array.isArray(copy)
      ? Object.fromEntries(Object.entries(copy).map(([k, v]) => [k, HOST_PATH_KEYS.has(k) ? v : walk(v)]))
      : walk(copy);
  if (recordGap && count + priorCount > 0) {
    redacted.gaps = [...(Array.isArray(redacted.gaps) ? redacted.gaps : []), `host-path-redacted: ${count + priorCount} occurrence(s)`];
  }
  return { value: redacted, count };
}
