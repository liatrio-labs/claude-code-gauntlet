# Deep Review Report Format

Use this template for the unified review report. Adapt section headers based on what was actually found — don't include empty sections.

**Emoji format:** Always use Unicode emoji characters (🔴 🟠 🟡 💡), never GitHub shortcodes (`:red_circle:`, `:orange_circle:`). Shortcodes don't render in terminal/chat output.

## GitHub Permalink Format

All code references in findings MUST use platform-appropriate permalinks so they remain stable:

**GitHub:**
```
https://github.com/{owner}/{repo}/blob/{full_sha}/{path}#L{start}-L{end}
```

**GitLab:**
```
https://gitlab.com/{group}/{project}/-/blob/{full_sha}/{path}#L{start}-L{end}
```

For self-hosted instances, replace the hostname with the one detected from the git remote URL. See SKILL.md Phase 2a for VCS detection and Phase 8 (Stage 0) for permalink format details.

**Rules:**
- MUST use the full 40-character SHA, never an abbreviated hash. If you only have a ref (branch name, short SHA, `HEAD`), resolve it first: `gh api repos/{owner}/{repo}/commits/{ref} --jq .sha`
- MUST include at least 1 line of context before and after the relevant line. For example, if the issue is on line 5, link to `#L4-L6`. If the issue spans lines 10-15, link to `#L9-L16`.
- For single-line issues, still use the range format with context (e.g., `#L4-L6`).

---

## Full Report Template

```markdown
# Deep Review: {title}

**Date:** {date}
**Scope:** {PR #N | Branch comparison: base...head | Local changes}
**Files reviewed:** {N} ({high_risk} high-risk, {med_risk} medium, {low_risk} low)
**Lines changed:** +{additions} / -{deletions}
**Dimensions checked:** {comma-separated list of dimensions that ran}

---

## Change Summary

{A brief, structured overview of what this change does. This section helps readers quickly understand the scope before diving into findings.}

- **What changed:** {1-2 sentences describing the functional change}
- **Key files:** {list the 3-5 most important files changed, with one-line descriptions}
- **Patterns observed:** {e.g., "New API endpoints added", "Refactor of auth module", "Database migration + model update"}

---

## Executive Summary

{2-3 sentences: what was reviewed, key finding themes, and the finding count.
Example: "This PR adds JWT-based authentication to the API layer. The token validation has a critical bypass path and the error handling in the auth middleware silently swallows connection failures. 3 findings require attention before merge."}

**Blocking issues:** {N} (critical + high-security)
**Action items:** {N} (high + medium)
**Suggestions:** {N} (see Improvement Suggestions section)

---

## 🔴 Critical Issues

{These MUST be fixed before merge. Include only findings with severity=critical and confidence>=80.}

### {finding.id}: {finding.title}

**File:** `{finding.file}:{finding.line_start}` | [permalink](https://github.com/{owner}/{repo}/blob/{full_sha}/{finding.file}#L{line_start-1}-L{line_end+1})
**Dimension:** {finding.dimension} | **Confidence:** {finding.confidence}%
**Flagged by:** {list of agents that found this}

{finding.description}

**Evidence:**
```
{finding.evidence — the actual code snippet or behavior demonstrating the issue}
```

**Suggested fix:**
{finding.suggestion}

---

## 🟠 High-Priority Issues

{Should be fixed. Same format as Critical, but with severity=high.}

---

## 🟡 Medium Issues

{Worth addressing. Briefer format:}

| # | File | Issue | Dimension | Confidence |
|---|------|-------|-----------|------------|
| {id} | [`{file}:{line}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1}) | {title} | {dimension} | {confidence}% |

{For each, a brief 1-2 sentence description below the table, or expand inline if the issue is nuanced.}

---

## 💡 Low-Priority Suggestions

{Nice to have. Bullet list format:}

