# REVIEW.md Specification

A `REVIEW.md` file lets project maintainers customize how deep-review behaves. It can live at the repository root and in subdirectories alongside CLAUDE.md files. It's optional — sensible defaults apply when absent.

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
<!-- Minimum confidence score (0-100) to include in the report. Default: 80 -->
75

## Max Findings
<!-- Maximum number of findings to include in the report. Default: no limit -->
<!-- When the cap is hit, highest-severity findings are kept and a note indicates how many were suppressed. -->
15

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
- `cross-file-impact` — Cross-file impact analysis (cross-file-impact-analyzer agent)
- `tests` — Test coverage gap analysis (test-analyzer agent)
- `conventions` — Convention compliance, intent alignment, and comment accuracy (conventions-and-intent agent)
- `types` — Type design analysis (type-design-analyzer agent)
- `simplification` — Code simplification (code-simplifier agent, runs post-review)

When omitted, all applicable dimensions run (the skill auto-detects which are relevant based on the changes).

When specified, ONLY the listed dimensions run — this is useful for projects that want to focus on specific areas.

### Skip

Glob patterns for files to exclude from review. Common uses:
- Generated code that shouldn't be manually modified
- Vendored dependencies
- Build output
- Migration files that are auto-generated
- Large data files

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

An integer from 0-100. Findings below this confidence score are filtered out before the report. Default is 80. Lower values (e.g., 70) surface more findings but may include more false positives. Higher values (e.g., 90) are stricter but may miss some real issues.

**Important:** The `confidence_threshold` field sets the default for non-security dimensions. Security findings always use a minimum threshold of 70, regardless of this setting. Setting `confidence_threshold: 90` would raise the bar for bugs, tests, conventions, and other dimensions to 90, but security findings would still be included at confidence 70+. This is by design — security false negatives are costlier than false positives.

### Max Findings

Maximum number of findings to include in the report. When the cap is hit, the highest-severity findings are kept and a note indicates how many were suppressed.

Default: no limit — all findings that survive the validation pipeline appear in the report. Set this in high-debt codebases to prevent review noise. Use `0` to explicitly mean "no limit" (same as omitting the setting).

Note: this controls findings in the **report**. PR inline comments have a separate cap of 8 (applied in Phase 6 delivery) to prevent notification fatigue — remaining findings appear in the summary comment.

```
## Max Findings
15
```

Suppressed findings are noted at the end of the report: "{N} additional findings were suppressed by the max_findings cap ({cap}). Set `max_findings: 0` or remove the setting to see all findings."

### Model Tier

Controls which LLM models are used for review agents. Two modes are available:

- `optimized` (default) — Sonnet for most agents, Opus only for security-reviewer. Faster and ~40% cheaper. Research shows the SWE-bench Verified gap between Opus and Sonnet is just 1.2 percentage points.
- `frontier` — Opus for all reasoning-heavy agents (bugs, security, cross-file, simplification). Maximum depth for high-stakes reviews.

When set in REVIEW.md, the mode selection prompt is skipped during Phase 0. When not set, the user is prompted at the start of each review.

### Default Delivery

Controls how review results are delivered. A comma-separated list of delivery methods:

- `chat` — Display the full report in the conversation
- `pr_comments` — Post findings as inline PR/MR comments
- `markdown` — Save as `deep-review-{date}.md`

When set in REVIEW.md, the delivery preference prompt is skipped during Phase 0. When not set, the user is prompted at the start of each review. Task creation is always offered separately after delivery, regardless of this setting.

```
## Default Delivery
chat,pr_comments
```

### Ignore

Patterns for suppressing known false positives. Format is `dimension:"pattern"` where:
- `dimension` is one of the review dimension names (or `*` for all)
- `pattern` is a substring to match against finding titles/descriptions

This is useful when a project has intentional patterns that agents consistently flag incorrectly.

## Hierarchy

REVIEW.md files mirror CLAUDE.md locations. A repository can have:

- A **root** `REVIEW.md` at the repo root (applies to all files by default)
- **Subdirectory** `REVIEW.md` files in any directory that also has a `CLAUDE.md` (applies to files in that directory tree)

