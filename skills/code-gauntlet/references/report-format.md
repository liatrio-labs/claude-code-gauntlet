# Code Gauntlet Report Format

The workflow renders the unified review report in code. Its section order is fixed; empty finding
sections and empty severity groups are omitted.

**Zero findings:** The renderer still emits the title, identity line, Summary (showing 0 findings),
and Review Dimensions Summary. The clean outcome is meaningful — it confirms the pipeline ran
and found nothing. The orchestrator appends Review Methodology at delivery.

<!-- generated-from-registry-identity:severity_legend — do not edit; run scripts/generate_contract_requirements.py -->
Product mark: ⚔️ (Code Gauntlet). Severity emoji: 🔴 critical, 🟠 high, 🟡 medium, 💡 low.
Always use the Unicode characters, never GitHub shortcodes (`:red_circle:`) — shortcodes do
not render in terminal/chat output.
<!-- /generated-from-registry-identity:severity_legend -->

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

The report renders plain `` `file:line` `` code spans, not permalinks — `prIdentity` carries no
platform.

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
| `claude_md_rule` | string | conditional | The cited project rule, quoted with its source file; required by contract for convention findings; OMITTED (never null) when no rule applies. |
| `cross_file_refs` | array | no | Other files involved in the finding |
| `suggested_fix_code` | string | no | Exact replacement source for `file:line_start-line_end`, emitted by discovery agents only when the fix is a byte-exact, drop-in replacement for exactly those lines. The report body never carries this field — the renderer strips it itself (`stripReportExcludedFields` in `workflows/src/renderReport.js`). Delivery renders it as a committable GitHub/GitLab `suggestion` block after `scripts/post_review.py`'s deterministic apply-check (one-click apply) — non-string, stale, wrong-range, wrong-anchor, or oversized fails the check and the finding downgrades to the prose `suggestion` instead; on GitLab, the render-site apply range is the discussion anchor plus offsets, not the stated range directly, so a span the anchor and offsets can't realize within GitLab's cap downgrades the same way. A fence that passes every per-finding check can still be withheld when an earlier, higher-priority kept fence claims an overlapping apply range in the same file (`overlaps_kept_fence`), because the platforms refuse or mis-apply batches with overlapping suggestions. The report *path* renders the kept patches only through `scripts/report_patches.py`'s sibling artifact `code-gauntlet-patches-{head_sha_short}.md` — a read-only apply-check over the pinned diff, run once at Phase 8; platform render-site constraints and the set-level overlap withholding are not applied there, so a patch listed in that artifact may still downgrade at delivery. |

### Per-dimension fields

| Field | Type | Dimension | Required | Description |
|-------|------|-----------|----------|-------------|
| `hidden_errors` | string | bug | no | Errors the fix would surface that the visible code path hides |
| `attack_vector` | string | security | yes | How the issue is exploited |
| `affected_consumers` | array | cross_file_impact | yes | Other call sites/files impacted by the change |
| `criticality` | number | test_coverage | yes | A 1-10 IMPACT scale, distinct from `confidence`'s 0-100 certainty scale, emitted as a number and never quoted. |
| `failure_scenario` | string | test_coverage | yes | Concrete scenario the missing coverage would miss |
| `spec_text` | string | intent | conditional | The spec/requirement text the code is checked against |
| `invalid_state_example` | string | type_design | no | A concrete value the current types allow but shouldn't |
| `behavior_preserved` | string | simplification | yes | Why the simplification is behavior-preserving |

