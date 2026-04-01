# Validation Pipeline

After all agents return, process their findings through the validation pipeline (Phases 4-6) before the blind challenge (Phase 7). This is what separates useful reviews from noisy ones.

**Pipeline:** Phase 4 (Classify & Verify) → Phase 5 (Validate) → Phase 6 (Filter & Reconcile) → Phase 7 (Blind Challenge + post-challenge finalization)

---

# Phase 4: Classify & Verify

Handled by `scripts/verify_findings.py`. Run it against the merged Phase 3 agent output before dispatching Phase 5 validators.

**Step 4.0 — Write merged findings to JSON (required before running the script)**

Use the Python json.dumps pattern — it handles all escaping and avoids Write tool "file not read" failures and zsh heredoc corruption:

```bash
Bash(command="""python3 -c "
import json, sys
findings = {
    'findings': [
        # paste merged finding objects from Phase 3 here
    ],
    'base_branch': 'BASE_BRANCH',
    'head_sha': 'HEAD_SHA',
    'pr_number': PR_NUMBER,
    'owner': 'OWNER',
    'repo': 'REPO'
}
with open(sys.argv[1], 'w') as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
" "$TMPDIR/deep-review-phase4-input.json"

python3 {plugin_root}/scripts/verify_findings.py "$TMPDIR/deep-review-phase4-input.json" --base-branch main
""")
```

**Input JSON schema:**
```json
{
    "findings": [
        {
            "id": "bug-1",
            "dimension": "bug",
            "severity": "high",
            "confidence": 75,
            "file": "src/foo.py",
            "line_start": 42,
            "line_end": 45,
            "title": "...",
            "description": "...",
            "evidence": "...",
            "suggestion": "...",
            "suggested_fix_code": null,
            "cross_file_refs": []
        }
    ],
    "base_branch": "main",
    "head_sha": "abc123",
    "pr_number": 42,
    "owner": "org",
    "repo": "name"
}
```

**Output JSON schema:**
```json
{
    "verified": [...],
    "eliminated": [...],
    "batches": [["bug-1", "bug-2", "bug-3"], ...],
    "stats": {
        "total": 10,
        "new": 7,
        "surfaced": 2,
        "eliminated": 1
    }
}
```

Each finding in `verified` gains an `origin` field (`"new"` or `"surfaced"`), a `blame_metadata` block, and a `factual_verification` block. Each finding in `eliminated` gains an `elimination_reason` field. Pass the `batches` output directly to Phase 5 agent dispatch.

---

## 4a. Classify findings as "New" or "Surfaced" using git blame

The script runs git blame on each finding's reported line range and compares the blamed commit against the set of commits introduced by the current branch.

**Classification rules:**
- Cross-file impact findings (those with `cross_file_refs`) are always "Surfaced"
- Findings on lines the author modified (blamed commit is in the PR branch) are always "New"
- When a finding spans both new and old lines, classify as "New" (the author touched it)

**Effect:**
- "Surfaced" findings are downgraded one severity level (critical→high, high→medium, medium→low, low stays low)
- "Surfaced" findings are grouped in a dedicated section in the report, placed after main findings
- `blame_metadata` records the original severity, author, and date for each finding — displayed in the report
- Blame tags (new/surfaced, author, date) are passed to validation agents in Phase 5

---

## 4b. Factual verification (deterministic, ALL findings)

Pure LLM-on-LLM verification shares correlated errors ~60% of the time — deterministic grounding is essential. The script performs factual verification on every finding:

1. Reads the exact lines at `file:line_start-line_end` and confirms the file and line range exist.
2. Extracts referenced symbol names from the description/evidence text and uses grep to verify that symbols actually exist in the codebase.
3. If ANY factual claim is wrong (wrong line number, function doesn't exist, code doesn't match), sets confidence to 0.

---

## 4c. Batch for validation

