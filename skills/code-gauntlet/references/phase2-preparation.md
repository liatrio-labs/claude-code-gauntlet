# Phase 2 Preparation Commands

## Contents

- [Phase 2 Composite A — pre-gather (status → checkout → SHA → prior-review gate → stale truncation)](#phase-2-composite-a--pre-gather-status--checkout--sha--prior-review-gate--stale-truncation)
- [Phase 2 Composite B — gather (diff, changed-files, line count, misc)](#phase-2-composite-b--gather-diff-changed-files-line-count-misc)
- [Discover REVIEW.md and stamp its raw text](#discover-reviewmd-and-stamp-its-raw-text)
- [Write the shared agent context file](#write-the-shared-agent-context-file)
- [Args object shape](#args-object-shape)

Read with [phase2-triage.md](phase2-triage.md) after the Phase 1 resolver result.
Use these literal recipes for target preparation and the raw-input handoff.

## Phase 2 Composite A — pre-gather (status → checkout → SHA → prior-review gate → stale truncation)

Generate the run nonce before this call and preserve it on resume. The status producer fetches and validates before exclusively creating `code-gauntlet-inputs-{nonce}/pr-description.json`. A valid existing snapshot is reused without fetching; malformed input stops preparation. Fetch failures before creation can be retried.

Run status → checkout → SHA → prior-review gate → stale truncation in one Bash call, in that order. Resolve the PR URL from its status receipt, since `origin` may be a fork. Substitute `{platform}`, `{pr_number}`, and `{plugin_root}` from Phase 1 before dispatch.

**GitHub PR Composite A:**

```bash
echo "=== status ==="
PR_STATUS=$({python} "{plugin_root}/scripts/pr_status.py" --platform github --pr {pr_number} --output-dir "{output_dir}" --snapshot-destination "code-gauntlet-inputs-{nonce}/pr-description.json") || exit 1
TARGET_SHA=${PR_STATUS%%$'\n'*}
TARGET_SHA=${TARGET_SHA#head_sha=}
CURRENT_SHA=$(git rev-parse HEAD)
echo "target=$TARGET_SHA current=$CURRENT_SHA"

echo "=== checkout ==="
if [ "$TARGET_SHA" = "$CURRENT_SHA" ]; then
  echo "already at target, no checkout needed"
elif [ "${CODE_GAUNTLET_HEADLESS:-}" = "1" ]; then
  echo "HEADLESS INPUT ERROR: working tree HEAD $CURRENT_SHA != PR head $TARGET_SHA"
  exit 1
else
  gh pr checkout {pr_number} || { echo "CHECKOUT FAILED: unable to checkout PR {pr_number}"; exit 1; }
fi

echo "=== sha ==="
HEAD_SHA_SHORT=$(git rev-parse --short=8 HEAD)
HEAD_SHA_FULL=$(git rev-parse HEAD)
echo "head_sha_short=$HEAD_SHA_SHORT"
echo "head_sha_full=$HEAD_SHA_FULL"

echo "=== pr_url ==="
PR_URL=${PR_STATUS#*pr_url=}
printf '%s\n' "$PR_URL"

echo "=== prior_review ==="
PRIOR_JSON=$({python} "{plugin_root}/scripts/detect_prior_review.py" --platform {platform} --pr-url "$PR_URL" --head-sha "$HEAD_SHA_FULL")
printf '%s\n' "$PRIOR_JSON"

echo "=== stale_truncate ==="
printf '%s\n' "$PRIOR_JSON" | {python} "{plugin_root}/scripts/stale_truncate.py" --output-dir "{output_dir}" --head-sha "$HEAD_SHA_SHORT"
```

The headless mismatch branch stops before SHA resolution, prior-review detection, or truncation can use the wrong commit.

**GitLab MR Composite A:**

```bash
echo "=== status ==="
PR_STATUS=$({python} "{plugin_root}/scripts/pr_status.py" --platform gitlab --pr {pr_number} --output-dir "{output_dir}" --snapshot-destination "code-gauntlet-inputs-{nonce}/pr-description.json") || exit 1
TARGET_SHA=${PR_STATUS%%$'\n'*}
TARGET_SHA=${TARGET_SHA#head_sha=}
CURRENT_SHA=$(git rev-parse HEAD)
echo "target=$TARGET_SHA current=$CURRENT_SHA"

echo "=== checkout ==="
if [ "$TARGET_SHA" = "$CURRENT_SHA" ]; then
  echo "already at target, no checkout needed"
elif [ "${CODE_GAUNTLET_HEADLESS:-}" = "1" ]; then
  echo "HEADLESS INPUT ERROR: working tree HEAD $CURRENT_SHA != PR head $TARGET_SHA"
  exit 1
else
  glab mr checkout {pr_number} || { echo "CHECKOUT FAILED: unable to checkout MR {pr_number}"; exit 1; }
fi

echo "=== sha ==="
HEAD_SHA_SHORT=$(git rev-parse --short=8 HEAD)
HEAD_SHA_FULL=$(git rev-parse HEAD)
echo "head_sha_short=$HEAD_SHA_SHORT"
echo "head_sha_full=$HEAD_SHA_FULL"

echo "=== pr_url ==="
PR_URL=${PR_STATUS#*pr_url=}
printf '%s\n' "$PR_URL"

echo "=== prior_review ==="
PRIOR_JSON=$({python} "{plugin_root}/scripts/detect_prior_review.py" --platform gitlab --pr-url "$PR_URL" --head-sha "$HEAD_SHA_FULL")
printf '%s\n' "$PRIOR_JSON"

echo "=== stale_truncate ==="
printf '%s\n' "$PRIOR_JSON" | {python} "{plugin_root}/scripts/stale_truncate.py" --output-dir "{output_dir}" --head-sha "$HEAD_SHA_SHORT"
```

**Branch Composite A:** first resolve the comparison base in its own Bash call. Type the printed hex into `{base_sha}` in Composite B; shell variables do not survive between calls.

```bash
{python} "{plugin_root}/scripts/resolve_branch.py" <<'CODE_GAUNTLET~BASE'
<base name exactly as chosen>
CODE_GAUNTLET~BASE
```

Resolve the target separately for the headless comparison. Type its printed hex into `{target_sha}` in the next composite and Composite B. Stop on either resolution failure.

```bash
{python} "{plugin_root}/scripts/resolve_branch.py" <<'CODE_GAUNTLET~BRANCH'
<branch name exactly as the user gave it>
CODE_GAUNTLET~BRANCH
```

Run this literal branch composite. The checkout heredoc stays outside command substitution so Bash 3.2 treats branch names as data.

```bash
echo "=== status ==="
CURRENT_SHA=$(git rev-parse HEAD) || exit 1

echo "=== checkout ==="
if [ "${CODE_GAUNTLET_HEADLESS:-}" = "1" ]; then
  if [ "$CURRENT_SHA" != "{target_sha}" ]; then
    echo "HEADLESS INPUT ERROR: working tree HEAD $CURRENT_SHA != branch head {target_sha}"
    exit 1
  fi
else
  if ! {python} "{plugin_root}/scripts/resolve_branch.py" --checkout <<'CODE_GAUNTLET~BRANCH'
<branch name exactly as the user gave it>
CODE_GAUNTLET~BRANCH
  then
    echo "Unable to checkout branch. The review requires the target code to be accessible locally."
    echo "You can checkout the branch manually and re-run the review."
    exit 1
  fi
fi

echo "=== sha ==="
HEAD_SHA_SHORT=$(git rev-parse --short=8 HEAD) || exit 1
HEAD_SHA_FULL=$(git rev-parse HEAD) || exit 1
echo "head_sha_short=$HEAD_SHA_SHORT"
echo "head_sha_full=$HEAD_SHA_FULL"

echo "=== stale_truncate ==="
{python} "{plugin_root}/scripts/stale_truncate.py" --output-dir "{output_dir}" --head-sha "$HEAD_SHA_SHORT" --unconditional
```

**Local Composite A:** run the branch composite's literal `sha` and `stale_truncate` sections, with no checkout or prior-review gate.

**`DEFERRED`:** the current SHA is the one a prior review covered. Run `{python} "{plugin_root}/scripts/stale_truncate.py" --output-dir "{output_dir}" --head-sha {head_sha_short} --unconditional` only after a "Yes — review again" answer; a "No — skip" answer stops with the files intact. An exit 2 truncated nothing; resolve it the same way. The gate's rationale and its four outcomes are in `scripts/gauntlet/stale.py`'s docstring.

After this call: interpret `prior_review`'s JSON per `references/phase1-preflight.md` → "Previously-Reviewed Gate" (branch order, question templates, degradations — unchanged). **Incremental** stores `last_reviewed_sha` for Composite B's incremental diff branch below. **Skip** stops the run here.

Stamp `reviewScope` from this resolved state before assembling the waist.
Local and branch targets stamp `{ requested: "full", kind: "full", since: null, commits: null, detector: null }`.
Headless PR/MR targets omit `requested` (see the derived waist fields in [SKILL.md: Assemble the args object](../SKILL.md#assemble-the-args-object)).
Interactive targets stamp the recorded gate answer, or `"full"` when no prior review existed.
PR/MR targets copy detector values verbatim into `detector`.
Copy `previously_reviewed`, `sha_resolvable`, `head_advanced`, `sha_is_ancestor`, and `incremental_safe`.
Set `error` to the first `prior_review.errors` value, or `null`.
Use `kind: "incremental"` only for an incremental request with `detector.incremental_safe: true`.
Set `since` to the detector's safe `last_reviewed_sha` only for incremental kind.
Set `since` to `null` otherwise.
Otherwise use `kind: "full"` and retain the detector.
The renderer derives the fallback explanation from retained detector facts.

> Headless mode: apply the prior-review policy in [headless-mode.md](headless-mode.md#prior-review-policy) before continuing.

All workflow-facing files use `{output_dir}/code-gauntlet-{purpose}-{head_sha_short}.{ext}` naming. The skill writes: `context-*.md` (shared agent context), `diff-*.patch` (unified diff), `files-*.json` (changed-file list), `project-rules-*.md` (REVIEW.md provenance and CLAUDE.md/AGENTS.md/QODO.md import resolution from `scripts/collect_project_rules.py`'s `--out`, folded into `context-*.md` by `scripts/write_shared_context.py` before measurement and writing). The run's own artifacts are `findings-*.json`, `report-*.md`, `post-review-*.json`, `checkpoint-all-*.json`, `patches-*.md` (Phase 8, `report_patches.py`), plus `persist-plan-*.json` on either derived `persist` path (see [SKILL.md: Assemble the args object](../SKILL.md#assemble-the-args-object)). On the default RETURN channel **Phase 8 writes them** (`materialize_artifacts.py`); on the writer paths the workflow's artifact-writer does. `scripts/stale_truncate.py` covers every purpose name in this list, including `persist-plan`, without needing an update per new artifact.

## Phase 2 Composite B — gather (diff, changed-files, line count, misc)

Once Composite A resolves (and, for PR/MR targets, any previously-reviewed question is answered), one Bash call gathers everything the args waist needs from disk — `files` gates the remaining sections, and `numstat` reads the patch saved by `diff`. **`files` runs before `diff`** so that the incremental path below — which must bound the diff to the files list — reads a list that already exists in this same call, rather than one the literal script hasn't produced yet:

```bash
echo "=== files ==="
if ! {python} "{plugin_root}/scripts/list_pr_files.py" --platform github --pr {pr_number} > "{output_dir}/code-gauntlet-files-{head_sha_short}.json"; then
  echo "files: FAILED"
  exit 1
fi
command cat "{output_dir}/code-gauntlet-files-{head_sha_short}.json"

echo "=== diff ==="
gh pr diff {pr_number} > "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"
head -c 200 "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"   # must start with "diff --git"; also confirm file size > 0

echo "=== numstat ==="
{python} "{plugin_root}/scripts/diff_numstat.py" "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"

echo "=== misc ==="
{python} -c "import datetime; print(datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'))"
{python} -c "import secrets; print(secrets.token_hex(8))"
```

On `files: FAILED`, stop and report the stderr reason (headless: non-zero outcome).

- **Files** → this path becomes `args.changedFilesPath`; keep the same array inline for `args.changedFiles` (the Summarize stage reads it by value — the workflow cannot open the file).
- **Context map** accompanies the framed context; Verify checks both digests before translating a context line.
- **Diff** → this path becomes `args.diffPath`, passed to the verify executor as `--diff-file`.
- **`numstat` counts the patch saved by the `diff` section with `scripts/diff_numstat.py`; `scripts/gauntlet/numstat.py`'s docstring holds the counting rules.** Check `binary_files > 0` before treating a low count as no diff. This feeds `changedLines`, and the 2k scan reads the same saved patch, never a fresh diff fetch. Never run a second `gh pr diff` to re-fetch it.
- **`misc`** gives a `generatedAt` candidate (re-stamp at args-assembly time if Phase 3 dispatch isn't immediate — `generatedAt` must reflect the actual assembly moment), and a `nonce` candidate matching `^[A-Za-z0-9._-]+$`.
- **Risk classification (2e)** and **AI-generated-code detection (2k)** — classify changed files by risk as in `references/phase2-triage.md`; this feeds the context file. Neither runs here.

**Incremental path** (Composite A's prior-review gate resolved Incremental): replace the `diff` section with:

```bash
{python} "{plugin_root}/scripts/incremental_diff.py" --files "{output_dir}/code-gauntlet-files-{head_sha_short}.json" --since {last_reviewed_sha} > "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"
```

Keep the `files` section's full server file list unchanged. The script reads that JSON to bound the diff; `changedLines` counts the saved patch. If the command fails or produces an empty patch, save the full server diff instead and disclose the fallback (see `phase2-triage.md` 2c branch 4).

**GitLab MR mode:** use `glab mr diff {pr_number}` in the `diff` section. Replace the `files` producer with:

```bash
if ! {python} "{plugin_root}/scripts/list_pr_files.py" --platform gitlab --pr {pr_number} > "{output_dir}/code-gauntlet-files-{head_sha_short}.json"; then
  echo "files: FAILED"
  exit 1
fi
command cat "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
```

**Branch comparison:** replace the `files` and `diff` sections with these pinned blocks using the resolved SHAs:

```bash
if ! {python} "{plugin_root}/scripts/list_pr_files.py" --git-range "{base_sha}...{target_sha}" > "{output_dir}/code-gauntlet-files-{head_sha_short}.json"; then
  echo "files: FAILED"
  exit 1
fi
command cat "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
```

```bash
if ! {python} "{plugin_root}/scripts/save_git_diff.py" --git-range "{base_sha}...{target_sha}" --output "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"; then
  echo "diff: FAILED"
  exit 1
fi
```

**Local changes:** replace the `files` and `diff` sections with these pinned blocks:

```bash
if ! {python} "{plugin_root}/scripts/list_pr_files.py" --git-range "HEAD" > "{output_dir}/code-gauntlet-files-{head_sha_short}.json"; then
  echo "files: FAILED"
  exit 1
fi
command cat "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
```

```bash
if ! {python} "{plugin_root}/scripts/save_git_diff.py" --git-range "HEAD" --output "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"; then
  echo "diff: FAILED"
  exit 1
fi
```

On `files: FAILED`, stop the handoff and report the stderr reason (headless: non-zero outcome).

If `gh pr diff` fails (e.g., 20K-line / 300-file API limit exceeded), the workflow's verify executor falls back to its own `git diff` chain inside an executor subagent (which has shell) — not in this composite.

**Composites A/B do not subsume 2d, 2g, or 2k.** Run 2d's collector as a separate script call after the changed-file list is saved; its receipt is the only REVIEW.md discovery source. Keep test discovery (2g) and AI-marker scanning (2k) as separate Glob/Grep tool calls under `references/phase2-triage.md`; never use `find` or `grep` from Bash for those searches.

## Discover REVIEW.md and stamp its raw text

1. **Project-rule inputs.** The script below collects CLAUDE.md, AGENTS.md, QODO.md, and REVIEW.md for shared context; Read REVIEW.md separately only for the raw waist in step 3, and never Read or Glob CLAUDE.md/AGENTS.md/QODO.md directly here.
2. **Run the collector first**, after 2c saves the changed-file list, as a standalone script call:

   ```bash
   {python} "{plugin_root}/scripts/collect_project_rules.py" --repo-root "$(git rev-parse --show-toplevel)" --out "{output_dir}/code-gauntlet-project-rules-{head_sha_short}.md" --changed-files "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
   ```

   Parse its one-line stdout JSON receipt and retain it; stop on failure instead of stamping a successful empty discovery. The script owns the repo-root, changed-file-directory, and ancestor walk. REVIEW.md text is copied with source provenance into review-rules blocks before project-rules blocks; the script does not parse its config fence. PROJECT_RULE_FILENAMES controls CLAUDE.md/AGENTS.md/QODO.md discovery, and REVIEW_RULE_FILENAME names the separate REVIEW.md source kind.
   Project-rule imports resolve relative to their containing file, within the repository, to Markdown targets, up to four hops; inline imports are recognized outside code spans and fences. The review pass does not follow imports or deduplicate scopes by content. A project import of a realpath already rendered as review-rules records `review_rules_source` and stops at that target. Direct project walk hits remain eligible. Other project imports, including out-of-walk REVIEW.md targets, remain project rules and do not add review scopes.
   Project rules remain additive: more specific directories win on conflict, with CLAUDE.md before AGENTS.md before QODO.md at equal specificity. REVIEW.md settings retain the separate pipeline precedence in `references/review-md-spec.md`.
3. **Stamp REVIEW.md raw inputs from the receipt.** Read exactly the paths in `receipt.review_md`, in receipt order, and stamp `args.reviewMd` as `[{ path, text, checksum }, ...]`, using each repo-relative path and the full raw file text, copying `checksum` from its receipt entry and `args.reviewMdSetChecksum` from `receipt.review_md_checksum`. Do not sort, walk directories, or parse the files yourself. An empty list stamps `[]`, an authoritative "discovered nothing", not an omitted field. Keep the text by value because the workflow has no filesystem. Set `reviewConfigPath` to the root REVIEW.md path when the list contains `REVIEW.md`, otherwise null.
   A capped REVIEW.md remains listed: Read its full raw text for settings, retain the cap gap, and do not add its text separately to the shared context. A failed raw Read stops this handoff; never replace it with empty text. See `references/review-md-spec.md` for format and hierarchy; `resolveReviewConfig` owns parsing and subtree settings.

Do not `Glob(pattern: "**/REVIEW.md")`; use the collector's receipt, not a second search. Never use `find` from Bash for locating these files. Never Read or Glob CLAUDE.md/AGENTS.md/QODO.md directly here.

Do not hand-parse or schema-validate REVIEW.md here — pass the raw text through. The workflow's
`resolveReviewConfig` (`workflows/src/args.js`) calls `buildReviewConfig` (`workflows/src/reviewConfig.js`), preserving a root layer
and depth-ordered subtree layers. Filter selects the root layer or matching subtree layers per
`finding.file`: thresholds override and `ignore` entries accumulate from root to the deepest match.
In particular, `resolveReviewConfig` never pins a numeric default for
`confidence_threshold` / `security_min_confidence` when REVIEW.md does not set one — the Filter
stage's own built-in defaults (non-security **55**, security **70**) apply exactly when absent.

**Do not stamp both `args.reviewMd` and `args.reviewConfig`** (or both `args.exclusionsText` and
`args.exclusionPatterns`) — the args waist refuses a waist that stamps both the raw and
pre-parsed form for the same axis (single authority).

## Write the shared agent context file

Run this after 2d, 2e and 2k. Set `{target_type}` to `pr`, `mr`, `branch` or `local`. For PR/MR, replace `{snapshot_args}` with `--snapshot-file "code-gauntlet-inputs-{nonce}/pr-description.json"`; otherwise omit it. Branch/local calls need no snapshot and never fetch descriptions:

```bash
{python} "{plugin_root}/scripts/write_shared_context.py" --output-dir "{output_dir}" --head-sha {head_sha_short} --target-type {target_type} --head-sha-full {head_sha_full} {snapshot_args} <<'CODE_GAUNTLET_TRIAGE_{nonce}'
{risk classification (2e) and AI-generated-code status (2k)}
CODE_GAUNTLET_TRIAGE_{nonce}
```

The quoted delimiter passes the text verbatim. Never pipe it through `echo` or unquote the delimiter.
The nonce in the delimiter keeps any line of the text from ending it.
The script writes the context file threaded to the discovery, validate and summarize agents from the collector's `--out` file, stdin text, framed unverified PR/MR claims when applicable, and saved diff, in that order.
Stamp the printed `contextLines`, `contextChars` and `removedTests` into the args waist verbatim, never estimated, carried over or re-derived.
An exit 2 wrote no usable context; stop and report its one-line error.
`scripts/gauntlet/shared_context.py`'s docstring holds the content and measurement rules.

Diff body rows have source gutters: `N7 +|text` for additions, `N7  |text` for head
context, and `O7 -|text` for deletions. The source text begins immediately after `|`.
Headers and metadata retain their original bytes. The writer also saves
`code-gauntlet-context-map-{head_sha_short}.json` beside the context. Its version-1
entries map physical context LF line numbers to file, side (`head` or `old`), and
source line. It hashes the final framed context and the original saved patch.
Malformed hunks keep the raw framed presentation and emit an unavailable, empty map
with a diagnostic. Both files must be written before a successful receipt.

The workflow's Summarize stage produces the change summary internally.

Discovery agents return findings through structured output (`agent()`/`parallel()` schema).

## Args object shape

```
{
  argsVersion: 1,
  mode: "interactive" | "headless",
  repoRoot: configResult.identity.repo_root, outputDir, headShaShort, nonce, generatedAt,
  python,
  diffPath, changedFilesPath, reviewConfigPath,
  removedTests: contextReceipt.removedTests,  // verbatim saved-diff inventory and checksum
  riskTable: [ ...{ path, risk } per changed file, from Phase 2e... ],  // REQUIRED, path set === changedFiles
  scopeAnswer: "light" | "full",  // interactive answer only when the trivial-scope question fires
  policy: { tier, subagentModel, provider, gateway, mappedAliases },
  ...configResult.waist,  // REQUIRED; copy verbatim from the resolver
  pluginRoot: configResult.identity.plugin_root,
  reviewScope: { requested: "incremental" | "full", kind: "incremental" | "full", since: string | null,
                 commits: integer | null, detector: null | { previously_reviewed, sha_resolvable,
                 head_advanced, sha_is_ancestor, incremental_safe, error } },  // REQUIRED
  limits: {},  // stamp {} unless a genuine REVIEW.md override exists
  delivery: { prIdentity },  // PR/MR targets only; see SKILL.md, "Assemble the args object"
                                             // the artifact-writer then persists postReview as the post_review-ready
                                             // wrapper { owner, repo, pr_number, sha, platform, web_origin, review_body, findings } so
                                             // Phase 8 posts it without hand-assembly

  // by-value inputs the in-memory stages need (the workflow has no disk):
  changedFiles, changedLines, baseBranch, reviewMd, reviewMdSetChecksum, exclusionsText,

  // the shared context file's measured size, from write_shared_context.py's receipt,
  // verbatim. contextChars may not be stamped without contextLines.
  contextLines, contextChars,

  // how the run's artifacts reach disk. `returnPrimaries: true` is the default and the
  // only path on which no model transcribes them — see phase2-triage.md, "Args Preparation". Omit `persist`
  // entirely to fall back to the legacy by-value writer; artifactPaths are the same either way.
  persist: {
    returnPrimaries: true
  },

  // verify handoff (sha-scoped) for the executor's pinned command:
  verify: {
    targetKind: "{target_kind}", // pr, mr, branch, or local, selected during preparation
    inputPathBase: "{output_dir}/code-gauntlet-phase4-input-{head_sha_short}",
    outputPathBase: "{output_dir}/code-gauntlet-phase4-output-{head_sha_short}"
  }
}
```
