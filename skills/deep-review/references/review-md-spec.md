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

Default: no limit. Set this in high-debt codebases to prevent review noise:

```
## Max Findings
15
```

Suppressed findings are noted at the end of the report: "{N} additional findings were suppressed by the max_findings cap ({cap}). Increase in REVIEW.md or remove to see all findings."

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
