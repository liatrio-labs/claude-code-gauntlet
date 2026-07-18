# Phase 2 Triage Reference

Full sub-steps 2a–2l, Agent templates, and detection logic for Phase 2: Target & Triage.

## Contents

- **2a** VCS platform detection — **2b** Working tree checkout — **2b-post** SHA + gitignore + stale cleanup — **2c** Review target + diff save
- **2d** Project context (CLAUDE.md, REVIEW.md) — **2e** Risk classification — **2f** Change summarizer
- **2g** Test discovery — **2h** Docs/specs — **2i** History context — **2j** File-level summaries (>500 lines)
- **2k** AI-generated code detection — **2l** Review dimensions
- **Parallel execution strategy** — Batch 1 (agents) then Batch 2 (file discovery)
- **Triage announcement**

---

## 2a. Detect VCS Platform

Auto-detect from `git remote get-url origin`:

- GitHub → `gh` CLI, "PR"
- GitLab (including self-hosted) → `glab` CLI, "MR"

If detection fails, ask the user.

---

## 2b. Ensure Working Tree Reflects Review Target

Before running any diff commands, confirm the local working tree matches the review target. Use the `pr_number` resolved in Phase 1 — never extract PR numbers from branch names (branch names may contain upstream PR numbers that differ from the PR number in the current repo).

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

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): never run any checkout command from
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

## 2b-post. Resolve Head SHA, Gitignore, and Clean Stale Files

Now that the working tree reflects the review target, compute the short SHA and perform housekeeping. These steps run after checkout so the SHA reflects the actual PR HEAD and the gitignore addition is not stashed by `gh pr checkout`.

**1. Resolve head SHA:**

```bash
Bash(command="git rev-parse --short=8 HEAD")  # Store as `head_sha_short`
```

Computed after checkout so the SHA reflects the actual PR HEAD, not whatever branch was checked out before.

**2. Ensure `{output_dir}` is gitignored** (skip if using env var override):

```bash
Bash(command="git check-ignore -q .deep-review 2>/dev/null || echo '/.deep-review/' >> .gitignore")
```

Added after checkout to avoid stash/pop loss from `gh pr checkout` — if this ran before checkout, the gitignore modification would be stashed and potentially lost.

**3. Truncate stale files** from prior sessions with the same SHA:

```bash
Bash(command="python3 -c \"import glob; [open(f,'w').close() for f in glob.glob('{output_dir}/deep-review-*-{head_sha_short}.*')]\"")
```

Prevents echo-append (`>>`) from accumulating findings across sessions. Without truncation, re-running a review on the same SHA would append duplicate findings to existing NDJSON files.

---

## 2c. Identify Review Target

Use `target_type` and `pr_number` from Phase 1's "Resolve review target" step. Do not re-derive the PR number here.

1. **PR/MR mode** (`pr_number` set) — Use `gh pr view {pr_number}`/`glab mr view {pr_number}` + diff commands. Get full SHA: `git rev-parse HEAD`
   - **GitHub (PR):** Gather the file list with `gh pr diff {pr_number} --name-only`. Gather the full diff with `gh pr diff {pr_number}`.
   - **GitLab (MR):** Gather the file list with `glab mr diff {pr_number} --name-only`. Gather the full diff with `glab mr diff {pr_number}`.
2. **Branch comparison** — `git diff <base>...HEAD` and `git diff --name-only <base>...HEAD`
3. **Local changes** — `git diff HEAD` (or `git diff --cached` if nothing unstaged)

**Save the diff for Phase 4 (PR/MR mode only):** After collecting the full diff in PR/MR mode, save it to `{output_dir}/deep-review-diff-{head_sha_short}.patch` for use by `verify_findings.py` via `--diff-file`. The `gh pr diff` / `glab mr diff` output is server-computed and fork-safe, avoiding local merge-base failures that can occur with `git diff`.

