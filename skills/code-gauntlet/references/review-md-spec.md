# REVIEW.md Specification

A `REVIEW.md` file lets project maintainers customize how code-gauntlet behaves. It can live at the repository root and in subdirectories alongside CLAUDE.md files. It's optional — sensible defaults apply when absent.

## Contents

- **Format** — Section overview, all sections optional
- **Section Details** — Focus, Skip, Rules, Severity/Confidence Thresholds, Model Tier, Default Delivery, Ignore
- **Hierarchy** — Root + subdirectory configs, merge rules, discovery prompts
- **Rule-Writing Principles** — Prescriptive vs directional, 15-25 rules per file
- **Scaffolding Templates** — Root template, subdirectory template

## Format

REVIEW.md is a markdown file with specific sections. Each section is optional.

```markdown
# Review Configuration

## Focus
<!-- Which review dimensions to prioritize. If specified, only these dimensions run. -->
<!-- Omit this section to run all applicable dimensions. -->
- bugs
- security
- error-handling

## Skip
<!-- File patterns to exclude from review (glob syntax). -->
<!-- These files won't be reviewed by any agent. -->
- "**/*.generated.cs"
- "**/*.designer.cs"
- "**/migrations/**"
- "vendor/**"
- "dist/**"

## Rules
<!-- Custom natural-language rules applied to all review agents. -->
<!-- These supplement (don't replace) the built-in review logic. -->
- All database queries must use parameterized statements, never string concatenation
- Public API endpoints must validate request body schema before processing
- Feature flags must have an expiration date comment
- Console.log statements should not be committed to main

## Severity Threshold
<!-- Minimum severity to include in the report. Default: low -->
<!-- Options: critical, high, medium, low -->
medium

## Confidence Threshold
<!-- Minimum confidence score (0-100) to include in the report. Default: 70 -->
<!-- Use a plain number for all dimensions, or key:value pairs for per-dimension control: -->
<!-- bugs: 75 -->
<!-- security: 70 -->
<!-- cross-file-impact: 75 -->
<!-- conventions: 80 -->
<!-- tests: 75 -->
<!-- types: 75 -->
<!-- simplification: 80 -->
75

## Model Tier
<!-- Default review mode. When set, skips the mode selection prompt. -->
<!-- Options: optimized (Sonnet default, Opus for security) or frontier (all Opus) -->
optimized

## Default Delivery
<!-- How to deliver review results. When set, skips the delivery preference prompt. -->
<!-- Options: chat, pr_comments, markdown (comma-separated) -->
chat

## Ignore
<!-- Specific finding patterns to suppress. Useful for known false positives. -->
<!-- Format: dimension:pattern -->
- compliance:"import order"
- security:"console.log in development mode"
```

## Section Details

### Focus

Controls which review dimensions run. Valid values map to agents:

- `bugs` — Bug detection and error handling (bug-detector agent)
- `security` — Security vulnerability scanning (security-reviewer agent)
- `cross-file-impact` — Cross-file impact analysis (cross-file-impact agent)
- `tests` — Test coverage gap analysis (test-analyzer agent)
- `conventions` — Convention compliance, intent alignment, and comment accuracy (conventions-and-intent agent)
- `types` — Type design analysis (type-design-analyzer agent)
- `simplification` — Code simplification (code-simplifier agent)

When omitted, all applicable dimensions run (the skill auto-detects which are relevant based on the changes).

When specified, ONLY the listed dimensions run — this is useful for projects that want to focus on specific areas.

### Skip

Glob patterns for files to exclude from review. Common uses:

- Generated code that shouldn't be manually modified
- Vendored dependencies
- Build output
- Migration files that are auto-generated
- Large data files

**Never skip test files.** This is the most common skip pattern mistake. Tests need review for coverage gaps, incorrect assertions, and missing edge cases — but with different emphasis than production code. If test files generate too much noise, add focused rules for test directories rather than skipping them entirely. Overly broad patterns like `**/test*/**` are especially dangerous because they can match production code in directories like `testing-utils/`.

### Rules

Custom natural-language rules that all review agents should check in addition to their built-in logic. These are especially useful for project-specific conventions that aren't captured in CLAUDE.md.

Rules should be:

- Specific and actionable (not vague guidelines)
- Objectively verifiable (an agent can determine compliance)
- Focused on the code being reviewed (not process/workflow rules)

### Severity Threshold

The minimum severity level to include in the report:

- `critical` — Only blocking issues
- `high` — Critical + high priority
- `medium` — Critical + high + medium (default for most projects)
- `low` — Everything (default)

### Confidence Threshold

An integer from 0-100. Findings below this confidence score are filtered out before the report. When you do not set it, non-security dimensions default to **55** and security to **70**. Higher values (e.g., 85) are stricter but may miss some real issues. Lower values surface more findings but may include more false positives.

