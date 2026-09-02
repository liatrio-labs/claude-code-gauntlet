# Delivery Guide

Implementation details for each delivery method in Phase 8, interactive and headless.

---

## PR/MR Comments (platform-aware)

**Only comment on files in the diff.** The GitHub/GitLab API rejects inline comments on files not part of the PR/MR diff (HTTP 422). Before posting each inline comment, verify the file path is in the changed files list. Cross-file impact findings about files outside the diff go in the top-level summary comment with a note: "This finding references `path/to/file.cs` which is not in this PR's diff."

**Batch ALL inline comments into a single review event** — one GitHub notification instead of N separate ones. Notification fatigue causes teams to auto-dismiss AI review within ~10 days.

**The inline comment set is the pipeline's, not yours.** When the user answers "Post to PR/MR" at the Phase 8 delivery question, post the `artifactPaths.postReview` payload verbatim — `selectDelivery` already applied the delivery tier, ranked the survivors, and capped them at `limits.deliveryCap`. Do not re-rank, re-filter, or re-apply the cap. There is no per-finding selection pass: the choice is post or don't.

### Comment body format

**You do not compose the comment body.** `scripts/post_review.py::render_comment_body` builds it
from the fields you supply. Its output, for reference:

```
**{emoji} [{SEVERITY}] {finding.title}**

{body}

**Suggested fix:**
{suggestion}

**Cited rule:**
> {claude_md_rule, falling back to spec_text — blockquoted, one `>` line per source line}

```suggestion
{suggested_fix_code}
```

<!-- generated-from-registry-identity:inline_trailer — do not edit; run scripts/generate_contract_requirements.py -->
⚔️ *Code Gauntlet*
<!-- /generated-from-registry-identity:inline_trailer -->

```

Every field-backed section after `{body}` is emitted only when its field is present — `null`, `""` and
whitespace-only all count as absent and produce no heading.

**`suggestion` always renders as prose; it never becomes a suggestion block.** Only
`suggested_fix_code` produces the committable ```suggestion fence, and only after
`post_review.py` runs it through a deterministic pre-render apply-check — the field must be a
string, non-empty after redaction, ship a matching `end_line`, land inside a valid diff range,
match this render site's actual apply range, differ from the current text, and stay within the
size bound. Any failure strips the field before render and the finding falls back to the prose
`suggestion`, with the reason recorded via `warn_skip`. This used to be documented as a
per-finding judgement call ("if `suggestion` looks like code, fence it"), which is precisely the
wrong shape: a ```suggestion fence is a one-click APPLY button, so turning prose into one on a
hunch writes the guess straight into the author's branch. The rule is still structural — a fence
comes from a field that exists to be a patch, or it does not appear — the apply-check is the
mechanism that keeps that promise now that agents populate the field too.

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
<!-- /generated-from-registry-identity:severity_legend -->

### Using post_review.py

**Do NOT post PR comments via direct `gh api` or `glab api` calls.** Use the bundled `scripts/post_review.py` script instead. It handles platform detection, diff validation, and API calls deterministically.

