# REVIEW.md Specification

A `REVIEW.md` file lets project maintainers customize how code-gauntlet behaves. It can live at the repository root and in subdirectories alongside CLAUDE.md files. It's optional — sensible defaults apply when absent.

For writing rules and starting a file, see the [authoring guide](../../../docs/review-md-authoring.md).

## Contents

- **Format** — Section overview, all sections optional
- **Section Details** — Rules and other prose, Severity/Confidence Thresholds, Default Delivery, Ignore
- **Hierarchy** — Root + subdirectory configs, merge rules, discovery prompts

## Format

REVIEW.md is a markdown file. The only part code-gauntlet's parser reads mechanically is a single
config block — a fenced ```` ```yaml # code-gauntlet ```` block, or an
`<!-- code-gauntlet-config -->` comment block — containing plain `key: value` lines in four
recognized snake_case keys. Everything else in the file (headings, prose, a `## Rules` list) is
free markdown and is never parsed (see "Rules and other prose" below).

The collector receipt carries a checksum of each file's normalized text and of the ordered list; the workflow discloses unproven or mismatched text as run gaps while applying the stamped settings.

<!-- code-gauntlet-defaults -->
Built-in defaults, applied whenever a key is absent from the config block: `confidence_threshold`
**55** for non-security dimensions, `security_min_confidence` **70**, `severity_threshold`
**low** (show everything). `workflows/src/filterFindings.js` defines these as constants;
`tests/test_review_md_contract.py` asserts this paragraph's three values match.
<!-- /code-gauntlet-defaults -->

````markdown
# Review Configuration

```yaml
# code-gauntlet
confidence_threshold: 75
severity_threshold: medium
security_min_confidence: 70
ignore:
  - "console.log in development mode"
  - "import order"
```
````

The config block can go anywhere in the file — the parser searches for it independent of any
heading.