Subdirectory REVIEW.md files are optional — they're only needed when different parts of the codebase need different review standards (e.g., stricter security rules for an API directory, different thresholds for a legacy module).

### Inheritance model

When a subdirectory has its own REVIEW.md, its settings combine with the root as follows:

| Section | Behavior | Rationale |
|---------|----------|-----------|
| `confidence_threshold` | **Override** — subdirectory value replaces root | A module may need stricter or looser thresholds |
| `severity_threshold` | **Override** — subdirectory value replaces root | Some areas warrant reporting lower-severity issues |
| `max_findings` | **Override** — subdirectory value replaces root | High-debt areas may need a cap |
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
  REVIEW.md              # confidence_threshold: 80, rules: [rule-A, rule-B]
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
- confidence_threshold = **80** (root applies)
- rules = **[rule-A, rule-B]** (root only)

### Discovery

REVIEW.md files are discovered lazily, following the same pattern as CLAUDE.md — loaded on demand for directories containing changed files. Deep-review checks each CLAUDE.md location for a matching REVIEW.md during Phase 2a context gathering.

#### Detection flow (Phase 2a)

Find all CLAUDE.md locations, check each for a matching REVIEW.md:

- **No REVIEW.md anywhere:**
  ```
  AskUserQuestion(
    question: "No REVIEW.md found. REVIEW.md lets you customize review behavior — confidence thresholds, ignore patterns, project-specific rules. Would you like to create one?",
    options: [
      "Yes — create at repo root",
      "Not now — continue without it"
    ]
  )
  ```
  If yes, use the scaffolding template from the Templates section below.
- **Root exists, subdirectory CLAUDE.md without matching REVIEW.md:**
  ```
  AskUserQuestion(
    question: "Found REVIEW.md at repo root, but {directory} has a CLAUDE.md without a matching REVIEW.md. A subdirectory REVIEW.md lets you set different review standards for this area. Create one?",
    options: [
      "Yes — create it (inherits root settings, adds directory-specific rules)",
      "Not now — root config applies to all directories"
    ]
  )
  ```
- **All locations covered** → proceed

---

## Rule-Writing Principles

When helping users add rules to REVIEW.md (during scaffolding or when updating), follow these principles drawn from research on AI reviewer effectiveness:

1. **15-25 rules per file.** Beyond this, LLM adherence degrades for ALL rules, not just new ones. The review system's own prompts consume ~50 instruction slots; each rule competes for the remaining capacity.
2. **Prescriptive for security/correctness, directional for design.** "All async methods MUST accept CancellationToken" (binary pass/fail) vs "Prefer immutable types where practical" (allows edge cases). Prescriptive rules produce low false-positive rates; directional rules handle nuance.
3. **Always include rationale.** "Never force push" is a flat instruction. "Never force push — this rewrites shared history and is unrecoverable for collaborators" helps the reviewer generalize to related scenarios (like `git reset --hard` on shared branches).
4. **Specific and verifiable.** Each rule should have a binary pass/fail condition. "Write clean code" is unverifiable. "All public API endpoints must validate request body schema before processing" is verifiable.
5. **Never duplicate linters.** If ESLint, mypy, tsc, clippy, or any deterministic tool catches it, don't make it a review rule. Deterministic tools are faster, cheaper, and more reliable for objective checks.
6. **Place critical rules first.** LLMs exhibit peripheral bias — they attend more strongly to instructions at the beginning and end of the prompt. Put security and correctness rules first.
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

When the user opts to create a REVIEW.md during Phase 2a, use these templates. The templates set sensible defaults and provide structural guidance without guessing at repo-specific content.

### Root REVIEW.md template

```markdown
# Review Configuration

<!-- Customizes how deep-review analyzes this repository.
     See references/review-md-spec.md in the deep-review skill for all options. -->

## Confidence Threshold

80

<!-- Minimum confidence (0-100) to include findings. Default: 80.
     Security findings always use a minimum of 70 regardless of this setting.
     Start at 80-85 and lower based on false-positive rates. -->

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