**Usage:**
```bash
python3 {plugin_root}/scripts/post_review.py <findings_json_path>
```

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
    "review_body": "Executive summary comment with finding counts (post_review.py appends the footer)",
    "findings": [
        {
            "file": "src/foo.py",
            "line": 42,
            "end_line": 45,
            "severity": "critical|high|medium|low",
            "title": "Finding title",
            "body": "Detailed explanation and context",
            "suggestion": "prose fix advice (optional; renders as a **Suggested fix:** block)",
            "claude_md_rule": "the cited project rule (optional; renders as **Cited rule:**)",
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

- `review_body` — executive summary comment (counts, no spoilers); prose only — the brand
  header is prepended by the script
- `findings` — array of inline comments
  - `file` — relative path in repository
  - `line` — line number in diff (new version)
  - `end_line` — optional; enables multi-line comments on GitHub
  - `severity` — emoji selected from: critical, high, medium, low
  - `title` — one-line finding summary
  - `body` — explanation and context (delivery alias of canonical `description`; see boundary note above)
  - `suggestion` — optional prose fix advice, rendered under a **Suggested fix:** heading. Carried on every finding the pipeline produces (canonical schema), so the delivery JSON should pass it straight through.
  - `claude_md_rule` / `spec_text` — optional; whichever survives sanitize renders under a **Cited rule:** heading as a blockquote (`claude_md_rule` preferred when both survive). These are how a convention or intent finding shows the reviewer the rule it is measured against.
  - `suggested_fix_code` — optional code block, rendered as a committable GitHub/GitLab suggestion IF it passes `post_review.py`'s deterministic apply-check at the render site; otherwise stripped and the finding falls back to the prose `suggestion`. Emitted by discovery agents when the fix is a byte-exact drop-in replacement, or supplied directly by a caller's own post-review JSON — same gate either way.
  - Every optional field above treats `null`, `""` and whitespace-only identically to absent: no heading is emitted at all.
- `owner` — repository owner (GitHub org/user or GitLab group)
- `repo` — repository name
- `pr_number` — GitHub PR number or GitLab MR IID
- `platform` — optional; "github" or "gitlab". Auto-detected from git remote if omitted.
- `sha` — optional; the full commit the review actually ran against. `post_review.py` stamps this into the prior-review marker, falling back to `git rev-parse HEAD` when absent. Always set it (the workflow's `prIdentity` wrapper already carries it) — if HEAD moved between the review and the post, the fallback records a commit no review examined, and the next run's incremental diff is scoped against it.

**Example workflow:**

```bash
# 1. Build findings JSON using Python (handles all escaping; no heredoc/Write tool issues)
# 2. Post review comments to PR
Bash(
  description="Posting {N} review comments to PR #{pr_number}",
  command="""python3 -c "
import json, sys
findings = {
    'review_body': 'Found 3 issues: 1 critical, 2 medium.',
    'findings': [
        {
            'file': 'app.js',
            'line': 42,
            'severity': 'critical',
            'title': 'SQL injection in query builder',
            'body': 'User input concatenated into SQL without parameterization.',
            'suggested_fix_code': 'const query = db.prepare(\'SELECT * FROM users WHERE id = ?\').get(id);'
        }
    ],
    'owner': 'myorg',
    'repo': 'myapp',
    'pr_number': 42,
    'sha': 'd4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3'
}
with open(sys.argv[1], 'w') as f:
    json.dump(findings, f, ensure_ascii=False, indent=2)
" "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json"

python3 {plugin_root}/scripts/post_review.py "{output_dir}/code-gauntlet-post-review-input-{head_sha_short}.json"
""")
```

**Script behavior:**

- **GitHub:** Posts a single batched review with inline comments (event: COMMENT), then summary.
- **GitLab:** Posts a summary note, then per-finding inline discussions with position metadata.
- **Diff validation:** Parses diff to verify each finding line is in the PR/MR. Skips invalid lines with warning.
- **Exit status:** Non-zero when nothing could be delivered, and — GitLab only — when a `--dry-run` finds any malformed inline position. The dry-run payload is still written; read it for what was wrong.
- **Metadata footer:** Appends both the prose `Generated by code-gauntlet | Reviewed up to: {sha}` line and the `code-gauntlet-findings` HTML comment to `review_body`, mechanically and idempotently (each half is skipped if already present).
<!-- generated-from-registry-identity:delivery_identity — do not edit; run scripts/generate_contract_requirements.py -->
- **Identity:** prepends `### ⚔️ Code Gauntlet` to `review_body` and appends `⚔️ *Code Gauntlet*` to every rendered comment body — one mark per delivered surface, never one per finding. Never hand-type either.
<!-- /generated-from-registry-identity:delivery_identity -->

The header the script prepends, verbatim:

<!-- generated-from-registry-identity:summary_header — do not edit; run scripts/generate_contract_requirements.py -->
### ⚔️ Code Gauntlet
<!-- /generated-from-registry-identity:summary_header -->

### Findings metadata footer

`scripts/post_review.py` appends this to `review_body` — never hand-type it. `<!-- Canonical source: scripts/review_marker.py -->`. What the code writes (for reference):

```html
---
Generated by code-gauntlet | Reviewed up to: {full_sha}

<!-- code-gauntlet-findings: {"version":"3.0","findings_count":{N},"sha":"{full_sha}"} -->
```

The `findings` key is reserved/optional — present only when the caller supplies it (owned by issue #36; absent today).

---

## Task Creation

**Never create tasks automatically.** The single Yes/No task-board question in Phase 8 Stage 2
(`references/phase8-delivery.md`) is the consent gate; this section covers what happens after a "Yes".

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): the task board is skipped (Phase 8 Stage 2), so no tasks are created and this section does not run. See `references/headless-mode.md`.

### Create FIX tasks

Read `references/fix-task-metadata.md` for the full template. The process is:

1. **Detect toolchain** (Step 2.5) — scan for package.json, Cargo.toml, go.mod, etc.
2. **Detect patterns_to_follow** (Step 3a) — identify 1-2 nearby files as style references
3. **TaskCreate** (Step 3b) — structured description with Issue, Location, Evidence, Suggested Fix
4. **TaskUpdate with metadata** (Step 3c) — full cw-execute-compatible metadata

After creating: "Created N tasks from review findings."

---

## Markdown File

Canonical behavior is the "Markdown only" branch of Phase 8 Stage 1 in `phase8-delivery.md`: surface the path to the already-persisted `artifactPaths.report` — no default write. A write outside `{output_dir}` happens only when the user names a destination.

---

## Chat

**Headless only** (`CODE_GAUNTLET_DELIVERY` includes `chat`): the full report body is included in the
final response message (which already repeats the `Headless config:` block). Interactive runs have no
chat delivery method — the Phase 8 delivery question offers Post to PR/MR or markdown-only, and chat gets
only the short completion summary.
