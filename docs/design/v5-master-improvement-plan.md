# V5 Master Improvement Plan

> **Driven by:** v5 benchmark (2026-04-02), 5 PRs, full session transcript analysis.
> **Benchmark baseline:** P=29.4%, R=29.4%, F1=29.4% (raw). Adjusted for benchmark scoring errors: P≈58%, R≈65%, F1≈61%.
> **Projected impact:** With all V5 fixes + correct scoring, estimated recall ceiling rises from 65% to ~76% (13/17 golden issues caught). Remaining 4 misses are 3 Low severity + 1 framework-specific semantic gap.

---

## V5-01: Session-Unique TMPDIR Filenames

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** calcom-PR10967, discourse-PR7

**Problem:** The pipeline uses generic filenames in `$TMPDIR` (`deep-review-findings.json`, `deep-review-pr-comments.json`, `deep-review-diff.patch`). When concurrent sessions run or a session spans a long gap, these files get overwritten. Caused Phase 4 to load findings from a different PR (calcom-PR10967, 7-hour session gap) and Phase 8 to post review comments to the wrong repository (discourse-PR7, concurrent session overwrote the delivery JSON).

**Change:** Use the first 8 characters of the HEAD commit SHA as a suffix on all temp filenames. Example: `deep-review-findings-a1b2c3d4.json`, `deep-review-diff-a1b2c3d4.patch`. The orchestrator resolves the short SHA once during Phase 1 (alongside plugin root resolution) and uses it for all subsequent temp file operations.

**Files to modify:**
- `skills/deep-review/SKILL.md` — Phase 1 adds short SHA resolution; all temp filename references updated
- `skills/deep-review/references/phase2-triage.md` — diff filename reference
- `skills/deep-review/references/validation-pipeline.md` — findings JSON filename references
- `skills/deep-review/references/phase8-delivery.md` — delivery JSON filename reference

---

## V5-02: Raw Diff Access for Low-Risk Files

**Status:** AGREED  
**Effort:** M  
**Sessions affected:** keycloak-PR37429 (systemic risk for all i18n/config/CSS PRs)

**Problem:** In keycloak-PR37429, the orchestrator read raw diffs for .properties files (saw Italian text in a Lithuanian file, Traditional Chinese in a Simplified Chinese file), then paraphrased them as "Replaced totpStep1 with generic text" before passing to agents. 45 .properties files were classified LOW risk, and bug-detector's prompt was scoped to "HIGH and MEDIUM risk files only." The evidence was present in the diff but destroyed during the handoff.

**Change:** Three design principles:

### Principle 1: Content-change promotion

After initial risk classification in Phase 2e, check LOW-risk files for substantive content changes — any diff line that changes a string value, numeric value, or identifier (not just formatting, whitespace, markup, or delimiters). Files with substantive content changes get promoted to MEDIUM. This is a type-agnostic heuristic the orchestrator applies on the raw diff.

Examples of promotion triggers:
- i18n file: translation text changed (not just tag formatting)
- Config file: values changed (not just comments or indentation)
- CSS/SCSS: numeric values changed (colors, sizes, percentages)
- Any file: identifiers or string literals changed

Examples that stay LOW:
- Lock files (always mechanical)
- Whitespace-only / formatting-only changes
- Generated code updates
- Tag case changes (`<br/>` → `<br />`)

### Principle 2: No paraphrasing

The orchestrator's job is to *select and scope* what agents see, not to *interpret* diff content. For files in an agent's scope, the orchestrator passes raw diff lines. It may add structural annotations alongside the diff (risk level, file role, location in the project), but must never substitute its own summary for the actual changed content. This prevents the entire class of "evidence destroyed during handoff" failures.

### Principle 3: Sweep appendix for LOW files

Context scoping tiers:
- **HIGH + MEDIUM:** full raw diff to all applicable agents
- **LOW (remaining after promotion):** compact raw diff (changed lines only, no context lines) delivered to bug-detector as a "sweep appendix" — a clearly-delimited section at the end of its prompt, separate from the primary scope. Other agents receive the file list only.