**Important:** The default security bar (70) is stricter than the default non-security bar (55) — security false negatives are costlier, but a low-confidence security guess is still noise. Setting an explicit `confidence_threshold` applies it to all non-security dimensions; the security bar is then `min(confidence_threshold, security_min_confidence)`. So `confidence_threshold: 90` raises the non-security bar to 90, but if `security_min_confidence` is set lower (e.g., 60), security findings are still included at that level.

**Per-dimension thresholds:** You can override the threshold for individual dimensions using a YAML-like key:value format. This is useful when some dimensions (e.g., conventions) generate more noise than others (e.g., bugs).

```
## Confidence Threshold
bugs: 75
security: 70
cross-file-impact: 80
conventions: 85
tests: 80
types: 80
simplification: 80
```

Rules for per-dimension thresholds:

- Dimension names must match Focus section values: `bugs`, `security`, `cross-file-impact`, `tests`, `conventions`, `types`, `simplification`
- If a plain number is provided (current format), it applies as the default for all non-security dimensions
- If per-dimension values are provided, they override the plain number default for that dimension
- Dimensions not listed use the plain number default (or 70 if no default is set)
- If `security_min_confidence` is set in REVIEW.md, it provides a lower floor for security findings — useful for repos that want to surface more borderline security issues
- Per-dimension settings in subdirectory REVIEW.md files override the inherited value for that dimension only

### Model Tier

Controls which LLM models are used for review agents. Two modes are available:

- `optimized` (default) — Sonnet for most agents, Opus only for security-reviewer. Faster and ~40% cheaper. Research shows the SWE-bench Verified gap between Opus and Sonnet is just 1.2 percentage points.
- `frontier` — All agents use Opus. Maximum depth for high-stakes reviews.

When set in REVIEW.md, the mode selection prompt is skipped during Phase 1. When not set, the user is prompted at the start of each review.

### Default Delivery

Controls how review results are delivered. A comma-separated list of delivery methods:

- `chat` — Display the full report in the conversation
- `pr_comments` — Post findings as inline PR/MR comments
- `markdown` — Save as `code-gauntlet-{date}.md`

When set in REVIEW.md, the delivery preference prompt is skipped during Phase 1. When not set, the user is prompted at the start of each review. Task creation is always offered separately after delivery, regardless of this setting.

```
## Default Delivery
chat,pr_comments
```

### Ignore

Patterns for suppressing known false positives. Format is `dimension:"pattern"` where:

- `dimension` is one of the review dimension names (or `*` for all)
- `pattern` is a substring to match against finding titles/descriptions

This is useful when a project has intentional patterns that agents consistently flag incorrectly.

**Date-stamp ignore patterns** for long-term maintenance. Add a comment with the date and reason above each pattern so quarterly audits can identify stale suppressions:

```
## Ignore
# 2026-03-25: EF Core migrations are generated, naming conventions don't apply
conventions:"file naming" for migration files
# 2026-03-25: Test helpers intentionally use nullable without guards
types:"nullable reference" for test assertion helpers
```

**Soft cap: 10-15 ignore patterns per file.** If you exceed this, it signals either rules that are too sensitive (remove or rewrite them) or a systematic mismatch between your rules and your codebase. Proliferating ignore patterns erodes trust in the review system — when engineers start ignoring entire categories of findings, the tool becomes actively harmful.

## Hierarchy

REVIEW.md files mirror CLAUDE.md locations. A repository can have:

- A **root** `REVIEW.md` at the repo root (applies to all files by default)
- **Subdirectory** `REVIEW.md` files in any directory that also has a `CLAUDE.md` (applies to files in that directory tree)

Subdirectory REVIEW.md files are optional — they're only needed when different parts of the codebase need different review standards (e.g., stricter security rules for an API directory, different thresholds for a legacy module).

**Placement decision test:** before adding a rule to a subdirectory REVIEW.md, ask "would this rule generate false positives in the other stack?" If yes, it belongs in the subdirectory. If the rule applies cleanly everywhere, it belongs in root. Example: "Never use `async void`" is meaningless in a React frontend — it goes in `backend/REVIEW.md`. "Validate all user input" applies everywhere — it goes in root.

### Inheritance model

When a subdirectory has its own REVIEW.md, its settings combine with the root as follows:

| Section | Behavior | Rationale |
|---------|----------|-----------|
| `confidence_threshold` | **Override** — subdirectory value replaces root | A module may need stricter or looser thresholds |
| `severity_threshold` | **Override** — subdirectory value replaces root | Some areas warrant reporting lower-severity issues |
| `model_tier` | **Override** — subdirectory value replaces root | A security-critical directory might always use frontier |
| `default_delivery` | **Override** — subdirectory value replaces root | Unlikely to vary by directory, but supported for consistency |
| `rules` | **Accumulate** — subdirectory rules add to root rules | Directory-specific conventions supplement project-wide ones |
| `ignore` | **Accumulate** — subdirectory patterns add to root patterns | Suppressions are additive |
| `focus` | **Override** — subdirectory value replaces root | A directory may need only specific dimensions |
| `skip` | **Accumulate** — subdirectory patterns add to root patterns | Skip patterns are additive |

