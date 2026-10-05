# Privacy Policy

**Last updated:** October 5, 2026

## Overview

claude-code-gauntlet is an open source plugin that runs through your existing Claude Code session. We operate no backend and collect no user data. Reviews use the model access you have configured in Claude Code and can read from and post to GitHub or GitLab through your authenticated CLI.

## What We Collect

Nothing.

## How the Plugin Works

- The plugin loads markdown skill and agent contracts, a generated JavaScript workflow bundle, and Python 3.10 or newer scripts locally. The shipped pipeline scripts use only the Python standard library; the benchmark harness's vendored scorer has separate dependencies.
- Review output files (`.code-gauntlet/`) are stored in your project directory on your machine
- Review agents analyze code through the model access configured in your Claude Code session
- Interactive runs ask before posting findings to an open PR or MR; headless posting defaults to dry-run. See [delivery options](README.md#what-it-posts).
- No telemetry, no tracking, no cookies

## Third-Party Services

The plugin uses your configured Claude Code model access and the authenticated `gh` or `glab` CLI to read review targets and post findings. These services receive the code or review content needed for those operations. Their data handling follows their own policies. The plugin has no separate analytics service. The benchmark harness under `bench/` is developer tooling that a review never runs; when you run it yourself, it calls an LLM judge API with your own key.

## Data Storage

Review files are saved within the output directory (`.code-gauntlet/` by default, configurable via `$CODE_GAUNTLET_OUTPUT_DIR`). These include review context, findings, a markdown report, and resume checkpoints. On the default path, the session materializes the result artifacts after the workflow returns. You can read, edit, delete, or ignore these files. When the output directory is inside the repo, it is ignored by default via `.git/info/exclude` (never by editing the tracked `.gitignore`). Outside that directory the plugin appends one ignore entry to `.git/info/exclude`, which does not appear in `git status`. Interactive runs check out the PR or MR branch when your working tree is not already on it. Posting writes a short-lived temp file that holds the comment payload.

## Contact

If you have questions about this privacy policy, open an issue at https://github.com/liatrio-labs/claude-code-gauntlet/issues.