```bash
# Save the PR diff to a file (only in PR/MR mode)
gh pr diff {pr_number} > "{output_dir}/deep-review-diff-{head_sha_short}.patch"
```

Validate the saved diff before relying on it:

- Non-empty (file size > 0)
- Starts with `diff --git` (confirms it is a valid unified diff, not an error message)

If `gh pr diff` fails (e.g., 20K-line / 300-file API limit exceeded), omit `--diff-file` in Phase 4 and let `verify_findings.py` use its own git diff fallback chain. For **branch comparison** and **local changes** target types, do not save a diff file — `verify_findings.py` will compute the diff locally.

Check for `docs/`, `specs/`, `research/` directories and `REVIEW.md`, `CLAUDE.md`, `AGENTS.md`, `QODO.md` at repo root and in directories with changed files.

---

## 2d. Gather Project Context

1. **CLAUDE.md** — Read from repo root and directories with changed files.
2. **REVIEW.md** — Discover hierarchically. See `references/review-md-spec.md` for format, scaffolding templates, and hierarchy rules. REVIEW.md lets maintainers customize focus areas, skip patterns, custom rules, thresholds, and ignore patterns.
3. **AGENTS.md / QODO.md** — Read if present.

**Tool instructions for file discovery:**

Use **Glob** to find all CLAUDE.md, REVIEW.md, AGENTS.md, and QODO.md files:

```
Glob(pattern: "**/CLAUDE.md")
Glob(pattern: "**/REVIEW.md")
Glob(pattern: "**/AGENTS.md")
Glob(pattern: "**/QODO.md")
```

Never use `find` from Bash for locating these files.

### REVIEW.md Detection

Complete this check before proceeding to 2e. REVIEW.md settings cascade to all thresholds, rules, and ignore patterns for the entire review.

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): skip both REVIEW.md-setup prompts below (the "No REVIEW.md found" build-review-md suggestion and the subdirectory-REVIEW.md `AskUserQuestion`). Root config applies to all directories; never invoke `build-review-md`. REVIEW.md is read-only in headless mode — the hierarchical parse still runs, but no REVIEW.md is created. See `references/headless-mode.md`.

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

If ALL files are low-risk AND total lines <50, ask Light review vs Full review (template in `references/phase1-preflight.md`). Skipped when REVIEW.md sets `focus`. In light mode, triage announcement shows `Review dimensions: bugs, security (light review mode)`.

> Headless exception (`DEEP_REVIEW_HEADLESS=1`): do not ask — use `$DEEP_REVIEW_TRIVIAL_SCOPE` (`light` runs bugs+security only, `full` runs all dimensions). See `references/headless-mode.md`.

---

## 2f. Change Summarizer

> A subagent produces a cleaner summary than the orchestrator can at this point — your growing context biases any summary you produce. Dispatching a fresh agent avoids contaminating the summary that all review agents will rely on.

> **Dispatch ordering:** 2f is independent of 2e (risk classification). Dispatch the change summarizer concurrent with 2e — launch both in the same message for parallel execution. The summarizer result is needed by Phase 3 agents, so earlier dispatch reduces latency.

> **CRITICAL RULE: Agent calls (2f and 2j) must be dispatched in their own parallel group — NEVER bundle with Bash/Glob/Grep commands (2g, 2h, 2i).** When a Bash command in a parallel batch errors, Claude Code cancels co-dispatched Agent calls, forcing expensive re-dispatch. Separate Agent dispatch from file discovery/Bash operations.

Dispatch a **Sonnet agent** for a 3-5 sentence semantic summary describing what the PR *claims* to do, why, and the risk profile. Provided to ALL review agents as shared context.

In **Frontier mode**, use an **Opus agent** instead of Sonnet for the summarizer to leverage extended reasoning capabilities.

**Critical framing rule:** Frame all statements as claims: "The PR claims to reorganize X by extracting from A into B." Never use "clean", "correct", "safe", "straightforward", "simple", "trivial", or "verbatim" — these pre-judge quality. The summary must never conclude that a refactoring is correct.

