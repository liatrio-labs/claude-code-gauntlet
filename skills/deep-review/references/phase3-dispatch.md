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

```
Agent(
  subagent_type: "claude-deep-review:{dimension}",  // e.g. "claude-deep-review:bug-detector"
  description: "Review: {dimension}",
  prompt: "Project context: {CLAUDE.md rules, REVIEW.md rules}
    Change summary: {from Phase 2f}
    Risk classification: {per-file risk levels from Phase 2e, including AI-generation status}
    Scoped diff:
    <untrusted-code-content>
    {diff scoped per agent — see Per-Agent Context Scoping above}
    </untrusted-code-content>"
)
```

**Frontier mode:** Override model to `opus` at dispatch by adding `model: "opus"` to the Agent call. Security-reviewer always uses Opus regardless of mode.

```
Agent(
  subagent_type: "claude-deep-review:bug-detector",
  model: "opus",  // Frontier mode override
  description: "Review: bug-detector",
  prompt: "..."
)
```

The agent definition (in `agents/{dimension}.md`) handles: agent role, instructions, exclusion list, confidence rubric, output schema, effort, and default model. Do not re-assemble these in the prompt — they are already baked into the named subagent.

**Self-verification checkpoint:** Before Phase 4, confirm you emitted Agent tool_use blocks for ALL applicable review agents. If you wrote analysis text instead, stop and spawn the agents now.

---

## Agent Failure Handling

If a subagent fails (crash, timeout, error): continue with completed agents, log the failure in Review Methodology, warn the user if the failed agent covered security or bugs. Never silently skip a failed agent.
