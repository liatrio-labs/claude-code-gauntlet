# Phase 3 Dispatch Reference

Context scoping, agent roster, dispatch template, and failure handling for Phase 3: Review Agents.

---

## What Each Agent Receives

Each named subagent definition (in `agents/{dimension}.md`) already embeds: agent role, instructions, false-positive exclusion list, confidence calibration rubric, JSON output schema, and tool/effort/model configuration.

The orchestrator provides only **two paths** in the dispatch prompt:

1. **Context file path** — absolute path to `{output_dir}/deep-review-context-{head_sha_short}.md` (written during Phase 2)
2. **Findings file path** — absolute path to `{output_dir}/deep-review-{agent}-{head_sha_short}.ndjson`

All shared context (project rules, change summary, risk classification, diff, test files, history) lives in the context file. Agents use Read to load it at the start of their investigation. This keeps each dispatch prompt to ~100 tokens, ensuring all 7 Agent tool_use blocks fit in a single response.

---

## Per-Agent Context Scoping

The context file contains the full diff. Agents scope their investigation based on their agent definition's instructions:

- **bug-detector**: Focuses on HIGH/MEDIUM risk files; LOW files are sweep-only
- **security-reviewer**: Reviews **all files** (security bugs lurk anywhere)
- **cross-file-impact**: Reviews **all files** + must search entire codebase for callers/implementors of changed public symbols
- **test-analyzer**: Focuses on changed production files + test files
- **conventions-and-intent**: Reviews **all files** (needs full scope for convention and intent checking)
- **type-design-analyzer**: Focuses on files with new type definitions (only dispatched when new types introduced)
- **code-simplifier**: Reviews **all changed files**

All agents can still **pull** additional context — scoping controls what they focus on, not what is accessible.

**Raw diff rule.** The context file contains raw diff lines. The orchestrator must never substitute its own summary for actual changed content. Evidence destroyed during summarization cannot be recovered by agents.

**Context scoping tiers:**
- **HIGH + MEDIUM files:** full raw diff available to all applicable agents
- **LOW files (after content-change promotion):** compact raw diff (changed lines only, no context lines) included in the context file as a clearly-delimited "Sweep appendix" section

---

## Agent Roster

**Always-on (6)** — default model per agent is defined in each agent's frontmatter; Frontier mode overrides to `opus` at dispatch:

1. **bug-detector** — Logic errors, edge cases, null handling, race conditions, API misuse. Subagent: `deep-review:bug-detector`.
2. **security-reviewer** — OWASP top 10, injection, auth bypass, data exposure, crypto. Always Opus. Subagent: `deep-review:security-reviewer`.
3. **cross-file-impact** — Caller/dependent tracing, cross-module impact. Subagent: `deep-review:cross-file-impact`.
4. **test-analyzer** — Coverage gaps, test quality, DAMP principles. Subagent: `deep-review:test-analyzer`.
5. **conventions-and-intent** — CLAUDE.md/REVIEW.md adherence, intent alignment, comment accuracy. Always dispatched. When no CLAUDE.md or project convention files exist, the context file notes: "No CLAUDE.md found — skip pass 1 (convention compliance), execute passes 2 and 3 only." Subagent: `deep-review:conventions-and-intent`.
6. **code-simplifier** — Simplification opportunities, dead code, redundancy. Subagent: `deep-review:code-simplifier`.

**Conditional (1):**

7. **type-design-analyzer** — Type encapsulation, invariant expression. Only if new types introduced. Subagent: `deep-review:type-design-analyzer`.

---

## Dispatch Mandate

**MANDATORY: Emit ALL Agent tool_use blocks in a SINGLE response.** You MUST dispatch all 7 (or 6) agents in one message containing multiple Agent tool calls. Never split agents across multiple responses — not 2+3+2, not 4+3, not any other combination. All agents are fully independent with no shared state. Batching adds 5-10 minutes of unnecessary latency. Each prompt is ~100 tokens (just two file paths) — all 7 fit easily in a single response.

### Anti-patterns (WRONG)

These are WRONG: dispatching 2 agents, waiting, then 3 more, then 2 more. Dispatching 4 agents then 3 in a follow-up. Dispatching 1 agent at a time. Any pattern that splits agents across multiple responses wastes minutes of wall-clock time and violates the protocol.

### Fallback recovery

If you emitted fewer than all agents in the previous message, dispatch the remaining agents immediately in the next message. Do not re-analyze or re-triage — just emit the remaining Agent tool calls.

### Never use `run_in_background: true`

Background agents cannot write files, lose output silently, and cause session hangs. Foreground parallel dispatch in one message is the canonical pattern. Never set `run_in_background: true` on Phase 3 agent calls.

---

## Agent Tool Call Template

