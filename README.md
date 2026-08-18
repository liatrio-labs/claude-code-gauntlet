# Code Gauntlet

[![CI](https://github.com/liatrio-labs/claude-code-gauntlet/actions/workflows/ci.yml/badge.svg)](https://github.com/liatrio-labs/claude-code-gauntlet/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/liatrio-labs/claude-code-gauntlet/badge)](https://securityscorecards.dev/viewer/?uri=github.com/liatrio-labs/claude-code-gauntlet)

Adversarial multi-agent code review for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Your PR runs a gauntlet: parallel concern-specialized reviewers find issues, then every finding must survive deterministic verification, skeptical validation, and a blind challenge before it reaches you. *Formerly published as **deep-review**.*

> **Scorecard note:** Branch-Protection and Code-Review score low by design.
> Required approving reviews stay at 0 because a single maintainer cannot
> self-approve, and an always-bypass Integration actor (Octo STS) lets
> semantic-release push from CI. Raising either into a “fix” deadlocks merges.

## How it reviews

Seven discovery agents examine your changes in parallel, each through a different lens:

| Agent | Model | Focus |
|-------|-------|-------|
| **bug-detector** | Sonnet | Logic errors, edge cases, error handling, resource leaks |
| **security-reviewer** | **Opus** | OWASP top 10, injection, auth, SSRF, deserialization |
| **cross-file-impact** | Sonnet | How changes affect callers and dependents across the codebase |
| **test-analyzer** | Sonnet | Test coverage gaps, test quality, missing edge cases |
| **conventions-and-intent** | Sonnet | CLAUDE.md compliance, spec alignment, comment accuracy |
| **type-design-analyzer** | Sonnet | Type encapsulation and invariant design |
| **code-simplifier** | Sonnet | Simplification opportunities |

Security runs on Opus rather than Sonnet. That is a judgment call, not a measured optimum: the routing survey behind it ([`docs/research/artifacts/12-model-routing-for-code-review.md`](docs/research/artifacts/12-model-routing-for-code-review.md)) concludes that published evidence for tier routing in code review is thin, and finds the frontier-tier case most defensible for deep data-flow security analysis.

Discovery only produces candidates. Each candidate then runs the gauntlet:

1. **Merge** — finding-ID deduplication across channels, schema validation, agent attribution
2. **Verify** — deterministic git-blame classification (new vs. surfaced code) and factual verification against the actual source, trusted only through a nonce + SHA + count receipt
3. **Validate** — an independent validator attempts to disprove each finding with its full content and codebase access, adjusting confidence either way
4. **Filter** — confidence and severity thresholds, prompt-injection filtering, cross-agent consolidation (co-located findings from different agents are grouped for one combined comment rather than dropped), consensus boosts, disagreement suppression, routing to the main report or improvement suggestions
5. **Blind challenge** — a fresh agent attempts to disprove each finding *without the original reasoning or evidence*; claims it cannot verify from the code are removed
6. **Deliver** — deterministic ranking and selection, posted verbatim

A finding that fails a stage is eliminated, downgraded, or routed to a lower tier. Every degradation along the way — a failed agent, a verification that arrived without a valid receipt — is recorded as an explicit gap in the report rather than silently absorbed.

## Benchmark results

Development is gated by a judged benchmark ([`bench/`](bench/README.md)). The golden PRs and their reference findings are not ours: they come from the MIT-licensed [Martian code-review benchmark](https://github.com/withmartian/code-review-benchmark), vendored at pinned upstream commit `dfc6cb4`, so the findings a run is scored against were established upstream, independently of this project. Scoring uses a pinned judge (`claude-opus-4-5-20251101`) with symmetric three-bucket adjudication (golden-matched / valid-extra / noise), blind to which tool produced a comment.

<!-- bench-results:begin — this block is slated to be generated from the run ledger (issue #185); keep hand edits inside it minimal -->
| Release | Run | PRs | Golden recall | Noise rate | Tokens |
|---|---|---|---|---|---|
| v3.0 | gate subset | 15 | 0.695 | 0.180 | 20.9M |
| v3.0 | holdout | 10 | 0.741 | 0.209 | 17.8M |
| v2 (previous architecture) | gate subset | 15 | 0.492 | 0.164 | 41.1M |
| CodeRabbit (anchor) | gate subset | 15 | 0.627 | 0.566 (not comparable&dagger;) | — |
| Claude CLI review (anchor) | gate subset | 15 | 0.339 | 0.481 (not comparable&dagger;) | — |
| claude-code review (anchor) | gate subset | 15 | 0.271 | 0.542 (not comparable&dagger;) | — |

The gate subset and the holdout are separate PR sets; anchors exist only for the gate subset, where every row above is the same 15 PRs under the same judge. The most recent measurement is smaller: a 6-PR paired mini of v3.12 (`mini-20260818-120540-b423885` + single-PR completion leg `custom-20260818-142206-b423885`, 2026-08-18, vs the v3.1 baseline `custom-20260723-102149-381e9ff`) came in at 0.667 recall / 0.106 noise against the baseline's 0.633 / 0.223, under the pre-registered 0.24 noise ceiling — at that size one finding moves recall by 3.3 points, so the recall side reads as a consistency check; the halved noise reflects v3.12's delivery consolidation folding co-located cross-agent findings into single comments.
<!-- bench-results:end -->

Releases since ship behind the always-on deterministic test suites; the heavier measurement tiers are owner-triggered (cadence and method in [`bench/MEASUREMENT.md`](bench/MEASUREMENT.md)).

The judge is an Anthropic model grading a Claude Code plugin, and one anchor (`claude-code review`) is also an Anthropic product; same-vendor self-preference is a known failure mode of LLM-as-judge setups and has not been measured here. The anchors are also not fresh runs of those tools: they are the comments the upstream dataset recorded for them at commit `dfc6cb4`, as those tools shipped then. That dataset records comments from 41 tools; so far three have been adjudicated under this project's pinned judge, and widening the anchor set is planned.

&dagger; Anchor noise rates are not comparable to Code Gauntlet's, and some unknown part of the gap between them is an artifact of how each side was scored. Anchors are adjudicated from stored upstream comments that carry no file/line anchors, so their non-golden comments were judged against the capped PR diff rather than the precise code slice Code Gauntlet's own comments receive (see `adjudicator_context_note` in [`bench/baselines.json`](bench/baselines.json)). That asymmetry inflates anchor noise by an unmeasured amount; recall is the like-for-like column.

Harness details and the run ledger are in [`bench/README.md`](bench/README.md). `bench/report.py` generates the interactive report — per-PR buckets, per-dimension recall, cost, judge drift — and a rendered copy is [published here](https://claude.ai/code/artifact/fbe487de-b09d-4d11-9b8c-c8c8891215ad).

## Installation

```bash
claude plugin marketplace add https://github.com/liatrio-labs/claude-code-gauntlet.git
claude plugin install code-gauntlet@code-gauntlet
```

To update later:

```bash
claude plugin update code-gauntlet@code-gauntlet
```

Requires Claude Code **>= 2.1.154** (the pipeline runs through the dynamic `Workflow` tool), git, and either the `gh` CLI (GitHub) or `glab` CLI (GitLab). The deterministic pipeline scripts use standard-library Python 3 only.

## Usage

The skill triggers automatically when you ask for a code review:

```
# Review a PR (GitHub)
code gauntlet PR #42

# Review a merge request (GitLab)
review MR !89 thoroughly

# Review local uncommitted changes
comprehensive review of my changes

# Focused review
code gauntlet PR #42, focus only on security and error handling
```

Or invoke it directly:

```
/code-gauntlet 42
```

Recommended: run the review from a session set to Sonnet at low effort. The pipeline dispatches its own sub-agents with pinned models for the heavy reasoning, so the orchestrating session only sequences phases and hands off arguments. Raising the orchestrator's effort does not change what the reviewers do — it mostly makes the run slower.

## Review behavior

Every discovery agent sees the full diff with cross-file context rather than a slice of it, because bugs at module boundaries are invisible to file-scoped reviewers, and each agent pulls the context it needs — tracing data flows beyond the diff through Read, Grep, and LSP — instead of working from a passive context dump.

Findings are then triaged by origin as well as by content. Git blame separates issues in code you wrote from pre-existing issues your changes merely exposed; surfaced findings are downgraded and grouped separately so they do not drown out the new ones. Re-reviewing a PR after new commits offers to review only the delta since the last review.

Code under review is untrusted input throughout: trust-boundary delimiters on the way in, and on the way out delivery filters `title` and `description` for injection patterns while [`scripts/post_review.py`](scripts/post_review.py) sanitizes echoed fields and redacts known token prefixes. The platform — GitHub or GitLab — is auto-detected from the git remote, and results can go to PR/MR comments, a markdown file, or a task board, in any combination.

## Configuration: REVIEW.md

Code Gauntlet tells you when it doesn't find a `REVIEW.md` — a non-blocking notice, not an offer — and you can scaffold one any time with `/build-review-md`, mirroring your CLAUDE.md locations: a root file for global defaults, subdirectory files for per-area standards (say, stricter security for `src/auth/`). Thresholds override child-to-parent; rules and ignore patterns accumulate.

````markdown
## Rules
- All database queries must use parameterized statements

```yaml
# code-gauntlet
ignore:
  - prompt injection via template tokens
```
````

Only the fenced `yaml # code-gauntlet` block above is parsed mechanically; `## Rules` and any other prose reach the review agents as advisory context but aren't otherwise interpreted. Confidence thresholds default to 55 for non-security findings and 70 for security findings — set `confidence_threshold` (and optionally `security_min_confidence`) in the config block to override them. You maintain the ignore list by hand — there's no auto-maintenance. Hierarchy rules and the full field reference: [review-md-spec.md](skills/code-gauntlet/references/review-md-spec.md). The companion `/build-review-md` skill walks you through initial setup.

## Architecture

A review runs in eight phases. Phases 1–2 happen in your session; phases 3–8 are the internal stages of **one deterministic program** — the skill makes a single `Workflow` tool invocation and `workflows/pipeline.js` takes it from there:

1. **Pre-flight** — eligibility (closed/merged, draft, trivially-scoped change), configuration resolution
2. **Target & triage** — platform detection, PR checkout, head-SHA resolution, prior-review gate, diff fetch, risk classification, test discovery, CLAUDE.md/REVIEW.md context, shared context file
3. **Summarize & discover** — change summary, then the parallel discovery agents (schema-enforced structured output)
4. **Merge & verify** — gauntlet stages 1–2
5. **Validate** — gauntlet stage 3
6. **Filter** — gauntlet stage 4
7. **Blind challenge** — gauntlet stage 5
8. **Report & deliver** — the workflow renders the report, persists all artifacts, and selects the delivery set deterministically (gauntlet stage 6); the session then posts that selection verbatim via `post_review.py`

Every merge, filter, and ranking decision inside that program is a pure function, not a model reconstructing JSON. The JS transforms are held at parity with their retained Python twins by frozen golden fixtures, and `workflows/pipeline.js` is a generated, dependency-free bundle byte-verified against a fresh build in CI. Each phase persists its own output, so an interrupted run resumes from the last completed phase instead of starting over, and a failed agent nulls out without taking its siblings down.

The rationale behind these choices — concern decomposition, blind challenge, context-pulling, hierarchical config, injection defense, actionability filtering — is documented per-decision in [`docs/research/`](docs/research/README.md).

## Project layout

```
claude-code-gauntlet/
├── .claude-plugin/            # Plugin + marketplace manifests
├── agents/                    # 13 named subagents: 7 discovery, change-summarizer,
│                              #   validator, challenger, executor, report-writer,
│                              #   artifact-writer
├── workflows/                 # Review pipeline: src/ (ESM modules), build.js (bundler),
│                              #   pipeline.js (generated bundle), test/ (node --test)
├── scripts/                   # Stdlib-only Python: verify_findings.py and
│                              #   post_review.py are invoked by the pipeline; the
│                              #   transform scripts are parity twins of the JS stages
├── tests/                     # pytest: scripts, JS/Python parity (frozen fixtures),
│                              #   bundle freshness
├── bench/                     # Benchmark harness: golden PRs, pinned judge, anchors,
│                              #   ledger, report generation
├── skills/
│   ├── code-gauntlet/         # Main orchestration skill + phase references
│   └── build-review-md/       # REVIEW.md configuration wizard
└── docs/research/             # Research artifacts informing the design
```

## Development

```bash
python -m pytest tests/ -q            # pipeline scripts, JS/Python parity, bundle freshness
node --test workflows/test/*.test.js  # workflow pipeline: orchestration contracts, transforms
python -m pytest bench/tests -q       # benchmark harness
```

After editing anything in `workflows/src/`, rebuild the bundle (`tests/test_bundle_fresh.py` enforces that the committed bundle matches a fresh build byte-for-byte):

```bash
node workflows/build.js
```

Node 24 is a development-only dependency — the shipped bundle runs inside Claude Code's workflow runtime. All Python is standard-library only and language-agnostic: nothing assumes the language of the codebase under review.

## License

[Apache 2.0](LICENSE)
