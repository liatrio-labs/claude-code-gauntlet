# Code Gauntlet

[![CI](https://github.com/liatrio-labs/claude-code-gauntlet/actions/workflows/ci.yml/badge.svg)](https://github.com/liatrio-labs/claude-code-gauntlet/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/liatrio-labs/claude-code-gauntlet/badge)](https://securityscorecards.dev/viewer/?uri=github.com/liatrio-labs/claude-code-gauntlet)

Code review for Claude Code where every finding has to survive a fight before you see it.

Seven specialist agents review each GitHub PR, GitLab MR, or local change for bugs, security, tests, and cross-file impact.
An eighth agent then reads changed code that none of them cited.
Each finding is checked against the source, attacked by a skeptical validator, and re-examined by a blind challenger.
Only the survivors reach your PR.

On a public benchmark of real PRs with known bugs, it catches nearly four in five of them ([benchmark results](#benchmark-results)).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/pipeline-overview-dark.svg">
  <img alt="Code Gauntlet pipeline overview: seven specialist reviewers and an optional gap hunter feed verification and delivery" src="docs/assets/pipeline-overview-light.svg">
</picture>

## What a finding looks like

This is a comment it wrote on [cal.com PR 10600](https://github.com/calcom/cal.com/pull/10600) during a benchmark run:

> **🟡 [MEDIUM] Backup code consumption is a non-atomic read-modify-write**
>
> The backupCodes array is read at the top of authorize(), then written back by an unconditional update keyed only on user id. Concurrent logins using the same code all succeed. Concurrent logins using different codes overwrite each other, so a used code is restored and can be reused.
>
> ⚔️ *Code Gauntlet*

A review also gives you a summary of every reported issue and a full markdown report saved under `.code-gauntlet/`.

## Quick start

```bash
claude plugin marketplace add https://github.com/liatrio-labs/claude-code-gauntlet.git
claude plugin install code-gauntlet@code-gauntlet
```

Open Claude Code in your repository and enter `/code-gauntlet 42` to review PR #42.
PR and MR reviews need the `gh` or `glab` CLI logged in first.
Plain words work too, because the plugin triggers on code review requests:

```text
code gauntlet PR #42                  # GitHub pull request
review MR !89 thoroughly              # GitLab merge request
comprehensive review of my changes    # local uncommitted changes
```

The plugin detects GitHub or GitLab from the git remote.
It saves the report first, and interactive runs ask before posting anything to the PR or MR.
Update later with `claude plugin update code-gauntlet@code-gauntlet`.

### Requirements

- You need Claude Code 2.1.154 or newer, with git on your path.
- You need Python 3.10 or newer, reachable as `python3`, `python`, or `py -3`. There is nothing to `pip install`.
- You need an authenticated `gh` for GitHub or `glab` for GitLab to review PRs and MRs.
- Native Windows works with Git for Windows installed, which gives Claude Code its Bash tool.

A live headless run on Windows verifies local-changes reviews.
PR reviews on Windows are covered by Windows CI tests, not yet by a live forge review.

### Approving the pipeline

The review runs as one Claude Code workflow, and Claude Code asks you to approve it.
To approve it ahead of time, add this rule to `permissions.allow` in your Claude Code settings:

```json
"Workflow(code-gauntlet:code-gauntlet-pipeline)"
```

The rule covers this plugin's pipeline and no other workflow.
Add it if the approval prompt offers no way to say yes, which some Claude Code versions do for a script this large.
Headless runs need the same rule, or `--allowedTools` with the same value.

### Time and usage

The pipeline pins each review agent's model, so your session's model and effort do not change them.
Setting `CLAUDE_CODE_SUBAGENT_MODEL` is the exception, because it overrides every agent.
The session only prepares the review and delivers the result, so we recommend Sonnet at low effort for it.

In benchmark runs from an Opus session, a full review took a median of 9 minutes per PR, from 3 to 17.
Each review used a median of $5.32 at API prices, and 80% fell between $2.57 and $7.86.
We have not measured the cost from a Sonnet session.

## How it reviews

A full review starts seven specialist reviewers in parallel:

| Agent | Model | Focus |
| --- | --- | --- |
| bug-detector | Sonnet | Logic errors, edge cases, error handling, defects in changed tests |
| security-reviewer | Opus | Trust boundaries, injection, authentication, attacker-controlled data |
| cross-file-impact | Sonnet | Callers and consumers the change can break elsewhere in the codebase |
| test-analyzer | Sonnet | Tests that are wrong or weak, critical coverage gaps, defects the tests expose |
| conventions-and-intent | Sonnet | Project rules, documented intent, comment accuracy |
| type-design-analyzer | Sonnet | Invariants, invalid states, type boundaries |
| code-simplifier | Sonnet | Simplifications that keep behavior, and defects found along the way |

After the specialists return, an eighth agent, **gap-hunter** (Sonnet), reviews changed regions that no specialist cited.
It works within a fixed read budget, and the report lists what the budget left out.
Light reviews and fully cited changes skip it.

The security reviewer runs on Opus, a judgment call that the [routing research](docs/research/artifacts/12-model-routing-for-code-review.md) explains.
When every changed file is low risk and the change is under 50 lines, the plugin offers a light review with only `bug-detector` and `security-reviewer`.

Agents see the full diff and follow callers, dependencies, and tests across files.
Discovery produces candidates, and each one then runs the gauntlet:

1. Merge combines duplicate findings and checks their structure.
2. Verify checks source facts in code and separates new issues from older ones.
3. Validate asks an independent skeptic to disprove each finding.
4. Filter routes each finding by its typed verdict and rejects prompt injection.
5. Admission is plain code that checks each finding has the evidence its kind requires.
6. Blind challenge asks a fresh agent to check the claim without the original reasoning or evidence.
7. Deliver gives each finding one final decision, ranks the survivors, and prepares comments and a report.

Validators and challengers return typed verdicts, not confidence scores.
A confirmed failure posts at its severity, and a latent defect posts at low severity.
A refutation needs a verified code citation, and a security finding needs refutations from both independent readers.
Preferences, unverified findings, and complaints about comment wording stay in the local report.
If verification or validation itself breaks, the report records the gap.

Git blame separates new issues from older ones your changes exposed.
The older ones drop one severity level and carry an origin label.
After new commits, the plugin offers to review only what changed since the last review.

[How it works](docs/how-it-works.md) shows the full design, stage by stage.

## What it posts

The plugin saves a markdown report locally.
For an open PR or MR, it then asks whether to post the selected findings as inline comments.
GitHub receives them as one review, and GitLab receives a summary note plus one discussion per finding.
Each defect gets one short comment, and a defect found at several places lists the other sites in that comment.
A finding whose location cannot be confirmed stays in the review summary and gets no inline comment.

In interactive runs you can also create a task board for the selected findings.
Headless runs support chat, markdown, and PR/MR comments.
Headless posting defaults to dry-run and to at most 25 inline comments per review ([headless configuration](skills/code-gauntlet/references/headless-mode.md)).

## Project rules

The plugin reads project rules as prose from `REVIEW.md`, `CLAUDE.md`, `AGENTS.md`, and `QODO.md`.
No `REVIEW.md` is required, and it holds rules only, not settings.
The [authoring guide](docs/review-md-authoring.md) covers writing rules.

## What it runs, reads, and sends

The plugin runs on your machine inside your Claude Code session. We operate no server and collect no data.

- It runs `git`, and `gh` or `glab` under your own login, to read the diff, the PR or MR, and earlier review comments.
- It reads the repository's rule files: `CLAUDE.md`, `AGENTS.md`, `QODO.md`, and `REVIEW.md`.
- It runs its own Python scripts, which use only the standard library and reach the network only through `gh` or `glab`.
- It sends the diff and the files the agents read to the model access you configured in Claude Code, and nowhere else.
- It writes review files under `.code-gauntlet/`, or the directory `CODE_GAUNTLET_OUTPUT_DIR` names. A rerun replaces the earlier files for the same commit.
- It adds one ignore entry to `.git/info/exclude` when that directory is inside the repository. It never edits your tracked `.gitignore`.
- It checks out the PR, MR, or branch under review in interactive runs when your working tree is elsewhere. That checkout can fetch the branch. Headless runs never check out.
- It posts to the PR or MR through `gh` or `glab`. Interactive runs ask first, and headless runs post only when configured to. Each post uses a temporary file that is deleted afterwards.
- It creates one Claude Code task per delivered finding if you accept the task-board offer.
- It ships no hooks, and the review agents can only read and search files. Two helper agents have more: one runs commands through Bash, and one writes the review files.
- It has no telemetry, analytics, or tracking.

The plugin treats code under review as untrusted input and filters prompt-injection attempts.
Before posting comments, it escapes raw HTML and @mentions outside trusted code fences and redacts known GitHub and GitLab token formats.
See [security boundaries](SECURITY.md#trust-boundaries) and [PRIVACY.md](PRIVACY.md).

## Benchmark results

Scores come from the MIT-licensed [Martian code review benchmark](https://github.com/withmartian/code-review-benchmark): real PRs from open source projects, each with a list of known bugs.
v4.0 reviewed the same 15 PRs three times from a Sonnet session at low effort, and a pinned judge, `claude-opus-4-5-20251101`, scored every comment.

| Measure | v4.0 |
|---|---|
| Known bugs caught, all 15 PRs (66 bugs) | 78.8% |
| Known bugs caught, 13 PRs with no overlap with our prompts (50 bugs) | 80.0% |
| Known bugs caught, PRs outside our development subset (21 bugs) | 73.0% |
| Comments that match a known bug, 13 PRs | 38.9% |
| Inline comments per PR | 9.3 |

The benchmark lists only some of each PR's real problems, so a comment that matches no known bug is not a false alarm.

Two limits apply.
Eleven of the PRs ran on a build just before the release, whose later fixes affect only reviews with over 40 findings.
The judge and the plugin both use Anthropic models, and same-vendor bias has not been measured here.

We run the benchmark by hand, not on every release.
The [method](bench/MEASUREMENT.md) and the [run history](bench/README.md#measurement-history) hold the detail and earlier results.

## Contributing

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup, architecture, and checks.
[AGENTS.md](AGENTS.md) holds the repository rules and [docs/research](docs/research/README.md) the design evidence.
Report bugs and ask questions in [issues](https://github.com/liatrio-labs/claude-code-gauntlet/issues).

We formerly published the plugin as `deep-review`.

## License

[Apache 2.0](LICENSE)