Dispatch all applicable agents in a **single message**. Each prompt contains only the context file path and findings file path — all shared context lives in the file. Use **absolute paths** (agents may not share the orchestrator's working directory).

**Template (same structure for every agent):**
```
Agent(
  subagent_type: "deep-review:{agent-name}",
  description: "Review: {agent-name}",
  prompt: "Review context: {abs_output_dir}/deep-review-context-{head_sha_short}.md
    Findings file: {abs_output_dir}/deep-review-{agent-name}-{head_sha_short}.ndjson
    Read the context file first to get the project rules, change summary, risk classification, and diff."
)
```

**Frontier mode:** Add `model: "opus"` to override the agent definition's default model. Security-reviewer always uses Opus regardless of mode.

**Correct dispatch pattern — all 7 in ONE message:**

```
Agent(subagent_type: "deep-review:bug-detector", description: "Review: bug-detector", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-bug-detector-abc12345.ndjson\nRead the context file first.")

Agent(subagent_type: "deep-review:security-reviewer", model: "opus", description: "Review: security-reviewer", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-security-reviewer-abc12345.ndjson\nRead the context file first.")

Agent(subagent_type: "deep-review:cross-file-impact", description: "Review: cross-file-impact", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-cross-file-impact-abc12345.ndjson\nRead the context file first.")

Agent(subagent_type: "deep-review:test-analyzer", description: "Review: test-analyzer", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-test-analyzer-abc12345.ndjson\nRead the context file first.")

Agent(subagent_type: "deep-review:conventions-and-intent", description: "Review: conventions-and-intent", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-conventions-and-intent-abc12345.ndjson\nRead the context file first.")

Agent(subagent_type: "deep-review:type-design-analyzer", description: "Review: type-design-analyzer", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-type-design-analyzer-abc12345.ndjson\nRead the context file first.")

Agent(subagent_type: "deep-review:code-simplifier", description: "Review: code-simplifier", prompt: "Review context: /abs/path/.deep-review/deep-review-context-abc12345.md\nFindings file: /abs/path/.deep-review/deep-review-code-simplifier-abc12345.ndjson\nRead the context file first.")
```

After dispatch, announce: "Dispatched N agents for Phase 3."

---

## Agent Output Channels

Agents emit findings via two channels:

**Channel 1 (primary): NDJSON files on disk.** Each agent writes findings directly via Bash to `{output_dir}/deep-review-{agent}-{head_sha_short}.ndjson`. Each line is a complete JSON finding. These files survive even if the agent's text output is truncated.

**Channel 2 (fallback): Agent text returns.** The orchestrator saves each agent's text return to `{output_dir}/deep-review-text-{agent}-{head_sha_short}.txt`. The merge script parses these for inline JSON blocks as a fallback for behavioral drift toward the old inline-emission pattern.

`SKIP: <reason>` lines in the agent's text output indicate confirmed non-issues — they are informational and not parsed as findings. The merge script uses their presence to distinguish "found nothing" from "was truncated before finding anything".

The `merge_findings.py` script handles both channels automatically — do not parse agent output manually.

---

### AST-Safe Emission Protocol

Agents must use ONLY `printf '%s\n'` (not `echo`) — zsh's builtin `echo` interprets `\n` as newlines even inside single quotes, breaking NDJSON when evidence fields contain code with `\n`. `printf '%s\n'` treats the argument as literal text. The sandbox's tree-sitter AST parser rejects all other quoting forms in subagent sessions:

```bash
printf '%s\n' '<json_payload_with_no_literal_single_quotes>' >> ".deep-review/deep-review-{agent}-{sha}.ndjson"
```

- **`printf '%s\n'`** instead of `echo` — prevents zsh `\n` interpretation that breaks NDJSON
- **Single quotes** around payload (`raw_string` AST node — in allowlist, auto-approved)
- **Literal path** in double quotes (`string` AST node — in allowlist, auto-approved)
- **Apostrophes** in JSON values: replace `'` with `\u0027` (valid JSON Unicode escape, `json.loads()` decodes automatically)
- **Prohibited** (produce unrecognized AST nodes, silently denied): `$'...'` (ANSI-C quoting), `$VAR` in paths, heredocs, `echo`, `python3 -c`, command substitution

The plugin PreToolUse hook does not propagate to subagent execution contexts (documented Claude Code platform gap, 7 GitHub issues as of v2.1.96). AST auto-approval is the primary mechanism; the hook provides defense-in-depth for the main session only.

---

## Merge Script Output Format

After all agents return, run `merge_findings.py` as described in SKILL.md "Merge Phase 3 Outputs". The script writes a Phase 4 input JSON file to `{output_dir}/deep-review-phase4-input-{head_sha_short}.json` with this structure:

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