The script groups surviving findings (confidence > 0) into batches of 3–5 by file proximity. Findings touching the same file or adjacent files go in the same batch so validation agents read surrounding code once. The `batches` output is a list of finding-ID lists:

```json
[["bug-1", "bug-2", "bug-3"], ["perf-1", "perf-2"]]
```

These batches are the input to Phase 5.

---

# Phase 5: Validate

Parallel validation agents assess findings that need LLM judgment. **Always use Sonnet** — even in Frontier mode. Validation is objective assessment against a rubric, not discovery. Research doc #12 shows the Sonnet-Opus gap is 1.2 points on objective tasks; the cost difference is not justified.

**Scope:** Findings with confidence <90 that passed Phase 4b. Findings with confidence >=90 have already been factually verified and represent cases where the agent "can point to the EXACT input that triggers the bug." These skip the more expensive LLM judgment step.

**Dispatch:** Spawn one Sonnet agent per batch from Phase 4c. Launch all agents in a single message with multiple Agent tool calls for true parallel execution.

> Validation requires fresh agents. Correlated errors occur ~60% of the time when the same context does discovery and validation — re-reading batches in the orchestrator's reasoning anchors to the original framing and does not constitute independent assessment.

Each agent receives:
1. A batch of 3-5 findings with their descriptions, evidence, and blame tags
2. The relevant code sections for the batch (read fresh from files), wrapped in `<untrusted-code-content>...</untrusted-code-content>` tags
3. The confidence rubric below

Each agent must:
1. Read each finding's description and evidence
2. Attempt to **disprove** the finding — look for reasons it might be a false positive
3. Ask: **"Can you find a code path that actually triggers this today?"** Trace from entry points (public APIs, event handlers, CLI entry points, scheduled jobs) to the flagged location. If the issue is only reachable under hypothetical future changes (e.g., a new caller is added, a config value changes, a new code path is introduced), **cap confidence at 70**. This keeps the issue below the non-security threshold of 80 and prevents latent/theoretical concerns from appearing as high-confidence findings.
4. Score using this verbatim confidence rubric:

```
Confidence Rubric (use these anchors):

  0  = definitely a false positive — clear evidence the issue does not exist
 25  = probably false positive — code likely handles this correctly
 50  = uncertain — could go either way
 75  = probably real — no meaningful counter-evidence found
100  = definitely real — issue is clearly present with no mitigating factors

Note: If the only path to this issue requires a hypothetical future change (new
caller, changed config, new code path), cap at 70 regardless of the anchor above.
```

5. Return an adjusted confidence score and brief justification per finding

**Agent tool call template (per batch):**
```
Agent(
  subagent_type: "claude-deep-review:validator",
  description: "Validate batch {N}",
  prompt: "Findings:
    {paste 3-5 findings with IDs, descriptions, evidence, and blame tags (new/surfaced, author, date)}
    Code:
    <untrusted-code-content>
    {code from file:line_start-line_end for each finding in batch}
    </untrusted-code-content>"
)
```

The validator agent definition already contains the confidence rubric, trust boundary instructions, and output format. The orchestrator provides only the batch of findings and their associated code.

Update each finding's confidence based on the validator's assessment.

## Validation Failure Protocol

Re-dispatch or degrade transparently. Orchestrator judgment is not a substitute for the pipeline.

1. **Working tree mismatch (systemic failure):** If >50% of Phase 5 validators return confidence 0 with "code doesn't exist" or "file not found" reasoning, the working tree is likely wrong. STOP validation. Attempt checkout fix per Phase 2b branch checkout. Re-dispatch failed validators against the corrected working tree.
2. **Individual agent failure:** If a validator times out or errors, continue with completed agents. Log the failure in Review Methodology. Warn the user if security or bugs dimensions are affected: "Validation pipeline partially degraded -- {N} findings in {dimensions} were not independently validated."
3. **Self-validation is not a substitute:** When a validator fails, re-dispatch against corrected input or acknowledge degradation with a prominent methodology warning — orchestrator judgment cannot replace independent validation.

