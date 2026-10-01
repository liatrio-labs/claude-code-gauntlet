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

function isSafeAbsolutePath(path) {
  return path.startsWith('/') && !hasBackslash(path) && !hasDotDotSegment(path);
}

function rootPrefix(root) {
  return root === '/' ? '/' : `${root}/`;
}

const LOCATION_SUFFIX_RE = /:(?:L?\d+)(?::\d+)?(?:-L?\d+)?$/;

function splitLocationSuffix(path) {
  const suffix = path.match(LOCATION_SUFFIX_RE)?.[0] || '';
  return { path: suffix ? path.slice(0, -suffix.length) : path, suffix };
}

export function normalizeAbsoluteRoot(root) {
  if (typeof root !== 'string' || root === '') return null;
  const normalized = normalizePathString(root);
  if (!isSafeAbsolutePath(normalized)) return null;
  return normalized;
}

const HOST_PATH_SEGMENT_SEPARATOR = '/(?:/|\\./)*';
const HOST_PATH_RIGHT_CONTINUATION_RE = /^[A-Za-z0-9._~+%@-]$/;
// A charset- and length-bounded id is printed so readers can find the finding; anything else uses the positional label.
const SAFE_FINDING_LABEL_RE = /^[A-Za-z0-9_.-]{1,64}$/;

function hostPathSpellings(root) {
  const normalized = normalizeAbsoluteRoot(root);
  if (normalized === null || normalized === '/') return [];
  const spellings = [normalized];
  if (normalized.startsWith('/private/')) spellings.push(normalized.slice('/private'.length));
  else spellings.push(`/private${normalized}`);
  return [...new Set(spellings)];
}

function hostPathPattern(root) {
  const segments = root.slice(1).split('/');
  const body = `/${segments.map((segment) => segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join(HOST_PATH_SEGMENT_SEPARATOR)}`;
  return { segments, matcher: new RegExp(body, 'y') };
}

function hostPathRightBoundary(text, end) {
  while (text[end] === '.') end += 1;
  const next = text[end];
  return next === undefined || !HOST_PATH_RIGHT_CONTINUATION_RE.test(next);
}

function hostPathLeftBoundary(text, start, segmentCount) {
  if (segmentCount > 1 || start === 0) return true;
  if (HOST_PATH_RIGHT_CONTINUATION_RE.test(text[start - 1])) return false;
  if (text[start - 1] !== '/') return true;
  let prefixEnd = start;
  while (text[prefixEnd - 1] === '/') prefixEnd -= 1;
  const prefixStart = prefixEnd - 5;
  return prefixStart >= 0 && text.slice(prefixStart, prefixEnd).toLowerCase() === 'file:'
    && (prefixStart === 0 || !/[A-Za-z]/.test(text[prefixStart - 1]));
}

export function mentionsHostRoot(text, roots) {
  if (typeof text !== 'string' || !Array.isArray(roots)) return false;
  const patterns = [];
  for (const root of roots) {
    for (const spelling of hostPathSpellings(root)) patterns.push(hostPathPattern(spelling));
  }
  for (let start = text.indexOf('/'); start !== -1; start = text.indexOf('/', start + 1)) {
    for (const { segments, matcher } of patterns) {
      matcher.lastIndex = start;
      const match = matcher.exec(text);
      if (match && hostPathLeftBoundary(text, start, segments.length)
        && hostPathRightBoundary(text, matcher.lastIndex)) return true;
    }
  }
  return false;
}

export function safeFindingLabel(value, fallback = null) {
  if (typeof value !== 'string' || !SAFE_FINDING_LABEL_RE.test(value)) {
    return fallback;
  }
  return value;
}

export function pathUnderRoot(root, path) {
  const normalizedRoot = normalizeAbsoluteRoot(root);
  if (normalizedRoot === null || typeof path !== 'string' || path === '') return false;
  const normalizedPath = normalizePathString(path);
  if (!isSafeAbsolutePath(normalizedPath)) return false;
  const prefix = rootPrefix(normalizedRoot);
  return normalizedPath === normalizedRoot || normalizedPath.startsWith(prefix);
}

export function repoRelativeFindingPath(repoRoot, file) {
  if (typeof file !== 'string' || file === '') return { reason: 'file must be a non-empty string' };
  // Slash normalization must expose location tails before they are separated from the path.
  let normalized = file;
  // Each changing pass removes characters, so input length bounds convergence.
  let location;
  for (let remaining = file.length; remaining >= 0; remaining -= 1) {
    const slashes = normalizePathString(normalized);
    const split = splitLocationSuffix(slashes);
    const path = normalizePathString(split.path);
    const next = `${path}${split.suffix}`;
    if (next === normalized) {
      location = { path, suffix: split.suffix };
      break;
    }
    normalized = next;
  }
  const { path, suffix } = location;
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
    const prefix = rootPrefix(root);
    if (!absolute.startsWith(prefix)) return { reason: 'absolute file path is outside repoRoot' };
    relative = absolute.slice(prefix.length);
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
