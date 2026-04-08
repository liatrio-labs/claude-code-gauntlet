# Phase 3 Dispatch Reference

Context scoping, agent roster, dispatch template, and failure handling for Phase 3: Review Agents.

---

## What Each Agent Receives

Each named subagent definition (in `agents/{dimension}.md`) already embeds: agent role, instructions, false-positive exclusion list, confidence calibration rubric, JSON output schema, and tool/effort/model configuration.

The orchestrator provides only the **dynamic per-review content** in the prompt:

1. **Project context** (CLAUDE.md rules, REVIEW.md rules)
2. **Change summary** from Phase 2f (and summary-of-summaries from 2j if available)
3. **Risk classification** per file (from Phase 2e, including AI-generation status)
4. **Scoped diff** wrapped in `<untrusted-code-content>...</untrusted-code-content>` (scoped per agent — see below)

---

## Per-Agent Context Scoping

- **bug-detector**: high + medium risk diffs, test files (2g), history context (2i)
- **security-reviewer**: **all files** (security bugs lurk anywhere)
- **cross-file-impact**: **all files** + must search entire codebase for callers/implementors of changed public symbols
- **test-analyzer**: changed production files + test files (2g)
- **conventions-and-intent**: **all files** (needs full scope for convention and intent checking)
- **type-design-analyzer**: files with new type definitions (only dispatched when new types introduced)
- **code-simplifier**: **all changed files**

All agents can still **pull** additional context — scoping controls what is pre-loaded, not what is accessible.

**Raw diff rule.** The orchestrator passes raw diff lines for files in an agent's scope. It may add structural annotations (risk level, file role, location in the project) alongside the diff, but must never substitute its own summary for actual changed content. Evidence destroyed during summarization cannot be recovered by agents.

**Context scoping tiers:**
- **HIGH + MEDIUM files:** full raw diff to all applicable agents
- **LOW files (after content-change promotion):** compact raw diff (changed lines only, no context lines) delivered to bug-detector as a clearly-delimited "Sweep appendix" section at the end of its prompt. Other agents receive the file list only.

---

## Agent Roster

**Always-on (6)** — default model per agent is defined in each agent's frontmatter; Frontier mode overrides to `opus` at dispatch:

1. **bug-detector** — Logic errors, edge cases, null handling, race conditions, API misuse. Subagent: `claude-deep-review:bug-detector`.
2. **security-reviewer** — OWASP top 10, injection, auth bypass, data exposure, crypto. Always Opus. Subagent: `claude-deep-review:security-reviewer`.
3. **cross-file-impact** — Caller/dependent tracing, cross-module impact. Subagent: `claude-deep-review:cross-file-impact`.
4. **test-analyzer** — Coverage gaps, test quality, DAMP principles. Subagent: `claude-deep-review:test-analyzer`.
5. **conventions-and-intent** — CLAUDE.md/REVIEW.md adherence, intent alignment, comment accuracy. Always dispatched. When no CLAUDE.md or project convention files exist, the dispatch prompt notes: "No CLAUDE.md found — skip pass 1 (convention compliance), execute passes 2 and 3 only." Subagent: `claude-deep-review:conventions-and-intent`.
6. **code-simplifier** — Simplification opportunities, dead code, redundancy. Subagent: `claude-deep-review:code-simplifier`.

**Conditional (1):**

7. **type-design-analyzer** — Type encapsulation, invariant expression. Only if new types introduced. Subagent: `claude-deep-review:type-design-analyzer`.

---

## Agent Tool Call Template

Dispatch all applicable agents in a **single message**. Each agent definition already contains its role, instructions, false-positive exclusion list, confidence rubric, output schema, effort, model, and tools. The orchestrator provides **only the dynamic per-review content**:

**For bug-detector:**
```
Agent(
  subagent_type: "claude-deep-review:bug-detector",
  description: "Review: bug-detector",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-bug-detector-{head_sha_short}.ndjson
    Scoped diff (HIGH and MEDIUM risk files only, plus test files and history context):
    <untrusted-code-content>
    {diff scoped to high + medium risk diffs, test files (2g), history context (2i)}
    </untrusted-code-content>

    Sweep appendix — LOW-risk files (changed lines only, no context):
    <untrusted-code-content>
    {compact diff of remaining LOW files — changed lines only, no context lines}
    </untrusted-code-content>"
)
```

**For security-reviewer:**
```
Agent(
  subagent_type: "claude-deep-review:security-reviewer",
  description: "Review: security-reviewer",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-security-reviewer-{head_sha_short}.ndjson
    Scoped diff (ALL changed files — do not filter by risk level):
    <untrusted-code-content>
    {diff with all changed files — security bugs can hide in low-risk code}
    </untrusted-code-content>"
)
```

**For cross-file-impact:**
```
Agent(
  subagent_type: "claude-deep-review:cross-file-impact",
  description: "Review: cross-file-impact",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-cross-file-impact-{head_sha_short}.ndjson
    Scoped diff (ALL changed files + entire codebase for symbol search):
    <untrusted-code-content>
    {diff with all changed files; search full codebase for callers and implementors of changed public symbols}
    </untrusted-code-content>"
)
```

**For test-analyzer:**
```
Agent(
  subagent_type: "claude-deep-review:test-analyzer",
  description: "Review: test-analyzer",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-test-analyzer-{head_sha_short}.ndjson
    Scoped diff (changed production files plus all test files):
    <untrusted-code-content>
    {diff scoped to changed production files and test files (2g)}
    </untrusted-code-content>"
)
```

