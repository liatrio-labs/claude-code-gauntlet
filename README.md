# claude-deep-review

A research-backed code review plugin for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that orchestrates parallel concern-specialized agents, cross-validates findings with deterministic verification, and produces unified review reports.

## What it does

Deep Review dispatches up to seven specialized agents in parallel, each examining your code changes through a different lens:

| Agent | Optimized | Frontier | Focus |
|-------|-----------|----------|-------|
| **bug-detector** | Sonnet | Opus | Logic errors, edge cases, error handling, resource leaks |
| **security-reviewer** | **Opus** | **Opus** | OWASP top 10, injection, auth, SSRF, deserialization |
| **cross-file-impact** | Sonnet | Opus | How changes affect callers and dependents across the codebase |
| **test-analyzer** | Sonnet | Opus | Test coverage gaps, test quality, missing edge cases |
| **conventions-and-intent** | Sonnet | Opus | CLAUDE.md compliance, spec alignment, comment accuracy |
| **type-design-analyzer** | Sonnet | Opus | Type encapsulation and invariant design (conditional) |
| **code-simplifier** | Sonnet | Opus | Simplification opportunities |

Two review modes are available. **Optimized** (default) uses Sonnet for most agents and Opus for security, balancing depth with cost. **Frontier** uses Opus for every agent. Security always runs on Opus in both modes because different models have complementary vulnerability-class detection profiles.

After agents report findings, a six-stage deterministic pipeline filters and reconciles them:

1. **`merge_findings.py`** — collects findings from agent NDJSON files, deduplicates, validates schema
2. **`verify_findings.py`** — git-blame classification (new vs surfaced), factual verification against the actual code, diff-line validation
3. **`apply_validations.py`** — applies independent validator confidence assessments
4. **`filter_findings.py`** — confidence/severity thresholds (default 70, security 60), injection filtering, cross-agent dedup, consensus detection, routing (main report vs improvement suggestions)
5. **`apply_challenges.py`** — applies blind-challenge results, severity downgrades, surfaced re-routing, final dedup, ranking
6. **`post_review.py`** — delivers findings as PR/MR comments, markdown, or chat

Between stages, findings live on disk as JSON. There is no LLM JSON reconstruction in the pipeline.

## Key features

- **Concern-parallel, not file-parallel** — each agent sees the full diff with cross-file context, because bugs at module boundaries are invisible to file-scoped reviewers.
- **Full-codebase investigation** — security and cross-file-impact agents trace data flows beyond the diff into the entire repository.
- **New vs. surfaced findings** — git blame classifies whether each finding is new code you wrote or pre-existing code exposed by your changes. Surfaced findings are downgraded and grouped separately so they don't drown out real issues.
- **Incremental review** — when re-reviewing a PR with new commits, the skill offers to review only the changes since the last review instead of starting over.
- **Prompt-injection defense** — code under review is treated as untrusted input, wrapped in trust-boundary delimiters with output scanning for injection artifacts.
- **Context-pulling** — agents actively investigate using Read/Grep/LSP rather than receiving a passive context dump.
- **GitHub and GitLab** — auto-detects the platform from the git remote, supports both `gh` and `glab` CLIs.
- **REVIEW.md configuration** — hierarchical config that mirrors CLAUDE.md locations. The skill scaffolds REVIEW.md files for you and maintains the ignore list as you dismiss findings.
- **Flexible delivery** — PR/MR comments, markdown file, chat, task board, or any combination.
- **Task-board integration** — create tasks from findings with user-controlled selection (`all`, `1,3,5`, `all critical and high`, `all except 6`).
- **Graceful degradation** — if an agent fails, the review continues with the remaining agents and notes the gap.

## Installation

Add the marketplace and install the plugin:

```bash
claude plugin marketplace add https://github.com/liatrio-labs/claude-deep-review.git
claude plugin install deep-review@deep-review
```

To update later:

```bash
claude plugin update deep-review@deep-review
```

## Usage

The skill triggers automatically when you ask for a code review in Claude Code:

```
# Review a PR (GitHub)
deep review PR #42

# Review a merge request (GitLab)
review MR !89 thoroughly

# Review local uncommitted changes
comprehensive review of my changes

# Focused review
deep review PR #42, focus only on security and error handling
```

Or invoke it directly:

```
/deep-review
```

## How it works

The review runs through eight phases:

