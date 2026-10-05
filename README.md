# Code Gauntlet

[![CI](https://github.com/liatrio-labs/claude-code-gauntlet/actions/workflows/ci.yml/badge.svg)](https://github.com/liatrio-labs/claude-code-gauntlet/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/liatrio-labs/claude-code-gauntlet/badge)](https://securityscorecards.dev/viewer/?uri=github.com/liatrio-labs/claude-code-gauntlet)

Adversarial code review for Claude Code: each GitHub PR or GitLab MR runs a gauntlet of seven specialist agents for bugs, security, tests and cross-file impact, and every finding must survive verification, a skeptical validator, and a blind challenge before it is posted.

<!-- pipeline-diagram -->

## Quick start

```bash
claude plugin marketplace add https://github.com/liatrio-labs/claude-code-gauntlet.git
claude plugin install code-gauntlet@code-gauntlet
```

Update later with `claude plugin update code-gauntlet@code-gauntlet`.

Run `/code-gauntlet 42` in your repository to review PR #42.
The plugin saves a markdown report under `.code-gauntlet/` and asks before posting inline comments.

Plain words work too, because the plugin triggers on code review requests:

```text
code gauntlet PR #42                  # GitHub pull request
review MR !89 thoroughly              # GitLab merge request
comprehensive review of my changes    # local uncommitted changes
```

The plugin detects GitHub or GitLab from the git remote.
You need Claude Code 2.1.154 or newer, git, Python 3.10 or newer, and an authenticated `gh` (GitHub) or `glab` (GitLab).
There is nothing to `pip install`.

The pipeline pins the review agents' models, so your own session's model and effort do not change them.
We recommend Sonnet at low effort for that session.

Full reviews took 16 to 22 minutes per PR in our [benchmark runs](bench/MEASUREMENT.md#ledger-sourced-costs).
The v3.0 measurement used 20.9 million tokens across 15 PRs, about 1.4 million per PR ([results](#benchmark-results)).

## How it reviews

A full review runs seven discovery agents in parallel:

| Agent | Model | Focus |
| --- | --- | --- |
| bug-detector | Sonnet | Logic errors, edge cases, error handling, resource leaks |
| security-reviewer | Opus | Injection, authentication, data flows, vulnerabilities |
| cross-file-impact | Sonnet | Callers and dependencies across the codebase |
| test-analyzer | Sonnet | Missing tests, weak assertions, untested paths |
| conventions-and-intent | Sonnet | Project rules, specs, comment accuracy |
| type-design-analyzer | Sonnet | Type boundaries and invariants |
| code-simplifier | Sonnet | Simplification opportunities |

The security reviewer runs on Opus, a judgment call that the [routing research](docs/research/artifacts/12-model-routing-for-code-review.md) explains.
When every changed file is low risk and the change is under 50 lines, the plugin offers a light review with only `bug-detector` and `security-reviewer`.

Discovery produces candidates, and each one then runs the gauntlet:

1. Merge combines duplicate findings and checks their structure.
2. Verify checks source facts and separates new issues from older ones.
3. Validate asks an independent skeptic to disprove each finding.
4. Filter applies confidence thresholds, rejects prompt injection, and groups related findings.
5. Blind challenge asks a fresh agent to check the claim without the original reasoning or evidence.
6. Deliver ranks the survivors and prepares comments and a report.

A finding the challenger rejects is removed, unless it is a security finding, which is downgraded to a suggestion instead.
Weakly supported findings are downgraded to suggestions too, and a downgrade removes a finding already at the lowest severity; contested findings are kept and marked in the report.
If verification or validation itself breaks, the report records the gap and the affected findings still face the blind challenge.

Agents see the full diff and follow callers, dependencies, and tests across files.
Git blame separates new issues from older ones your changes exposed, which the report downgrades and groups separately.
After new commits, the plugin offers to review only what changed since the last review.

## What it posts

The plugin saves a markdown report locally. For an open PR or MR, it then asks whether to post the selected findings as inline comments in one review.
Here is one such comment, rendered from a [repository fixture](tests/fixtures/parity/apply_challenges/issue47_extra_fields_pass_through/input.json) finding and trimmed:

> **🟠 [HIGH] Missing test for the payment failure rollback path**
>
> processPayment rolls back the transaction when the gateway raises, but no test exercises that path.
>
> **Suggested fix:**
> Mock the gateway to raise PaymentGatewayError and assert the transaction is rolled back.
>
> ⚔️ *Code Gauntlet*

In interactive runs you can also create a task board for the selected findings.
Headless runs support chat, markdown, and PR/MR comments, and posting defaults to dry-run ([headless configuration](skills/code-gauntlet/references/headless-mode.md)).

## Safety and privacy

The plugin treats code under review as untrusted input and filters prompt-injection attempts.
Before posting comments, it escapes raw HTML and @mentions outside trusted code fences and redacts known GitHub and GitLab token formats.
See [security boundaries](SECURITY.md#trust-boundaries).

The plugin runs locally through Claude Code and collects no data.
See [PRIVACY.md](PRIVACY.md).

## Configuration: REVIEW.md

Use `REVIEW.md` to set project rules, confidence thresholds, and findings to ignore. Run `/build-review-md` to create one.
Root settings apply project-wide, while matching subdirectory settings override thresholds and add ignores.

````markdown
## Rules
- All database queries must use parameterized statements

```yaml
# code-gauntlet
confidence_threshold: 75
ignore:
  - prompt injection via template tokens
```
````

Prose guides the agents; the fenced config block controls thresholds and ignores.
No `REVIEW.md` is required. See the [configuration reference](skills/code-gauntlet/references/review-md-spec.md) for defaults and hierarchy.

## Benchmark results

Scores come from the MIT-licensed [Martian benchmark](https://github.com/withmartian/code-review-benchmark), pinned at commit `dfc6cb4`.
A pinned judge, `claude-opus-4-5-20251101`, scores each review without knowing which tool wrote it.
Recall is the share of reference findings the review catches, and noise is the share of reported findings the same pinned model rejects as ungrounded, vague, or incoherent.

<!-- bench-results:begin — this block is slated to be generated from the run ledger (issue #185); keep hand edits inside it minimal -->
| Release | Run | PRs | Golden recall | Noise rate | Tokens |
|---|---|---|---|---|---|
| v3.0 | gate subset | 15 | 0.695 | 0.180 | 20.9M |
| v3.0 | holdout | 10 | 0.741 | 0.209 | 17.8M |
| v2 (previous architecture) | gate subset | 15 | 0.492 | 0.164 | 41.1M |
| CodeRabbit (anchor) | gate subset | 15 | 0.627 | 0.566 (not comparable&dagger;) | — |
| Claude CLI review (anchor) | gate subset | 15 | 0.339 | 0.481 (not comparable&dagger;) | — |
| claude-code review (anchor) | gate subset | 15 | 0.271 | 0.542 (not comparable&dagger;) | — |

A later six-PR v3.26 run measured 0.667 recall and 0.138 noise.
At that size, one finding moves recall by 3.3 points.
Read it as a consistency check ([run history](bench/README.md#measurement-history)).
<!-- bench-results:end -->

We run the benchmark by hand ([method](bench/MEASUREMENT.md)), not on every release.

Gate-subset rows cover the same 15 PRs under the same judge, and the holdout uses 10 different PRs.
The judge and plugin use Anthropic models, and same-vendor bias has not been measured here.

&dagger; Anchors use stored upstream comments, not fresh tool runs.
Those comments lack file and line anchors, so they receive different scoring context, which can inflate anchor noise by an unmeasured amount.
Their noise rates are therefore not comparable. Recall is the comparable column.
See `adjudicator_context_note` in [bench/baselines.json](bench/baselines.json).

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture, and checks.
[AGENTS.md](AGENTS.md) holds the repository rules, [docs/research](docs/research/README.md) the design evidence, and [CHANGELOG.md](CHANGELOG.md) the release changes.

We formerly published the plugin as `deep-review`.

## License

[Apache 2.0](LICENSE)
