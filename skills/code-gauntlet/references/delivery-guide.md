# Delivery Guide

## Contents

- [PR/MR Comments (platform-aware)](#prmr-comments-platform-aware)
- [Task Creation](#task-creation)
- [Markdown File](#markdown-file)
- [Chat](#chat)

Implementation details for each delivery method in Phase 8, interactive and headless.

---

## PR/MR Comments (platform-aware)

**Only comment on files in the diff.** The GitHub/GitLab API rejects inline comments on files not part of the PR/MR diff (HTTP 422). Before posting each inline comment, verify the file path is in the changed files list. Cross-file impact findings about files outside the diff go in the top-level summary comment with a note: "This finding references `path/to/file.cs` which is not in this PR's diff."

Batch inline comments into a single GitHub review event to avoid one notification per finding.

Follow [phase8-delivery.md](phase8-delivery.md#stage-1-deliver-the-report) for delivery authorization
and the immutable `artifactPaths.postReview` payload. This guide documents the posting interface.

### Comment body format

**You do not compose the comment body.** `scripts/gauntlet/delivery/compose.py::render_comment_body` builds it
from the fields you supply. Its output, for reference:

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

Every field-backed section after `{body}` is emitted only when its field is present — `null`, `""` and
whitespace-only all count as absent and produce no heading.

**`suggestion` always renders as prose; it never becomes a suggestion block.** Only
`suggested_fix_code` produces the committable ```suggestion fence, and only after
`post_review.py` runs it through a deterministic pre-render apply-check — the field must be a
string, non-empty after redaction, have fence rights and a whole-line anchor matching every
canonical source line, preserve the held claimed span length, land inside a valid diff range,
match this render site's actual apply range, differ from the current text, and stay within the
size bound. Unconfirmed and legacy locations stay in the review body with the label
`location unconfirmed`; they carry no inline comment, fence, patch artifact, or fix task. Any failure strips the field before render and the finding falls back to the prose
`suggestion`, with the reason recorded via `warn_skip`. A suggestion fence is a one-click apply action, so it requires a dedicated patch field and a proved apply range.

A multi-line fence on GitLab carries an offsets header, `` ```suggestion:-m+n ``, with `m`/`n`
decided by the poster from the discussion's anchor line — never supplied on the finding itself.
A single-line fix still renders a plain `` ```suggestion `` fence.

**A fence that would overlap another kept fence in the same file demotes to prose.** GitLab
refuses to batch-apply a set of suggestions with any two overlapping ranges in one file, and
serially applying one outdates the other — so before rendering, the poster walks the fences in
delivery order (the pipeline's own priority ranking, not line position) and withholds any whose
apply range overlaps one it has already kept in that file: the earlier fence wins, and a withheld
fence claims no range of its own. Overlap uses GitLab's closed-interval semantic: two single-line
fences anchored at the identical line collide; two fences that merely touch (`[n, m]` next to
`[m + 1, k]`) do not. A demoted finding still ships with its prose `suggestion` — only the
one-click affordance is withheld — and which of a run's ranked fences are withheld is a pure
function of the findings and the diff, so a rerun reaches the same verdict regardless of what has
already posted.
This rule is scoped to overlapping ranges: GitHub's batch apply can also fail on multi-line
suggestions that are merely adjacent rather than overlapping, and a fence is never withheld for
that case.

<!-- generated-from-registry-identity:severity_legend — do not edit; run scripts/generate_contract_requirements.py -->
Product mark: ⚔️ (Code Gauntlet). Severity emojis: 🔴 critical, 🟠 high, 🟡 medium, 💡 low.
Rule source labels: documented_rule -> Cited rule, code_comment -> Cited comment, repo_precedent -> Repo precedent, self_inconsistency -> Inconsistency; unknown values -> Cited rule.
<!-- /generated-from-registry-identity:severity_legend -->

### Using post_review.py

**Do NOT post PR comments via direct `gh api` or `glab api` calls.** Use the bundled `scripts/post_review.py` script instead. It handles platform detection, diff validation, and API calls deterministically.

**Usage:**

```bash
{python} "{plugin_root}/scripts/post_review.py" "<findings_json_path>" [--report "PATH"]
```

For a legacy bare findings array, pass `--report PATH` together with `--owner`, `--repo`,
`--pr-number`, `--platform`, `--web-origin`, and `--sha`. The script derives `review_body` from the
report Summary and forms the wrapper in code. Wrapper inputs already carrying `review_body` use
that body as is.

Every finding needs both `admission` and `disposition` stamps for delivery. A finding missing
either stamp is held back, unless its disposition is `eliminate`. Bare arrays still use the
wrapper arguments above, but unstamped findings post no comments.

**Findings JSON schema:**

> `suggested_fix_code` below is optional and gated: every discovery agent may emit it, but
> `post_review.py` only renders it as a `suggestion` fence when it passes the deterministic
> apply-check (string, non-stale, matching range and anchor, within the size bound); otherwise
> the finding falls back to the prose `suggestion`. Callers constructing their own post-review
> JSON pass through the same gate.

> `post_review.py` reads the **v2-aliased** field names (`body`, `line`, `end_line`).
> The persist boundary adds these aliases alongside the canonical names
> (`description`, `line_start`, `line_end`) — a union schema; existing keys are
> never overwritten. Report markdown uses the canonical names — see
> `report-format.md`.

```json
{
    "review_body": "The report's Summary section body: counts first, selected findings index, and any remainder with selection reasons",
    "findings": [
        {
            "file": "src/foo.py",
            "line": 42,
            "end_line": 45,
            "severity": "critical|high|medium|low",
            "title": "Finding title",
            "body": "Detailed explanation and context",
            "suggestion": "prose fix advice (optional; renders as a **Suggested fix:** block)",
            "claude_md_rule": "the cited project rule (optional; heading is keyed by rule_source)",
            "rule_source": "documented_rule|code_comment|repo_precedent|self_inconsistency (when claude_md_rule is present)",
            "spec_text": "the contradicted spec text (optional; renders as **Cited rule:** when there is no claude_md_rule)",
            "suggested_fix_code": "code block (optional; renders as suggestion)"
        }
    ],
    "owner": "repository-owner",
    "repo": "repository-name",
    "pr_number": 123,
    "platform": "github|gitlab",
    "sha": "<full_sha>"
}
```

**Fields:**

The poster prepares text and applies host-specific containment in code. Pass the original fields
unchanged. See [delivery rendering](../../../docs/delivery-rendering.md#outbound-text-preparation)
when maintaining escaping or host rendering.

- `review_body` — the pipeline-rendered Summary body: counts, selected findings index, and any remainder. The change summary stays in Change Context. Code applies UTF-8 body budgets and preserves the footer; see [delivery rendering](../../../docs/delivery-rendering.md#posted-body-budgets).
- `findings` — array of inline comments
  - `file` — relative path in repository
  - `line` — line number in diff (new version)
  - `end_line` — optional; enables multi-line comments on GitHub
  - `severity` — emoji selected from: critical, high, medium, low
  - `title` — one-line finding summary
  - `body` — explanation and context (delivery alias of canonical `description`; see boundary note above)
  - `suggestion` — optional prose fix advice, rendered under a **Suggested fix:** heading. Carried on every finding the pipeline produces (canonical schema), so the delivery JSON should pass it straight through.
  - `claude_md_rule` / `spec_text` — optional; whichever survives sanitize renders under a heading as a blockquote (`claude_md_rule` preferred when both survive). When `claude_md_rule` is rendered, `rule_source` selects **Cited rule**, **Cited comment**, **Repo precedent**, or **Inconsistency**. Unknown or absent values use **Cited rule**. A `spec_text` fallback uses **Cited rule**. The raw `rule_source` value is never rendered.
  - `suggested_fix_code` — optional code block, rendered as a committable GitHub/GitLab suggestion IF it passes `post_review.py`'s deterministic apply-check at the render site; otherwise stripped and the finding falls back to the prose `suggestion`. A body-budget cut also omits a suggestion block whole. Emitted by discovery agents when the fix is a byte-exact drop-in replacement, or supplied directly by a caller's own post-review JSON — same gate either way.
    A patch containing marker-shaped text is downgraded with reason `marker_shaped`.
  - Every optional field above treats `null`, `""` and whitespace-only identically to absent: no heading is emitted at all.
- `owner` — repository owner (GitHub org/user or GitLab group)
- `repo` — repository name
- `pr_number` — GitHub PR number or GitLab MR IID
- `platform` — optional; "github" or "gitlab". If omitted, the poster recognizes validated `github.com`/`gitlab.com` hosts and their dot subdomains from git remote. Other hosts must set `platform` explicitly.
- `web_origin` — optional; the PR forge's http(s) origin (scheme, host and optional port, no path). On GitHub it selects the `gh` host; when omitted, `gh` uses its default host.
- `sha` — optional; the full commit the review actually ran against. `post_review.py` stamps this into the prior-review marker, falling back to `git rev-parse HEAD` when absent. Always set it (the workflow's `prIdentity` wrapper already carries it) — if HEAD moved between the review and the post, the fallback records a commit no review examined, and the next run's incremental diff is scoped against it.

A wrong type on the fields below is refused before any forge access; other fields are not type-checked.
`owner`/`repo`: strings; `pr_number`: non-bool integer or string; `platform`: string or null; `web_origin`: string when present.
`findings`: array of objects; `file`: string when present; `line`/`end_line`: non-bool integer or null; `consolidation_key`: string or null.

**Example workflow:**

Standalone caller example only. During Phase 8, pass the persisted `artifactPaths.postReview`
file unchanged; never reconstruct it from this example.

Use the Write tool to save the input JSON at
`{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json`. Finding text stays
in the JSON file rather than becoming Python or shell source:

```json
[
  {
    "file": "app.js",
    "line": 42,
    "severity": "critical",
    "title": "SQL injection in query builder",
    "body": "User input concatenated into SQL without parameterization.",
    "suggested_fix_code": "const query = db.prepare('SELECT * FROM users WHERE id = ?').get(id);"
  }
]
```

Then post the file:

```bash
{python} "{plugin_root}/scripts/post_review.py" \
  "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json" \
  --report "{output_dir}/code-gauntlet-report-{head_sha_short}.md" \
  --owner "myorg" --repo "myapp" --pr-number 42 --platform github --sha "FULL_HEAD_SHA"
```

**Script behavior:**

- **GitHub:** Posts a single batched review with inline comments (event: COMMENT), then summary.
- **GitLab:** Posts a summary note, then per-finding inline discussions with position metadata.
- **GitLab posting token:** Use a dedicated Reporter-role token. On GitLab CE 19.4.1 it is the
  lowest role that can read a private project's merge requests and post notes and inline
  discussions (a Guest could post on a public project but could not read a private project's
  merge requests). A Reporter can still run `react`
  (alias `award`), `internal_note`, `submit_review`, `todo` and `unsubscribe` and the `shrug` and
  `tableflip` substitutions, a Guest the same without `internal_note`. On GitLab CE 19.4.1, a
  Developer has 29 available commands, including `approve`, `close`, `label`, `rebase` and
  `title`; the count includes the `shrug` and `tableflip` substitutions. The prose escapes stay
  on at every role, because GitLab deletes recognized command lines even when it refuses them.
- **Delivery keys:** Keys hash prepared title and rendered sections. Findings whose posted text
  changes under this contract may post once more on an open MR, then deduplicate on the new key.
  A `null` title uses the absent-title key. Grouping, patch gating and folds do not change it.
- **Diff validation:** Parses diff to verify each finding line is in the PR/MR. Skips invalid lines with warning.
- **Exit status:** Non-zero when nothing could be delivered, and — GitLab only — when a `--dry-run` finds any malformed inline position. The dry-run payload is still written; read it for what was wrong. Non-zero on both platforms when the composed summary body is still over the platform byte budget after composition; that refusal happens before the post, so no dry-run payload is written for it.
- **Metadata footer:** Appends the prose `Generated by code-gauntlet | Reviewed up to: {sha}` line and the `code-gauntlet-findings` HTML comment to `review_body`. Preparation removes terminated comments before composition; the prose half deduplicates only against a standalone footer line carrying the same head SHA. An over-budget body always ends with the full canonical footer.
<!-- generated-from-registry-identity:delivery_identity — do not edit; run scripts/generate_contract_requirements.py -->
- **Identity:** prepends `### ⚔️ Code Gauntlet` to `review_body` and appends `⚔️ *Code Gauntlet*` to every rendered comment body — one mark per delivered surface, never one per finding. Never hand-type either.
<!-- /generated-from-registry-identity:delivery_identity -->

The header the script prepends, verbatim:

<!-- generated-from-registry-identity:summary_header — do not edit; run scripts/generate_contract_requirements.py -->
### ⚔️ Code Gauntlet
<!-- /generated-from-registry-identity:summary_header -->

### Findings metadata footer

`scripts/gauntlet/delivery/compose.py::compose_review_body` composes this footer and fits the body to the platform budget. `scripts/gauntlet/delivery/post.py` posts the composed body. Never hand-type it. `<!-- Canonical source: scripts/gauntlet/marker.py -->`. What the code writes (for reference):

```html
---
Generated by code-gauntlet | Reviewed up to: {full_sha}

<!-- code-gauntlet-findings: {"version":"3.0","findings_count":{N},"sha":"{full_sha}"} -->
```

---

## Task Creation

After the single consent question, follow [Phase 8 Stage 2](phase8-delivery.md#stage-2-task-board).
The renderer returns one object per delivered finding with `subject`, `description`, and `metadata`.
Its description contains Issue, Location, optional Evidence, optional Suggested Fix, Category,
and optional Details or Toolchain sections. Metadata includes task identity, scope, requirements,
proof artifacts, verification commands, commit template, and review context. Pass these fields
unchanged through `TaskCreate(subject, description)` then `TaskUpdate(taskId, metadata)`.
Do not create tasks headless or when the delivery payload is absent. Do not hand-compose tasks.

Category is normalized severity plus dimension, separated by ` | `. Unknown severity becomes low.
Medium and low select trivial complexity and haiku; the other labels select standard and sonnet.

---

## Markdown File

Canonical behavior is the "Markdown only" branch of Phase 8 Stage 1 in `phase8-delivery.md`: surface the path to the already-persisted `artifactPaths.report` — no default write. A write outside `{output_dir}` happens only when the user names a destination.

---

## Chat

**Headless only** (`configResult.resolved.delivery` includes `"chat"`): the full report body is included in the
final response message. The report carries the `Headless config:` block. If no report materializes, repeat
the block from the Phase 1 Bash result as the fallback receipt. Interactive runs have no
chat delivery method — the Phase 8 delivery question offers Post to PR/MR or markdown-only, and chat gets
only the short completion summary.
