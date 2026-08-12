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

For self-hosted instances, replace the hostname with the one detected from the git remote URL. See `references/phase2-triage.md` § "2a. Detect VCS Platform" for VCS detection and Phase 8 (Stage 0) for permalink format details.

**Rules:**

- MUST use the full 40-character SHA, never an abbreviated hash. If you only have a ref (branch name, short SHA, `HEAD`), resolve it first: `gh api repos/{owner}/{repo}/commits/{ref} --jq .sha`
- MUST include at least 1 line of context before and after the relevant line. For example, if the issue is on line 5, link to `#L4-L6`. If the issue spans lines 10-15, link to `#L9-L16`.
- For single-line issues, still use the range format with context (e.g., `#L4-L6`).

---

## Finding Fields Reference

The pipeline's declaration lives in `workflows/src/registry.js`. A field this table lists but the registry does not declare is rejected at the dispatch boundary (the item schema is closed — `additionalProperties: false`) before any stage sees it — which is why `tests/test_dimensions_registry.py` pins this table, the registry, and the agent contracts to each other.

### Canonical fields — every finding, every dimension

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique ID, e.g. `bug-1`, `sec-2` |
| `file` | string | yes | Relative file path |
| `line_start` | number | yes | Starting line number |
| `line_end` | number | no | Ending line number |
| `title` | string | yes | One-line summary |
| `description` | string | yes | Detailed explanation. Single-paragraph prose, at most 500 chars, no code blocks or bullet lists. |
| `severity` | string | yes | `critical`, `high`, `medium`, or `low` |
| `confidence` | number | yes | 0-100 CERTAINTY score |
| `dimension` | string | yes | Review dimension (bug, security, cross-file, etc.) |
| `origin` | string | no | Blame classification stamped by `scripts/verify_findings.py` — the one canonical field no agent emits. |
| `evidence` | string | no | Code snippet or behavior demonstrating the issue. Required by the discovery PROMPT (must be non-empty) though schema-optional. |
| `suggestion` | string | no | Prose fix advice; rendered by `post_review.py` as a "Suggested fix:" block. |
| `claude_md_rule` | string | no | The cited project rule, quoted with its source file; required by contract for convention findings; OMITTED (never null) when no rule applies. |
| `cross_file_refs` | array | no | Other files involved in the finding |

### Per-dimension fields

| Field | Type | Dimension | Description |
|-------|------|-----------|-------------|
| `hidden_errors` | string | bug | Errors the fix would surface that the visible code path hides |
| `attack_vector` | string | security | How the issue is exploited |
| `affected_consumers` | array | cross_file_impact | Other call sites/files impacted by the change |
| `criticality` | number | test_coverage | A 1-10 IMPACT scale, distinct from `confidence`'s 0-100 certainty scale, emitted as a number and never quoted. |
| `failure_scenario` | string | test_coverage | Concrete scenario the missing coverage would miss |
| `spec_text` | string | intent | The spec/requirement text the code is checked against |
| `invalid_state_example` | string | type_design | A concrete value the current types allow but shouldn't |
| `behavior_preserved` | string | simplification | Why the simplification is behavior-preserving |

Per-dimension fields are optional and never nullable (a not-applicable value is OMITTED). `required` is one flat list shared by all dimensions, so a field a contract calls required for its own dimension is enforced by the agent contract prose, not the schema.

### Delivery-side fields — not produced by the review pipeline

| Field | Type | Description |
|-------|------|-------------|
| `suggested_fix_code` | string | Exact replacement source for `file:line_start-line_end`; `scripts/post_review.py` renders it as a committable GitHub/GitLab `suggestion` block (one-click apply) IF a caller supplies it. No agent emits it and no schema declares it, so the review pipeline never populates it. It is retained as a delivery-side capability for callers that construct their own post-review JSON. Emitting it from agents was deliberately deferred because a one-click-apply patch generated with no deterministic check that it applies at the stated line range is worse than no patch. |

