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

const HOST_PATH_RIGHT_CONTINUATION_RE = /^[A-Za-z0-9._~+%@-]$/;
// A charset- and length-bounded id is printed so readers can find the finding; anything else uses the positional label.
const SAFE_FINDING_LABEL_RE = /^[A-Za-z0-9_.-]{1,64}$/;

const HOST_PATH_HEX_RE = /^[0-9A-Fa-f]{2}$/;
const HOST_PATH_HOME_RE = /^\/(?:Users|home)\/([^/]+)\//;

function hostPathSpellings(root) {
  const normalized = normalizeAbsoluteRoot(root);
  if (normalized === null || normalized === '/') return [];
  const hasPrivatePrefix = normalized.slice(0, '/private/'.length).toLowerCase() === '/private/';
  const alias = hasPrivatePrefix
    ? normalized.slice('/private'.length)
    : `/private${normalized}`;
  return [...new Set([normalized, alias])];
}

function hostPathPattern(root, caseInsensitive, derived = false, protectedChildren = []) {
  const segments = root.slice(1).split('/');
  const flags = `${caseInsensitive ? 'i' : ''}y`;
  return {
    segments: segments.map((segment) => new RegExp(segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), flags)),
    derived,
    protectedChildren: protectedChildren.map((segment) => new RegExp(segment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'iy')),
  };
}

function derivedHostHome(root) {
  const normalized = root.startsWith('/private/') ? root.slice('/private'.length) : root;
  const match = normalized.match(HOST_PATH_HOME_RE);
  if (!match) return null;
  const home = normalized.slice(0, match[0].length - 1);
  return { home, child: normalized.slice(home.length + 1).split('/')[0] };
}

export function prepareHostRootPatterns(roots) {
  const patterns = [];
  const derivedHomes = [];
  if (!Array.isArray(roots)) return patterns;
  for (const root of roots) {
    const normalized = normalizeAbsoluteRoot(root);
    if (normalized === null || normalized === '/') continue;
    const caseInsensitive = normalized.slice(1).split('/').length > 1;
    for (const spelling of hostPathSpellings(normalized)) {
      patterns.push(hostPathPattern(spelling, caseInsensitive));
    }
    const derived = derivedHostHome(normalized);
    if (derived !== null) {
      let entry = derivedHomes.find((candidate) => candidate.home === derived.home);
      if (!entry) {
        entry = { home: derived.home, children: [] };
        derivedHomes.push(entry);
      }
      entry.children.push(derived.child);
    }
  }
  for (const { home, children } of derivedHomes) {
    patterns.push(hostPathPattern(home, false, true, [...new Set(children)]));
  }
  return patterns;
}

function decodeAsciiEscapes(text) {
  const chars = [];
  const escaped = [];
  for (let index = 0; index < text.length;) {
    const digits = text.slice(index + 1, index + 3);
    const byte = text[index] === '%' && HOST_PATH_HEX_RE.test(digits) ? parseInt(digits, 16) : -1;
    if (byte >= 0 && byte < 0x80) {
      chars.push(String.fromCharCode(byte));
      escaped.push(true);
      index += 3;
    } else {
      chars.push(text[index]);
      escaped.push(false);
      index += 1;
    }
  }
  return { text: chars.join(''), escaped };
}

function isEscaped(view, index) {
  return view.escaped[index] === true;
}

function isPathContinuation(view, index) {
  const char = view.text[index];
  return char !== '/' && (isEscaped(view, index) || HOST_PATH_RIGHT_CONTINUATION_RE.test(char));
}

function hostPathFileSchemePrefix(view, slashRunEnd) {
  let prefixEnd = slashRunEnd;
  while (view.text[prefixEnd - 1] === '/') prefixEnd -= 1;
  const prefixStart = prefixEnd - 5;
  if (prefixStart < 0 || view.text.slice(prefixStart, prefixEnd).toLowerCase() !== 'file:') return false;
  for (let index = prefixStart; index < prefixEnd; index += 1) {
    if (isEscaped(view, index)) return false;
  }
  return prefixStart === 0
    || (!isEscaped(view, prefixStart - 1) && !/[A-Za-z]/.test(view.text[prefixStart - 1]));
}

function hostPathOptionFlag(view, start) {
  const letter = start - 1;
  const dash = start - 2;
  if (dash < 0 || view.text[dash] !== '-' || !/[A-Za-z]/.test(view.text[letter])
    || isEscaped(view, dash) || isEscaped(view, letter)) return false;
  return dash === 0 || (!isEscaped(view, dash - 1)
    && view.text[dash - 1] !== '/' && !HOST_PATH_RIGHT_CONTINUATION_RE.test(view.text[dash - 1]));
}

function hostPathLeftBoundary(view, start) {
  if (start === 0) return true;
  const previous = view.text[start - 1];
  if (isPathContinuation(view, start - 1)) return hostPathOptionFlag(view, start);
  if (previous !== '/') return true;
  return hostPathFileSchemePrefix(view, start);
}

function hostPathRightBoundary(view, end) {
  while (view.text[end] === '.' && !isEscaped(view, end)) end += 1;
  return view.text[end] === undefined || !isPathContinuation(view, end);
}

function hostPathMatchEnd(view, start, pattern) {
  let index = start + 1;
  for (let segmentIndex = 0; segmentIndex < pattern.segments.length; segmentIndex += 1) {
    const segment = pattern.segments[segmentIndex];
    segment.lastIndex = index;
    if (!segment.exec(view.text)) return -1;
    index = segment.lastIndex;
    if (segmentIndex === pattern.segments.length - 1) break;
    index = hostPathNextSegment(view, index);
    if (index === -1) return -1;
  }
  return index;
}

function hostPathNextSegment(view, index) {
  if (view.text[index] !== '/') return -1;
  index += 1;
  while (view.text[index] === '/' || (view.text[index] === '.' && view.text[index + 1] === '/')) {
    index += view.text[index] === '/' ? 1 : 2;
  }
  return index;
}

function hasProtectedHomeChildContinuation(view, end, pattern) {
  // A derived home must not mask a longer segment beside a known root.
  if (!pattern.derived || pattern.protectedChildren.length === 0) return false;
  const childStart = hostPathNextSegment(view, end);
  if (childStart === -1) return false;
  for (const child of pattern.protectedChildren) {
    child.lastIndex = childStart;
    if (child.exec(view.text) && isPathContinuation(view, child.lastIndex)) return true;
  }
  return false;
}

function mentionsInView(view, patterns) {
  for (let start = view.text.indexOf('/'); start !== -1; start = view.text.indexOf('/', start + 1)) {
    if (view.text[start + 1] === '/' || !hostPathLeftBoundary(view, start)) continue;
    for (const pattern of patterns) {
      const end = hostPathMatchEnd(view, start, pattern);
      if (end !== -1 && !hasProtectedHomeChildContinuation(view, end, pattern)
        && hostPathRightBoundary(view, end)) return true;
    }
  }
  return false;
}

// Undetected: paths outside known roots or homes; backslash and drive spellings;
// double encoding, non-ASCII escapes, and Unicode normalization variants.
export function mentionsHostRoot(text, roots, preparedPatterns = null) {
  if (typeof text !== 'string' || !Array.isArray(roots)) return false;
  const patterns = preparedPatterns || prepareHostRootPatterns(roots);
  const raw = { text, escaped: [] };
  if (mentionsInView(raw, patterns)) return true;
  return text.includes('%') && mentionsInView(decodeAsciiEscapes(text), patterns);
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
