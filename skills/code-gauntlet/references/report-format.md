# Code Gauntlet Report Format

Use this template for the unified review report. Adapt section headers based on what was actually found — don't include empty sections.

**Zero findings:** If all findings are eliminated during the pipeline, produce a clean report that includes the executive summary (showing 0 findings) and the Review Methodology section. Omit empty severity sections entirely. The clean outcome is meaningful — it confirms the pipeline ran and found nothing.

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

## Finding Fields Reference

Each finding produced by the review pipeline has these fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique ID, e.g. `bug-1`, `sec-2` |
| `dimension` | string | yes | Review dimension (bug, security, cross-file, etc.) |
| `severity` | string | yes | `critical`, `high`, `medium`, or `low` |
| `confidence` | number | yes | 0-100 confidence score |
| `file` | string | yes | Relative file path |
| `line_start` | number | yes | Starting line number |
| `line_end` | number | yes | Ending line number |
| `title` | string | yes | One-line summary |
| `description` | string | yes | Detailed explanation |
| `evidence` | string | yes | Code snippet or behavior demonstrating the issue |
| `suggestion` | string | yes | Prose fix or improvement advice |
| `suggested_fix_code` | string | no | Direct code replacement for `file:line_start-line_end`. When present, `post_review.py` renders it as a GitHub/GitLab `suggestion` block (one-click apply). Null when the agent has no concrete fix to propose. |
| `cross_file_refs` | array | no | Other files involved in the finding |

**`suggested_fix_code` usage:** Agents that can propose an exact code replacement populate this field with the replacement source code. Agents that cannot (e.g., the fix is architectural or requires user judgment) leave it null. The field is rendered by `scripts/post_review.py` as a committable suggestion block in PR comments -- see `references/delivery-guide.md` for the full delivery JSON schema.

---

## Full Report Template

```markdown
# Code Gauntlet: {title}

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

{If finding.suggested_fix_code is present — render as a committable suggestion block:}
```suggestion
{finding.suggested_fix_code}
```

---

## 🟠 High-Priority Issues

{Should be fixed. Same format as Critical, but with severity=high.}

---

## 🟡 Medium Issues

{Worth addressing. Briefer format:}

| # | File | Issue | Dimension | Confidence |
|---|------|-------|-----------|------------|
| {id} | [`{file}:{line_start}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line_start-1}-L{line_end+1}) | {title} | {dimension} | {confidence}% |

{For each, a brief 1-2 sentence description below the table, or expand inline if the issue is nuanced.}

---

## 💡 Low-Priority Suggestions

{Nice to have. Bullet list format:}

- **{id}**: [`{file}:{line_start}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line_start-1}-L{line_end+1}) — {title} ({dimension}, {confidence}%)

---

## Surfaced Findings

{Pre-existing issues surfaced by this PR's changes. These were not introduced by this PR
but interact with it. Consider addressing them, but they are not blocking.
Severity has been downgraded one level from the original classification (see the Verify stage's new/surfaced classification).}

| # | File | Issue | Dimension | Confidence | Originally from |
|---|------|-------|-----------|------------|-----------------|
| {id} | `{file}:{line}` | {title} | {dimension} | {confidence}% | {blame info — author, date} |

---

## Improvement Suggestions

{Findings from test-analyzer, conventions-and-intent comment accuracy pass, and code-simplifier. These render in this dedicated section rather than the severity-grouped totals above (the `report_tag` governs report presentation). Whether they are ALSO posted as PR comments depends on the Phase 1 delivery tier (`args.delivery.tier`): under `all` (the default) the pipeline's `selectDelivery` includes every challenge-survivor regardless of tag, so suggestions post as PR inline comments alongside main findings (subject to `limits.deliveryCap`); under `main_only` they stay in this report section and are not posted. The report always lists them either way.}

### Test Coverage

{Findings from test-analyzer, if any. Omit sub-section if empty.}

- **{id}**: [`{file}:{line_start}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line_start-1}-L{line_end+1}) — {title} ({confidence}%)

### Documentation

{Findings from conventions-and-intent comment accuracy pass, if any. Omit sub-section if empty.}

- **{id}**: [`{file}:{line_start}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line_start-1}-L{line_end+1}) — {title} ({confidence}%)

### Code Quality

{Findings from code-simplifier, if any. Omit sub-section if empty.}

- **{id}**: [`{file}:{line_start}`](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line_start-1}-L{line_end+1}) — {title} ({confidence}%)

---

## Review Dimensions Summary

{Brief per-dimension summary showing what each agent found or confirmed was clean.}

| Dimension | Agent | Findings | Notes |
|-----------|-------|----------|-------|
| Correctness & Error Handling | bug-detector | {N issues} | {summary or "Clean"} |
| Security | security-reviewer | {N issues} | {summary} |
| Cross-file Impact | cross-file-impact | {N issues} | {summary} |
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
### Code Gauntlet

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
Generated by code-gauntlet | Reviewed up to: {full_sha}

<!-- code-gauntlet-findings: {"version":1,"sha":"{full_sha}","findings":[{"id":"{finding.id}","file":"{finding.file}","line":{finding.line_start},"dim":"{finding.dimension}","title_hash":"{first 8 chars of SHA-256 of finding.title}"}]} -->
```

The `code-gauntlet-findings` hidden HTML comment enables incremental review. On subsequent reviews, Phase 1 parses the `sha` to scope the incremental diff, and Phase 8 delivery uses the findings array to classify each finding as introduced/fixed/preexisting. The `title_hash` enables fuzzy matching when line numbers shift due to rebases.

---

## Inline PR Comment Format

When posting inline comments at specific lines:

```markdown
**[{severity}] [{dimension}]** {title}

[View in context](https://github.com/{owner}/{repo}/blob/{full_sha}/{file}#L{line-1}-L{line+1})

{description}

**Suggested fix:**
{suggestion}

[If suggested_fix_code is present — append a committable suggestion block:]
```suggestion
{suggested_fix_code}
```

---
*Confidence: {confidence}% | code-gauntlet*

```

**`suggested_fix_code` field:** Optional. When an agent can propose a direct code replacement for the lines at `finding.file:line_start-line_end`, it populates `suggested_fix_code` with the replacement code. The `scripts/post_review.py` delivery script renders this as a GitHub `suggestion` block (one-click apply) or GitLab suggestion. When null or absent, only the prose `suggestion` field is shown. See `references/delivery-guide.md` for the findings JSON schema used by `post_review.py`.