---

## Full Report Template

This template interpolates **canonical** finding fields (`description`, `line_start`, …).
Delivery JSON and the Inline PR Comment Format below use the v2 aliases (`body`, `line`, …).

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

{If finding.claude_md_rule or finding.spec_text is present — the rule the finding is measured
against. `agents/report-writer.md` instructs the same:}
**Cited rule:** {finding.claude_md_rule or finding.spec_text}

{If a caller supplied suggested_fix_code — the review pipeline never does:}
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

This table is generated in code by `dimensionsSummaryTable()` in `workflows/src/stages.js`
and delivered to you pre-rendered, as the `dimensionsTable` field of the dispatch input.
Paste it here **verbatim, unmodified** — never reconstruct, reclassify, or edit its rows.
(The same treatment the footer/marker gets from `scripts/post_review.py`: the code owns
the content, you place it.) Column reference:

| Dimension | Agent | Findings | Notes |
|-----------|-------|----------|-------|

## Review Methodology

| Aspect | Details |
|--------|---------|
| **Agents dispatched** | {list each agent with completion status: completed/failed/skipped} |
| **Model tier** | {optimized — list which agents used which model} |
| **Review scope** | {Full, or Incremental since {sha} (N commits)} |
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
```

**Do not hand-type a footer or marker here.** `scripts/post_review.py` appends both, mechanically and idempotently, to whatever `review_body` you compose above: the prose line `Generated by code-gauntlet | Reviewed up to: {full_sha}` and the `code-gauntlet-findings` hidden HTML comment. `<!-- Canonical source: scripts/review_marker.py -->`.

What the code writes (for reference — never compose this by hand):

```
---
Generated by code-gauntlet | Reviewed up to: {full_sha}

<!-- code-gauntlet-findings: {"version":"3.0","findings_count":{N},"sha":"{full_sha}"} -->
```

The `findings` key is reserved/optional — present only when the caller supplies it (owned by issue #36; absent today). The `code-gauntlet-findings` hidden HTML comment enables incremental review: on a rerun, `scripts/detect_prior_review.py` parses the `sha` to scope the incremental diff (see `references/phase1-preflight.md`). Readers never branch on the payload's `version` field.

---

## Inline PR Comment Format

**You do not compose this body — `scripts/post_review.py::render_comment_body` does.** You supply
the finding fields (see `references/delivery-guide.md` for the input JSON); the script renders
them. What follows is a transcription of that function's output so you can predict a posted
comment, and it must be kept in step with it — a template that promises a rendering the code
does not perform is the exact defect class issue #47 was filed for.

Every section after the description is emitted **only when its field is present**; `null`, `""`
and whitespace-only all count as absent, and no heading is emitted at all.

```markdown
**{emoji} [{SEVERITY}] {title}**

{body}

**Suggested fix:**
{suggestion}

**Cited rule:**
> {claude_md_rule, falling back to spec_text when there is no surviving rule — blockquoted, one `>` line per source line}

[If a caller supplied suggested_fix_code — the review pipeline never does:]
```suggestion
{suggested_fix_code}
```

```

`{emoji}` is 🔴 critical / 🟠 high / 🟡 medium / 💡 low, `{SEVERITY}` is the severity uppercased,
and `{body}` is the v2 alias of `description` applied at the persist boundary. The renderer emits
no permalink and no confidence footer — those belong to the report markdown, not to inline
comments.

```

**`suggested_fix_code` field:** Optional and delivery-side only. No agent emits it and no schema declares it, so the review pipeline never populates this field. `scripts/post_review.py` renders it as a GitHub `suggestion` block (one-click apply) or GitLab suggestion IF a caller supplying their own post-review JSON sets it. Absent that, only the prose `suggestion` field is shown. See `references/delivery-guide.md` for the findings JSON schema used by `post_review.py`.
