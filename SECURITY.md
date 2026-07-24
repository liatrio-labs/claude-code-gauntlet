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

- We aim to acknowledge a report within a few business days.
- We will tell you whether we could reproduce it, and how we assess scope and severity.
- Fixes ship forward on the current release line, cut automatically by `semantic-release` from `main` (see
  `CHANGELOG.md`). Pick up a fix with `claude plugin update code-gauntlet@code-gauntlet`.
- We will coordinate disclosure timing with you and credit you in the advisory unless you ask us not to. There is
  no bug bounty for this project.

## Supported versions

Only the latest released 3.x minor line receives security fixes. The current release is 3.1.2. Fixes ship forward
on that line; there are no patch releases or backports to earlier 3.x minors, and none at all to 2.x, which is the
retired architecture (the former deep-review pipeline) and is no longer maintained.

| Version | Security fixes |
|---|---|
| 3.1.x (current) | Yes |
| Earlier 3.x | No — upgrade to the latest 3.x |
| 2.x and earlier | No — retired architecture, no backports |

## Scope

### In scope

- **Bypassable prompt-injection defenses.** Code under review is untrusted input. A crafted repository, diff,
  comment, or `REVIEW.md` that gets the pipeline to exfiltrate data, post attacker-chosen content, or run commands
  the user did not ask for is a vulnerability.
- **Command injection** in the pipeline scripts or the workflow bundle — anywhere a finding field, file path,
  branch name, or PR/MR body can reach a shell, `git`, `gh`, or `glab` invocation.
- **Secret leakage** — credentials, tokens, or environment values written into `.code-gauntlet/` artifacts, the
  rendered report, or a posted PR/MR comment.
- **Integrity failures at the trust boundary** — anything that lets a forged or replayed verification receipt
  (nonce, head SHA, finding count) be accepted as trusted, or that lets a finding skip a gauntlet stage while the
  report still claims the stage ran.

### Out of scope

- **Findings quality.** Missed issues, false positives, wrong severity, and noisy output are bug reports, not
  vulnerabilities — open an issue.
- **Claude Code itself and Anthropic's APIs.** Report those upstream to Anthropic.
- **Third-party repositories under review.** A vulnerability the plugin finds, or fails to find, in someone else's
  code belongs to that project's own disclosure process.
- **`git`, `gh`, and `glab`.** Report defects in those tools to their maintainers.
- **Attacks that presume the developer's machine is already compromised.** An attacker who can already edit local
  files, read the environment, or run arbitrary commands as the user has everything the plugin has.