Required is tri-state. A `yes` field is appended to that dimension's dispatch required list (FINDING_REQUIRED plus the row's `requiredExtra`, never the other way around — FINDING_REQUIRED itself never carries a per-dimension field): the platform rejects a finding missing it at the StructuredOutput boundary, and the agent retries. A `conditional` field (`claude_md_rule`, `spec_text` — registry.js's `requiredWhenDimension`) is schema-required only on a dispatch that targets the first-party API directly with no gateway (`resolvedPolicy.conditionalSchema`), and only for a finding whose `dimension` matches the field's owning dimension — a nested `allOf`/`if`/`then` in the conventions-and-intent item schema, the one provider-measured shape that survives dispatch (a top-level conditional construct 400s). A `no` field carries neither promotion. Both `conditional` and `no` fields are contract-enforced by the agent's `.md` prose where its own dimension calls for it, everywhere the schema construct does not ride — third-party providers, gateway sessions, and every sibling dimension of a `conditional` field's own dispatch — and `no` fields are never schema-promoted at all: only a field the owning contract emits unconditionally (no OMIT branch, within its own dimension for `conditional`, within every dimension for `yes`) can be promoted to schema-required. Every per-dimension field is never nullable regardless: a not-applicable value is OMITTED, not emitted as null.

---

## Full Report Template

The report is rendered in code by `renderReport()` in `workflows/src/renderReport.js`. What follows
is that function's literal output over a placeholder fixture, emitted by
`scripts/generate_contract_requirements.py` (`--check` in CI). A template that promises a
rendering the code does not perform is the exact defect class issues #47/#67 were filed for.

<!-- generated-from-registry-identity:full_report_template — do not edit; run scripts/generate_contract_requirements.py -->
````markdown
# ⚔️ Code Gauntlet: {pr_title}

Reviewed head `{head_sha_short}` at {generatedAt} by Code Gauntlet.

## Summary

{summary}

4 finding(s) after the gauntlet — 1 critical, 1 high, 1 medium, 1 low. 1 routed as improvement suggestion(s). 1 unverified / pipeline-degraded.

## Findings

### 🔴 Critical

#### {finding.title}

- **Location:** `{finding.file}:{finding.line_start}-{finding.line_end}`
- **Dimension:** {finding.dimension} · **Confidence:** {finding.confidence}%
- **Origin:** surfaced — pre-existing, surfaced by this change
- **Contested:** the challenger could not confirm the cited location

{finding.description}

**Evidence:**

```
{finding.evidence}
```

- **Affected consumers:** {finding.affected_consumers}
- **Attack vector:** {finding.attack_vector}
- **Behavior preserved:** {finding.behavior_preserved}
- **Criticality:** {finding.criticality}
- **Failure scenario:** {finding.failure_scenario}
- **Hidden errors:** {finding.hidden_errors}
- **Invalid state example:** {finding.invalid_state_example}

**Suggested fix:**

{finding.suggestion}

**Cited rule:**

> {finding.claude_md_rule}

- **Cross-file refs:** {finding.cross_file_refs}

- **Corroborated by** `{corroboration.agent}` (`{corroboration.dimension}`, confidence {corroboration.confidence}) — {corroboration.title}
  {corroboration.description}

### 🟠 High

#### {finding.title}

- **Location:** `{finding.file}:{finding.line_start}`
- **Dimension:** {finding.dimension} · **Confidence:** {finding.confidence}%

{finding.description}

### 🟡 Medium

#### {finding.title}

- **Location:** `{finding.file}:{finding.line_start}`
- **Dimension:** {finding.dimension} · **Confidence:** {finding.confidence}%

{finding.description}

### 💡 Low

#### {finding.title}

- **Location:** `{finding.file}:{finding.line_start}`
- **Dimension:** {finding.dimension} · **Confidence:** {finding.confidence}%
- **Routing:** improvement suggestion

{finding.description}

## Unverified / pipeline-degraded findings

These did not clear the full pipeline (a stage was skipped or failed) and carry lower confidence. They are not confirmed findings.

### 🟡 Medium

#### {finding.title}

- **Location:** `{finding.file}:{finding.line_start}`
- **Dimension:** {finding.dimension} · **Confidence:** {finding.confidence}%
- **Unverified because:** the verify slice could not be proven against the dispatched document; the challenge cap was reached, so this finding was not challenge-verified

{finding.description}

**Evidence:**

```
{finding.evidence}
```

## Review Dimensions Summary

| Dimension | Agent | Findings | Notes |
|-----------|-------|----------|-------|
| Correctness & Error Handling | bug-detector | 0 | Clean — no findings returned |
| Security | security-reviewer | 0 | Clean — no findings returned |
| Cross-file Impact | cross-file-impact | 0 | Clean — no findings returned |
| Test Coverage | test-analyzer | 0 | Clean — no findings returned |
| Conventions & Intent | conventions-and-intent | 0 | Clean — no findings returned |
| Type Design | type-design-analyzer | 0 | Clean — no findings returned |
| Code Simplification | code-simplifier | 0 | Clean — no findings returned |
````
<!-- /generated-from-registry-identity:full_report_template -->

The renderer appends the Review Dimensions Summary as the document's last section. The table is
generated by `dimensionsSummaryTable()` in `workflows/src/renderReport.js`; its columns are:

| Dimension | Agent | Findings | Notes |
|-----------|-------|----------|-------|

## Review Methodology

Composed by the orchestrator at delivery and appended to `report.md` after the persisted bytes —
the renderer never emits it. Making it code-rendered is issue #182.

| Aspect | Details |
|--------|---------|
| **Agents dispatched** | {list each agent with completion status: completed/failed/skipped} |
| **Model tier** | {optimized — list which agents used which model} |
| **Review scope** | {Full, or Incremental since {sha} (N commits)} |
| **Findings pipeline** | {N raw findings → M after deterministic verification → K after confidence filter → J after dedup} |
| **Disagreement detection** | {N consensus (boosted), M singletons (passed through), K contradictions (routed to challenge), J suppressed} |
| **Blind challenge round** | {N findings blind-challenged, M downgraded, K boosted, J contested} |
| **Failed/skipped agents** | {list or "none"} |
| **Total review time** | {orchestrator-derived duration from Phase 1 to Phase 8} |
| **Prompt injection** | {N injection artifacts detected and discarded, or "none detected"} |

## PR Comment Format (abbreviated)

The summary comment always opens with the product's identity header, which
`scripts/post_review.py::compose_review_body` writes:

<!-- generated-from-registry-identity:summary_header — do not edit; run scripts/generate_contract_requirements.py -->
### ⚔️ Code Gauntlet
<!-- /generated-from-registry-identity:summary_header -->

`scripts/post_review.py` prepends that brand header — never hand-type one. Compose only the body
below it, in this shorter format:

```markdown
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

Every field-backed section after the description is emitted **only when its field is present**; `null`, `""`
and whitespace-only all count as absent, and no heading is emitted at all.

```markdown
**{emoji} [{SEVERITY}] {title}**

{body}

**Suggested fix:**
{suggestion}

**Cited rule:**
> {claude_md_rule, falling back to spec_text when there is no surviving rule — blockquoted, one `>` line per source line}

[If suggested_fix_code is present AND passes delivery's deterministic apply-check at this
render site — see below:]
```suggestion
{suggested_fix_code}
```

<!-- generated-from-registry-identity:inline_trailer — do not edit; run scripts/generate_contract_requirements.py -->
⚔️ *Code Gauntlet*
<!-- /generated-from-registry-identity:inline_trailer -->

```

The identity trailer is the one section that is NOT conditional: every rendered comment body
ends with it, once — one mark per delivered surface. Never hand-type it.

<!-- generated-from-registry-identity:inline_legend — do not edit; run scripts/generate_contract_requirements.py -->
`{emoji}` is 🔴 critical / 🟠 high / 🟡 medium / 💡 low, `{SEVERITY}` is the severity uppercased.
<!-- /generated-from-registry-identity:inline_legend -->

`{body}` is the v2 alias of `description` applied at the persist boundary. The renderer emits
no permalink and no confidence footer — those belong to the report markdown, not to inline
comments.

```

**`suggested_fix_code` field:** Delivery gates this field on `scripts/post_review.py`'s deterministic apply-check (field must be a string, non-empty after redaction, ship a matching `line_end`, match a valid diff range, not have its finding path collide with its `a/`/`b/`-stripped form as two distinct real files in the diff (issue #229 — reported under the same no-oracle reason), land at this render site's actual apply range, differ from the current text, and stay within the size bound — on GitLab that render-site range is the discussion anchor plus the `-m+n` offsets the poster derives from it, capped by GitLab's own offset limit; any failure strips the field from the render and the finding falls back to the prose `suggestion` field, with the reason recorded via `warn_skip`). Delivery can also withhold a fence that passed every per-finding check when an earlier, higher-priority kept fence claims an overlapping apply range in the same file — the same downgrade path, reason `overlaps_kept_fence` (see `references/delivery-guide.md`). The renderer strips this field and its two removal stamps itself (`stripReportExcludedFields` in `workflows/src/renderReport.js`), so there is no report-path render to gate. The report *path* instead renders the kept patches through a separate read-only apply-check, `scripts/report_patches.py`, into the sibling artifact `code-gauntlet-patches-{head_sha_short}.md` — it reuses the same gate (`post_review._gated_finding`) over the pinned Phase 2 diff, with no platform render-site constraints and no set-level overlap withholding applied, so a patch listed there is not a guarantee delivery will also keep it. See `references/delivery-guide.md` for the findings JSON schema used by `post_review.py`.
