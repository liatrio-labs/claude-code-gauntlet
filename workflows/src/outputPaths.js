import { normalizeAbsoluteRoot, pathUnderRoot } from './paths.js';

// A stable marker lets callers recognize path escapes without parsing prose.
export const PATH_ESCAPE_MARKER = 'path-escape';

export function requireAbsoluteOutputDir(outputDir) {
  const root = normalizeAbsoluteRoot(outputDir);
  if (root === null) {
    throw new Error(
      `outputDir must be an absolute confined root (POSIX /-prefix, no .. or \\ segments); got ${JSON.stringify(outputDir)}`,
    );
  }
  return root;
}

function assertPlannedPathUnderOutputDir(outputDir, path) {
  if (!pathUnderRoot(outputDir, path)) {
    throw new Error(`planned artifact path escapes outputDir: ${path}`);
  }
}

// Returns one reason naming all present fields outside outputDir, or null.
// Missing fields belong to the write-proof check; explicit empty values fail confinement.
export function pathEscapeReason(outputDir, fields) {
  const escaped = [];
  for (const { name, path, present } of fields) {
    if (!present) continue;
    if (!pathUnderRoot(outputDir, path)) {
      escaped.push(`${name}=${path == null ? String(path) : path}`);
    }
  }
  if (!escaped.length) return null;
  return `${PATH_ESCAPE_MARKER}: writer-reported paths outside outputDir (${escaped.join(', ')})`;
}

// Returns the four confined artifact paths; throws for an invalid root or a path escape.
export function plannedArtifactPaths(outputDir, sha) {
  const root = requireAbsoluteOutputDir(outputDir);
  const paths = {
    findings: `${root}/code-gauntlet-findings-${sha}.json`,
    report: `${root}/code-gauntlet-report-${sha}.md`,
    postReview: `${root}/code-gauntlet-post-review-${sha}.json`,
    checkpoints: `${root}/${checkpointPath('all', sha)}`,
  };
  for (const p of Object.values(paths)) assertPlannedPathUnderOutputDir(root, p);
  return paths;
}

// Returns the confined plan path; throws for an invalid root or a path escape.
// The plan is separate from the four public artifact paths.
export function persistPlanPath(outputDir, sha) {
  const root = requireAbsoluteOutputDir(outputDir);
  const path = `${root}/code-gauntlet-persist-plan-${sha}.json`;
  assertPlannedPathUnderOutputDir(root, path);
  return path;
}

// Returns a bare filename for the phase's persisted checkpoint.
export function checkpointPath(phase, sha) {
  return `code-gauntlet-checkpoint-${phase}-${sha}.json`;
}