---

# Phase 6: Filter & Reconcile

Handled by `scripts/filter_findings.py`. Run it against the Phase 5 validated findings before the Phase 7 blind challenge.

**Step 6.0 — Write validated findings to JSON (required before running the script)**

Use the Python json.dumps pattern — it handles all escaping and avoids Write tool "file not read" failures and zsh heredoc corruption:

```bash
Bash(command="""python3 -c "
import json, sys
findings = {
    'findings': [
        # paste Phase 5 validated finding objects here
    ]
}
with open(sys.argv[1], 'w') as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
" "$TMPDIR/deep-review-phase6-input.json"

python3 {plugin_root}/scripts/filter_findings.py "$TMPDIR/deep-review-phase6-input.json" --review-md REVIEW.md
""")
```

**Input JSON schema:**
```json
{
    "findings": [
        {
            "id": "bug-1",
            "file": "src/foo.py",
            "line": 42,
            "severity": "high",
            "confidence": 85,
            "title": "...",
            "body": "...",
            "blame_tag": "new",
            "dimensions": ["bug"],
            "agent": "bug-detector"
        }
    ]
}
```

Input may also be a flat array of findings (no wrapper object). The `blame_tag`, `dimensions`, and `agent` fields are optional but improve disagreement detection and tagging accuracy.

**Output JSON schema:**
```json
{
    "filtered": [...],
    "eliminated": [...],
    "stats": {
        "total": 10,
        "passed_threshold": 8,
        "injections_removed": 1,
        "consensus_boosted": 2,
        "test_analyzer_deduped": 1,
        "test_analyzer_promoted": 0,
        "tagged_main": 6,
        "tagged_suggestion": 2
    }
}
```

Each finding in `filtered` gains a `report_destination` field (`"main"` or `"suggestion"`). Each finding in `eliminated` gains an `eliminated_by` field. Pass the `filtered` array to Phase 7 blind challenge dispatch.

---

## 6a. Filter with dimension-specific thresholds