This ensures no content change is invisible to all agents while keeping token budgets manageable. If 45 LOW files have only formatting changes, the appendix is tiny. If they have real content changes, most get promoted to MEDIUM by Principle 1.

**Files to modify:**
- `skills/deep-review/references/phase2-triage.md` — Step 2e risk classification (add content-change promotion rule)
- `skills/deep-review/references/phase3-dispatch.md` — context scoping tiers, no-paraphrase rule, sweep appendix template
- Agent dispatch prompt templates (raw diff instead of summaries for scoped files)

---

## V5-03: Phase 5 Validator Intent Context

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** discourse-PR7 (3 TPs killed by validators)

**Problem:** In discourse-PR7, Phase 5 validators killed all 3 golden findings by reasoning that CSS lightness value changes "may be intentional design decisions." The PR's purpose was mechanical dark-light-choose wrapping that should preserve original values — any value change is by definition a bug. Validators lacked context about the PR's stated intent and applied generic "could be intentional" reasoning.

**Change:** Include the Phase 2f change summary in the Phase 5 validator dispatch prompt. The change summary is already generated (and framed as claims per B7) and available to the orchestrator. Add to the validator dispatch template:

> **PR context (treat as claims, not facts):** {change_summary}
>
> Consider whether each finding is consistent or inconsistent with the PR's stated intent. A finding that contradicts the PR's own goals is more likely to be a real issue than an intentional choice.

**Files to modify:**
- `skills/deep-review/references/validation-pipeline.md` — Phase 5 dispatch template

---

## V5-04: Incremental Finding Emission

