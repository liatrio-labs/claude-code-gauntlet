# REVIEW.md Specification

A `REVIEW.md` file lets project maintainers customize how code-gauntlet behaves. It can live at the repository root and in subdirectories alongside CLAUDE.md files. It's optional — sensible defaults apply when absent.

## Contents

- **Format** — Section overview, all sections optional
- **Section Details** — Rules and other prose, Severity/Confidence Thresholds, Default Delivery, Ignore
- **Hierarchy** — Root + subdirectory configs, merge rules, discovery prompts
- **Rule-Writing Principles** — Prescriptive vs directional, 15-25 rules per file
- **Scaffolding Templates** — Root template, subdirectory template

## Format

REVIEW.md is a markdown file. The only part code-gauntlet's parser reads mechanically is a single
config block — a fenced ```` ```yaml # code-gauntlet ```` block, or an
`<!-- code-gauntlet-config -->` comment block — containing plain `key: value` lines in four
recognized snake_case keys. Everything else in the file (headings, prose, a `## Rules` list) is
free markdown: it is never parsed, but the whole file's text — this config block included — is
folded into the shared context every review agent reads, so prose still reaches the agents as
advisory guidance (see "Rules and other prose" below).

<!-- code-gauntlet-defaults -->
Built-in defaults, applied whenever a key is absent from the config block: `confidence_threshold`
**55** for non-security dimensions, `security_min_confidence` **70**, `severity_threshold`
**low** (show everything). `scripts/filter_findings.py` and `workflows/src/filterFindings.js`
both define these as constants; `tests/test_review_md_contract.py` asserts this paragraph's three
numbers match both files.
<!-- /code-gauntlet-defaults -->

````markdown
# Review Configuration