1. **Pre-flight** — eligibility checks (not closed, draft, or already reviewed), configuration gate (review mode, delivery preference), plugin root and session SHA resolution.
2. **Target & triage** — detects GitHub/GitLab, fetches the diff, classifies files by risk, detects AI-generated code, discovers tests, gathers CLAUDE.md and REVIEW.md context, produces a change summary.
3. **Review agents** — launches the agents in parallel. They investigate via Read/Grep/Glob/LSP and write findings to NDJSON files on disk.
4. **Classify and verify** — `merge_findings.py` collects agent output, then `verify_findings.py` runs git-blame classification, factual verification, and diff-line validation.
5. **Validate** — independent Sonnet validators assess each finding's confidence with codebase context. `apply_validations.py` applies the adjustments.
6. **Filter and reconcile** — `filter_findings.py` applies thresholds, injection filtering, cross-agent dedup, consensus detection, and routes findings to the main report or to improvement suggestions.
7. **Blind challenge** — fresh agents attempt to disprove each finding without seeing the original reasoning. `apply_challenges.py` applies challenge results, severity downgrades, surfaced re-routing, final dedup, and ranking.
8. **Report and deliver** — generates the report and delivers it via PR/MR comments, markdown, chat, or task board. `post_review.py` handles comment posting.

## REVIEW.md configuration

Deep Review will offer to scaffold a `REVIEW.md` when it doesn't find one. The file mirrors your CLAUDE.md locations — if you have subdirectory CLAUDE.md files, the skill offers to create matching REVIEW.md files at the same levels.

A **root REVIEW.md** sets global defaults. **Subdirectory REVIEW.md** files can set different standards per area (for example, stricter security for `src/auth/`). Settings (thresholds) override from child to parent; rules and ignore patterns accumulate.

```markdown
## Rules
- All database queries must use parameterized statements
- Public API endpoints must validate request body schema

## Ignore
# Suppress known findings. Deep Review suggests additions when you dismiss findings.
- security:"prompt injection via template tokens" (not exploitable in current architecture, 2026-03-24)

## Skip
- "vendor/**"
- "**/*.generated.cs"

## Confidence Threshold
# Default: 70. Security uses minimum 60 regardless of this setting.
70
```

See [skills/deep-review/references/review-md-spec.md](skills/deep-review/references/review-md-spec.md) for the full specification including hierarchy rules.

## Why this design

The architecture choices behind Deep Review — concern decomposition, deterministic verification, blind-challenge rounds, context-pulling, hierarchical config, and the rest — are documented in [`docs/research/`](docs/research/README.md). Each design decision is paired with the research artifact that informed it, so the rationale travels with the code.

## Project structure

```
claude-deep-review/
├── .claude-plugin/
│   ├── plugin.json                       # Plugin manifest
│   └── marketplace.json                  # Marketplace manifest
├── agents/                               # 10 named subagent definitions
│   ├── bug-detector.md                   #   7 discovery agents (Read/Grep/Glob/LSP/Bash)
│   ├── security-reviewer.md
│   ├── cross-file-impact.md
│   ├── test-analyzer.md
│   ├── conventions-and-intent.md
│   ├── type-design-analyzer.md
│   ├── code-simplifier.md
│   ├── validator.md                      #   3 quality-gate agents (Read/Grep/Glob/LSP)
│   ├── challenger.md
│   └── change-summarizer.md
├── scripts/                              # stdlib-only Python pipeline
│   ├── merge_findings.py                 #   Phase 3→4: collect + dedupe agent findings
│   ├── verify_findings.py                #   Phase 4: blame classification, factual verification
│   ├── apply_validations.py              #   Phase 5→6: apply validator confidence adjustments
│   ├── filter_findings.py                #   Phase 6: thresholds, dedup, routing
│   ├── apply_challenges.py               #   Phase 7→8: challenge results, dedup, rank
│   ├── post_review.py                    #   Phase 8: PR/MR comment posting
│   └── validate_ndjson.py                #   Agent-side NDJSON self-validation
├── tests/                                # pytest suite for all pipeline scripts
├── skills/
│   ├── deep-review/
│   │   ├── SKILL.md                      # Main orchestration (8 phases)
│   │   └── references/                   # Phase-specific reference files
│   └── build-review-md/                  # Companion skill: REVIEW.md configuration wizard
├── docs/
│   └── research/                         # Research artifacts informing the design
├── LICENSE
└── README.md
```

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- `gh` CLI (for GitHub) or `glab` CLI (for GitLab)
- Git
- Python 3 (for the deterministic pipeline scripts; standard library only — no external dependencies)

## Recommended settings

For best results, increase the output token budget so all review agents can be dispatched in a single response:

```bash
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000
```

The default (8,000 tokens) is too small for dispatching seven parallel agents. With the file-based context handoff, each agent prompt is roughly 100 tokens, but the orchestrator's triage text and reasoning can consume significant budget. 64,000 tokens provides ample room.

## Development

Run the test suite from the repo root:

```bash
python -m pytest tests/ -q
```

All scripts are pure standard-library Python and language-agnostic — they make no assumptions about the language of the codebase being reviewed.

## License

See [LICENSE](LICENSE).
