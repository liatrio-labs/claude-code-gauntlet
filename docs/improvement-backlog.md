# Deep Review: Improvement Backlog

> **This backlog is ARCHIVED.** All B1-B8 items were implemented (2026-03-26, commits f1809e8–b8f6a6f).
> The active roadmap is **[v3 Master Improvement Plan](design/v3-master-improvement-plan.md)** — 29 items driven by v2 benchmark + session transcript analysis + 19 research artifacts.

---

## v1 Benchmark Baseline (2026-03-26)

Driven by benchmark analysis against the [Code Review Bench](https://github.com/withmartian/code-review-benchmark). Full analysis: `code-review-benchmark/offline/results/claude-sonnet-4-5-20250929/deep-review-improvement-analysis.md`

P=20.3%, R=70.6%, F1=31.6%, Rank #27/39 (10 PRs, Frontier mode)

## v2 Benchmark (2026-03-30)

P=25.0%, R=40.0%, F1=30.8% (5 PRs, different set). Details: `docs/temp/v2-refactor.md`

Candidate volume halved (11.9→6.6). Test-coverage FPs cut 90%. But recall dropped from 70.6%→40.0% due to systemic pipeline failure: branch mismatch broke Phases 5+7 in all 5 sessions.

---

## B1-B8: IMPLEMENTED

| Item | Status | Commit |
|------|--------|--------|
| B1: Improvement Suggestions section | Done | f1809e8 |
| B2: Remove Positive Observations | Done | f1809e8 |
| B3: Remove verdict system | Done | f1809e8 |
| B4: Phase restructuring (8 phases) | Done | multiple |
| B5: Triggerability validation | Done | 42d2cd7 |
| B6: Default cap 8→6 | Done | delivery-guide updates |
| B7: Summarizer claims framing | Done | phase2-triage updates |
| B8: Subagent hardening | Done | e9b6424 |

---

## Original B1-B8 Details (for reference)

### B1: Improvement Suggestions Section

**Problem:** Test coverage findings account for 39% of all false positives (37/94). Comment/documentation accuracy findings add another 13% (12/94). These are valid review output but they're a different *kind* of output than "here's a bug." Mixing them into severity-ranked findings dilutes the signal and inflates noise when posted as PR comments. The benchmark never considers "add tests" or "fix this Javadoc" a valid finding.

**Change:** Create an "Improvement Suggestions" section in the report, separate from the severity-ranked main findings. Route three categories here:
- Test coverage gaps (test-analyzer)
- Comment/documentation accuracy (conventions-and-intent pass 3)
- Code simplification suggestions (code-simplifier)

Sub-group by type within the section for scannability (Test Coverage, Documentation, Code Quality).

**Behavioral rules:**
- Findings in this section still go through the full validation + challenge pipeline — no quality shortcuts
- Excluded from PR inline comments by default (available if user selects "Let me pick" in the walkthrough)
- Don't count toward the finding total in the executive summary
- Don't influence the review verdict (which is being removed per B3)
- During dedup: if a test-analyzer finding overlaps with another agent's finding at the same location, the non-test dimension wins the merge and the finding stays in the main report. Only standalone test gaps with no corresponding bug/security/cross-file finding get routed to suggestions

**Expected impact:** Removing 37 test + 12 comment FPs from the main findings. Precision ~20% to ~29%. Loses 1 TP (a test-gap finding that matched a golden comment about datetime serialization — the dedup rule above should recover this if bug-detector also flags it). F1 improvement: ~+9 points.

**Research basis:** Doc #15 — optimal volume is 5-6 comments per PR; adoption threshold is 75-80% precision. Doc #13 — 31% of small PRs receive no findings; silence is a feature.

**Complexity:** M

---

## B2: Remove Positive Observations Section

**Problem:** The "Positive Observations" section (3-5 bullet points of what the code does well) adds length without helping engineers fix anything. Top benchmark tools are ruthlessly focused — they report what's wrong and nothing else. The Review Dimensions Summary table already communicates "I looked at X and it's clean."

**Change:** Remove the Positive Observations section from the report format (`references/report-format.md`). No replacement needed.

**Research basis:** Doc #15 — engagement decays in ~10 days; every section that isn't actionable reduces the chance the actionable parts get read.

**Complexity:** S

---

## B3: Remove Verdict System

**Problem:** The APPROVE / APPROVE WITH SUGGESTIONS / REQUEST CHANGES verdict is non-deterministic (two runs of the same PR may produce different verdicts) and doesn't add information beyond what the findings already communicate. Engineers can see "2 critical, 1 high" and draw their own conclusion. Research explicitly warns against AI approvals counting toward review thresholds.

**Change:**
- Remove the verdict from the report format and executive summary
- Always post PR comments using the `COMMENT` event type, never `REQUEST_CHANGES` or `APPROVE`
- Remove the verdict determination logic from Phase 6/report generation

**Research basis:** Doc #15 — "Advisory-first tools sustain adoption while overly blocking tools get disabled within a month. AI approval should never count toward required review thresholds."

**Complexity:** S

---

## B4: Phase Restructuring — Validation Agents

**Problem:** Phase 4 currently bundles six different operations (blame classification, deterministic verification, LLM judgment, threshold filtering, injection scanning, disagreement detection) in the main orchestrator context. By this point the context is bloated with all agent outputs, degrading reasoning quality. The LLM judgment step (4b Step 2) currently only runs for findings with confidence <90, missing an opportunity to validate all findings with fresh eyes. Research shows context length degrades reasoning 13-85%.

**Change:** Split current Phases 4-7 into a cleaner 5-phase structure where each phase has one job:

| Phase | Job | Executor | Notes |
|-------|-----|----------|-------|
| 4 | Classify & Verify | Main (deterministic) | Blame classification, read exact lines, LSP/Grep symbol checks, confidence=0 for factually wrong findings, group survivors into batches of 3-5 for validation agents |
| 5 | Validate | Parallel Sonnet agents (always Sonnet, even in Frontier) | Each agent gets a batch of findings + relevant code + blame tags. Assesses: is this real? Is it triggerable today? Returns adjusted confidence per finding |
| 6 | Filter & Reconcile | Main (rules) | Apply dimension-specific thresholds using new confidence scores, injection scan, disagreement detection, route to main vs suggestions section |
| 7 | Blind Challenge | Parallel agents (Sonnet/Opus per mode) | Unchanged — fresh agents, claim + raw code, try to disprove. Post-challenge: dedup, cap, rank |
| 8 | Report & Deliver | Main | Merge current Phase 6 (Generate Report) and Phase 7 (Deliver) — they were always sequential with no work between them |

Phases 1-3 unchanged.

**Key design decisions:**
- Phase 5 always uses Sonnet, even in Frontier mode — validation is objective assessment with focused context, not deep discovery. Cost optimization without quality loss.
- Phase 4 groups findings by file proximity so validation agents can read surrounding code once and validate several nearby findings efficiently.
- Phase 5 and Phase 7 remain strictly separate — validation agents have context about the finding's origin; challenge agents are blind. Merging them would undermine the blind principle.
- Blame classification in Phase 4 is natural setup for Phase 5 — validation agents need to know "new code" vs "pre-existing" to reason about triggerability.

**Research basis:** Doc #04 — context length degrades reasoning 13-85%. Doc #02 — deterministic grounding beats LLM-on-LLM; fresh agents provide better judgment. Doc #12 — Sonnet-Opus gap compressed to 1.2 points on objective tasks.

**Complexity:** L

---

## B5: Triggerability in Validation and Exclusion List

**Problem:** 22% of false positives are speculative/defensive findings — technically accurate claims about issues that aren't triggerable with the current code. Examples: "will raise TypeError if assignment_source is ever passed" (no caller passes it), "if someone changes == to !=, no test catches it." These get 100% confidence because the claims are factually correct, so threshold filtering doesn't help. The current challenge prompt asks about "defensive code, framework guarantees, type system protections" but doesn't ask about present-tense triggerability.

**Change:**
1. **Phase 5 validation agents** (from B4): include "Can you find a code path that actually triggers this today?" as a core assessment question. Findings that are only triggerable under hypothetical future changes get confidence capped at 65 (below the 70 threshold for non-security dimensions). *(Note: original B5 design used cap 70 / threshold 80; V5-09 lowered both.)*
2. **False-positive exclusion list** (`references/false-positive-exclusions.md`): add a new category — "Latent issues not triggerable by current code paths." Examples: contract violations with no current caller, error paths with no current trigger, pattern deviations with no current behavioral impact.
3. **Challenge prompt** (Phase 7): add "Is there a code path in the current codebase that triggers this?" to the disproval checklist alongside existing items (defensive code, framework guarantees, etc.).

**Research basis:** Doc #02 — confidence calibration rubric defines 90-100 as "exact trigger identifiable." Speculative findings don't meet this bar but are rated there because they're factually correct. The rubric needs to gate on triggerability, not just factual accuracy.

**Complexity:** M

---

## B6: Default PR Comment Cap 8 to 6

**Problem:** The default PR inline comment cap is 8 ("Default — top 8 by severity"). Research shows optimal engagement at 5-6 comments per PR. The existing cap of 8 slightly exceeds this.

**Change:** Lower the default from 8 to 6 in SKILL.md Phase 7 and `references/delivery-guide.md`. The "Let me pick" flow remains uncapped — user-selected findings are always respected.

**Research basis:** Doc #15 — optimal volume is 5-6 comments per PR; engagement decays with more. Adoption threshold is 75-80% precision, which is easier to hit with fewer, higher-quality comments.

**Complexity:** S

---

## B7: Fix Summarizer Bias on Refactorings

**Problem:** The Phase 2e change summarizer produces a semantic summary that becomes shared context for ALL review agents. When it characterizes a PR as "a clean refactoring that moves code verbatim," agents start from "this is probably fine" instead of independently evaluating the code. This caused the only 0% recall result in the benchmark (sentry/#80528) — the tool approved a refactoring PR that contained two real bugs (returning wrong variable, unnecessary DB re-fetch). qodo caught one of these bugs on the same PR.

**Change:** Apply the same principle used in the blind challenge round: frame the summary as a *claim* about the PR, not a *conclusion*. The summary should describe what the PR *says* it does, not validate whether it succeeded.

Examples:
- Before: "This is a clean refactoring that extracts incident creation logic. Code moved verbatim with no behavioral changes."
- After: "The PR claims to reorganize incident creation logic by extracting it from mark_failed.py into incidents.py and incident_occurrence.py. The following functions were moved: [list]. Verify that the extracted code preserves the original behavior."

The summary must never use evaluative language (clean, correct, safe, straightforward, simple) — those judgments are the agents' job. Factual description only: what files changed, what moved where, what the PR description says.

**Research basis:** Doc #14 — sycophancy corrupts verification in 18/20 configurations. The summarizer-to-agent relationship mirrors the "original reasoning to challenger" relationship where pre-loading conclusions biases the evaluator. Doc #17 — models implicitly optimize for lower-friction outputs; a summary that says "clean refactoring" gives agents permission to do less work.

**Complexity:** S

---

## B8: Subagent Hardening — Tool Allowlists, Effort, Model Defaults

**Problem:** All review agents currently inherit the full tool set (Write, Edit, Bash, MCP tools, etc.). The security boundary preventing agents from modifying files or executing commands is enforced only by prompt instructions — a prompt injection in untrusted code could cause an agent to call Write/Edit/Bash. Additionally, high-volume phases (validation, challenge) use the same effort level as discovery phases, wasting tokens on focused assessment tasks.

**Changes:**

1. **Tool allowlists on every Agent call.** Structurally enforce read-only access for all review agents:

   | Agent Phase | Tools |
   |-------------|-------|
   | Phase 2e/2i summarizers | `[]` (empty — all context inline) |
   | Phase 3 review agents | `[Read, Grep, Glob, LSP]` |
   | Phase 5 validators | `[Read, Grep, Glob, LSP]` |
   | Phase 7 challengers | `[Read, Grep, Glob, LSP]` |

   No review agent gets Write, Edit, Bash, or any MCP tool. Prompt injection cannot cause file modifications because the tools don't exist in the agent's environment.

2. **effort field per phase.** Reduce token usage on focused assessment tasks:

   | Agent Phase | Effort | Rationale |
   |-------------|--------|-----------|
   | Phase 2e/2i summarizers | `medium` | Summarization is straightforward |
   | Phase 3 review agents | `high` | Core investigative work |
   | Phase 5 validators | `medium` | Focused disproval on pre-identified findings |
   | Phase 7 challengers | `high` | Last line of defense — thorough challenge matters |

3. **Model defaults in agent frontmatter.** Make model routing visible in agent definitions:
   - `security-reviewer.md`: `model: opus` (always Opus in both modes)
   - All other agents: `model: sonnet` (default for Optimized; orchestrator overrides to opus in Frontier)

4. **Update SECURITY BOUNDARY note** to reference structural tool controls.

**Research basis:** Doc #06 — LSP provides 900x faster symbol resolution; must be in allowlist. Doc #05 — every major AI review tool has been exploited via prompt injection; structural tool restrictions are the strongest defense. Doc #12 — Sonnet-Opus gap compressed to 1.2 points; model defaults in frontmatter simplify routing.

**Complexity:** M

---

## Priority Order

Based on expected F1 impact and complexity:

| Priority | Item | F1 Impact | Complexity | Dependencies |
|----------|------|-----------|------------|--------------|
| 1 | B1: Improvement Suggestions section | +9 pts | M | None |
| 2 | B4: Phase restructuring | +5-8 pts (enables B5) | L | None |
| 3 | B5: Triggerability validation | +2-3 pts | M | B4 (validation agents) |
| 4 | B8: Subagent hardening | Security + cost | M | None |
| 5 | B7: Summarizer bias fix | +1-2 pts recall | S | None |
| 6 | B2: Remove Positive Observations | Noise reduction | S | None |
| 7 | B3: Remove verdict system | Noise reduction | S | None |
| 8 | B6: Default cap 8→6 | Minor precision | S | None |

B2, B3, B5, B6, B7, and B8 can be done independently in any order. B4 should precede B5 since the validation agents are where triggerability checks live.

---

## Validation Plan

After implementing B1-B7, re-run the same 10 benchmark PRs. Target metrics:

| Metric | Current | Target | Notes |
|--------|---------|--------|-------|
| Precision | 20.3% | >40% | Driven by B1 (suggestions section) + B5 (triggerability) |
| Recall | 70.6% | >70% | Maintain or improve — B7 (summarizer fix) should recover sentry/#80528 |
| F1 | 31.6% | >50% | Would rank top 5-8 on the benchmark |
| Candidates/PR | 11.8 | 5-7 (main findings) | Suggestions section absorbs the rest |
| PR comments (default) | 11.8 | 6 (cap) | B6 |

Follow-up: run on all 50 benchmark PRs for a direct leaderboard comparison.
