# Phase 2 Triage Reference

Sub-steps, detection logic, and **args preparation** for Phase 2: Target, Triage & Args Preparation.

## Contents

- **2a** VCS platform detection — **2b** Working tree checkout — **2b-post** SHA + gitignore + previously-reviewed gate + stale cleanup — **2c** Review target + diff/changed-files save
- **2d** Project context (CLAUDE.md, REVIEW.md parse) — **2e** Risk classification
- **2g** Test discovery — **2h** Docs/specs — **2i** History context
- **2k** AI-generated code detection — **2l** Review dimensions
- **Shared agent context file** — **Args preparation** (the args waist the workflow consumes)
- **Triage announcement**

> **What moved into the workflow (v3).** The change summarizer (v2 steps **2f** and **2j**) is now the workflow's **Summarize** stage — Phase 2 no longer dispatches summarizer agents. The old two-batch "agents then file discovery" execution strategy is likewise gone; Phase 2 is now a straight-line context-and-args build, and the only agent dispatch is the single `Workflow` call in Phase 3. The 2f/2j content below is retained only to explain what the Summarize stage now does internally.

---

## 2a. Detect VCS Platform

Auto-detect from `git remote get-url origin`:

- GitHub → `gh` CLI, "PR"
- GitLab (including self-hosted) → `glab` CLI, "MR"

If detection fails, ask the user.

---

## 2b. Ensure Working Tree Reflects Review Target

Before running any diff commands, confirm the local working tree matches the review target. Use the `pr_number` resolved in Phase 1 — never extract PR numbers from branch names (branch names may contain upstream PR numbers that differ from the PR number in the current repo).

**This section is the canonical target-type table.** SKILL.md's "Phase 2 Composite A" `status`/`checkout` sections are one runnable instantiation of it (the PR/MR row, plus the headless row inlined as a real `exit 1`) — do not re-derive the branch/local variants or the checkout-failure message independently there; apply steps 1/3/4 below directly, per SKILL.md's cross-reference.

**1. Resolve the target's head SHA:**

- **PR/MR mode (`pr_number` set):** `gh pr view {pr_number} --json headRefOid --jq '.headRefOid'` (GitHub) / `glab mr view {pr_number} --output json | jq '.sha'` (GitLab)
- **Branch comparison:** `git rev-parse <branch>`
- **Local changes:** HEAD — no-op, already on correct state

**2. Compare against current HEAD:**

```
git rev-parse HEAD
```

If the SHA matches → proceed to 2c.

**3. If mismatch → checkout:**

| Target type | Command |
|---|---|
| PR/MR number or URL | `gh pr checkout <number>` (GitHub) / `glab mr checkout <number>` (GitLab) |
| Branch name | `git checkout <branch>` |
| Local changes | no-op |

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): never run any checkout command from
> this table — the harness pre-places the working tree at the pinned head. If the
> step-2 SHA comparison mismatches, print `HEADLESS INPUT ERROR: working tree HEAD
> <sha> != PR head <sha>` and stop with a non-zero outcome; never silently review
> different code. See `references/headless-mode.md`.

**4. If checkout fails → STOP immediately:**

```
Unable to checkout [branch/PR]. The review requires the target code to be accessible locally.
You can checkout the branch manually and re-run the review.
```

No fallback or workaround — a silently wrong working tree produces unreliable review results.

---

## 2b-post. Resolve Head SHA, Gitignore, Previously-Reviewed Gate, and Clean Stale Files

Now that the working tree reflects the review target, compute the short SHA and perform housekeeping. **These steps run as SKILL.md's "Phase 2 Composite A — pre-gather" — one Bash call whose sections form the genuine dependency chain below.** See SKILL.md for the full runnable script; this section explains the reasoning behind its ordering.

**1. Resolve head SHA** (`sha` section) — after checkout, so it reflects the actual PR HEAD, not whatever branch was checked out before.

**2. Ensure `{output_dir}` is ignored via `.git/info/exclude`** (`gitignore` section; skip if using env var override). Never append to the repo's tracked `.gitignore` — that silently dirties the reviewed repo's working tree with an undisclosed edit to a user file. `info/exclude` is repo-local, untracked, and shared across worktrees. Added after checkout to avoid stash/pop loss from `gh pr checkout` — if this ran before checkout, the exclude-file edit would be stashed and potentially lost. Disclose the outcome in the triage output (one line): either `.code-gauntlet/ excluded via .git/info/exclude` or, if the exclude file is unwritable, `note: .code-gauntlet/ is NOT ignored (info/exclude unwritable) — artifacts will show as untracked files` — never fall back to editing `.gitignore`.

**3. Previously-reviewed gate** (`owner_repo` + `prior_review` sections; PR/MR targets only — skip for `local`):