## Rules
<!-- Custom natural-language rules applied to all review agents. -->
<!-- These supplement (don't replace) the built-in review logic. -->
- All database queries must use parameterized statements, never string concatenation
- Public API endpoints must validate request body schema before processing
- Feature flags must have an expiration date comment
- Console.log statements should not be committed to main

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
heading. It is shown here after `## Rules` only because that's how the scaffolding templates below
lay a file out.

One more setting is read through a separate mechanism: the root REVIEW.md text is checked directly for a
`## Default Delivery` heading (not the config block above, and not the Filter stage's
`parseReviewMd`/`parse_review_md`), and only in headless mode, where it feeds `CODE_GAUNTLET_DELIVERY`
precedence. A `## Model Tier` heading is **no longer read anywhere** (issue #153): the model policy is
fixed to the single benchmarked configuration, and the only remaining pin is the fail-loud
`CODE_GAUNTLET_MODEL_TIER` env knob documented in `references/headless-mode.md`. An existing
`## Model Tier` section in a repo's REVIEW.md is inert prose — harmless, and no longer worth removing or
warning about.

**Legacy forms.** `parseReviewMd`/`parse_review_md` also still recognize the pre-rename block
forms — a fenced ```` ```deep-review ```` block and an `<!-- deep-review-config -->` comment block
— so a REVIEW.md written before the `code-gauntlet` rename keeps working unmodified. When a file
somehow has both a current-form block and a legacy-form block, the parser checks the current form
first and uses that one.

## Section Details

### Rules and other prose

`## Rules` (or any other free-text section — a `## Focus` or `## Skip` heading some REVIEW.md files
still carry from older guidance) is never parsed for structure. Its content is advisory: the whole
REVIEW.md file is gathered by value and folded into the shared context every review agent reads, so
a well-written `## Rules` list genuinely steers the agents the same way a CLAUDE.md convention does.
A `## Focus` or `## Skip` heading has no such effect beyond that — code-gauntlet does not read them
as instructions to gate dimensions or exclude files; they are just more prose an agent may or may
not act on. Two structural things actually do gate/exclude, and neither is REVIEW.md-configurable:

- **Which dimensions run** is decided automatically by the trivial-scope gate (all changed files
  low-risk and under 50 changed lines → light scope, `bug` + `security` only; otherwise every
  dimension runs). There is no REVIEW.md key that changes this.
- **Which findings are suppressed** is the `ignore` list in the config block (see below) — a
  substring match against finding text, not a file-path exclusion.

Rules that all agents should check should be:

- Specific and actionable (not vague guidelines)
- Objectively verifiable (an agent can determine compliance)
- Focused on the code being reviewed (not process/workflow rules)

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
- `pr_comments` — Post findings as inline PR/MR comments
- `markdown` — Surface the path to the already-persisted report under the output directory (`artifactPaths.report`); no default new file

Read in **headless mode only**, where it sits between the `CODE_GAUNTLET_DELIVERY` env pin and the headless default. Interactive runs ignore it and ask once at the end of the run instead (`references/phase8-delivery.md` Stage 1) — the report is on disk either way, so the decision costs nothing to defer.

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

**Soft cap: 10-15 ignore patterns per file.** If you exceed this, it signals either rules that are too sensitive (remove or rewrite them) or a systematic mismatch between your rules and your codebase. Proliferating ignore patterns erodes trust in the review system — when engineers start ignoring entire categories of findings, the tool becomes actively harmful.

## Hierarchy

REVIEW.md discovery walks the repo root, every changed file's directory, and their
ancestors up to root — the same directory set the project-rules pass walks
(`scripts/collect_project_rules.py`), not a CLAUDE.md-location anchor (issue #80). A
repository can have:

- A **root** `REVIEW.md` at the repo root (applies to all files by default)
- **Subdirectory** `REVIEW.md` files in any directory on that walked set (applies to files in that directory tree)

Subdirectory REVIEW.md files are optional — they're only needed when different parts of the codebase need different review standards (e.g., stricter security rules for an API directory, different thresholds for a legacy module).

**Placement decision test:** before adding a rule to a subdirectory REVIEW.md, ask "would this rule generate false positives in the other stack?" If yes, it belongs in the subdirectory. If the rule applies cleanly everywhere, it belongs in root. Example: "Never use `async void`" is meaningless in a React frontend — it goes in `backend/REVIEW.md`. "Validate all user input" applies everywhere — it goes in root.

### Inheritance model

When a subdirectory has its own REVIEW.md, its settings combine with the root as follows:

| Section | Behavior | Rationale |
|---------|----------|-----------|
| `confidence_threshold` | **Override** — subdirectory value replaces root | A module may need stricter or looser thresholds |
| `severity_threshold` | **Override** — subdirectory value replaces root | Some areas warrant reporting lower-severity issues |
| `default_delivery` | **Override** — subdirectory value replaces root | Unlikely to vary by directory, but supported for consistency |
| `rules` (and other free prose) | **Accumulate** — subdirectory content adds to root content | Directory-specific conventions supplement project-wide ones |
| `ignore` | **Accumulate** — subdirectory patterns add to root patterns | Suppressions are additive |

In short: **settings override, rules and patterns accumulate.**

**Current implementation note.** This section describes the intended per-file scoping. The
shipped merge (`resolveReviewConfig`, `workflows/src/args.js`) does not scope by subtree yet: it
sorts every discovered REVIEW.md root-first by path depth and folds them into **one flat config
applied to every finding in the run**, not per-file. A deeper entry's setting still overrides a
shallower one's in that single merged config, and `ignore` still accumulates across all of
them — so the override/accumulate rules above hold — but a subdirectory REVIEW.md's threshold
currently governs the whole review, not just files under that subdirectory. The worked example
below states the intended per-file result; treat it as the target, not the current behavior.

### Example

```
repo/
  REVIEW.md              # confidence_threshold: 70, rules: [rule-A, rule-B]
  CLAUDE.md
  api/
    CLAUDE.md
    REVIEW.md            # confidence_threshold: 70, rules: [rule-C]
  legacy/
    CLAUDE.md            # no REVIEW.md — root config applies
```

For a file in `api/`:

- confidence_threshold = **70** (overridden by api/REVIEW.md)
- rules = **[rule-A, rule-B, rule-C]** (accumulated)

For a file in `legacy/`:

- confidence_threshold = **70** (root applies)
- rules = **[rule-A, rule-B]** (root only)

### Discovery

REVIEW.md discovery is repo root + changed-file directories + their ancestors — the same
directory set as the project-rules pass (`scripts/collect_project_rules.py`). Code-gauntlet
walks that set during Phase 2d context gathering and checks each directory for a matching
REVIEW.md. AGENTS.md/QODO.md resolution (`references/phase2-triage.md` 2d step 3,
`scripts/collect_project_rules.py`) shares that directory walk but remains a separate pass
with its own source list: REVIEW.md config and AGENTS.md/CLAUDE.md project rules are still
read and applied independently — only the directory walk is shared, not the content, and
finding no AGENTS.md/QODO.md never affects REVIEW.md discovery or precedence.

#### Detection flow (Phase 2d)

Walk the repo root + changed-file directories + their ancestors (the project-rules directory set), and
check each for a matching REVIEW.md. **Every outcome is a non-blocking notice — none of them is a
question** (issue #35). Emit at most one notice per run, alongside the triage announcement:

- **No REVIEW.md anywhere:**

  ```text
  No REVIEW.md found — reviewing with built-in defaults. Run `build-review-md` any time to configure
  thresholds, ignore patterns, and project rules.
  ```

- **Root exists, a walked directory has none:**

  ```text
  Root REVIEW.md applies to every directory in this review. {directory} has no REVIEW.md of its own —
  add one if that area needs different standards.
  ```

- **All locations covered** → say nothing and proceed.

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): suppress both notices — root config applies,
> `build-review-md` is never invoked, and REVIEW.md is read-only. The hierarchical parse still runs; no
> REVIEW.md is created. See `references/headless-mode.md`.

---

## Rule-Writing Principles

When helping users add rules to REVIEW.md (during scaffolding or when updating), follow these principles drawn from research on AI reviewer effectiveness:

1. **15-25 rules per file, ~50 rules across all REVIEW.md files combined.** Beyond these limits, LLM adherence degrades for ALL rules, not just new ones. The review system's own prompts consume ~50 instruction slots; each rule competes for the remaining capacity. With a root + two subdirectory files (e.g., backend + frontend), budget roughly 15-20 for root and 10-15 per subdirectory.
2. **Prescriptive for security/correctness, directional for design.** "All async methods MUST accept CancellationToken" (binary pass/fail) vs "Prefer immutable types where practical" (allows edge cases). Prescriptive rules produce low false-positive rates; directional rules handle nuance.
3. **Always include rationale.** "Never force push" is a flat instruction. "Never force push — this rewrites shared history and is unrecoverable for collaborators" helps the reviewer generalize to related scenarios (like `git reset --hard` on shared branches).
4. **Specific and verifiable.** Each rule should have a binary pass/fail condition. "Write clean code" is unverifiable. "All public API endpoints must validate request body schema before processing" is verifiable.
5. **Never duplicate linters.** If ESLint, mypy, tsc, clippy, or any deterministic tool catches it, don't make it a review rule. Deterministic tools are faster, cheaper, and more reliable for objective checks.
6. **Place critical rules first, commonly violated rules last.** LLMs exhibit peripheral bias — they attend more strongly to instructions at the beginning and end of the prompt. Put security and correctness rules first (highest stakes), and place the rules your team violates most frequently last (highest recall value). The middle of the list gets the least attention, so put stable, well-understood conventions there.
7. **Use severity prefixes sparingly.** `CRITICAL:` for rules that are never acceptable to violate (3-4 per file max). Overuse makes the emphasis invisible.

**Effective rules:**

```
- CRITICAL: Never commit secrets, API keys, or connection strings in source
  files. Use environment variables or secret managers.
- All public API endpoints must enforce authentication and authorization.
  Missing auth on a single endpoint exposes the entire resource.
- Prefer composition over inheritance. Deep hierarchies make behavior
  unpredictable and testing difficult.
```

**Ineffective rules:**

```
- Write clean code
- Follow best practices
- Check for security issues
```

---

## Scaffolding Templates

When the user opts to create a REVIEW.md during Phase 2d, use these templates. The templates set sensible defaults and provide structural guidance without guessing at repo-specific content.

### Root REVIEW.md template

````markdown
# Review Configuration

<!-- Customizes how code-gauntlet analyzes this repository.
     See references/review-md-spec.md in the code-gauntlet skill for all options. -->

## Rules

<!-- Add 15-25 project-specific rules. Each rule should be:
     - Specific and verifiable (pass/fail, not vague)
     - Include rationale (why this matters — helps the reviewer generalize)
     - Use CRITICAL: prefix only for security/correctness rules (3-4 max)
     - Don't duplicate what linters or type checkers already catch

     Organize by category. Place security and correctness rules first.

     Examples of well-written rules:

     ### Security
     - CRITICAL: Never commit secrets, API keys, or connection strings.
       Use environment variables or secret managers.
     - All API endpoints must enforce authentication and authorization.
       Missing auth on a single endpoint exposes the entire resource.

     ### Error Handling
     - Public API endpoints must return structured error responses.
       Never expose stack traces or internal details to clients.

     ### Architecture
     - Changes to shared API contracts require review of all consumers.
       Flag PRs that modify contract types without corresponding updates.
-->

## Default Delivery

<!-- How to deliver review results. Comma-separated list.
     Options: chat, pr_comments, markdown
     When set, skips the delivery preference prompt.
     Task creation is always offered separately after delivery.
     Uncomment and adjust to your preference. -->
<!-- chat,pr_comments -->

<!-- The block below is the only part of this file code-gauntlet parses
     mechanically. Every key is optional — omit a key entirely to use its
     built-in default rather than guessing at a starting number. -->
```yaml
# code-gauntlet
# confidence_threshold: <0-100>
#   Minimum confidence to include findings. Built-in default when omitted: 55
#   for non-security dimensions, 70 for security. Setting this applies it to
#   all non-security dimensions; there is no per-dimension override.
# security_min_confidence: <0-100>
#   Effective security threshold is min(confidence_threshold,
#   security_min_confidence) — a ceiling, not a floor. Set this lower than
#   confidence_threshold to keep borderline security findings; there is no
#   minimum it cannot go below.
# severity_threshold: <critical|high|medium|low>
#   Minimum severity to include. Built-in default when omitted: low (show
#   everything). Useful for high-debt codebases where low/medium noise drowns
#   out critical issues.
# ignore:
#   Suppress known false positives. Each entry is a substring matched
#   case-insensitively against a finding's title + description + suggestion,
#   first match wins — not scoped by dimension. The pattern is the substring ONLY, never a
#   reason or date appended to it (that changes what it matches); track why an
#   entry was added in the commit/PR that added it instead. E.g.:
#   ignore:
#     - <substring>
#     - <another substring>
```
````

### Subdirectory REVIEW.md template

````markdown
# Review Configuration — [directory name]

<!-- Settings here override root REVIEW.md. Rules and ignore patterns
     accumulate (add to root), settings (thresholds) replace root.
     Only create subdirectory configs when this area needs DIFFERENT standards
     than the root — e.g., stricter security for an API directory. -->

## Rules

<!-- Directory-specific rules (these ADD to root REVIEW.md rules).
     Aim for 5-10 rules covering technology or domain-specific patterns.
     Don't contradict root rules — extend them. -->

<!-- Optional config block — same keys as the root template, uncommented only
     if this directory needs a different threshold or suppression list than root. -->
```yaml
# code-gauntlet
# ignore:
#   - "..."
```
````