**Status:** AGREED  
**Effort:** M  
**Sessions affected:** keycloak-PR37429 (bug-detector analyzed golden issue #3 for 21K chars, self-dismissed, truncated before producing JSON)

**Problem:** Discovery agents currently investigate all issues, then produce a single JSON array at the end. If the agent runs out of output tokens, all findings are lost. Additionally, the "investigate everything then decide" structure encourages the doubt-and-skip pattern — agents accumulate uncertainty across findings and self-dismiss at the end. BF-12/BF-13 added textual prohibitions against pre-filtering, but the agent in keycloak-PR37429 still analyzed the anchor sanitization logic for 205 seconds, talked itself out of reporting it, and truncated before producing any JSON.

**Change:** Restructure agent output format to emit each finding's JSON block immediately after investigating it, before moving to the next:

1. Agent investigates issue A → emits finding JSON (or explicit skip with one-line reason)
2. Agent investigates issue B → emits finding JSON (or explicit skip)
3. ...continues until done or output limit reached

Benefits:
- Truncation loses only the last in-progress investigation, not everything
- "Emit first, then move on" structure makes it harder to accumulate doubt and self-dismiss at the end
- Partial results are still useful — an agent that finds 3 issues then truncates still produces 3 findings

The orchestrator's Phase 3 merge logic parses multiple JSON blocks from each agent's output instead of expecting a single array. This is a straightforward parsing change.

**Files to modify:**
- All 7 discovery agents (`agents/bug-detector.md`, `agents/security-reviewer.md`, `agents/cross-file-impact.md`, `agents/test-analyzer.md`, `agents/conventions-and-intent.md`, `agents/type-design-analyzer.md`, `agents/code-simplifier.md`) — output format section
- `skills/deep-review/SKILL.md` — Phase 3 merge (parse incremental blocks)
- `skills/deep-review/references/phase3-dispatch.md` — dispatch template expectations

---

## V5-05: Tiered Symbol Extraction and Proportional Confidence Reduction

**Status:** AGREED  
**Effort:** S-M  
**Sessions affected:** discourse-PR8 (TP nearly killed by "Concrete" extracted as code symbol)

**Problem:** `verify_findings.py` extracts symbols from finding descriptions/evidence using regex (CamelCase, snake_case, backtick-delimited tokens), then searches the codebase via grep. If a symbol isn't found, confidence is zeroed. In discourse-PR8, the word "Concrete" from "Concrete example: user_count=100..." was extracted as a code symbol, not found, and confidence was zeroed on a true positive. The orchestrator manually overrode this — fragile recovery.

**Change:** Two improvements to `verify_findings.py`:

### Tiered extraction by signal strength

- **Definite code:** Backtick-delimited spans, triple-backtick code blocks → extract and verify
- **Very likely code:** Tokens containing `_`, `()`, `.`, `::`, `->`, `[]`, `#` — punctuation patterns that don't appear in English prose → extract and verify
- **Ambiguous, skip:** Pure CamelCase with no code-punctuation indicators → do not extract (eliminates "Concrete", "Between", "However", etc.)

### Proportional confidence reduction instead of binary zero

- All extracted symbols found → no change
- Some missing → confidence reduced proportionally using `miss_ratio * 70` (e.g., 3 of 4 found → -18; 1 of 4 found → -53)
- No extractable symbols in description → skip symbol verification entirely
- Floor at 30 so a finding with strong evidence on other dimensions isn't completely killed by one renamed symbol

**Files to modify:**
- `scripts/verify_findings.py` — `verify_factual` function (extraction regex, confidence adjustment logic)
- `tests/test_verify_findings.py` — update and add tests for tiered extraction and proportional reduction

---

## V5-06: Always Dispatch Conventions Agent

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** keycloak-PR37429 (`santizeAnchors` typo missed)

**Problem:** In keycloak-PR37429, conventions-and-intent was skipped because no CLAUDE.md existed. This agent has three passes: (1) convention compliance (needs CLAUDE.md rules), (2) intent alignment (checks against specs/PR description), (3) comment accuracy (checks whether comments match code). Skipping the entire agent because pass 1 has no input means passes 2 and 3 never run.

**Change:** In phase3-dispatch.md, change the dispatch condition from "skip if no CLAUDE.md" to "always dispatch." When no CLAUDE.md exists, the orchestrator's prompt to the agent notes "No CLAUDE.md or project convention files found — skip pass 1 (convention compliance), execute passes 2 and 3 only." The agent already has conditional logic per pass — this just makes the dispatch unconditional.

**Files to modify:**
- `skills/deep-review/references/phase3-dispatch.md` — dispatch conditions for conventions-and-intent

---

## V5-07: Enforce Phase 5 Degradation Protocol

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** calcom-PR10967 (Phase 5 skipped entirely after Phase 4 failure)

**Problem:** In calcom-PR10967, after Phase 4's catastrophic failure (stale TMPDIR file loaded wrong findings), the orchestrator skipped Phase 5 entirely and jumped to Phase 6, substituting inline analysis. This violates the explicit degradation protocol ("Pass all findings to Phase 5 as-is with `origin: 'new'`") and reintroduces the correlated-error problem Phase 5 exists to solve. 5 of 7 false positives had confidence 82-95 that validators might have lowered.

**Change:** Rewrite the Phase 4 failure recovery section as a numbered checklist with an explicit prohibition:

> **Phase 4 failure recovery:**
> 1. Note in methodology: "Phase 4 verification skipped due to script failure."
> 2. Take all Phase 3 merged findings as-is.
> 3. Set `origin: "new"` on every finding (conservative — assume all are new).
> 4. Create batches of 3-5 findings by file proximity (manual grouping).
> 5. Dispatch Phase 5 validation agents with these batches. Do NOT skip Phase 5.
>
> **Do NOT substitute inline analysis for Phase 5 dispatch.** The entire point of Phase 5 is independent validation from fresh agents. Inline analysis by the orchestrator has the same correlated-error problem (~60% rate) that Phase 5 exists to solve.

**Files to modify:**
- `skills/deep-review/references/validation-pipeline.md` — Phase 4 failure recovery section
- `skills/deep-review/SKILL.md` — Script Failure Recovery section (Phase 4 entry)

---

## V5-08: Promote Code-Simplifier to Always-On Phase 3 Agent

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** calcom-PR10967 (redundant optional chaining never flagged)

**Problem:** The code-simplifier currently runs as a conditional second pass after Phase 6, only if no critical/high findings survived. This means on most PRs with real bugs, it never executes. Its findings skip Phase 5 validation and Phase 7 challenge — less verified than other agents' findings. If code-simplifier can find unique issues (like redundant defensive code), it should be a first-class agent with full pipeline verification.

**Change:** Two parts:

### Part A: Promote to Phase 3

Move code-simplifier from the conditional post-Phase-6 second pass to the always-on Phase 3 roster. It dispatches in parallel with the other agents, and its findings go through the full Phase 4-7 pipeline. Remove the "Code-Simplifier Second Pass" machinery from validation-pipeline.md.

Existing mechanisms handle integration:
- Phase 6 dimension routing already sends code-simplifier findings to "suggestion"
- Phase 6 dedup already drops code-simplifier findings that overlap with other agents at the same file/line
- Suggestion findings are excluded from the executive summary count and PR inline comments by default

### Part B: Add redundant guard pattern

Add to code-simplifier.md's investigation checklist under the existing "redundancy" category:

> Defensive code that duplicates a guarantee already provided by surrounding control flow (e.g., optional chaining inside a branch that already null-checked the value, type narrowing after an instanceof guard).

**Files to modify:**
- `skills/deep-review/references/phase3-dispatch.md` — add code-simplifier to always-on roster
- `skills/deep-review/references/validation-pipeline.md` — remove "Code-Simplifier Second Pass" section
- `agents/code-simplifier.md` — add redundant guard pattern to investigation checklist

---

## V5-09: Confidence Calibration, Threshold Adjustment, and Validator Contestation

**Status:** AGREED (flag for careful implementation)  
**Effort:** M  
**Sessions affected:** calcom-PR10967 (interface mismatch at 75 killed), discourse-PR7 (3 findings at 30-60 killed after validator penalty)

**Problem:** Three related failures in the confidence/threshold system:

1. **Agents conflate confidence with impact.** The type-design-analyzer found a verifiable interface mismatch and reported confidence 75 — it was certain the issue existed but uncertain about impact. Confidence should measure certainty ("does this issue exist?"), not impact ("does it matter?"). That's what severity is for.

2. **The 80 threshold kills plausible findings before Phase 7.** Findings in the 65-79 range that survived Phase 4 and Phase 5 are killed by Phase 6 threshold and never reach Phase 7 blind challenge — the pipeline's most rigorous quality gate.

3. **Validators can unilaterally kill findings with no appeal.** A single validator can drop confidence 48 points (78→30) and the finding never reaches Phase 7 for a second opinion. Large drops represent disagreements between discovery agent and validator — these should be contested, not resolved by the validator alone.

**Change:** Three parts working together:

### Part A: Confidence calibration across all discovery agents

Add a calibration anchor to each discovery agent's confidence section (duplicated for reliability, same pattern as false-positive exclusions):

> **Confidence measures certainty the issue exists, not its impact.** A verified interface mismatch that may never cause a runtime crash is still confidence 90+ (you verified it exists). A plausible race condition you can't prove is reachable is confidence 60-70. Use severity for impact, confidence for certainty.
>
> Calibration check: "Could I show another engineer the evidence and they'd agree the issue exists?" If yes → 80+. If "probably but they might disagree" → 60-79. If "I'm extrapolating" → below 60.

### Part B: Lower threshold from 80 to 70

Unify non-security threshold with the existing security threshold at 70. Findings in the 70-79 range that survived Phase 4 factual verification and Phase 5 validation have earned the right to face Phase 7 challenge. The Phase 7 50-finding cap prevents cost explosion.

**Companion change: triggerability cap 70 → 65.** Phase 5 validators cap confidence at 70 for findings that are only triggerable under hypothetical future changes (the "triggerability cap" from B5). With the general threshold lowered to 70, these hypothetical-only findings would now pass the threshold — defeating the purpose of the cap. Lower the triggerability cap to 65 to maintain the 5-point buffer below threshold. This applies in `validator.md` (two locations: assessment guidance and confidence rubric) and `SKILL.md` (Phase 5 reference).

### Part C: Validator contestation mechanism

When a Phase 5 validator drops confidence by more than 25 points from the discovery agent's original score, the finding is marked `contested: true` and bypasses the Phase 6 threshold — it proceeds directly to Phase 7 for independent arbitration.

Implementation in `filter_findings.py`:
- Requires the agent's original confidence preserved through Phase 5 (already tracked in validation metadata as the pre-validation score)
- During threshold filtering, check `original_confidence - current_confidence > 25`
- If contested, skip both confidence AND severity threshold checks, add `contested: true` and `contestation_reason` to finding metadata
- Rationale for severity bypass: if a validator and discovery agent disagree strongly enough to trigger contestation, the finding should reach Phase 7 for independent arbitration regardless of severity — Phase 7 is the final arbiter
- Phase 7 challenger assesses the finding independently — its score determines the final disposition using existing Phase 7 rules

Discourse-PR7 trace with all three parts (assumes V5-03 validator intent context reduces penalty severity):
- bug-2: agent 78, validator ~70 with intent context (drop ~8, not contested), threshold 70 → **survives** (Parts A+B+V5-03)
- conv-2: agent 85, validator 55 (drop 30, **contested**) → **bypasses threshold** → Phase 7 arbitrates (Part C)
- conv-5: agent 78, validator 30 (drop 48, **contested**) → **bypasses threshold** → Phase 7 arbitrates (Part C)

Note: Without V5-03 (validator intent context), bug-2 at confidence 60 would still be eliminated even with the lowered threshold. The three V5-09 parts work together with V5-03 to cover all three findings.

**Files to modify:**
- All 7 discovery agents — confidence calibration paragraph (Part A)
- `scripts/filter_findings.py` — threshold constant 80→70, contestation logic (Parts B+C)
- `tests/test_filter_findings.py` — update threshold tests, add contestation tests
- `skills/deep-review/references/validation-pipeline.md` — document new threshold and contestation mechanism

---

## V5-10: Broaden Silent Failures in Bug-Detector

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** calcom-PR8087 (unguarded dynamic import not flagged as error handling issue)

**Problem:** In calcom-PR8087, `await appStore[dirName]()` could fail silently if `dirName` doesn't match any key — the function would return undefined with no error propagation. The test-analyzer spotted the failure mode but framed it as a test gap. The bug-detector didn't flag it because its concept of "silent failures" is focused on swallowed exceptions (empty catch blocks, ignored promise rejections), not operations that silently degrade by returning null/undefined.

**Change:** Expand the "silent failures" concept in bug-detector.md's error handling section:

> **Silent failures** include not just swallowed exceptions (empty catch blocks, ignored Promise rejections), but also operations that silently degrade by returning null/undefined/empty when they can't fulfill their contract — dynamic lookups, registry accesses, computed property access, and function calls that depend on runtime-determined keys or paths. When surrounding code proceeds on the assumption such an operation succeeded without checking, the failure propagates invisibly.

This broadens the agent's concept of silent failure to cover the full class of "operations that degrade without signaling," not just the "swallowed exception" subset.

**Files to modify:**
- `agents/bug-detector.md` — error handling investigation section

---

## V5-11: Refactoring Contract Preservation in Bug-Detector

**Status:** AGREED  
**Effort:** S  
**Sessions affected:** discourse-PR8 (Ember `afterModel` promise return removed, creating race condition)

**Problem:** In discourse-PR8, `afterModel` previously returned `model.findMembers().then(...)`, making Ember wait for data before rendering. The PR removed the return, making `findMembers()` fire-and-forget — a High severity race condition. No agent understood that removing a promise return from a lifecycle hook changes async semantics. The bug is framework-agnostic: removing any mechanism by which a function communicates results or completion to its caller is a potential semantic contract change.

**Change:** Add a refactoring contract preservation principle to bug-detector.md:

> **Refactoring contract preservation.** When code is restructured, verify that each modified function's observable contract is preserved — what it returns, what it throws, what side effects it guarantees to complete before returning. Pay special attention to subtractive changes: removed `return` statements (especially before async operations or values callers depend on), removed `await`/`.then()` chains, removed error propagation (`throw`/`reject`), and removed callback invocations. These changes produce no errors and pass type checking but silently alter the function's contract for callers and frameworks.

**Files to modify:**
- `agents/bug-detector.md` — logic/data flow investigation section