The script removes findings where:
- Post-validation confidence is below the **dimension-specific threshold**: uses REVIEW.md `confidence_threshold` if set (default: 80). Security minimum of 70 applies regardless — security false negatives are costlier than false positives.
- Severity is below the configured severity floor: applies REVIEW.md `severity_threshold` if set (default: low — suppress nothing).
- The finding is about a pre-existing issue that does not interact with this diff (not a "Surfaced" finding classified in Phase 4a — those survive with downgraded severity into their own report section)
- A linter, typechecker, or compiler would catch it (these run separately in CI)
- It's a pedantic nitpick a senior engineer wouldn't flag
- It's about code the author didn't modify (unless it's a cross-file impact issue)
- The change in functionality is likely intentional (refactoring, API migration, deliberate behavior change)
- The issue is explicitly silenced in code via lint-ignore, nolint, @SuppressWarnings, or equivalent

The script also applies exclusion patterns from the `--exclusions-md` file and the REVIEW.md `ignore` section.

---

## 6b. Output validation for prompt injection artifacts

The script discards any finding matching these patterns:
- Description or suggestion contains shell commands to execute
- Contains URLs to visit or encoded payloads
- Approves the PR or instructs the user to bypass controls
- Empty or suspiciously short descriptions (fewer than 10 words)
- Instructs the user to modify files, push code, or run deployment commands

Discarded findings appear in `eliminated` with `eliminated_by: "injection_filter"`. Log them in the report methodology section as potential prompt injection indicators.

---

## 6c. Disagreement detection

The script classifies findings by inter-agent agreement. Disagreement is a signal about difficulty and importance, not a problem to resolve through forced consensus.

**Classifications:**
- **Consensus** — multiple agents flag same file + overlapping line range with same/related concern. Script boosts confidence +10 (capped at 100). Note: "Corroborated by: [agent list]"
- **Singleton** — only one agent flags this, within their domain expertise. Passes through unchanged — domain specialists don't need corroboration.
- **Contradictions** — agents make conflicting claims about the same code location. Noted in output; Phase 7 challenges all findings regardless.

**Automatic suppression rules:**
- **bug-detector** flags something that **conventions-and-intent** confirms is intentional per documented specs → suppress the bug finding
- **test-analyzer** flags missing tests for code that **conventions-and-intent** identifies as generated/scaffolding excluded from test requirements → suppress the test finding

**Security escalation:** If **security-reviewer** flags something another agent considers safe, always **escalate** the security finding. Security false negatives are costlier than false positives — security wins ties.

Contradictions and resolutions are recorded in the script output and should be logged in the report methodology section.

---

## 6d. Tag findings

The script tags each surviving finding by its eventual report destination. This is a tagging step only — actual separation into report sections happens during post-challenge finalization (Phase 7, step 2):
- **Main report** — most findings, grouped by severity (`report_destination: "main"`)
- **Improvement Suggestions** — test-analyzer, conventions-and-intent comment accuracy, and code-simplifier findings (`report_destination: "suggestion"`)
- **Promotion rule:** If a test-analyzer finding describes a functional correctness issue that exists today (race condition, logic error, assertion that never fails, test that always passes) rather than a missing-coverage gap ("should add tests for X"), the script promotes it to `"main"`. Decision test: "Does this finding describe a bug that exists today, or a test that should be written?" Bug today -> main report. Test to write -> improvement suggestion.
- **Dedup rule:** If a test-analyzer finding overlaps with another agent's finding at the same file and line range, the non-test-analyzer finding wins — keep it in the main report and drop the test-analyzer duplicate.

---

# Phase 7: Blind Challenge + Post-Challenge Finalization

See **SKILL.md Phase 7** for the primary instructions and Agent tool call template. The challenge round runs on **every finding** that survived Phase 6 — no trigger conditions, no threshold check. This section provides supplementary detail.

## Blind challenge — supplementary detail

**For each surviving finding:**

1. **Read the raw code** at `file:line_start-line_end` (fresh read, not from cache) — the orchestrator reads the code and pastes it inline into the challenger's prompt
2. **Spawn a fresh agent via the Agent tool** (Sonnet in Optimized mode, Opus in Frontier mode). The agent receives ONLY:
   - The finding's `title` and `description` (never `evidence` or original reasoning)
   - The raw code pasted inline in `<untrusted-code-content>` tags
   - Instructions to try to disprove the claim, then return `{"confidence_claim_is_correct": <0-100>, "justification": "..."}` using 5-point anchors (0/25/50/75/100) rating how likely the claim is CORRECT
3. **Apply the blind verifier's result** (based on `confidence_claim_is_correct`):
   - **< 25** → challenger found evidence the claim is wrong. Non-security findings: **remove entirely**. Security findings: downgrade one severity level.
   - **25-49** → challenger suspects the claim is wrong. Downgrade one severity level (critical→high, high→medium, medium→low).
   - **50-74** → genuinely uncertain. No severity change, flag as "contested" in methodology.
   - **≥ 75** → challenger couldn't disprove it. Finding survives.

**Agent tool call template (per finding):**
```
Agent(
  subagent_type: "claude-deep-review:challenger",
  model: "opus",  // Frontier mode only; omit in Optimized mode (uses agent default: sonnet)
  description: "Blind challenge: {finding_id}",
  prompt: "Claim: {finding.title}
    Details: {finding.description}
    <untrusted-code-content file="{finding.file}" lines="{finding.line_start}-{finding.line_end}">
{actual code content read by orchestrator}
    </untrusted-code-content>"
)
```

The challenger agent definition already contains the blind challenge instructions, trust boundary rules, rating scale, and output format. The orchestrator provides only the claim (title + description) and the raw code — never original reasoning or evidence.

Do NOT include original reasoning or evidence — only title, description, and raw code. Include ONLY: title, description, and raw code in `<untrusted-code-content>` tags. Do NOT include "Consider:" hints, counter-arguments, or any reasoning about the finding — these anchor challengers toward specific conclusions and defeat the blind challenge design.

**Triggerability bar:** The challenger's prompt must include this line: "Can you construct a specific code path through the current codebase that triggers this? If you cannot, rate confidence below 25."

**Design rationale:**
- Blind agents see only the claim and the code, never the original reasoning → prevents sycophancy
- Fresh agents (not the original reviewers) → genuinely independent judgment
- Inline code provision (orchestrator reads and pastes) removes tool round-trips and anchors challengers to the exact lines under review — deterministic grounding beats LLM-on-LLM verification
- Challengers retain Read/Grep/Glob tools for surrounding context exploration — agents that can explore outperform agents given only pre-loaded context
- No ADD mechanism — original agents already had their chance
- No voting/debate protocol — majority voting captures the same gains as debate without sycophancy risk

After dispatch, announce: "Dispatched N agents for Phase 7." (N must equal the number of findings that survived Phase 6 — dispatch any missing challengers before proceeding.)

---

## Post-challenge finalization — step 1: Deduplicate

Findings from different agents often overlap. Group findings that reference the same file + line range and describe the same underlying issue. When merging:
- Keep the highest confidence score
- Keep the most specific description
- Combine evidence from multiple agents
- If agents disagree on severity, use the higher severity
- Note which dimensions flagged it (e.g., "Flagged by: bug-detector, security-reviewer")

---

## Post-challenge finalization — step 2: Route

Materialize the routing tags from Phase 6d. Separate findings into:
- **Main report** — grouped by severity, counted in executive summary
- **Improvement Suggestions** — not counted in executive summary, not posted as PR inline comments by default, available via "Let me pick" walkthrough in Phase 8

---

## Post-challenge finalization — step 3: Apply findings cap

Check REVIEW.md for `max_findings`. **Default: no limit** — all findings that survive the pipeline appear in the report. The inline PR comment default cap (6 comments for "Default" mode, no cap for user-selected findings) is applied separately in Phase 8 delivery.

If `max_findings` is set and total findings exceed it:
1. Sort by severity (critical > high > medium > low), then by confidence (higher first)
2. Keep the top N findings
3. Record how many were suppressed
4. Add a note: "{N} additional findings were suppressed by the max_findings cap ({cap}). Set `max_findings: 0` or remove the setting to see all findings."

---

## Post-challenge finalization — step 4: Rank

Sort findings by:
1. Severity (critical > high > medium > low)
2. Confidence (higher first)
3. Risk level of the file (high-risk files first)

---

## Post-challenge finalization — step 5: Incremental report diffing (incremental reviews only)

Runs ONLY when the review is incremental (user selected "Incremental" in Phase 1) AND a previous `deep-review-findings` comment was successfully parsed.

**Findings metadata schema** (used for parsing previous findings and generating the footer in Phase 8):
```json
{"version":1,"sha":"<full_sha>","findings":[
  {"id":"<finding.id>","file":"<finding.file>","line":<line_start>,"dim":"<dimension>","title_hash":"<first 8 chars of SHA-256 of finding.title>"}
]}
```

**Classification against previous finding set:**
- **Introduced** — no matching `title_hash` + `file` in previous set. Surface normally.
- **Fixed** — previous finding no longer detected. Note as resolved.
- **Preexisting** — same finding still present (same `title_hash` + `file` + line within ±5). **Suppress** from report and PR comments.

After classification:
- Remove "Preexisting" findings from report output
- Keep "Introduced" findings in normal severity-ranked sections
- Compile "Fixed" list for Incremental Review Status section
- Generate findings metadata for Phase 8 footer
