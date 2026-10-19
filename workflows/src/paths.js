import { escapeRegExp } from './wire.js';

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
  while (out.length > 1 && !/^[A-Za-z]:\/$/.test(out) && out.endsWith('/')) out = out.slice(0, -1);
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
  return (path.startsWith('/') || /^[A-Za-z]:\//.test(path)) && !hasBackslash(path) && !hasDotDotSegment(path);
}

function rootPrefix(root) {
  return root.endsWith('/') ? root : `${root}/`;
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

export function hostPlatformForRoots(input = {}) {
  return [input.repoRoot, input.outputDir, input.pluginRoot].some((root) =>
    typeof root === 'string' && /^[A-Za-z]:\//.test(root)) ? 'windows' : 'posix';
}

function pathComparisonKey(path) {
  return /^[A-Za-z]:\//.test(path) ? path.toLowerCase() : path;
}

const HOST_PATH_RIGHT_CONTINUATION_RE = /^[A-Za-z0-9._~+%@-]$/;
// A charset- and length-bounded id is printed so readers can find the finding; anything else uses the positional label.
const SAFE_FINDING_LABEL_RE = /^[A-Za-z0-9_.-]{1,64}$/;

const HOST_PATH_HEX_RE = /^[0-9A-Fa-f]{2}$/;
const HOST_PATH_HOME_RE = /^\/(?:Users|home)\/([^/]+)\//;

function hostPathSpellings(root) {
  const hasPrivatePrefix = root.slice(0, '/private/'.length).toLowerCase() === '/private/';
  const alias = hasPrivatePrefix
    ? root.slice('/private'.length)
    : `/private${root}`;
  return [root, alias];
}

function hostPathPattern(root, caseInsensitive) {
  const source = root.slice(1).split('/').map(escapeRegExp).join('/(?:/|\\./)*');
  return new RegExp(`/${source}`, caseInsensitive ? 'iy' : 'y');
}

function driveHostPathPatterns(root) {
  const drive = root[0];
  const tail = root.slice(3).split('/').map(escapeRegExp);
  return [
    new RegExp(`${drive}:/` + tail.join('/(?:/|\\./)*'), 'iy'),
    new RegExp(`${drive}:\\\\+` + tail.join('\\\\+'), 'iy'),
    new RegExp(`/${drive}/` + tail.join('/(?:/|\\./)*'), 'iy'),
  ];
}

export function prepareHostRootPatterns(roots) {
  const patterns = { roots: [], homes: [] };
  const derivedHomes = new Set();
  if (!Array.isArray(roots)) return patterns;
  for (const root of roots) {
    const normalized = normalizeAbsoluteRoot(root);
    if (normalized === null || normalized === '/') continue;
    if (/^[A-Za-z]:\//.test(normalized)) {
      patterns.roots.push(...driveHostPathPatterns(normalized).map((detect) => ({
        detect, rewrite: new RegExp(detect.source, detect.flags), backslash: true,
      })));
      const home = normalized.match(/^([A-Za-z]:\/Users\/[^/]+)\//i);
      if (home) derivedHomes.add(home[1]);
      continue;
    }
    const unaliased = normalized.startsWith('/private/') ? normalized.slice('/private'.length) : normalized;
    for (const spelling of hostPathSpellings(normalized)) {
      const caseInsensitive = spelling.slice(1).split('/').length > 1;
      patterns.roots.push({
        detect: hostPathPattern(spelling, caseInsensitive),
        rewrite: unaliased.slice(1).split('/').length > 1 ? hostPathPattern(spelling, false) : null,
        backslash: false,
      });
    }
    const home = unaliased.match(HOST_PATH_HOME_RE);
    if (home) derivedHomes.add(home[0].slice(0, -1));
  }
  for (const home of derivedHomes) {
    if (/^[A-Za-z]:\//.test(home)) patterns.homes.push(...driveHostPathPatterns(home).map((detect) => ({ detect, backslash: true })));
    else patterns.homes.push({ detect: hostPathPattern(home, false), backslash: false });
  }
  return patterns;
}

function decodeAsciiEscapes(text) {
  const chars = [];
  const escaped = [];
  const offsets = [];
  for (let index = 0; index < text.length;) {
    offsets.push(index);
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
  offsets.push(text.length);
  return { text: chars.join(''), escaped, offsets };
}

function isPathContinuation(view, index) {
  const char = view.text[index];
  return char !== '/' && (view.escaped[index] === true || HOST_PATH_RIGHT_CONTINUATION_RE.test(char));
}

function hostPathFileSchemePrefix(view, slashRunEnd) {
  let prefixEnd = slashRunEnd;
  while (view.text[prefixEnd - 1] === '/') prefixEnd -= 1;
  const prefixStart = prefixEnd - 5;
  if (prefixStart < 0 || view.text.slice(prefixStart, prefixEnd).toLowerCase() !== 'file:') return null;
  for (let index = prefixStart; index < prefixEnd; index += 1) {
    if (view.escaped[index] === true) return null;
  }
  return prefixStart === 0
    || (view.escaped[prefixStart - 1] !== true && !/[A-Za-z0-9+.-]/.test(view.text[prefixStart - 1])) ? prefixStart : null;
}

function hostPathOptionFlag(view, start) {
  const letter = start - 1;
  const dash = start - 2;
  if (view.text[dash] !== '-' || !/[A-Za-z]/.test(view.text[letter])
    || view.escaped[dash] === true || view.escaped[letter] === true) return false;
  return dash === 0 || (view.escaped[dash - 1] !== true
    && view.text[dash - 1] !== '/' && !HOST_PATH_RIGHT_CONTINUATION_RE.test(view.text[dash - 1]));
}

function hostPathLeftBoundary(view, start) {
  const previous = view.text[start - 1];
  if (isPathContinuation(view, start - 1)) return hostPathOptionFlag(view, start);
  if (previous !== '/') return true;
  return hostPathFileSchemePrefix(view, start) !== null;
}

function hostPathRightBoundary(view, end, backslash) {
  if (backslash && view.text[end] === '\\') return true;
  while (view.text[end] === '.' && view.escaped[end] !== true) end += 1;
  return !isPathContinuation(view, end);
}

function mentionsInView(view, patterns) {
  for (let start = 0; start < view.text.length; start += 1) {
    if (view.text[start] !== '/' && !(/^[A-Za-z]:[/\\]/.test(view.text.slice(start, start + 3)))) continue;
    for (const { detect: matcher, backslash } of patterns) {
      matcher.lastIndex = start;
      // Boundaries run only after a match: the left one rescans its slash run, which is quadratic per candidate.
      if (matcher.exec(view.text) && hostPathLeftBoundary(view, start)
        && hostPathRightBoundary(view, matcher.lastIndex, backslash)) return true;
    }
  }
  return false;
}

export function mentionsPreparedHostRoot(text, patterns) {
  if (typeof text !== 'string') return false;
  const matchers = [...patterns.roots, ...patterns.homes];
  const raw = { text, escaped: [] };
  if (mentionsInView(raw, matchers)) return true;
  return text.includes('%') && mentionsInView(decodeAsciiEscapes(text), matchers);
}

const REWRITE_CONTINUATION_RE = /[A-Za-z0-9._~+%@\-\p{L}\p{N}\p{M}]/u;
const REWRITE_TAIL_END_RE = /[\s"'`()\[\]{}<>|,;]/u;

function rewriteContinuation(text, index) {
  if (index < 0 || index >= text.length) return false;
  if (index > 0 && text.codePointAt(index - 1) > 0xFFFF) index -= 1;
  return REWRITE_CONTINUATION_RE.test(String.fromCodePoint(text.codePointAt(index)));
}

function rewriteRightBoundary(view, end, backslash) {
  const { text, escaped } = view;
  let next = end;
  while (text[next] === '.' && escaped[next] !== true) next += 1;
  if (next !== end && (text[next] === '/' || text[next] === '\\')) return false;
  return !rewriteContinuation(text, next)
    && (escaped[next] !== true || text[next] === '/' || (backslash && text[next] === '\\'));
}

function rawOffset(view, index) {
  return view.offsets?.[index] ?? index;
}

const LOCATION_SUFFIX_START_RE = new RegExp(LOCATION_SUFFIX_RE.source.slice(0, -1), 'y');

function repoRootMatch(view, roots, start) {
  if (view.text[start] !== '/' && !/^[A-Za-z]:[/\\]/.test(view.text.slice(start, start + 3))) return null;
  for (const { rewrite, backslash } of roots) {
    if (!rewrite) continue;
    rewrite.lastIndex = start;
    if (!rewrite.exec(view.text) || !rewriteRightBoundary(view, rewrite.lastIndex, backslash)) continue;
    const schemeStart = hostPathFileSchemePrefix(view, start);
    if (schemeStart === null && (view.text[start - 1] === '/' || view.text[start - 1] === '\\'
      || ((rewriteContinuation(view.text, start - 1) || isPathContinuation(view, start - 1))
        && !hostPathOptionFlag(view, start)))) continue;
    return { end: rewrite.lastIndex, backslash, schemeStart };
  }
  return null;
}

function rewrittenHostTail(text, view, start, end, backslash, roots) {
  const separator = (char) => char === '/' || (backslash && char === '\\');
  if (!separator(view.text[end])) return { end, replacement: './' };
  let stop = end;
  while (stop < view.text.length && (view.escaped[stop] === true || !REWRITE_TAIL_END_RE.test(view.text[stop]))) {
    if (view.text[stop] === ':') {
      LOCATION_SUFFIX_START_RE.lastIndex = stop;
      if (LOCATION_SUFFIX_START_RE.exec(view.text)) stop = LOCATION_SUFFIX_START_RE.lastIndex;
      break;
    }
    if (repoRootMatch(view, roots, stop)) break;
    stop += 1;
  }
  let tail = view.text.slice(end, stop);
  tail = tail.replace(backslash ? /[/\\]+/g : /\/+/g, '/');
  if (hasDotDotSegment(splitLocationSuffix(tail).path)) return { end: stop, replacement: null };
  const dots = tail.match(/\.+$/)?.[0] || '';
  tail = tail.slice(0, tail.length - dots.length);
  if (hasDotDotSegment(splitLocationSuffix(tail).path)) return { end: stop, replacement: null };
  if (rawOffset(view, stop) - rawOffset(view, start) !== stop - start) {
    while (separator(view.text[end])) end += 1;
    const rawTail = text.slice(rawOffset(view, end), rawOffset(view, stop));
    return { end: stop, replacement: /^\.*$/.test(rawTail) ? `./${rawTail}` : rawTail };
  }
  tail = tail.slice(1);
  return { end: stop, replacement: `${tail || './'}${dots}` };
}

export function rewritePreparedRepoRoot(text, roots) {
  if (typeof text !== 'string') return text;
  const chunks = [];
  const view = text.includes('%') ? decodeAsciiEscapes(text) : { text, escaped: [] };
  let copied = 0;
  for (let start = 0; start < view.text.length; start += 1) {
    const match = repoRootMatch(view, roots, start);
    if (!match) continue;
    const tail = rewrittenHostTail(text, view, start, match.end, match.backslash, roots);
    if (tail.replacement !== null) {
      chunks.push(text.slice(copied, rawOffset(view, match.schemeStart ?? start)), tail.replacement);
      copied = rawOffset(view, tail.end);
    }
    start = tail.end - 1;
  }
  chunks.push(text.slice(copied));
  return chunks.join('');
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
  const normalizedPath = normalizeAbsoluteRoot(path);
  if (normalizedPath === null) return false;
  const key = pathComparisonKey(normalizedRoot);
  const pathKey = pathComparisonKey(normalizedPath);
  return pathKey === key || pathKey.startsWith(rootPrefix(key));
}

export function repoRelativeFindingPath(repoRoot, file) {
  if (typeof file !== 'string' || file === '') return { reason: 'file must be a non-empty string' };
  // Slash normalization must expose location tails before they are separated from the path.
  const driveRoot = /^[A-Za-z]:\//.test(repoRoot || '');
  let normalized = file;
  let driveAbsolute = false;
  if (driveRoot) {
    if (/^[A-Za-z]:\//.test(file)) driveAbsolute = true;
    else if (/^[A-Za-z]:\\/.test(file)) {
      normalized = file.replace(/\\/g, '/');
      driveAbsolute = true;
    } else if (/^\/[A-Za-z]\//.test(file)) {
      normalized = `${file[1]}:${file.slice(2)}`;
      driveAbsolute = true;
    }
  }
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
  if ((path.startsWith('/') || driveAbsolute) && hasDotDotSegment(path)) return { reason: 'file path contains a .. segment' };

  let relative;
  if (!path.startsWith('/') && !driveAbsolute) {
    relative = normalizePathString(path);
    while (relative.startsWith('./')) relative = relative.slice(2);
  } else {
    const root = normalizeAbsoluteRoot(repoRoot);
    if (root === null) return { reason: 'repoRoot must be an absolute path without .. segments or backslashes' };
    const absolute = normalizePathString(path);
    if (pathComparisonKey(absolute) === pathComparisonKey(root)) return { reason: 'absolute file path resolves to repoRoot' };
    const prefix = rootPrefix(root);
    if (!pathComparisonKey(absolute).startsWith(pathComparisonKey(prefix))) return { reason: 'absolute file path is outside repoRoot' };
    relative = absolute.slice(prefix.length);
  }

  if (relative === '' || relative === '.') return { reason: 'file path does not name a repository file' };
  if (hasDotDotSegment(relative)) return { reason: 'file path contains a .. segment' };
  if (relative.startsWith('\\')) return { reason: 'file path starts with a backslash' };
  if (relative.split('/')[0] === '~') return { reason: 'file path starts with a home anchor' };
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\//.test(relative) || /(?:^|[/\\])[A-Za-z]:([/\\]|$)/.test(relative)) {
    return { reason: 'file path starts with a URI scheme or drive letter' };
  }
  // Controls can obscure any position; shaping characters only hide segment starts.
  if (/[\p{Cc}\p{Zl}\p{Zp}\u061C\u200E\u200F\u202A-\u202E\u2066-\u2069]/u.test(file)
    || file.split('/').some((segment) =>
      /^[\p{Cf}\p{Mn}\p{Me}\p{Default_Ignorable_Code_Point}\u2800\u115F\u1160\u3164\uFFA0]/u.test(segment))) {
    return { reason: 'file path contains a disallowed character' };
  }
  if (relative !== relative.trim()) return { reason: 'file path has leading or trailing whitespace' };
  return { file: `${relative}${suffix}` };
}


export function trustedScriptPath(pluginRoot, basename) {
  const root = normalizeAbsoluteRoot(pluginRoot);
  if (root === null) throw new Error('pluginRoot must be an absolute confined root');
  return `${rootPrefix(root)}scripts/${basename}`;
}