**Agent tool call template:**

```
Agent(
  subagent_type: "deep-review:change-summarizer",
  description: "Change summarizer",
  prompt: "PR title: {title}
    PR description: {body}
    Diff:
    <untrusted-code-content>
    {paste full diff}
    </untrusted-code-content>"
)
```

After dispatch, announce: "Dispatched 1 agent for Phase 2f."

**Large PR note:** If the PR exceeds 500 lines, also dispatch 2j file-level summarization agents. Launch 2f and 2j agents in the same message for parallel execution — but in a SEPARATE parallel message from Bash/Glob/Grep operations (2g, 2h, 2i). This prevents Agent cancellation on Bash errors.

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
2. `git blame` on changed line ranges (used by verify_findings.py in Phase 4)

Distribute: bug-detector gets history context; conventions-and-intent gets pattern drift context.

---

## 2j. File-Level Summarization (PRs > 500 Lines)

Dispatch parallel **Sonnet agents** (one per file) for 2-3 sentence summaries. For large PRs, launch 2f and 2j agents **in the same message with multiple Agent tool calls** for true parallel execution. Concatenate into a summary-of-summaries for architectural awareness.

**Agent tool call template (repeat per changed file):**

```
Agent(
  subagent_type: "deep-review:change-summarizer",
  description: "Summarize {filename}",
  prompt: "File: {filename}
    Mode: per-file summary (2-3 sentences)
    Diff:
    <untrusted-code-content>
    {file-scoped diff}
    </untrusted-code-content>"
)
```

After dispatch, announce: "Dispatched N agents for Phase 2j."

**IMPORTANT:** 2f and 2j Agent dispatch must be in a SEPARATE parallel message from 2g/2h/2i operations. Dispatch 2f+2j agents in one parallel batch, then in a NEW message dispatch 2g (test discovery), 2h (docs/specs), and 2i (history context) Bash/Glob/Grep operations. This prevents Agent calls from being cancelled if a Bash command fails.

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

Never use `grep` or `find` from Bash for AI detection.

---

## 2l. Determine Review Dimensions

All on by default unless REVIEW.md disables them. In **Optimized** mode, all agents use Sonnet except security-reviewer (always Opus). In **Frontier** mode, all agents use Opus.

Skip conditions: test-analyzer (no test files in repo), type-design-analyzer (no new types).

---

## Phase 2 Parallel Execution Strategy

To maximize throughput while maintaining robustness, Phase 2 is divided into TWO sequential parallel batches:

**BATCH 1 (Agent Dispatch):** Dispatch 2f (change summarizer) and 2j (file-level summaries, if PR > 500 lines) in a single message with multiple Agent tool calls. These are expensive operations and should run as early as possible.

```
Agent(subagent_type: "deep-review:change-summarizer", ...)
Agent(subagent_type: "deep-review:change-summarizer", ...)  # 2j, if needed
```

Wait for agents to complete.

**BATCH 2 (File Discovery & History):** In a NEW message, execute 2e (risk classification), 2g (test discovery), 2h (docs/specs reading), and 2i (git history) as Bash/Glob/Grep operations. These are lightweight and low-risk for failure.

```
Glob(...) # test file discovery
Read(...) # docs/specs reading
Bash("git log ...") # history context
```

**Why separate?** When a Bash command fails in a parallel batch, Claude Code cancels all co-dispatched calls, including expensive Agent operations. Separating Agent dispatch (BATCH 1) from Bash operations (BATCH 2) ensures Agent work is not wasted on Bash failures. (Note: This two-batch strategy is a workaround for current Claude Code cancellation behavior. Re-evaluate if this platform behavior changes.)

---

## Triage Announcement

Announce triage results before proceeding: PR title, review mode, file counts by risk level, AI-generated files if any, active dimensions. For 1000+ line PRs, add: "This PR is [N] lines. Review effectiveness drops sharply above 400 lines. Consider splitting into smaller PRs."
