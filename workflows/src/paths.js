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
  // Slash normalization must expose location tails before they are separated from the path.
  let normalized = file;
  // Each changing pass removes characters, so input length bounds convergence.
  for (let remaining = file.length; remaining > 0; remaining -= 1) {
    const slashes = normalizePathString(normalized);
    const tail = slashes.match(/:(?:L?\d+)(?::\d+)?(?:-L?\d+)?$/)?.[0] || '';
    const next = `${normalizePathString(tail ? slashes.slice(0, -tail.length) : slashes)}${tail}`;
    if (next === normalized) break;
    normalized = next;
  }
  const suffix = normalized.match(/:(?:L?\d+)(?::\d+)?(?:-L?\d+)?$/)?.[0] || '';
  const path = suffix ? normalized.slice(0, -suffix.length) : normalized;
  // Reject traversal lexically because resolving it across symlinks could escape the root.
  if (path.startsWith('/') && hasDotDotSegment(path)) return { reason: 'file path contains a .. segment' };

  let relative;
  if (!path.startsWith('/')) {
    relative = normalizePathString(path);
    while (relative.startsWith('./')) relative = relative.slice(2);
  } else {
    const root = normalizeAbsoluteRoot(repoRoot);
    if (root === null) return { reason: 'repoRoot must be an absolute path without .. segments or backslashes' };
    const absolute = normalizePathString(path);
    if (absolute === root) return { reason: 'absolute file path resolves to repoRoot' };
    const prefix = root === '/' ? '/' : `${root}/`;
    if (!absolute.startsWith(prefix)) return { reason: 'absolute file path is outside repoRoot' };
    relative = absolute.slice(root === '/' ? 1 : root.length + 1);
  }

  if (relative === '' || relative === '.') return { reason: 'file path does not name a repository file' };
  if (hasDotDotSegment(relative)) return { reason: 'file path contains a .. segment' };
  if (relative.startsWith('\\')) return { reason: 'file path starts with a backslash' };
  if (relative.split('/')[0] === '~') return { reason: 'file path starts with a home anchor' };
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\//.test(relative) || /^[A-Za-z]:([/\\]|$)/.test(relative)) {
    return { reason: 'file path starts with a URI scheme or drive letter' };
  }
  // Controls can obscure any position; shaping characters only hide segment starts.
  if (/[\p{Cc}\p{Zl}\p{Zp}\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/u.test(file)
    || file.split('/').some((segment) => /^[\p{Cf}\p{Mn}\p{Me}\p{Default_Ignorable_Code_Point}\u2800\u115F\u1160\u3164\uFFA0]/u.test(segment))) {
    return { reason: 'file path contains a disallowed character' };
  }
  if (relative !== relative.trim()) return { reason: 'file path has leading or trailing whitespace' };
  return { file: `${relative}${suffix}` };
}