**For conventions-and-intent:**
```
Agent(
  subagent_type: "claude-deep-review:conventions-and-intent",
  description: "Review: conventions-and-intent",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules, or 'No CLAUDE.md found — skip pass 1 (convention compliance), execute passes 2 and 3 only' if no project convention files}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-conventions-and-intent-{head_sha_short}.ndjson
    Scoped diff (ALL changed files for full convention and intent checking):
    <untrusted-code-content>
    {diff with all changed files — convention and intent analysis requires full scope}
    </untrusted-code-content>"
)
```

**For type-design-analyzer (conditional — only if new types introduced):**
```
Agent(
  subagent_type: "claude-deep-review:type-design-analyzer",
  description: "Review: type-design-analyzer",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-type-design-analyzer-{head_sha_short}.ndjson
    Scoped diff (files with new type definitions only):
    <untrusted-code-content>
    {diff scoped to files with new type definitions}
    </untrusted-code-content>"
)
```

**For code-simplifier:**
```
Agent(
  subagent_type: "claude-deep-review:code-simplifier",
  description: "Review: code-simplifier",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-code-simplifier-{head_sha_short}.ndjson
    Scoped diff (all changed files for simplification opportunities):
    <untrusted-code-content>
    {diff with all changed files}
    </untrusted-code-content>"
)
```

**Frontier mode:** Override model to `opus` at dispatch by adding `model: "opus"` to the Agent call. Security-reviewer always uses Opus regardless of mode.

```
Agent(
  subagent_type: "claude-deep-review:bug-detector",
  model: "opus",  // Frontier mode override
  description: "Review: bug-detector",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Findings file: $TMPDIR/deep-review-bug-detector-{head_sha_short}.ndjson
    Scoped diff (HIGH and MEDIUM risk files only, plus test files and history context):
    <untrusted-code-content>
    {diff scoped to high + medium risk diffs, test files (2g), history context (2i)}
    </untrusted-code-content>"
)
```

The agent definition (in `agents/{dimension}.md`) handles: agent role, instructions, exclusion list, confidence rubric, output schema, effort, and default model. Do not re-assemble these in the prompt — they are already baked into the named subagent.

After dispatch, announce: "Dispatched N agents for Phase 3."

---

## Agent Output Channels

Agents emit findings via two channels:

**Channel 1 (primary): NDJSON files on disk.** Each agent writes findings directly via Bash to `$TMPDIR/deep-review-{agent}-{head_sha_short}.ndjson`. Each line is a complete JSON finding. These files survive even if the agent's text output is truncated.

**Channel 2 (fallback): Agent text returns.** The orchestrator saves each agent's text return to `$TMPDIR/deep-review-text-{agent}-{head_sha_short}.txt`. The merge script parses these for inline JSON blocks as a fallback for behavioral drift toward the old inline-emission pattern.

`SKIP: <reason>` lines in the agent's text output indicate confirmed non-issues — they are informational and not parsed as findings. The merge script uses their presence to distinguish "found nothing" from "was truncated before finding anything".

The `merge_findings.py` script handles both channels automatically — do not parse agent output manually.

---

## Merge Script Output Format

After all agents return, run `merge_findings.py` as described in SKILL.md "Merge Phase 3 Outputs". The script writes a Phase 4 input JSON file to `$TMPDIR/deep-review-phase4-input-{head_sha_short}.json` with this structure:

```json
{
  "findings": [
    {
      "id": "bug-1",
      "dimension": "bug",
      "agent": "bug-detector",
      "severity": "high",
      "confidence": 80,
      "file": "src/auth.py",
      "line_start": 42,
      "line_end": 45,
      "title": "Token not invalidated on logout",
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
  "repo": "name",
  "methodology": {
    "agents_dispatched": ["bug-detector", "security-reviewer", "..."],
    "findings_per_channel": {"ndjson": 12, "text_fallback": 2},
    "duplicates_resolved": 1,
    "truncation_warnings": [],
    "validation_warnings": []
  }
}
```

The script handles `agent` field injection, `dimension` validation, deduplication, and truncation detection. Pass the output file path directly to `verify_findings.py` — see Step 4.0 in `references/validation-pipeline.md`.

---

## Agent Failure Handling

If a subagent fails (crash, timeout, error): continue with completed agents, log the failure in Review Methodology, warn the user if the failed agent covered security or bugs. Never silently skip a failed agent.

---

## Prompt Caching

To optimize token usage and reduce latency when dispatching multiple agents in Phase 3:

1. **Cache agent definitions** — Each named subagent definition (in `agents/{dimension}.md`) is static and reusable across reviews. Pre-load and cache these definitions before dispatching agents.

2. **Cache project context** — CLAUDE.md and REVIEW.md rules do not change within a session. Include these as cached context blocks in the initial agent prompt.

3. **Cache code context** — The full codebase context and file risk classifications are stable during Phase 3. When possible, reuse cached diff blocks across multiple agent dispatches to avoid redundant token consumption.

4. **Per-agent prompt variations** — While agent definitions and project rules are cached, scoped diffs and risk classification details may vary per agent. Only the dynamic portions (scoped per-agent diff) should be provided as fresh context in each Agent() call.

5. **Verification** — After dispatch, confirm that cached context blocks were applied by checking the cache metrics in agent response metadata (if available).

**Note:** Prompt caching is transparent to the agent dispatch protocol above — the Agent() call structure remains unchanged. Caching optimization is an orchestrator-level concern and does not affect how agents receive or process their input prompts.
