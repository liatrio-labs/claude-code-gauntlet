# Phase 2 Triage Reference

Full sub-steps 2a–2l, Agent templates, and detection logic for Phase 2: Target & Triage.

---

## 2a. Detect VCS Platform

Auto-detect from `git remote get-url origin`:
- GitHub → `gh` CLI, "PR"
- GitLab (including self-hosted) → `glab` CLI, "MR"

If detection fails, ask the user.

---

## 2b. Ensure Working Tree Reflects Review Target

Before running any diff commands, confirm the local working tree matches the review target.

**1. Resolve the target's head SHA:**
- **PR/MR number or URL:** `gh pr view <number> --json headRefOid --jq '.headRefOid'` (GitHub) / `glab mr view <number> --output json | jq '.sha'` (GitLab)
- **Branch name:** `git rev-parse <branch>`
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

**4. If checkout fails → STOP immediately:**
```
Unable to checkout [branch/PR]. The review requires the target code to be accessible locally.
You can checkout the branch manually and re-run the review.
```
No fallback or workaround — a silently wrong working tree produces unreliable review results.

---

## 2c. Identify Review Target

1. **PR/MR mode** — user provides a number/URL. Use `gh pr view`/`glab mr view` + diff commands. Get full SHA: `git rev-parse HEAD`
2. **Branch comparison** — `git diff <base>...HEAD` and `git diff --name-only <base>...HEAD`
3. **Local changes** — `git diff HEAD` (or `git diff --cached` if nothing unstaged)

Check for `docs/`, `specs/`, `research/` directories and `REVIEW.md`, `CLAUDE.md`, `AGENTS.md`, `QODO.md` at repo root and in directories with changed files.

---

## 2d. Gather Project Context

1. **CLAUDE.md** — Read from repo root and directories with changed files.
2. **REVIEW.md** — Discover hierarchically. See `references/review-md-spec.md` for format, scaffolding templates, and hierarchy rules. REVIEW.md lets maintainers customize focus areas, skip patterns, custom rules, thresholds, and ignore patterns.
3. **AGENTS.md / QODO.md** — Read if present.

### REVIEW.md Detection

Complete this check before proceeding to 2e. REVIEW.md settings cascade to all thresholds, rules, and ignore patterns for the entire review.

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

### Light Review for Trivial PRs

If ALL files are low-risk AND total lines <50, ask Light review vs Full review (template in `references/phase1-preflight.md`). Skipped when REVIEW.md sets `focus`. In light mode, triage announcement shows `Review dimensions: bugs, security (light review mode)`.

---

## 2f. Change Summarizer

> A subagent produces a cleaner summary than the orchestrator can at this point — your growing context biases any summary you produce. Dispatching a fresh agent avoids contaminating the summary that all review agents will rely on.

> **Dispatch ordering:** 2f is independent of 2e (risk classification). Dispatch the change summarizer concurrent with 2e — launch both in the same message for parallel execution. The summarizer result is needed by Phase 3 agents, so earlier dispatch reduces latency.

Dispatch a **Sonnet agent** for a 3-5 sentence semantic summary describing what the PR *claims* to do, why, and the risk profile. Provided to ALL review agents as shared context.

In **Frontier mode**, use an **Opus agent** instead of Sonnet for the summarizer to leverage extended reasoning capabilities.

**Critical framing rule:** Frame all statements as claims: "The PR claims to reorganize X by extracting from A into B." Never use "clean", "correct", "safe", "straightforward", "simple", "trivial", or "verbatim" — these pre-judge quality. The summary must never conclude that a refactoring is correct.

**Agent tool call template:**
```
Agent(
  subagent_type: "claude-deep-review:change-summarizer",
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

**Large PR note:** If the PR exceeds 500 lines, also dispatch 2j file-level summarization agents. Launch 2f and 2j agents in the same message for parallel execution (see Critical Rule #2).

---

## 2g. Related Test Discovery

For each changed production file, find test files by convention (`Tests`, `.test`, `.spec`, `_test`, `_spec` patterns; `tests/`, `__tests__/`, `spec/` directories). Include in context for bug-detector and test-analyzer.

---

## 2h. Docs/Specs Context

If `docs/`, `specs/`, `research/` exist, read relevant files. Send only to conventions-and-intent agent and Phase 8 report generation — NOT all agents (avoids biasing toward confirming intent rather than finding bugs).

---

## 2i. History Context Preprocessing

**Deterministic preprocessing, not an LLM agent.** For each changed file:
1. `git log --oneline -10 -- <file>` for recent change history
2. `git blame` on changed line ranges (used by verify_findings.py in Phase 4)

Distribute: bug-detector gets history context; conventions-and-intent gets pattern drift context.

---

## 2j. File-Level Summarization (PRs > 500 Lines)

Dispatch parallel **Sonnet agents** (one per file) for 2-3 sentence summaries. For large PRs, launch 2f and 2j agents **in the same message with multiple Agent tool calls** for true parallel execution. Concatenate into a summary-of-summaries for architectural awareness.

**Agent tool call template (repeat per changed file):**
```
Agent(
  subagent_type: "claude-deep-review:change-summarizer",
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

---

## 2k. AI-Generated Code Detection

Scan for AI co-author trailers, attribution comments, AI tool metadata. **Elevate AI-generated files one risk level** (research shows 75% more logic errors in AI-authored code). Include AI-generation status in risk classification sent to all agents.

---

## 2l. Determine Review Dimensions

All on by default unless REVIEW.md disables them. In **Optimized** mode, all agents use Sonnet except security-reviewer (always Opus). In **Frontier** mode, all agents use Opus.

Skip conditions: test-analyzer (no test files in repo), conventions-and-intent (no CLAUDE.md AND no docs/specs), type-design-analyzer (no new types), code-simplifier (POST-review only, only if no critical/high).

---

## Triage Announcement

Announce triage results before proceeding: PR title, review mode, file counts by risk level, AI-generated files if any, active dimensions. For 1000+ line PRs, add: "This PR is [N] lines. Review effectiveness drops sharply above 400 lines. Consider splitting into smaller PRs."
