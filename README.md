# claude-deep-review

A comprehensive, research-backed code review skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that orchestrates parallel concern-specialized agents, cross-validates findings with deterministic verification, and produces unified review reports.

## What it does

Deep Review dispatches 6-7 specialized agents in parallel, each examining your code changes through a different lens:

| Agent | Optimized | Frontier | Focus |
|-------|-----------|----------|-------|
| **bug-detector** | Sonnet | Opus | Logic errors, edge cases, error handling, resource leaks |
| **security-reviewer** | **Opus** | **Opus** | OWASP top 10, injection, auth, SSRF, deserialization |
| **cross-file-impact** | Sonnet | Opus | How changes affect callers and dependents across the codebase |
| **test-analyzer** | Sonnet | Opus | Test coverage gaps, test quality, missing edge cases |
| **conventions-and-intent** | Sonnet | Opus | CLAUDE.md compliance, spec alignment, comment accuracy |
| **type-design-analyzer** | Sonnet | Opus | Type encapsulation and invariant design (conditional) |
| **code-simplifier** | Sonnet | Opus | Simplification opportunities |

Two review modes are available. **Optimized** (default) is ~40% cheaper and faster — Sonnet for most agents, Opus only for security. **Frontier** uses Opus for all agents. Security always gets Opus in both modes because different models have complementary vulnerability-class detection profiles.

Agents write findings to NDJSON files in a repo-local `.deep-review/` directory via Bash append — a structurally separate channel from investigation, enforced by a PreToolUse hook that blocks non-emission Bash commands. The output directory is overridable via `$DEEP_REVIEW_OUTPUT_DIR` for CI environments.

After agents report findings, a six-script deterministic pipeline filters false positives:

1. **`merge_findings.py`** — collects findings from agent NDJSON files and text returns, deduplicates, validates schema
2. **`verify_findings.py`** — git blame classification (new vs surfaced), factual verification against actual code, diff-line validation
3. **`apply_validations.py`** — applies independent validator confidence assessments
4. **`filter_findings.py`** — confidence/severity thresholds (default 70, security 60), injection filtering, cross-agent dedup, consensus detection, routing (main report vs improvement suggestions)
5. **`apply_challenges.py`** — applies blind challenge results, severity downgrades, surfaced re-routing, final dedup, ranking
6. **`post_review.py`** — delivers findings as PR/MR comments, markdown, or chat

Between scripts, findings live on disk as JSON — zero LLM JSON reconstruction in the pipeline.

## Key features

