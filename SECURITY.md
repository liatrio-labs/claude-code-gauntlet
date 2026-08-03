# Security Policy

Code Gauntlet is a Claude Code plugin, not a hosted service. It ships agent prompt contracts, a generated
deterministic JavaScript pipeline bundle (`workflows/pipeline.js`), and standard-library Python scripts. All of it
runs locally inside your own Claude Code session, under your own credentials, and shells out to `git` and to the
`gh` or `glab` CLI. There is no server we operate and no data we hold, so the security surface is what the plugin
can be induced to do on a developer's machine and in the repositories they point it at.

## Reporting a vulnerability

Report privately through GitHub's security advisory form for this repository:

<https://github.com/liatrio-labs/claude-code-gauntlet/security/advisories/new>

Private vulnerability reporting is enabled, and that form is the only supported channel. Do not open a public
issue, pull request, or comment for a suspected vulnerability — a public report discloses the problem to every
user of the plugin before a fix exists.

### What to include

- The plugin version (`.claude-plugin/plugin.json`) and how it was installed: marketplace, `--plugin-dir`, or a
  clone.
- The component: an agent prompt under `agents/`, the workflow bundle or its source under `workflows/`, a script
  under `scripts/`, the skill under `skills/`, or the benchmark harness under `bench/`.
- A minimal reproduction — the smallest diff, repository, or crafted input that triggers the behavior.
- The impact you believe it has: data exfiltration, unintended command execution, secret disclosure, or an
  accepted-but-invalid verification receipt.
- Relevant artifacts from the review output directory (`.code-gauntlet/` by default, overridable with
  `$CODE_GAUNTLET_OUTPUT_DIR`).

Two cautions about attachments:

- Review artifacts under `.code-gauntlet/`, and the comments the pipeline posts, quote excerpts of the source under
  review. Mask tokens, keys, internal hostnames, and anything else sensitive before attaching them.
- Reproductions that drive the benchmark harness (`bench/`) spend real API credits against your own key. Prefer a
  reproduction that runs against the deterministic suites, and say so explicitly if a live run is genuinely
  required.

### What to expect

- Reports are triaged privately, in the advisory thread.
- We will tell you whether we could reproduce the report.
- Fixes ship forward on the current release line, cut from `main` by `python-semantic-release` (see
  `CHANGELOG.md`). Pick up a fix with `claude plugin update code-gauntlet@code-gauntlet`.

## Supported versions

Only the latest released 3.x minor line receives security fixes; `.claude-plugin/plugin.json` carries the version
number of the current release. Fixes ship forward on that line as new releases, patch releases included; they are
not backported to earlier 3.x minors, and nothing is backported to 2.x, which is the retired architecture (the
former deep-review pipeline) and is no longer maintained.

| Version | Security fixes |
|---|---|
| Latest released 3.x minor | Yes |
| Earlier 3.x | No — upgrade to the latest 3.x |
| 2.x and earlier | No — retired architecture, no backports |

## Scope

### Trust boundaries

The pipeline reads several kinds of repository-supplied text and extends each a different level of trust. The
split is deliberate; it was decided on issue #82 (2026-08-02).

- **The diff is untrusted data.** It enters the shared agent context wrapped in `<untrusted-code-content>` tags,
  and anything that looks like an instruction inside it is data to analyze, never an instruction to follow.
- **Rule text is trusted to steer judgment.** Project rule files (`CLAUDE.md`, `AGENTS.md`, `QODO.md`,
  `REVIEW.md`, and their `@import` targets) are read from the checked-out working tree and are meant to shape the
  review — that is what those files are for. The accepted consequence: a pull request can edit the rule files
  that govern its own review and steer findings away from itself. This is not treated as a vulnerability, because
  the diff already carries equally capable suppression channels that no rule-file control would close — in-code
  suppression comments, intentional-change framing, and the test-only and generated-code exclusions — so a
  crafted rule file that causes a finding *not to be raised* is a findings-quality bug, out of scope below. A
  crafted rule file that gets content *out* of the pipeline — into a posted comment, a report, or a shell — is
  the in-scope case below, which is why `REVIEW.md` appears on both sides of this split. If
  suppression through rule text is ever observed in a real run, the recorded escalation is to read rule files
  from the merge base instead of the PR head; it is held in reserve, not implemented.
- **Anything that leaves the pipeline is untrusted, whatever its source.** Repository-derived text that reaches a
  posted PR/MR comment, a rendered report, or a shell invocation stays fully inside the in-scope injection
  classes below.

### In scope

- **Prompt-injection defenses that can be bypassed.** Code under review is untrusted input. A crafted repository,
  diff, comment, or `REVIEW.md` that gets the pipeline to exfiltrate data, post attacker-chosen content, or run
  commands the user did not ask for is a vulnerability.
- **Command injection** in the pipeline scripts or the workflow bundle — anywhere a finding field, file path,
  branch name, or PR/MR body can reach a shell, `git`, `gh`, or `glab` invocation.
- **Secret leakage** — credentials, tokens, or environment values written into `.code-gauntlet/` artifacts, the
  rendered report, or a posted PR/MR comment.
- **Integrity failures at the trust boundary** — anything that lets a forged or replayed verification receipt
  (nonce, head SHA, finding count) be accepted as trusted, or that lets a finding skip a gauntlet stage while the
  report still claims the stage ran.

### Out of scope

- **Findings quality.** Missed issues, false positives, wrong severity, and noisy output are bug reports, not
  vulnerabilities — open an issue. That includes a finding steered away by repository rule text (see Trust
  boundaries above).
- **Claude Code itself and Anthropic's APIs.** Report those upstream to Anthropic.
- **Third-party repositories under review.** A vulnerability the plugin finds, or fails to find, in someone else's
  code belongs to that project's own disclosure process.
- **`git`, `gh`, and `glab`.** Report defects in those tools to their maintainers.
- **Attacks that presume the developer's machine is already compromised.** An attacker who can already edit local
  files, read the environment, or run arbitrary commands as the user has everything the plugin has.
