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
  if (hasBackslash(file)) return { reason: 'file path contains a backslash' };
  // Reject traversal lexically because resolving it across symlinks could escape the root.
  if (hasDotDotSegment(file)) return { reason: 'file path contains a .. segment' };

  if (!file.startsWith('/')) {
    let relative = file;
    while (relative.startsWith('./')) relative = relative.slice(2);
    relative = normalizePathString(relative);
    if (relative === '' || relative === '.') return { reason: 'file path does not name a repository file' };
    if (/^[A-Za-z]:/.test(relative)) return { reason: 'file path has a leading drive letter' };
    return { file: relative };
  }

  const root = normalizeAbsoluteRoot(repoRoot);
  if (root === null) return { reason: 'repoRoot must be an absolute path without .. segments or backslashes' };
  const absolute = normalizePathString(file);
  if (absolute === root) return { reason: 'absolute file path resolves to repoRoot' };
  if (!pathUnderRoot(root, absolute)) return { reason: 'absolute file path is outside repoRoot' };

  const remainder = absolute.slice(root === '/' ? 1 : root.length + 1).replace(/^\/+/, '');
  if (remainder === '') return { reason: 'absolute file path does not name a repository file' };
  return { file: remainder };
}
