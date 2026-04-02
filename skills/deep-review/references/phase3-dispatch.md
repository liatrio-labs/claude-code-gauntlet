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
- **cross-file-impact-analyzer**: **all files** + must search entire codebase for callers/implementors of changed public symbols
- **test-analyzer**: changed production files + test files (2g)
- **conventions-and-intent**: **all files** (needs full scope for convention and intent checking)
- **type-design-analyzer**: files with new type definitions (only dispatched when new types introduced)
- **code-simplifier**: **all changed files** (dispatched after Phase 6 filtering — see Phase 6)

All agents can still **pull** additional context — scoping controls what is pre-loaded, not what is accessible.

---

## Agent Roster

**Always-on (5)** — default model per agent is defined in each agent's frontmatter; Frontier mode overrides to `opus` at dispatch:

1. **bug-detector** — Logic errors, edge cases, null handling, race conditions, API misuse. Subagent: `claude-deep-review:bug-detector`.
2. **security-reviewer** — OWASP top 10, injection, auth bypass, data exposure, crypto. Always Opus. Subagent: `claude-deep-review:security-reviewer`.
3. **cross-file-impact** — Caller/dependent tracing, cross-module impact. Subagent: `claude-deep-review:cross-file-impact`.
4. **test-analyzer** — Coverage gaps, test quality, DAMP principles. Subagent: `claude-deep-review:test-analyzer`.
5. **conventions-and-intent** — CLAUDE.md/REVIEW.md adherence, intent alignment, comment accuracy. Subagent: `claude-deep-review:conventions-and-intent`.

**Conditional (2):**

6. **type-design-analyzer** — Type encapsulation, invariant expression. Only if new types introduced. Subagent: `claude-deep-review:type-design-analyzer`.
7. **code-simplifier** — Simplification opportunities, dead code. POST-review only, only if no critical/high. Subagent: `claude-deep-review:code-simplifier`.

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
    Scoped diff (HIGH and MEDIUM risk files only, plus test files and history context):
    <untrusted-code-content>
    {diff scoped to high + medium risk diffs, test files (2g), history context (2i)}
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
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
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
    Scoped diff (files with new type definitions only):
    <untrusted-code-content>
    {diff scoped to files with new type definitions}
    </untrusted-code-content>"
)
```

**For code-simplifier (conditional — post-review only, if no critical/high findings):**
```
Agent(
  subagent_type: "claude-deep-review:code-simplifier",
  description: "Review: code-simplifier",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
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
    Scoped diff (HIGH and MEDIUM risk files only, plus test files and history context):
    <untrusted-code-content>
    {diff scoped to high + medium risk diffs, test files (2g), history context (2i)}
    </untrusted-code-content>"
)
```

The agent definition (in `agents/{dimension}.md`) handles: agent role, instructions, exclusion list, confidence rubric, output schema, effort, and default model. Do not re-assemble these in the prompt — they are already baked into the named subagent.

After dispatch, announce: "Dispatched N agents for Phase 3."

---

## Parsing Agent Output

Agents emit findings incrementally — one JSON block per finding, interspersed with investigation prose and `SKIP: <reason>` lines. The orchestrator must parse each agent's text output to extract findings:

1. Scan the agent's output for top-level JSON objects (delimited by `{` ... `}` at the outermost nesting level).
2. For each extracted JSON block, validate it has an `"id"` field matching the agent's prefix (e.g., `"bug-1"`, `"security-2"`).
3. Discard `SKIP:` lines and non-JSON text — these are investigation notes.
4. If an agent's output appears truncated (ends mid-JSON or mid-sentence), collect all complete JSON blocks emitted before the truncation point. Log the truncation in Review Methodology but do not discard the partial results.

This replaces the previous expectation of a single JSON array per agent. The merge step (in SKILL.md) combines parsed findings from all agents into the unified findings object for Phase 4.

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