Runs the gate documented in `phase1-preflight.md` → "Previously-Reviewed Gate", which resolves whether this PR/MR was already reviewed and whether the incremental path is safe. `{owner}`/`{repo}` are resolved first from the PR's **own URL** — never the `origin` remote, which is the fork in a fork clone (`gh repo view` would resolve the current clone instead). GitLab: `glab mr view {pr_number} --output json | jq -r '.web_url'`, then take the path segments before `/-/merge_requests/`.

This runs **here, not in Phase 1**: the gate compares the last-reviewed commit against the PR head and counts the commits between them, so it needs the working tree at the review target and the PR's objects fetched. Before checkout it would measure whatever branch the session started on. By this point the checkout section has already switched the working tree to the PR and the gitignore section may already have written to `.git/info/exclude`; a Skip answer below stops the review but reverts neither — the tree stays checked out on the PR branch.

If the gate resolves to **Incremental**, store `last_reviewed_sha` — section 2c's incremental branch consumes it. If it resolves to **Skip**, stop the run here. Otherwise continue as a full review.

**4. Truncate stale files** (`stale_truncate` section) from prior sessions with the same SHA — prevents a re-run from blending old artifacts with new. This section is **conditional**, not unconditional: it truncates immediately unless the gate found `previously_reviewed: true` and `head_advanced: false` (the current SHA IS the SHA a prior review already covered), in which case truncation is deferred — printed as `DEFERRED` — until the "review again?" answer is known, since a "Skip — keep the existing review" answer must be able to leave those exact files untouched. Every other outcome (no prior review, prior review at an older SHA, or rewritten history) truncates files for *this* SHA, which could not already hold a prior review's output, so it is safe to fold into the same composite call. See SKILL.md's Composite A for the exact conditional and the unconditional-truncate follow-up run only after a "Yes — review again" answer.

---

## 2c. Identify Review Target

Use `target_type` and `pr_number` from Phase 1's "Resolve review target" step. Do not re-derive the PR number here.

1. **PR/MR mode** (`pr_number` set) — Use `gh pr view {pr_number}`/`glab mr view {pr_number}` + diff commands. Get full SHA: `git rev-parse HEAD`
   - **GitHub (PR):** Gather the file list with `gh pr diff {pr_number} --name-only`. Gather the full diff with `gh pr diff {pr_number}`.
   - **GitLab (MR):** Gather the file list with `glab mr diff {pr_number} --name-only`. Gather the full diff with `glab mr diff {pr_number}`.