- **{id}**: [`{file}:{line}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1}) — {title} ({dimension}, {confidence}%)

---

## Surfaced Findings

{Pre-existing issues surfaced by this PR's changes. These were not introduced by this PR
but interact with it. Consider addressing them, but they are not blocking.
Severity has been downgraded one level from the original classification (see Phase 4a).}

| # | File | Issue | Dimension | Confidence | Originally from |
|---|------|-------|-----------|------------|-----------------|
| {id} | `{file}:{line}` | {title} | {dimension} | {confidence}% | {blame info — author, date} |

---

## Improvement Suggestions

{Findings from test-analyzer, conventions-and-intent comment accuracy pass, and code-simplifier. These are not included in the finding totals above and are not posted as PR inline comments by default. They are available via the "Let me pick" walkthrough in Phase 8 Stage 1.}

### Test Coverage

{Findings from test-analyzer, if any. Omit sub-section if empty.}

- **{id}**: [`{file}:{line}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1}) — {title} ({confidence}%)

### Documentation

{Findings from conventions-and-intent comment accuracy pass, if any. Omit sub-section if empty.}

- **{id}**: [`{file}:{line}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1}) — {title} ({confidence}%)

### Code Quality

{Findings from code-simplifier, if any. Omit sub-section if empty.}

- **{id}**: [`{file}:{line}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1}) — {title} ({confidence}%)

---

## Review Dimensions Summary

{Brief per-dimension summary showing what each agent found or confirmed was clean.}

| Dimension | Agent | Findings | Notes |
|-----------|-------|----------|-------|
| Correctness & Error Handling | bug-detector | {N issues} | {summary or "Clean"} |
| Security | security-reviewer | {N issues} | {summary} |
| Cross-file Impact | cross-file-impact-analyzer | {N issues} | {summary} |
| Test Coverage | test-analyzer | {N issues} | {summary} |
| Conventions & Intent | conventions-and-intent | {N issues} | {summary} |
| Type Design | type-design-analyzer | {N issues or "Skipped"} | {summary} |
| Code Simplification | code-simplifier | {N issues or "Skipped"} | {summary} |

## Review Methodology

| Aspect | Details |
|--------|---------|
| **Agents dispatched** | {list each agent with completion status: completed/failed/skipped} |
| **Model tier** | {mode: Optimized/Frontier — list which agents used which model} |
| **Findings pipeline** | {N raw findings → M after deterministic verification → K after confidence filter → J after dedup} |
| **Disagreement detection** | {N consensus (boosted), M singletons (passed through), K contradictions (routed to challenge), J suppressed} |
| **Blind challenge round** | {N findings blind-challenged, M downgraded, K boosted, J contested} |
| **Failed/skipped agents** | {list or "none"} |
| **Total review time** | {duration from Phase 1 to Phase 8} |
| **Prompt injection** | {N injection artifacts detected and discarded, or "none detected"} |
```

---

## PR Comment Format (abbreviated)

When posting as a PR comment, use this shorter format:

```markdown
### Deep Review

Found {N} issues ({critical} critical, {high} high, {medium} medium):

{For each critical/high issue:}
1. **[{dimension}]** {title} — [`{file}:{line}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1})

   {1-2 sentence description}

{If medium/low exist:}
<details>
<summary>{N} additional suggestions</summary>

{bullet list of medium/low issues, each with permalink}

</details>

---
Generated by deep-review | Reviewed up to: {full_sha}

<!-- deep-review-findings: {"version":1,"sha":"{full_sha}","findings":[{"id":"{finding.id}","file":"{finding.file}","line":{finding.line_start},"dim":"{finding.dimension}","title_hash":"{first 8 chars of SHA-256 of finding.title}"}]} -->
```

The `deep-review-findings` hidden HTML comment enables incremental review. On subsequent reviews, Phase 1 parses this comment to classify findings as introduced/fixed/preexisting. The `title_hash` enables fuzzy matching when line numbers shift due to rebases.

---

## Inline PR Comment Format

When posting inline comments at specific lines:

```markdown
**[{severity}] [{dimension}]** {title}

[View in context](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1})

{description}

**Suggested fix:**
{suggestion}

---
*Confidence: {confidence}% | deep-review*
```