In short: **settings override, rules and patterns accumulate.**

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

REVIEW.md files are discovered lazily, following the same pattern as CLAUDE.md — loaded on demand for directories containing changed files. Code-gauntlet checks each CLAUDE.md location for a matching REVIEW.md during Phase 2c context gathering.

#### Detection flow (Phase 2c)

Find all CLAUDE.md locations, check each for a matching REVIEW.md:

> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): skip both REVIEW.md-setup `AskUserQuestion` prompts below — root config applies, `build-review-md` is never invoked, and REVIEW.md is read-only. The hierarchical parse still runs; no REVIEW.md is created. See `references/headless-mode.md`.

- **No REVIEW.md anywhere:**

  ```
  AskUserQuestion(
    questions: [{
      question: "No REVIEW.md found. REVIEW.md lets you customize review behavior — confidence thresholds, ignore patterns, project-specific rules. Would you like to create one?",
      header: "REVIEW.md Setup",
      multiSelect: false,
      options: [
        { label: "Yes — create at repo root", description: "Scaffold a REVIEW.md with sensible defaults" },
        { label: "Not now — continue without it", description: "Use default settings for this review" }
      ]
    }]
  )
  ```

  If yes, use the scaffolding template from the Templates section below.
- **Root exists, subdirectory CLAUDE.md without matching REVIEW.md:**

  ```
  AskUserQuestion(
    questions: [{
      question: "Found REVIEW.md at repo root, but {directory} has a CLAUDE.md without a matching REVIEW.md. A subdirectory REVIEW.md lets you set different review standards for this area. Create one?",
      header: "Subdirectory REVIEW.md",
      multiSelect: false,
      options: [
        { label: "Yes — create it", description: "Inherits root settings, adds directory-specific rules" },
        { label: "Not now — root config applies", description: "Use root REVIEW.md settings for all directories" }
      ]
    }]
  )
  ```

- **All locations covered** → proceed

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

When the user opts to create a REVIEW.md during Phase 2c, use these templates. The templates set sensible defaults and provide structural guidance without guessing at repo-specific content.

### Root REVIEW.md template

```markdown
# Review Configuration

<!-- Customizes how code-gauntlet analyzes this repository.
     See references/review-md-spec.md in the code-gauntlet skill for all options. -->

## Confidence Threshold

70

<!-- Minimum confidence (0-100) to include findings. Default: 70.
     Security findings always use a minimum of 60 regardless of this setting.
     Start at 70-80 and adjust based on false-positive rates.

     To set per-dimension thresholds, replace the plain number with key:value pairs:
     bugs: 75
     security: 70
     cross-file-impact: 75
     conventions: 80
     tests: 75
     types: 75
     simplification: 75
     Omitted dimensions use the plain number default (or 70 if no default is set).
     Security cannot be set below 60. -->

## Severity Threshold

<!-- Minimum severity to include in the report. Default: low (show everything).
     Options: critical, high, medium, low
     Uncomment and set to filter out lower-severity findings.
     Useful for high-debt codebases where low/medium noise drowns out critical issues. -->
<!-- medium -->

## Default Delivery

<!-- How to deliver review results. Comma-separated list.
     Options: chat, pr_comments, markdown
     When set, skips the delivery preference prompt.
     Task creation is always offered separately after delivery.
     Uncomment and adjust to your preference. -->
<!-- chat,pr_comments -->

## Skip

<!-- Files where AI review adds no value. Uncomment patterns that apply. -->
<!-- **/dist/** -->
<!-- **/build/** -->
<!-- **/node_modules/** -->
<!-- **/*.generated.* -->
<!-- **/vendor/** -->
<!-- package-lock.json -->
<!-- yarn.lock -->
<!-- pnpm-lock.yaml -->

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

## Ignore

<!-- Suppress known false positives. Date-stamp for audit trail.
     Format: dimension:"pattern" (reason, date)

     Example:
     - security:"hardcoded string" in test fixtures (test data not secrets, 2026-01-15)
     - conventions:"file naming" for migration files (generated, 2026-01-15)
-->
```

### Subdirectory REVIEW.md template

```markdown
# Review Configuration — [directory name]

<!-- Settings here override root REVIEW.md. Rules and ignore patterns
     accumulate (add to root), settings (thresholds, model tier) replace root.
     Only create subdirectory configs when this area needs DIFFERENT standards
     than the root — e.g., stricter security for an API directory. -->

## Rules

<!-- Directory-specific rules (these ADD to root REVIEW.md rules).
     Aim for 5-10 rules covering technology or domain-specific patterns.
     Don't contradict root rules — extend them. -->

## Ignore

<!-- Directory-specific suppressions (these ADD to root ignores). -->
```
