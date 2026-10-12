import { normalizeAbsoluteRoot, pathUnderRoot } from './paths.js';

// A stable marker lets callers recognize invalid writer path receipts without parsing prose.
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

// Collect writer-reported fields that fail the fence. Check ALL present fields (do not
// bail early) so the one gap can name every escape. Missing keys are left to write-proof
// (`{}` echo → "no write proof"); an explicit null/empty still fails the fence (gap, not
// TypeError). Persist remains all-or-nothing on partial().
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

// The four artifacts writeArtifacts plans (and asks the writer to echo). Exported so a
// faithful mock/recorder echoes the SAME paths the write-proof gate checks against — the
// gate rejects any echo that fails to account for all four planned paths.
// Every stamped path must remain under the absolute output directory.
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

// The persist plan's own path. Deliberately matches the Phase 2 stale-file truncation
// glob `code-gauntlet-*-<sha>.*`, so no skill change is needed to clean it up. It is
// NOT an artifactPaths key — the public contract stays at exactly four.
export function persistPlanPath(outputDir, sha) {
  const root = requireAbsoluteOutputDir(outputDir);
  const path = `${root}/code-gauntlet-persist-plan-${sha}.json`;
  assertPlannedPathUnderOutputDir(root, path);
  return path;
}

// --- Checkpoints ------------------------------------------------------------

// checkpointPath(phase, sha) -> bare filename for a phase's persisted checkpoint.
// The skill layer reads these on a rerun and injects the recovered outputs into
// the args waist (args.checkpoints); the pipeline has no disk access of its own.
export function checkpointPath(phase, sha) {
  return `code-gauntlet-checkpoint-${phase}-${sha}.json`;
}