- **Concern-parallel, not file-parallel** — each agent sees the full diff with cross-file context, because bugs at module boundaries are invisible to file-scoped reviewers
- **Full-codebase investigation** — security and cross-file-impact agents trace data flows beyond the diff into the entire repository (matching [Anthropic's own approach](https://docs.anthropic.com/en/docs/claude-code/code-review))
- **New vs surfaced findings** — git blame classifies whether each finding is new code you wrote or pre-existing code exposed by your changes. Surfaced findings are downgraded and grouped separately so they don't drown out real issues
- **Incremental review** — when re-reviewing a PR with new commits, offers to review only the changes since the last review instead of starting from scratch
- **Prompt injection defense** — code under review is untrusted input, wrapped in trust boundary delimiters with output scanning for injection artifacts
- **Context-pulling** — agents actively investigate using Read/Grep/LSP rather than receiving a passive context dump (51% fewer false positives per research)
- **GitHub + GitLab** — auto-detects platform from git remote, supports both `gh` and `glab` CLIs
- **REVIEW.md configuration** — hierarchical config mirroring CLAUDE.md locations. The skill scaffolds REVIEW.md files for you and maintains the ignore list as you dismiss findings
- **Flexible delivery** — PR/MR comments, markdown file, chat, task board, or any combination
- **Task board integration** — create tasks from findings with user-controlled selection ("all", "1,3,5", "all critical and high", "all except 6")
- **Graceful degradation** — if an agent fails, the review continues with remaining agents and notes the gap

## Installation

Add the marketplace and install the plugin:

```bash
claude plugin marketplace add https://github.com/liatrio-labs/claude-deep-review.git
claude plugin install claude-deep-review@claude-deep-review
```

To update later:

```bash
claude plugin update claude-deep-review@claude-deep-review
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

The review runs through 8 phases:

1. **Pre-flight** — eligibility checks (not closed, draft, or already reviewed), configuration gate (review mode, delivery preference), plugin root and session SHA resolution
2. **Target & Triage** — detects GitHub/GitLab, fetches diff, classifies files by risk, detects AI-generated code, discovers tests, gathers CLAUDE.md + REVIEW.md context, produces a change summary
3. **Review Agents** — launches 6-7 agents in parallel. Agents investigate via Read/Grep/Glob/LSP and write findings to NDJSON files on disk via Bash append
4. **Classify & Verify** — `merge_findings.py` collects agent output, then `verify_findings.py` runs git blame classification, factual verification, and diff-line validation
5. **Validate** — independent Sonnet validators assess each finding's confidence with codebase context. `apply_validations.py` applies their adjustments
6. **Filter & Reconcile** — `filter_findings.py` applies thresholds, injection filtering, cross-agent dedup, consensus detection, and routes findings to main report vs improvement suggestions
7. **Blind Challenge** — fresh agents attempt to disprove each finding without seeing original reasoning. `apply_challenges.py` applies challenge results, severity downgrades, surfaced re-routing, final dedup, and ranking
8. **Report & Deliver** — generates report, delivers via PR/MR comments, markdown, chat, or task board. `post_review.py` handles comment posting

## REVIEW.md configuration

Deep Review will offer to scaffold a `REVIEW.md` when it doesn't find one. The file mirrors your CLAUDE.md locations — if you have subdirectory CLAUDE.md files, the skill offers to create matching REVIEW.md files at the same levels.

**Root REVIEW.md** sets global defaults. **Subdirectory REVIEW.md** files can set different standards per area (e.g., stricter security for `src/auth/`). Settings (thresholds) override from child to parent; rules and ignore patterns accumulate.

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

See [references/review-md-spec.md](skills/deep-review/references/review-md-spec.md) for the full specification including hierarchy documentation.

## Research backing

Every architectural decision is grounded in published research:

| Decision | Research basis |
|----------|---------------|
| 6-7 agents | Quality plateaus at 4-6 agents (DeepMind 2025); unstructured parallel systems amplify errors 17.2x |
| Concern decomposition | Anthropic, Ellipsis, Qodo all use concern-parallel, not file-parallel |
| Deterministic verification | LLM-on-LLM verification shares correlated errors ~60% of the time; deterministic grounding is essential |
| Context-pulling | cubic achieved 51% fewer false positives switching from push to pull |
| Security threshold 60 | Anthropic's own judge filtered 7/8 security findings at threshold 80, including real ones |
| Overconfidence calibration | Xiong et al. (ICLR 2024): LLMs cluster confidence in 80-100 range |
| Full-codebase security | Cat Wu (Anthropic): "agents often take the entire codebase into account" |
| Prompt injection defense | Every major AI review tool has been exploited; 5-layer defense recommended |
| LSP-first navigation | 900x faster than grep (50ms vs 30-60s); eliminates false matches |
| AI-code risk elevation | CodeRabbit: 75% more logic errors in AI-authored code |
| 500-line large-PR threshold | Review effectiveness drops sharply above 400 lines |
| Git blame classification | SonarQube's baseline matching is the gold standard; blame-based new/surfaced is the best approach for AI-native tools |
| Deterministic pipeline scripts | LLM-on-LLM verification shares correlated errors ~60%; mandatory steps are workflows not agent decisions (artifact #17) |
| Dual-channel emission (Bash append) | Format compliance degrades beyond 4K output tokens (LongGenBench); production frameworks universally separate investigation from structuring (artifact #24) |
| PreToolUse hook enforcement | Structural enforcement > instruction-level guidance; Claude Code hooks provide hard behavioral constraints on subagents |
| Suggest-not-modify config | Google SRE Workbook: tools should suggest config changes through reviewable mechanisms, not modify automatically |
| REVIEW.md hierarchy | Nearest-only with explicit inheritance (Ruff, Biome v2) after ESLint's painful cascading lessons |
| No arbitrary cap | Pipeline precision means all surviving findings are real — caps mask pipeline quality issues |

## Project structure

```
claude-deep-review/
├── .claude-plugin/
│   └── plugin.json                       # Plugin manifest (hooks reference)
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
├── hooks/
│   └── hooks.json                        # PreToolUse hook — restricts subagent Bash to finding emission
├── scripts/                              # 7 stdlib-only Python scripts (deterministic pipeline)
│   ├── merge_findings.py                 #   Phase 3→4: collect + deduplicate agent findings
│   ├── verify_findings.py                #   Phase 4: blame classification, factual verification
│   ├── apply_validations.py              #   Phase 5→6: apply validator confidence adjustments
│   ├── filter_findings.py                #   Phase 6: thresholds, dedup, routing
│   ├── apply_challenges.py               #   Phase 7→8: challenge results, dedup, rank
│   ├── post_review.py                    #   Phase 8: PR/MR comment posting
│   └── validate_bash_subagent.py         #   Hook: Bash command pattern validation
├── tests/                                # 483 pytest tests
├── skills/
│   ├── deep-review/
│   │   ├── SKILL.md                      # Main orchestration (8 phases)
│   │   └── references/                   # 11 phase-specific reference files
│   └── build-review-md/                  # Companion skill: REVIEW.md configuration wizard
├── docs/
│   └── research/                         # 24 research artifacts informing design
└── README.md
```

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
- `gh` CLI (for GitHub) or `glab` CLI (for GitLab)
- Git

## Recommended settings

For optimal performance, increase the output token budget so all review agents can be dispatched in a single response:

```bash
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000
```

The default (8,000 tokens) is too small for dispatching 7 parallel agents. With the file-based context handoff, each agent prompt is ~100 tokens, but the orchestrator's triage text and thinking can consume significant budget. 64,000 tokens provides ample room.