2. **Branch comparison** — `git diff <base>...HEAD` and `git diff --name-only <base>...HEAD`
3. **Local changes** — `git diff HEAD` (or `git diff --cached` if nothing unstaged)
4. **Incremental** (2b-post step 3's gate resolved "Incremental" and stored `last_reviewed_sha`; PR/MR mode only) — reuses branch 1's server-computed `--name-only` file list (never branch 1's diff content) and diffs only those files: `git diff {last_reviewed_sha}..HEAD -- <that file list>`. Do **not** use the unbounded `git diff {last_reviewed_sha}...HEAD` / `git diff --name-only {last_reviewed_sha}...HEAD` form: `incremental_safe` guarantees `{last_reviewed_sha}` is an ancestor of HEAD, so the three-dot form collapses to two-dot, and unbounded it would include every commit since the last review — including an unrelated base-branch merge (e.g. `git merge main`) that pulls in files the PR never touched. Same validation rules as below (non-empty, starts with `diff --git`); note the residual limitation: a base merge that also touches a PR file still shows up. If the diff fails or is empty, fall back to branch 1's full server diff and disclose the fallback. Record the incremental scope (`last_reviewed_sha`) in the triage announcement and the Phase 8 methodology.

**Save the diff and the changed-file list (the workflow has no git access):** Persist both git-derived inputs to disk so the workflow can consume them.

1. **Changed files** → `{output_dir}/code-gauntlet-files-{head_sha_short}.json` as a JSON array (this path becomes `args.changedFilesPath`). Keep the same array inline for `args.changedFiles` — the Summarize stage reads it by value, because the workflow cannot open the file. On the incremental path, this stays branch 1's full server `--name-only` list unchanged — the same list branch 4 uses to bound the diff, never a narrower incremental-only set. Saved **first**, so branch 4's incremental diff (below) has a file list already on disk to bound against.
2. **Diff** → `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch`. In PR/MR mode use the server-computed, fork-safe diff (or, when 2b-post step 3 resolved incremental, branch 4's file-bounded `{last_reviewed_sha}..HEAD -- <file list>` diff, read back from the JSON just written above); for branch/local targets use `git diff`. This path becomes `args.diffPath` and is passed to the verify executor as `--diff-file`.

**These saves run as SKILL.md's "Phase 2 Composite B — independent-gather"** — one Bash call whose `files`/`diff`/`numstat`/`misc` sections are mutually independent on the default path (`files` runs first specifically so the incremental path's bounded diff has a file list to read). See SKILL.md for the exact script.

`changedLines` (threaded into `args.changedLines`, Args Preparation below) must be counted from the diff actually saved above — the incremental diff on the incremental path, never branch 1's full-PR diff — since it feeds the 2e trivial/light-scope gate and the Summarize bucketing threshold. Composite B computes it **against the patch file already saved by the same call's `diff` section** with **hunk-state tracking**, never `git apply --numstat` and never a bare line-prefix test: `git apply` refuses a range of inputs that are still valid, countable diffs (a patch that no longer applies cleanly, renames, mode-only changes), and on refusal its piped `awk` consumer previously printed a silent, plausible `0` instead of failing loud. A bare prefix test (treating any line starting with `---` or `+++` followed by a space as a file header) is also wrong: a REMOVED content line whose own text begins `--` followed by a space appears in the patch as `---` followed by a space, and an ADDED line whose text begins `++` followed by a space appears as `+++` followed by a space — both then read as a header and get skipped, undercounting `changed_lines` (markdown front matter and diffs-of-diffs hit this routinely). The scan instead tracks hunk state directly — `@@` enters a hunk, `diff --git` leaves it, and only lines inside a hunk are counted as added/removed — the same arithmetic `git diff --numstat` does per-file, verified to agree with it on content lines beginning `--`/`++` followed by a space, markdown `---`/`+++` delimiters, a patch-of-a-patch, a binary file, a rename, a mode-only change, a no-trailing-newline file, CRLF endings, and an empty diff. The scan also reports a separate `binary_files` count so a binary-heavy diff's low/zero textual count isn't mistaken for "no diff." Never a second `gh pr diff` invocation to re-fetch content already on disk. The same rule holds for 2k below: the AI-generated-code scan reads this same saved patch, not a fresh diff fetch.

Validate the saved diff before relying on it:

- Non-empty (file size > 0)
- Starts with `diff --git` (confirms it is a valid unified diff, not an error message)

If `gh pr diff` fails (e.g., 20K-line / 300-file API limit exceeded), the workflow's verify executor falls back to its own git diff chain — but note that fallback runs inside an executor subagent (which has shell), not in the script. For **branch comparison** and **local changes** target types, produce the diff with `git diff <base>...HEAD` / `git diff HEAD` and the file list with `git diff --name-only`.

Check for `docs/`, `specs/`, `research/` directories and `REVIEW.md`, `CLAUDE.md` at repo root and in directories with changed files.

---

## 2d. Gather Project Context

1. **CLAUDE.md** — Read from repo root and directories with changed files.
2. **REVIEW.md** — Discover hierarchically. See `references/review-md-spec.md` for format, scaffolding templates, and hierarchy rules. REVIEW.md lets maintainers customize focus areas, skip patterns, custom rules, thresholds, and ignore patterns.
3. **AGENTS.md / QODO.md — resolved by `scripts/collect_project_rules.py`, never `Read` directly.** A plain `Read` of a repo's CLAUDE.md does not expand Claude Code's `@path` import directive — verified empirically — and Anthropic's own docs tell AGENTS.md-using repos to write exactly that: a CLAUDE.md whose entire body is an import pointer. Measured against the benchmark mirror repos at current HEAD: sentry's and grafana's root CLAUDE.md is the identical 11-byte string `@AGENTS.md\n`; discourse's is the 40-byte inline pointer `See @AI-AGENTS.md for all instructions.\n`. A plain `Read` returns that literal pointer text as the entirety of "project rules" for three of five repos, silently — and a fourth hardcoded filename would still miss discourse's arbitrary `AI-AGENTS.md` target. Resolving the pointer, not naming more files, is the fix.

   Invoke the script directly (a standalone script call, not `python3 -c` JSON assembly), after 2c has saved the changed-files list:

   ```
   python3 "{plugin_root}/scripts/collect_project_rules.py" --repo-root "$(git rev-parse --show-toplevel)" --out "{output_dir}/code-gauntlet-project-rules-{head_sha_short}.md" --changed-files "{output_dir}/code-gauntlet-files-{head_sha_short}.json"
   ```

   `PROJECT_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "QODO.md")` is the one place a source name is added. QODO.md is discovered on identical terms as maintainer-authored review-tool metadata (distinct from Qodo's own `.pr_agent.toml` behavior config) — no repo in the benchmark set has exercised that path yet, but the script does not special-case it. **REVIEW.md is deliberately not a source here** — it has its own structured parse path (step 2 above, `references/review-md-spec.md`) and its own precedence semantics; this script does not duplicate it.

   The script follows `@path` imports found in any discovered file (recognized inline mid-sentence, not only on a standalone line; skipped inside code spans and fenced code blocks; relative paths resolve against the containing file's own directory — matching Claude Code's real import contract up to its 4-hop depth cap), confines every resolved target inside the repo via `realpath`, and requires it to be `.md`. Every skip or refusal (outside the repo, non-markdown, over a byte/depth cap, a cycle) is recorded, never silent; stdout is exactly one line of JSON — the provenance receipt — on every path, including failure. The assembled markdown block itself goes to `--out`, not stdout.

   **Precedence.** Sources accumulate — CLAUDE.md, AGENTS.md, and QODO.md text are additive rule content, not competing settings, so every discovered file's content is included, not just the first found. A directory-level file's rules apply to that subtree. On a direct conflict between two rules, the more specific directory wins; at equal specificity, CLAUDE.md wins over AGENTS.md over QODO.md, matching `PROJECT_RULE_FILENAMES`'s declared order. This is separate from REVIEW.md's own precedence (`references/review-md-spec.md` → Hierarchy), which this script does not touch.

**Tool instructions for file discovery:**

Use **Glob** to find all CLAUDE.md and REVIEW.md files:

```
Glob(pattern: "**/CLAUDE.md")
Glob(pattern: "**/REVIEW.md")
```

Never use `find` from Bash for locating these files.

### REVIEW.md Detection

Complete this check before proceeding to 2e. REVIEW.md settings cascade to all thresholds, rules, and ignore patterns for the entire review.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): skip both REVIEW.md-setup prompts below (the "No REVIEW.md found" build-review-md suggestion and the subdirectory-REVIEW.md `AskUserQuestion`). Root config applies to all directories; never invoke `build-review-md`. REVIEW.md is read-only in headless mode — the hierarchical parse still runs, but no REVIEW.md is created. See `references/headless-mode.md`.

Find all CLAUDE.md locations, check each for a matching REVIEW.md:

- **No REVIEW.md anywhere:**

  ```
  No REVIEW.md found. For a guided setup, run build-review-md first, then restart the review. Or continue without one.
  ```

- **Root exists but subdirectory CLAUDE.md has no matching REVIEW.md:**

  ```
  AskUserQuestion(
    questions: [{
      question: "Found REVIEW.md at repo root, but {directory} has a CLAUDE.md without a matching REVIEW.md. A subdirectory REVIEW.md lets you set different review standards for this area. Create one?",
      header: "Subdirectory REVIEW.md",
      multiSelect: false,
      options: [
        { label: "Yes — create it", description: "Inherits root settings, adds directory-specific rules" },
        { label: "Not now — root config applies", description: "Use root REVIEW.md settings for all directories" }
      ]
    }]
  )
  ```

- **All locations covered** → proceed.

See `references/review-md-spec.md` section Discovery for the full prompts and scaffolding templates. Merge configs hierarchically: settings override, rules and patterns accumulate.

---

## 2e. Classify Changed Files by Risk Level

- **High risk** — auth, security, payment, data access, public APIs, DB migrations, crypto, infra/deploy, permission/RBAC. Also >200 lines changed. Also: files implementing a cache, proxy, decorator, or delegation pattern (caching proxies are a common source of recursive delegation and stale-data bugs — flag these even if the diff appears mechanical).
- **Medium risk** — business logic, services, controllers, middleware, state management. 50-200 lines changed.
- **Low risk** — tests, docs, config, generated code, lockfiles, formatting-only. <50 lines changed.

High-risk files get expanded context (callers, callees, related tests); low-risk get lighter review.

**Content-change promotion.** After initial classification, check LOW-risk files for substantive content changes — any diff line that changes a string value, numeric value, or identifier (not just formatting, whitespace, markup, or delimiters). Files with substantive content changes get promoted to MEDIUM. This is type-agnostic.

Promotion triggers: i18n text changes, config value changes, CSS/SCSS numeric changes, changed string literals or identifiers.
Stay LOW: lock files, whitespace-only changes, generated code updates, tag case changes (`<br/>` → `<br />`).

### Light Review for Trivial PRs

If ALL files are low-risk AND total lines <50, ask Light review vs Full review (template in `references/phase1-preflight.md`). Skipped when REVIEW.md sets `focus`. A `light` answer stamps `agentFlags: { deep: false }`, which the Discover stage honours by dispatching only the two core agents (`bug-detector`, `security-reviewer`); `full` stamps `{}` and runs all seven. Announce the actual dimension set — `bugs, security` for light, the full list for full.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): do not ask — use `$CODE_GAUNTLET_TRIVIAL_SCOPE` (`light` stamps `agentFlags: { deep: false }` → bugs+security only, `full` stamps `{}` → all dimensions). At args assembly, **re-read the variable with a fresh `echo`** — never recall its value from earlier context (a live run recalled `full` while the actual value was `light`). See `references/headless-mode.md` and the assembly rule in SKILL.md.

---

## 2f. Change Summary (now the workflow's Summarize stage)

The semantic change summary is no longer produced in Phase 2. The workflow's **Summarize** stage dispatches the `change-summarizer` agent internally (its model comes from `resolvePolicy` — Sonnet) and threads the result to the report writer. The skill neither dispatches a summarizer nor writes the summary into the context file.

For reference, the Summarize stage produces a 3–5 sentence summary of what the change *claims* to do, its rationale, and its risk profile, framed strictly as claims (never "clean", "correct", "safe", "straightforward", "trivial", or "verbatim" — the summary must never conclude a refactoring is correct). The change-summarizer agent definition holds the authoritative framing rules.

**Large changes.** For >500-line changes that also span more files than one summarize bucket, the Summarize stage fans out per-file buckets through `parallel()` and stitches the partials with a single merge call — again, internal to the workflow, not a Phase 2 dispatch. (This subsumes the old 2j file-level summarization step.)

---

## 2g. Related Test Discovery

For each changed production file, find test files by convention (`Tests`, `.test`, `.spec`, `_test`, `_spec` patterns; `tests/`, `__tests__/`, `spec/` directories). Include in context for bug-detector and test-analyzer.

**Tool instructions:**

Use **Glob** to find test files. Pattern examples:

- `**/*.test.js`, `**/*.test.ts` (Jest/Vitest style)
- `**/*.spec.js`, `**/*.spec.ts` (Jasmine/Mocha style)
- `**/tests/**/*.py`, `**/__tests__/**/*.py` (Python)
- `**/*_test.go`, `**/*_test.rs` (Go/Rust)

Example:

```
Glob(pattern: "**/*.test.{js,ts,py}")
Glob(pattern: "**/__tests__/**/*")
Glob(pattern: "**/tests/**/*")
```

Never use `find` or `grep` from Bash for test discovery.

---

## 2h. Docs/Specs Context

If `docs/`, `specs/`, `research/` exist, read relevant files. Send only to conventions-and-intent agent and Phase 8 report generation — NOT all agents (avoids biasing toward confirming intent rather than finding bugs).

**Tool instructions for file discovery:**

Use **Glob** to find documentation and specification files:

```
Glob(pattern: "docs/**/*.md")
Glob(pattern: "specs/**/*.md")
Glob(pattern: "research/**/*.md")
```

Then use **Read** to load relevant files for each changed file's directory. Never use `find` from Bash for locating docs/specs.

---

## 2i. History Context Preprocessing

**Deterministic preprocessing, not an LLM agent.** For each changed file:

1. `git log --oneline --max-count=50 -- <file>` for recent change history
2. `git blame` on changed line ranges (used by the Verify stage's `verify_findings.py` executor for new/surfaced classification)

Distribute: bug-detector gets history context; conventions-and-intent gets pattern drift context.

---

## 2j. File-Level Summarization (now internal to the Summarize stage)

Per-file summarization for large changes is no longer a Phase 2 dispatch — it is the bucket fan-out described in 2f above. When the change exceeds 500 lines and spans more files than one summarize bucket, the workflow's Summarize stage fans out one `change-summarizer` call per per-file bucket through `parallel()` and stitches the partials with a merge call. There is no separate 2j step and no "agents then file discovery" batching to arrange — the only agent dispatch the skill makes is the single `Workflow` call in Phase 3.

---

## 2k. AI-Generated Code Detection

Scan for AI co-author trailers, attribution comments, AI tool metadata. **Elevate AI-generated files one risk level** (research shows 75% more logic errors in AI-authored code). Include AI-generation status in risk classification sent to all agents.

**Tool instructions:**

Use **Grep** to search for AI co-author indicators in changed files:

- Git trailers: `Co-Authored-By`, `Co-authored-by`, `Copilot-By`
- Comments: patterns like `AI-generated`, `generated by`, `GPT`, `Claude`, `Copilot`, `ChatGPT`
- Metadata: language-specific markers (e.g., `<!-- AI generated -->`, `# AI generated`)

Example:

```
Grep(pattern: "Co-[Aa]uthored-[Bb]y|Copilot-By", type: "text", glob: "**/*.py")
Grep(pattern: "AI-generated|generated by (GPT|Claude|Copilot|ChatGPT)", glob: "**/*.{js,ts,py}")
Grep(pattern: "<!-- AI|# AI generated|// AI generated", glob: "**/*.{js,ts,py,md,html}")
```

Also run the same patterns directly against the saved diff — `Grep(pattern: "...", path: "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch")` — to catch markers in added lines the working-tree scan above might miss. This patch is already on disk from Composite B's `diff` section (2c); never invoke a fresh `gh pr diff` here to re-fetch it.

Never use `grep` or `find` from Bash for AI detection.

---

## 2l. Determine Review Dimensions

All on by default unless REVIEW.md disables them. All agents use Sonnet except security-reviewer (always Opus) — the single benchmarked model policy.

Skip conditions: test-analyzer (no test files in repo), type-design-analyzer (no new types).

---

## Shared Agent Context File

The workflow's **summarize, discovery, and validate** agents Read a shared context file. The workflow threads exactly this path to them: `{output_dir}/code-gauntlet-context-{head_sha_short}.md`. The skill must write it there before the Phase 3 `Workflow` call, or the agents' "Read the shared context" step hits a missing file. The **challenger** is not in that list and never has been — it is structurally blind (it receives only a finding's title, description, and location and opens the code itself), so the Challenge stage is given no context path at all.

Write it with `python3 -c "import json; ..."`. Contents, concatenated in this order into one `content` string:

1. CLAUDE.md / REVIEW.md project rules (2d steps 1–2, gathered by value).
2. AGENTS.md / QODO.md project rules, resolved by `scripts/collect_project_rules.py` (2d step 3) and folded in via `open(path).read()` on its `--out` file, **inside this same `python3 -c` invocation** — never retyped by the model. CLAUDE.md's "Artifact persistence" section records the artifact-writer's transcription of a multi-KB payload diverging from its input on 3 of 3 measured runs; hand-copying this block into `content` risks the identical failure one stage earlier, for no reason, when the file is already on disk to `open()`.
3. Risk classification (2e) and AI-generated-code status (2k).
4. The full diff inside `<untrusted-code-content>` tags. Raw diff lines only — never substitute a summary for changed content; evidence destroyed during summarization cannot be recovered by agents.

**Do not guard the read in step 2.** The `open(path).read()` on `collect_project_rules.py`'s `--out` file must be unconditional — no `try`/`except`, no `os.path.exists()` check, no empty-string fallback if it raises. A missing rules file means the collection step (2d step 3) did not run; that must fail the context-file write, because a context file with a silently empty rules section is indistinguishable from a repo that genuinely has no convention files. This is the same file-based handoff already established for the diff: SKILL.md's `diff` section writes it with `gh pr diff {pr_number} > "{output_dir}/code-gauntlet-diff-{head_sha_short}.patch"`, and its `numstat` section reads it back with a plain, unguarded `open(path, 'r', errors='replace')` — no existence check there either. The rules block follows that precedent, not a new one.

This is a deliberate asymmetry with `contextLines`/`contextChars` below, which *do* degrade to a disclosed gap rather than fail. An unmeasured context file still IS the context file, just one whose size is unknown — hard-failing there would trade a partial-but-usable read for a dead run. The rules file has no such partial-but-usable state: `collect_project_rules.py` always succeeds and always writes a file: a repo with no CLAUDE.md/AGENTS.md/QODO.md anywhere yields a clean `ok:true` and an **empty** `--out` file, not a missing one. A missing file is therefore unambiguously "the collection step did not run" — never a legitimate state a well-behaved run can produce — which is exactly what makes hard-failing correct here and wrong there.

**What this does not cover.** No test can force a model-executed Phase 2 to actually invoke `collect_project_rules.py` before writing the context file — that is a live-execution property, not a static one, and a test asserting only that this doc mentions the script would be the "phrase count" guard CLAUDE.md's own design doctrine forbids (a wording-defeated guard is already on record elsewhere in this file: an earlier `contextPath` guard was defeated in a single edit by rewording, whole suite green). The unconditional `open()` above is the guard that exists: if the collection step was skipped, the write fails loudly instead of producing a plausible-looking, silently rules-less context file. It proves nothing about whether the step ran — only that if it didn't, the run cannot quietly proceed as though it had.

**Ordering is load-bearing, not cosmetic.** All four pieces above must be concatenated into `content` — the exact string measured for `contextLines`/`contextChars` below — *before* that measurement runs, never appended to the file afterward. `contextReadPlan` sizes every agent's `Read` plan strictly from those two stamped numbers; a project-rules block folded in after the count is taken is a block those numbers don't describe, so the plan built from them stops short of the file's true end — silently reopening issue #48 for exactly the reason a partial `Read` looks identical to a complete one. Build the full `content` string first, in the order above, *then* measure it, *then* write it — never write-then-append, and never measure a string that is still missing a piece.

The **change summary** is no longer written into the context file — the workflow's Summarize stage produces it internally and threads it to the report writer. The NDJSON `## Validator` section is likewise dropped: v3 agents return findings through structured output, not by appending NDJSON, so there is no per-agent validator step to record. (The emission machinery still ships — its removal is the deferred S8 migration.)

### Measure it (issue #48)

A single `Read` of this file returns only **part** of it and carries **no truncation notice** — the partial result is indistinguishable from a complete one. Measured on run `wf_cef39739-577`: all 7 discovery agents' first `Read` of a 95,057-byte / 2,028-line context file came back as 58,145 chars ending at line 1083. Six agents inferred the cutoff and paginated to the end; `security-reviewer` did not, and reviewed roughly the first half of the diff while returning `complete: true`.

The workflow has no disk, so it cannot measure the file. The skill must, **in the same `python3 -c` invocation that writes it**, and stamp the result into the args waist as `contextLines` / `contextChars`:

```python
lines = content.count("\n") + (0 if content.endswith("\n") else 1)
print(json.dumps({"contextLines": lines, "contextChars": len(content)}))
```

`contextLines` counts as the Read tool's `cat -n` numbering does: a file with no trailing newline still displays its final partial line, so it counts. Do **not** substitute `wc -l`, which counts newline *terminators* and therefore reports one fewer for exactly that case — an undercount by one drops the file's last line from every agent's read plan, silently. `contextChars` is `len(content)` and is advisory: it only narrows the per-call chunk size when lines are long, so a small code-point-vs-UTF-16 divergence between the runtimes is harmless.

If the file came out **empty**, omit both fields — test `not content`, not `lines == 0`: the formula returns `1` for empty content (`"".count("\n") + 1`), so a naive zero-check never fires and the waist would happily accept `{"contextLines": 1, "contextChars": 0}`, telling every agent the shared context is a single line. An empty shared context is a Phase 2 bug to fix, not a value to pass on.

`contextReadPlan` (`workflows/src/stages.js`) turns the pair into the exact `Read(offset, limit)` calls covering the file, and `sharedContextLine` enumerates them in the Summarize, Discover, and Validate prompts. **Both fields are optional**: stamp neither and every prompt degrades to fixed 750-line stepping with no known terminus, and the run reports a `context_unmeasured` gap. That is a real degradation — it puts end-detection back in the agent's hands — so it is disclosed, never silent. Optionality exists because this step is model-executed and can be skipped; it is not a licence to skip it. Stamping `contextChars` without `contextLines` is rejected at the waist (chars alone cannot size a line-offset plan), as is any non-positive or fractional value.

---

## Args Preparation

Assemble the args waist the workflow consumes. It is a single JSON object passed as the `Workflow` tool's `args` parameter (Phase 3) — not written to disk. The workflow validates it up front (`validateArgs`) and rejects a malformed waist before any dispatch.

**Omit optional fields you have no value for — never stamp an explicit `null`.** The waist tolerates an explicit `null` as equivalent to absent for `reviewConfig`, `exclusionPatterns`, `delivery`, and `checkpoints`, but omitting is the norm: a live run once stamped `reviewConfig: null` and paid a 21.3s round trip re-deriving it before dispatch. Two fields are the opposite case — `null` there is a meaningful value, not a stand-in for absent: `reviewConfigPath: null` (no REVIEW.md found — pure provenance) is fine, and `limits.deliveryCap: null` (uncapped delivery) is an explicit choice, not an oversight — do not "fix" either one away.

**Required fields (`validateArgs` fails loud without them):**

| Field | Value |
|---|---|
| `argsVersion` | `1` |
| `mode` | `"headless"` under `CODE_GAUNTLET_HEADLESS=1`, else `"interactive"` |
| `repoRoot` | `git rev-parse --show-toplevel` |
| `outputDir` | resolved `{output_dir}` (absolute) |
| `headShaShort` | `head_sha_short` from 2b-post |
| `nonce` | freshly generated, matching `^[A-Za-z0-9._-]+$` (interpolated into the verify executor argv per slice — no whitespace/shell metacharacters) |
| `generatedAt` | current wall-clock as an ISO8601 string — the workflow's injected clock (it never calls `new Date()`) |
| `diffPath` | `{output_dir}/code-gauntlet-diff-{head_sha_short}.patch` |
| `changedFiles` | the changed-file array, by value (Summarize bucketing; the workflow has no disk access) |
| `changedLines` | total changed line count, by value (Summarize bucketing threshold) |
| `agentFlags` | scope-gating flag map (opt-out): `{}` for full scope (all dimensions on), `{ deep: false }` for light scope (bugs+security only). Values must be booleans; only literal `false` disables |
| `policy` | `{ tier, subagentModel }` — see below |
| `limits` | `{ summarizeBucketSize: 20, validateBatch: 25, challengeCap: 40, verifySliceSize: 200, deliveryCap }` (override from REVIEW.md if set) |
| `delivery` | `{ tier: "all" \| "main_only" }` — the Phase 8 PR-comment tier (default `all`); optional (absent ⇒`all`) |

`limits.deliveryCap` is the Phase 8 PR-comment cap, threaded from `CODE_GAUNTLET_PR_COMMENT_CAP` (the same knob echoed as `pr_comment_cap`; headless default `6`, bench `25`) — the **workflow cannot read `process.env`**, so passing it through the waist is the only path. `delivery.tier` is the Phase 8 delivery tier from the Phase 1 answer (interactive) or `CODE_GAUNTLET_DELIVERY_TIER` (headless, default `all`); same env-blindness, same reason it rides the waist. The Challenge stage hands every survivor to the workflow's `selectDelivery(survivors, deliveryCap, tier)`, which applies the tier (`all` keeps every survivor, `main_only` keeps main-tagged only), ranks, and keeps the top `deliveryCap` as the persisted post-review payload (`artifactPaths.postReview`) Phase 8 posts verbatim. Omit `deliveryCap` (or leave it `null`) to deliver uncapped; omit `delivery` to default the tier to `all`.

**`policy` (model policy the workflow runs under):**

- `tier` — always `"optimized"`, the single benchmarked policy (recorded from the `model_tier` knob). A REVIEW.md `Model Tier` value other than `optimized` self-heals to `optimized` with a loud methodology warning (never a question, never an abort); the env knob `CODE_GAUNTLET_MODEL_TIER` keeps its fail-loud contract unchanged. See Phase 1 for the exact resolution split. Alternate model modes are roadmap work (issue #17).
- `subagentModel` — read `CLAUDE_CODE_SUBAGENT_MODEL` from the environment (or `null`). **The workflow cannot read `process.env`**, so this capture is the only path for it. If set, warn the user and record it in the methodology — it silently overrides the entire per-stage model policy.

**Other inputs (optional unless noted):**

- `contextLines` / `contextChars` — the shared context file's measured size, from the write step above. **Not provenance — consumed.** `contextReadPlan` turns them into the exact `Read` calls the Summarize/Discover/Validate prompts enumerate, so the agent is told which calls to make instead of having to notice an unannounced truncation. Both optional (absent ⇒ count-free read-to-end wording); `contextChars` requires `contextLines`; both must be positive integers.
- `changedFilesPath` — `{output_dir}/code-gauntlet-files-{head_sha_short}.json`, the on-disk companion to `changedFiles`. Optional provenance only — the workflow has no disk access and never opens it.
- `baseBranch` — the base branch name (verify/blame).
- `reviewConfig` — the parsed REVIEW.md object (thresholds + `ignore`), consumed by the Filter stage.
- `exclusionPatterns` — the parsed exclusion-pattern list, consumed by the Filter stage.
- `reviewConfigPath` — the REVIEW.md path (or `null`), carried for provenance.
- `persist` — optional, `{ assembleScriptPath: "{plugin_root}/scripts/assemble_artifacts.py" }`. When present, the artifact-writer emits only unique content (findings JSON, report markdown, a persist-plan JSON), and a pinned executor runs `assemble_artifacts.py` to *derive* the post-review and checkpoint artifacts from `findings.json` plus the plan, returning a content-proof receipt instead of re-emitting them by value. Absent → the workflow falls back to the legacy full by-value writer path, unchanged. `artifactPaths` and Phase 8 are the same either way.

**`verify` handoff (sha-scoped paths for the executor's pinned command):**

```
verify: {
  scriptPath: "{plugin_root}/scripts/verify_findings.py",
  inputPathBase: "{output_dir}/code-gauntlet-phase4-input-{head_sha_short}",
  outputPathBase: "{output_dir}/code-gauntlet-phase4-output-{head_sha_short}"
}
```

The skill supplies only the path base and slice sizing (`limits.verifySliceSize`). The Verify stage does the rest **inside the workflow**: it slices the mid-workflow merged findings, dispatches the artifact-writer to persist each `${inputPathBase}.slice{i}.json` (the workflow has no disk, so the writer materializes them before the executor loop), then dispatches one `executor` per slice to run `verify_findings.py --input <slice> --output <slice-out> --nonce {nonce}.{i} --head-sha {headShaShort} --base-branch {baseBranch} [--diff-file {diffPath}]` and return the receipt envelope verbatim. An untrusted slice is re-dispatched exactly once with the distinct nonce `{nonce}.{i}.r1` before it degrades, so the base `nonce` you stamp must stay inside `^[A-Za-z0-9._-]+$` for that suffixed form too. The skill never pre-writes slice contents — it cannot, since the merged findings do not exist until Discover→Merge run.

**`checkpoints` (resume only):** omit on a fresh run. On resume-from-checkpoint (Phase 8), set it to the content of the persisted checkpoint artifact so the workflow skips completed phases.

---

## Triage Announcement

Announce triage results before proceeding: PR title, review mode, file counts by risk level, AI-generated files if any, active dimensions, incremental scope (`Full`, or `Incremental since {last_reviewed_sha} (N commits)` when 2b-post step 3 resolved Incremental — `Full` is the no-op case for a normal review). For 1000+ line PRs, add: "This PR is [N] lines. Review effectiveness drops sharply above 400 lines. Consider splitting into smaller PRs."

If `collect_project_rules.py`'s receipt (2d step 3) carries a non-empty `gaps[]`, fold each entry into this announcement as a one-line note. A skipped or refused project-rules source (outside the repo, non-markdown, over a cap, a cycle, a missing import target) is exactly the kind of silent degradation this announcement exists to surface — a receipt line on stdout nobody reads is not disclosure.