One more setting, `## Default Delivery`, is read through a separate mechanism; see [Default Delivery](#default-delivery).

A `## Model Tier` heading is inert prose. The model policy remains fixed, and its environment pin is documented in `references/headless-mode.md`.

**Legacy forms.** `parseReviewMd` (`workflows/src/reviewConfig.js`)/`parse_review_md` also still recognize the pre-rename block
forms — a fenced ```` ```deep-review ```` block and an `<!-- deep-review-config -->` comment block
— so a REVIEW.md written before the `code-gauntlet` rename keeps working unmodified. When a file
somehow has both a current-form block and a legacy-form block, the parser checks the current form
first and uses that one.

## Section Details

### Rules and other prose

`## Rules` and any other free-text section, including a `## Focus` or `## Skip` heading, is never
parsed for structure. Its content is advisory: the collector copies the whole REVIEW.md file into a
`review-rules` block tagged with its source path; every context-reading agent (summarize, discovery,
validate) receives every block and can apply its prose to that directory's subtree. No heading gates
dimensions or excludes files. Structural controls:

- **Which dimensions run** follows the trivial-scope choice: `light` runs `bug` + `security`
  only when all changed files are low-risk and under 50 changed lines; otherwise every dimension
  runs. There is no REVIEW.md key that changes this.
- **Which findings are suppressed** is the `ignore` list in the config block (see below) — a
  substring match against finding text, not a file-path exclusion.

### Severity Threshold

`severity_threshold` in the config block. The minimum severity level to include in the report:

- `critical` — Only blocking issues
- `high` — Critical + high priority
- `medium` — Critical + high + medium
- `low` — Everything (built-in default)

### Confidence Threshold

`confidence_threshold` (and optionally `security_min_confidence`) in the config block. An integer
from 0-100. Findings below the effective threshold are filtered out before the report. When you do
not set `confidence_threshold`, non-security dimensions default to **55** and security to **70**
(see the defaults block above). Higher values are stricter but may miss some real issues. Lower
values surface more findings but may include more false positives.

**The effective security threshold is a ceiling that only ever gets lower, never a floor.** It is
`min(confidence_threshold, security_min_confidence)` — the lower of the two configured numbers, not
a minimum either one is held to. Setting `confidence_threshold: 90` raises the non-security bar to
90; security stays governed by `min(90, security_min_confidence)`, so if `security_min_confidence`
is left at its default 70, security findings still pass at 70+, and setting it explicitly lower
(e.g. 60) lets more borderline security findings through. There is no minimum `security_min_confidence`
can be set to.

Per-dimension confidence thresholds (a `bugs: 75` / `security: 70` / ... key:value form) are not
supported. `confidence_threshold` applies uniformly to every non-security dimension; there is no
way to give `conventions` a different bar than `bugs`.

### Default Delivery

Controls how review results are delivered. A comma-separated list of delivery methods:

- `chat` — Display the full report in the conversation
- `pr_comments` — Post findings to the PR/MR
- `markdown` — Surface the path to the already-persisted report under the output directory (`artifactPaths.report`); no default new file

Only the root `REVIEW.md` may provide this setting. The section opener is a line matching
`^## Default Delivery[ \t]*$`. Its body ends before the first line matching
`^#{1,6}[ \t]`, a fence opener matching ``^[ \t]{0,3}(`{3,}|~{3,})``, or EOF.
Remove HTML comments from the body non-greedily across lines. Take the first remaining
non-blank line and trim it. It is a candidate only when it matches `^[a-z_]+(,[a-z_]+)*$`.
Any other remainder, including prose, comment residue, or nothing, means the setting is unset
and the next precedence source applies. The delivery rule validates candidates; unknown tokens,
duplicates, and `pr_comments` on a local target fail loud as `HEADLESS CONFIG ERROR: REVIEW.md default_delivery=<value> not in {chat,pr_comments,markdown}`.
For a local target, the error uses `{chat,markdown}`.
An environment pin wins over this setting. Interactive mode ignores it.

```

## Default Delivery

chat,pr_comments

```

### Ignore

The `ignore` key in the config block: a plain list of substrings matched case-insensitively against
each finding's title, description, and suggestion combined (`title + "\n" + description + "\n" +
suggestion`), first match wins. It is not scoped by dimension — an entry suppresses any finding
whose text contains it, regardless of which agent raised it.

This is useful when a project has intentional patterns that agents consistently flag incorrectly.

**Write the raw substring only — no surrounding quotes needed, and never append a reason or date to
the pattern itself.** Quotes are optional (the parser strips one matching pair if present, so
`"pattern"` and `pattern` behave identically), but a reason or date tacked onto the end changes what
the entry matches: `ignore` compares the pattern as a literal substring against unquoted finding
text, so `"file naming (EF Core migrations are generated, 2026-03-25)"` only matches a finding whose
title, description, or suggestion happens to contain that entire sentence — which essentially never
happens — and silently suppresses nothing. Keep the pattern to just the words that actually appear
in the finding:

```yaml
# code-gauntlet
ignore:
  - file naming
  - nullable reference
```

Track *why* a pattern was added wherever you already track file history — the commit that adds the
entry, or a PR/issue comment — not inside the pattern string. The parser only reads consecutive `-`
list lines under `ignore:` anyway; a comment line between entries breaks the list silently, so there
is no in-file place to put a per-entry note that both parses correctly and doesn't corrupt the match.

## Hierarchy

`scripts/collect_project_rules.py` owns REVIEW.md discovery across the repo root, changed-file directories, and their ancestors, independently of CLAUDE.md locations. A repository can have:

- A **root** `REVIEW.md` at the repo root (its thresholds and ignore apply to all files by default)
- **Subdirectory** `REVIEW.md` files in any directory on that walked set (their thresholds and ignore apply to files in that directory tree)

Subdirectory REVIEW.md files are optional — they're only needed when different parts of the codebase need different thresholds or ignore patterns (e.g., stricter security for an API directory or suppressions for a legacy module).

### Inheritance model

When a subdirectory has its own REVIEW.md, its settings combine with the root as follows:

| Section | Behavior | Rationale |
|---------|----------|-----------|
| `confidence_threshold` | **Override** — subdirectory value replaces root | A module may need stricter or looser thresholds |
| `security_min_confidence` | **Override** — subdirectory value replaces root | A module may need a different security confidence ceiling |
| `severity_threshold` | **Override** — subdirectory value replaces root | Some areas warrant reporting lower-severity issues |
| `default_delivery` | **Root-only** — only the root REVIEW.md text is checked | Headless delivery is a review-wide setting |
| `rules` (and other free prose) | **Advisory per source subtree** - every context-reading agent (summarize, discovery, validate) receives every source-path-tagged block in the shared context | The path identifies where the prose is advisory; settings enforcement belongs to the pipeline |
| `ignore` | **Accumulate per matching subtree** — matching subdirectory patterns add to root patterns for files under that subtree | Suppressions are additive within the governed subtree |

Thresholds and `ignore` are scoped per subtree by `finding.file`: the root layer is the default,
and matching directory layers override present thresholds and append their ignore lists. Matching uses a byte-exact, case-sensitive `dir/` prefix with no normalization of
finding paths; a non-matching or non-string file uses the root layer. `.reviewignore` remains
global, and `default_delivery` remains root-only.

### Example

```
repo/
  REVIEW.md              # confidence_threshold: 70, rules: [rule-A, rule-B]
  CLAUDE.md
  api/
    CLAUDE.md
    REVIEW.md            # confidence_threshold: 80, rules: [rule-C]
  legacy/
    CLAUDE.md            # no REVIEW.md — root config applies
```

For a file in `api/`:

- confidence_threshold = **80** (overridden by api/REVIEW.md)
- Advisory context contains root rule-A/rule-B and api rule-C in separate source-tagged blocks; rule-C is advisory for api/.

For a file in `legacy/`:

- confidence_threshold = **70** (root applies)
- Advisory context contains both source-tagged blocks; root rule-A/rule-B apply here, while api rule-C is advisory for api/.

### Discovery

The collector's `review_md` receipt is the authoritative REVIEW.md discovery list: repo root, changed-file directories, and ancestors, in the collector's deterministic order. Phase 2d runs the collector before Reading exactly those paths for the raw workflow waist. The collector copies their bounded prose with source provenance into the shared context; the pipeline separately parses the full raw text and applies thresholds and ignores per subtree. Missing CLAUDE.md/AGENTS.md/QODO.md files do not affect REVIEW.md discovery or precedence. A cap omits prose with a disclosed gap, while the listed file still supplies full raw settings text to the waist.

A capped file still counts as discovered. A refused candidate is absent from `review_md`; announce its gap separately. Collector or raw Read failure stops the handoff and is never reported as successful empty discovery.

REVIEW.md is read-only in both modes. When none is discovered, the report renderer appends a brief tip pointing here. The tip appears only in the report.
