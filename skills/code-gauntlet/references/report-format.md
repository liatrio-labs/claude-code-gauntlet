# Code Gauntlet Report Format

## Contents

- [Permalink Format](#permalink-format)
- [Finding Fields Reference](#finding-fields-reference)
- [Full Report Template](#full-report-template)
- [PR Comment Format (abbreviated)](#pr-comment-format-abbreviated)
- [Inline PR Comment Format](#inline-pr-comment-format)

The workflow renders the unified review report in code. Its section order is fixed; empty finding
sections and empty severity groups are omitted.

**Zero findings:** The renderer still emits the title, identity line, Summary (showing 0 findings),
Change Context, and Review Dimensions Summary, followed by the code-rendered Review Methodology section. The
clean outcome is meaningful — it confirms the pipeline ran and found nothing.

<!-- generated-from-registry-identity:severity_legend — do not edit; run scripts/generate_contract_requirements.py -->
Product mark: ⚔️ (Code Gauntlet). Severity emoji: 🔴 critical, 🟠 high, 🟡 medium, 💡 low.
Rule source labels: documented_rule -> Cited rule, code_comment -> Cited comment, repo_precedent -> Repo precedent, self_inconsistency -> Inconsistency; unknown values -> Cited rule.
Always use the Unicode characters, never GitHub shortcodes (`:red_circle:`) — shortcodes do
not render in terminal/chat output.
<!-- /generated-from-registry-identity:severity_legend -->

**Identity appears once per surface.** The report title, the summary-comment header and every
inline comment's trailer are **rendered in code** — never hand-typed. In chat, only the final
delivery summary carries the mark, on its first line. Severity emoji are decorative and never the
sole carrier of meaning — the `[SEVERITY]` bracket always accompanies them. **Never decorate a
machine-parsed block:** `Resolved config:` / `Headless config:` and their knob lines,
`HEADLESS CONFIG ERROR:` / `HEADLESS INPUT ERROR:`, the prose footer, and either HTML marker.

## Permalink Format

Report links use the target platform and the web origin captured in `delivery.prIdentity`.

<!-- generated-from-registry-identity:permalink_formats — do not edit; run scripts/generate_contract_requirements.py -->
- `github`: blob `{origin}/{owner}/{repo}/blob/{sha}/{path}`; line `#L{start}`; range `#L{start}-L{end}`; ref `#{number}`; ref URL `{origin}/{owner}/{repo}/pull/{number}`.
- `gitlab`: blob `{origin}/{owner}/{repo}/-/blob/{sha}/{path}`; line `#L{start}`; range `#L{start}-{end}`; ref `!{number}`; ref URL `{origin}/{owner}/{repo}/-/merge_requests/{number}`.
- Encode owner, repo, and file paths one segment at a time with `encodeURIComponent` semantics; also percent-encode `!`, `'`, `(`, `)`, and `*`, while preserving `/` separators. A file path containing an empty, `.`, or `..` segment renders as a plain code span.
<!-- /generated-from-registry-identity:permalink_formats -->

The renderer produces these representative lines from the same templates and encoding code:

<!-- generated-from-registry-identity:permalink_sample — do not edit; run scripts/generate_contract_requirements.py -->
**GitHub:**

Reviewed head `{head_sha_short}` at {generatedAt} for [`{owner}/{repo}#7`](https://github.com/%7Bowner%7D/%7Brepo%7D/pull/7) by Code Gauntlet.

- **Location:** [`{finding.file}:10-12`](https://github.com/%7Bowner%7D/%7Brepo%7D/blob/0123456789abcdef0123456789abcdef01234567/%7Bfinding.file%7D#L10-L12)

**GitLab:**

Reviewed head `{head_sha_short}` at {generatedAt} for [`group/sub/{repo}!7`](https://gitlab.com/group/sub/%7Brepo%7D/-/merge_requests/7) by Code Gauntlet.

- **Location:** [`{finding.file}:10-12`](https://gitlab.com/group/sub/%7Brepo%7D/-/blob/0123456789abcdef0123456789abcdef01234567/%7Bfinding.file%7D#L10-12)
<!-- /generated-from-registry-identity:permalink_sample -->

Without a valid identity or linkable file path, the same location renders as a plain code span.

---

## Finding Fields Reference

The pipeline's declaration lives in `workflows/src/registry.js`. A field this table lists but the registry does not declare is rejected at the dispatch boundary (the item schema is closed — `additionalProperties: false`) before any stage sees it — which is why `tests/test_registry_contracts.py` pins this table, the registry, and the agent contracts to each other.

### Canonical fields — every finding, every dimension

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique ID, e.g. `bug-1`, `sec-2` |
| `file` | string | yes | Relative file path |
| `line_start` | number | yes | Starting line number |
| `line_end` | number | no | Ending line number |
| `title` | string | yes | One-line summary |
| `description` | string | yes | Detailed explanation. Single-paragraph prose, at most 500 chars, no code blocks or bullet lists. |
| `severity` | string | yes | `critical`, `high`, `medium`, or `low`; the report renders any other value as `low`, while the original value stays in `findings.json`. |
| `confidence` | number | yes | 0-100 CERTAINTY score |
| `dimension` | string | yes | Review dimension (bug, security, cross-file, etc.) |
| `origin` | string | no | Blame classification stamped by `scripts/verify_findings.py` — the one canonical field no agent emits. |
| `evidence` | string | no | Code snippet or behavior demonstrating the issue. Required by the discovery PROMPT (must be non-empty) though schema-optional. |
| `anchor` | string | yes | Exact text, copied from the source at the PR head, of the 1 to 4 contiguous lines in file this finding is about; at most 400 characters. |
| `searched_paths` | array | no | Repository files or directories searched for coverage. |
| `candidate_tests` | array | no | Inspected tests, including their input and assertion. |
| `production_element` | object | no | {"type":"object","$comment":"source-reference","properties":{"file":{"type":"string","$comment":"repo-path"},"line_start":{"type":"integer","minimum":1},"line_end":{"type":"integer","minimum":1},"anchor":{"type":"string","minLength":1}},"required":["file","line_start","line_end","anchor"],"additionalProperties":false} |
| `duplicate_of` | object | no | {"type":"object","$comment":"source-reference","properties":{"file":{"type":"string","$comment":"repo-path"},"line_start":{"type":"integer","minimum":1},"line_end":{"type":"integer","minimum":1},"anchor":{"type":"string","minLength":1}},"required":["file","line_start","line_end","anchor"],"additionalProperties":false} |
| `precedent` | object | no | {"type":"object","$comment":"source-reference","properties":{"file":{"type":"string","$comment":"repo-path"},"line_start":{"type":"integer","minimum":1},"line_end":{"type":"integer","minimum":1},"anchor":{"type":"string","minLength":1}},"required":["file","line_start","line_end","anchor"],"additionalProperties":false} |
| `sites` | array | no | Additional locations of this issue. |
| `suggestion` | string | no | Prose fix advice; rendered by `post_review.py` as a "Suggested fix:" block. |
| `claude_md_rule` | string | conditional | The cited project rule, quoted with its source file; required by contract for convention findings; OMITTED (never null) when no rule applies. |
| `cross_file_refs` | array | no | Other files involved in the finding |
| `suggested_fix_code` | string | no | Exact replacement source for `file:line_start-line_end`, emitted by discovery agents only when the fix is a byte-exact, drop-in replacement for exactly those lines. The report body never carries this field — the renderer excludes it and its removal stamps through the registry-derived `reportExtraFields()` projection in `workflows/src/renderReport.js`. Delivery renders it as a committable GitHub/GitLab `suggestion` block after `scripts/gauntlet/delivery/gate.py`'s deterministic apply-check (one-click apply) — non-string, stale, wrong-range, wrong-anchor, or oversized fails the check and the finding downgrades to the prose `suggestion` instead; on GitLab, the render-site apply range is the discussion anchor plus offsets, not the stated range directly, so a span the anchor and offsets can't realize within GitLab's cap downgrades the same way. A fence that passes every per-finding check can still be withheld when an earlier, higher-priority kept fence claims an overlapping apply range in the same file (`overlaps_kept_fence`), because the platforms refuse or mis-apply batches with overlapping suggestions. The report *path* renders the kept patches only through `scripts/report_patches.py`'s sibling artifact `code-gauntlet-patches-{head_sha_short}.md` — a read-only apply-check over the pinned diff, run once at Phase 8; platform render-site constraints and the set-level overlap withholding are not applied there, so a patch listed in that artifact may still downgrade at delivery. |

For convention findings, `rule_source` selects the heading for `claude_md_rule`: `documented_rule` uses **Cited rule**, `code_comment` uses **Cited comment**, `repo_precedent` uses **Repo precedent**, and `self_inconsistency` uses **Inconsistency**. Intent findings include `spec_text` and omit `rule_source`; comment_accuracy findings omit `rule_source`, `claude_md_rule`, and `spec_text`. Unknown or absent values use **Cited rule**. A `spec_text` fallback also uses **Cited rule**. The raw `rule_source` value is never rendered.

### Per-dimension fields

| Field | Type | Dimension | Required | Description |
|-------|------|-----------|----------|-------------|
| `hidden_errors` | string | bug | no | hidden errors |
| `attack_vector` | string | security | yes | attack vector |
| `affected_consumers` | array | cross_file_impact | yes | {"type":"array","items":{"type":"string","$comment":"repo-path"}} |
| `criticality` | number | test_coverage | yes | {"type":"number","minimum":1,"maximum":10} |
| `failure_scenario` | string | test_coverage | yes | failure scenario |
| `test_kind` | string | test_coverage | conditional | {"type":"string","enum":["defective_test","critical_gap","production_defect","additional_case"]} |
| `rule_source` | string | convention | no | rule source |
| `spec_text` | string | intent | conditional | spec text |
| `invalid_state_example` | string | type_design | no | invalid state example |
| `quality_basis` | string | type_design | conditional | {"type":"string","enum":["current_correctness","duplication","simpler_equivalent","stack_idiom","other"]} |
| `behavior_preserved` | string | type_design | no | behavior preserved |

Required is tri-state. A `yes` field is appended to that dimension's dispatch required list (FINDING_REQUIRED plus the row's `requiredExtra`, never the other way around — FINDING_REQUIRED itself never carries a per-dimension field): the platform rejects a finding missing it at the StructuredOutput boundary, and the agent retries. A `conditional` field (`claude_md_rule`, `spec_text` — registry.js's `requiredWhenDimension`) is schema-required only on a dispatch that targets the first-party API directly with no gateway (`resolvedPolicy.conditionalSchema`), and only for a finding whose `dimension` matches the field's owning dimension — a nested `allOf`/`if`/`then` in the conventions-and-intent item schema, the one provider-measured shape that survives dispatch (a top-level conditional construct 400s). A `no` field carries neither promotion. Both `conditional` and `no` fields are contract-enforced by the agent's `.md` prose where its own dimension calls for it, everywhere the schema construct does not ride — third-party providers, gateway sessions, and every sibling dimension of a `conditional` field's own dispatch — and `no` fields are never schema-promoted at all: only a field the owning contract emits unconditionally (no OMIT branch, within its own dimension for `conditional`, within every dimension for `yes`) can be promoted to schema-required. Every per-dimension field is never nullable regardless: a not-applicable value is OMITTED, not emitted as null.

<!-- generated-from-registry-location: do not edit; scripts/generate_contract_requirements.py -->
## Location Status and Rights

`location_status` is pipeline-owned and excluded from discovery output.
`anchor` is required only at discovery dispatch; pipeline-authored and replayed findings may omit it.
Unconfirmed findings remain in the review body, labelled `location unconfirmed`.

| Status | body | sourceLink | inline | fence | patch | fixTask |
|---|---|---|---|---|---|---|
| confirmed | yes | yes | yes | yes | yes | yes |
| relocated | yes | yes | yes | yes | yes | yes |
| unconfirmed | yes | no | no | no | no | no |

## Final Disposition and Rights

Missing or unknown disposition is held back. Delivery intersects these rights with location rights.

| Disposition | inline | body | report | fixTask | patch | fence | sourceLink |
|---|---|---|---|---|---|---|---|
| deliver | yes | yes | finding | yes | yes | yes | yes |
| body | no | yes | finding | no | no | no | yes |
| held_back | no | no | unverified | no | no | no | no |
| report_only | no | no | finding | no | no | no | no |
| eliminate | no | no | eliminated | no | no | no | no |

### test_coverage admission

- `test_kind: defective_test`: {"phase": "semantic", "predicates": ["pr_owned_test", "confirmed_test_defect", "test_diff_site_for_inline"], "required": [], "tier": "main"}; default disposition `held_back`.
- `test_kind: critical_gap`: {"optional": ["production_element"], "phase": "semantic", "predicates": ["criticality_at_least_8", "resolved_production_element", "complete_searches", "complete_behavioral_absence"], "required": ["criticality", "failure_scenario", "searched_paths", "candidate_tests"], "tier": "main"}; default disposition `held_back`.
- `test_kind: production_defect`: {"core": true, "dimension": "bug", "phase": "semantic", "predicates": ["present_defect"], "required": [], "tier": "main"}; default disposition `held_back`.
- `test_kind: additional_case`: {"defaultDisposition": "report_only", "phase": "deterministic", "predicates": [], "required": [], "tier": "suggestion"}; default disposition `report_only`.

### type_design admission

- `quality_basis: current_correctness`: {"core": true, "phase": "semantic", "predicates": ["present_defect"], "required": [], "tier": "main"}; default disposition `held_back`.
- `quality_basis: duplication`: {"phase": "semantic", "predicates": ["resolved_duplicate", "confirmed_duplication"], "required": ["duplicate_of"], "tier": "suggestion"}; default disposition `held_back`.
- `quality_basis: simpler_equivalent`: {"anyOf": ["suggestion", "suggested_fix_code"], "phase": "semantic", "predicates": ["concrete_equivalent", "behavior_preserved"], "required": ["behavior_preserved"], "tier": "suggestion"}; default disposition `held_back`.
- `quality_basis: stack_idiom`: {"phase": "semantic", "predicates": ["resolved_precedent", "precedent_not_added", "applicable"], "required": ["precedent"], "tier": "suggestion"}; default disposition `held_back`.
- `quality_basis: other`: {"defaultDisposition": "report_only", "phase": "deterministic", "predicates": [], "required": [], "tier": "suggestion"}; default disposition `report_only`.

### simplification admission

- `quality_basis: current_correctness`: {"core": true, "phase": "semantic", "predicates": ["present_defect"], "required": [], "tier": "main"}; default disposition `held_back`.
- `quality_basis: duplication`: {"phase": "semantic", "predicates": ["resolved_duplicate", "confirmed_duplication"], "required": ["duplicate_of"], "tier": "suggestion"}; default disposition `held_back`.
- `quality_basis: simpler_equivalent`: {"anyOf": ["suggestion", "suggested_fix_code"], "phase": "semantic", "predicates": ["concrete_equivalent", "behavior_preserved"], "required": ["behavior_preserved"], "tier": "suggestion"}; default disposition `held_back`.
- `quality_basis: stack_idiom`: {"phase": "semantic", "predicates": ["resolved_precedent", "precedent_not_added", "applicable"], "required": ["precedent"], "tier": "suggestion"}; default disposition `held_back`.
- `quality_basis: other`: {"defaultDisposition": "report_only", "phase": "deterministic", "predicates": [], "required": [], "tier": "suggestion"}; default disposition `report_only`.
<!-- /generated-from-registry-location -->

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

4 findings after the gauntlet — 1 critical, 1 high, 1 medium, 1 low. 1 routed as improvement suggestion. 2 unverified / pipeline-degraded.

- 🔴 [CRITICAL] `{finding.file}:10-12`: {finding.title}
- 🟠 [HIGH] `{finding.file}:10`: {finding.title}
- 🟡 [MEDIUM] `{finding.file}:10`: {finding.title}
- 💡 [LOW] `{finding.file}:10` (improvement suggestion): {finding.title}

## Change Context

This summary describes the change context for the report.

{summary}

## Findings

### 🔴 Critical

#### {finding.title}

- **Location:** `{finding.file}:10-12`
- **Dimension:** {finding.dimension}
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

- **Corroborated by** `{corroboration.agent}` (`{corroboration.dimension}`, confidence ) — {corroboration.title}
  {corroboration.description}

### 🟠 High

#### {finding.title}

- **Location:** `{finding.file}:10`
- **Dimension:** {finding.dimension}

{finding.description}

### 🟡 Medium

#### {finding.title}

- **Location:** `{finding.file}:10`
- **Dimension:** {finding.dimension}

{finding.description}

### 💡 Low

#### {finding.title}

- **Location:** `{finding.file}:10`
- **Dimension:** {finding.dimension}
- **Routing:** improvement suggestion
- **Reachability:** only under a future change (severity demoted to low)

{finding.description}

## Unverified / pipeline-degraded findings

These did not clear the full pipeline (a stage was skipped or failed) and carry lower confidence. They are not confirmed findings.

### 🟡 Medium

#### {finding.title}

- **Location:** `{finding.file}:10`
- **Dimension:** {finding.dimension}
- **Unverified because:** the validator did not score this finding, so it was not skeptically validated; the verify slice could not be proven against the dispatched document; the challenge cap was reached, so this finding was not challenge-verified

{finding.description}

**Evidence:**

```
{finding.evidence}
```

### 💡 Low

#### {finding.title}

- **Location:** `{finding.file}:10`
- **Dimension:** {finding.dimension}
- **Unverified because:** the challenger failed or returned no score, so this finding was not challenge-verified

{finding.description}

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

## Review Methodology

```text
Resolved config:
  model_tier=optimized (fixed)
  pr_comment_cap=null (default)
  delivery_tier=all (default)
  review_md=absent (discovery)
  pipeline_version={pipeline_version} (bundle)
  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)
```

| Aspect | Details |
|--------|---------|
| Version | {pipeline_version} (plugin and pipeline) |
| Provider | provider=firstParty; gateway=false; conditional schema active=true |
| Per-stage models | change-summarizer=claude-sonnet-5-5, bug-detector=claude-sonnet-5-5, security-reviewer=claude-opus-5-5, cross-file-impact=claude-sonnet-5-5, test-analyzer=claude-sonnet-5-5, conventions-and-intent=claude-sonnet-5-5, type-design-analyzer=claude-sonnet-5-5, code-simplifier=claude-sonnet-5-5, validator=claude-sonnet-5-5, challenger=claude-sonnet-5-5, executor=claude-sonnet-5-5, artifact-writer=claude-sonnet-5-5 |
| Delivery selection | selection policy: tier=all, cap=uncapped |
| Review scope | Full |
| Findings pipeline | discovered={stats.discovered}; validate: accepted={stats.validate.accepted}, rejected={stats.validate.rejected}; filter: accepted={stats.filter.accepted}, rejected={stats.filter.rejected}; challenge: accepted={stats.challenge.accepted}, rejected={stats.challenge.rejected}; merge: per-channel: ndjson={stats.merge.ndjson}, text_fallback={stats.merge.text_fallback}; duplicates resolved={stats.merge.duplicates_resolved}; dropped-no-id={stats.merge.dropped_no_id}; truncation warnings={stats.merge.truncation_warnings}; validation warnings={stats.merge.validation_warnings} |
| Gaps | 0 |
| Dimensions | dispatched=7; degraded=0 |

This section covers the pipeline up to the report stage; persistence, delivery, and duration are reported by the orchestrator at delivery.
````
<!-- /generated-from-registry-identity:full_report_template -->

Report display folds preserve finding cardinality and whole severity buckets. Full confirmed values
remain in `findings.json`; unverified values remain in the challenge checkpoint. Display folds
and UTF-8 posting budgets are separate code-owned mechanisms documented in
[delivery rendering](../../../docs/delivery-rendering.md#report-display-folds).

An absent or blank change summary uses this exact sentence:

```text
No change summary was produced for this run.
```

The renderer appends the Review Dimensions Summary, then the Review Methodology section as the
document's last section. The table is generated by `dimensionsSummaryTable()` in
`workflows/src/renderReport.js`; its columns are:

| Dimension | Agent | Findings | Notes |
|-----------|-------|----------|-------|

The renderer owns `## Review Methodology` and always emits it last. It contains a `text` fenced
identity receipt (`Headless config:` in headless mode or `Resolved config:` in interactive mode),
followed by an `Aspect`/`Details` table. The receipt lists the mode's resolved knobs in registry
order, then `pipeline_version` and `plugin_root`. The table covers the plugin and pipeline
version, provider and conditional schema, per-stage models, any subagent override, delivery
selection, review scope, findings and merge counts, the integer gap count, and dispatched and
degraded dimensions. Incremental scope shows its safe `since` value and commit count; a full
scope that was requested as incremental derives one of the detector-backed fallback reasons
from `reviewScope.detector`. A closing line says that the section covers the pipeline through
report; persistence, delivery, and duration are reported by the orchestrator at delivery.

## PR Comment Format (abbreviated)

The persisted wrapper's `review_body` is exactly the report's Summary section body: the code-owned
counts sentence, the selected findings index, and any remainder count with its selection reasons.
The Summary contains no change-summarizer prose. Index bullets show severity, a folded title, a
head-SHA location permalink (or a code span without a usable identity), and an improvement
suggestion label where applicable. Locations link to source, not to discussion threads.
The code-owned index preserves report group order and accounts for omitted units in the remainder.
Unverified findings contribute to counts, never the index. See [delivery rendering](../../../docs/delivery-rendering.md#summary-index-and-host-rendering)
for object membership, consolidation, display containment, index limits, and posted body budgets.
The footer contains `Generated by code-gauntlet | Reviewed up to: {full_sha}`. A model-authored
title that contains footer-like text does not satisfy the footer check unless it occupies a
standalone Markdown line separated by CR or LF.

**Do not hand-type a footer or marker here.** `scripts/gauntlet/delivery/compose.py::compose_review_body` composes both and fits the body to the platform budget. `scripts/gauntlet/delivery/post.py` posts the composed body. Preparation removes terminated comments before composition; the prose half deduplicates only against a standalone footer line carrying the same head SHA. An over-budget body always ends with the full canonical footer. The prose line is `Generated by code-gauntlet | Reviewed up to: {full_sha}` and the other half is the `code-gauntlet-findings` hidden HTML comment. `<!-- Canonical source: scripts/gauntlet/marker.py -->`.

What the code writes (for reference — never compose this by hand):

```
---
Generated by code-gauntlet | Reviewed up to: {full_sha}

<!-- code-gauntlet-findings: {"version":"3.0","findings_count":{N},"sha":"{full_sha}"} -->
```

The `code-gauntlet-findings` hidden HTML comment enables incremental review: on a rerun, `scripts/detect_prior_review.py` parses the `sha` to scope the incremental diff (see `references/phase1-preflight.md`). Readers never branch on the payload's `version` field.

---

## Inline PR Comment Format

**You do not compose this body — `scripts/gauntlet/delivery/compose.py::render_comment_body` does.** You supply
the finding fields (see `references/delivery-guide.md` for the input JSON); the script renders
them. The generated sample below stays in step with that function so it describes the actual posted comment.

Every field-backed section after the description is emitted **only when its field is present**; `null`, `""`
and whitespace-only all count as absent, and no heading is emitted at all.

<!-- generated-from-registry-identity:inline_sample — do not edit; run scripts/generate_contract_requirements.py -->
````markdown
**{emoji} [SEVERITY] {finding.title}**

{body}

**Suggested fix:**
{suggestion}

**{rule_source_label}:**
> {claude_md_rule, falling back to spec_text — blockquoted, one `>` line per source line}

```suggestion
{suggested_fix_code}
```

⚔️ *Code Gauntlet*
````
<!-- /generated-from-registry-identity:inline_sample -->

The identity trailer is the one section that is NOT conditional: every rendered comment body
ends with it, once — one mark per delivered surface. Never hand-type it.

<!-- generated-from-registry-identity:inline_legend — do not edit; run scripts/generate_contract_requirements.py -->
`{emoji}` is 🔴 critical / 🟠 high / 🟡 medium / 💡 low, `{SEVERITY}` is the normalized severity uppercased.
<!-- /generated-from-registry-identity:inline_legend -->

`{body}` is the v2 alias of `description` applied at the persist boundary. The renderer emits
no permalink and no confidence footer — those belong to the report markdown, not to inline
comments.
Posted fields use the code-owned outbound contract in
[delivery rendering](../../../docs/delivery-rendering.md#outbound-text-preparation).

**`suggested_fix_code` field:** Delivery gates this field on `scripts/gauntlet/delivery/gate.py`'s deterministic apply-check (field must be a string, non-empty after redaction, free of `marker_shaped` text, ship a matching `line_end`, match a valid diff range, not have its finding path collide with its `a/`/`b/`-stripped form as two distinct real files in the diff, land at this render site's actual apply range, differ from the current text, and stay within the size bound — on GitLab that render-site range is the discussion anchor plus the `-m+n` offsets the poster derives from it, capped by GitLab's own offset limit; any failure strips the field from the render and the finding falls back to the prose `suggestion` field, with the reason recorded via `warn_skip`). Delivery can also withhold a fence that passed every per-finding check when an earlier, higher-priority kept fence claims an overlapping apply range in the same file — the same downgrade path, reason `overlaps_kept_fence` (see `references/delivery-guide.md`). The renderer excludes this field and its two removal stamps through the registry-derived `reportExtraFields()` projection in `workflows/src/renderReport.js`, so there is no report-path render to gate. The report *path* instead renders the kept patches through a separate read-only apply-check, `scripts/report_patches.py`, into the sibling artifact `code-gauntlet-patches-{head_sha_short}.md` — it reuses the same gate (`gauntlet.delivery.gate.evaluate_fix`) over the pinned Phase 2 diff, including the `marker_shaped` downgrade, with no platform render-site constraints and no set-level overlap withholding applied, so a patch listed there is not a guarantee delivery will also keep it. See `references/delivery-guide.md` for the findings JSON schema used by `post_review.py`.
