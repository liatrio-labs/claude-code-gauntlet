# Privacy Policy

**Last updated:** May 7, 2026

## Overview

claude-code-gauntlet is an open source Claude Code plugin that runs entirely on your local machine. We do not collect, store, or transmit any user data.

## What We Collect

Nothing.

## How the Plugin Works

- All skills, agents, and scripts are markdown and Python stdlib files loaded locally by Claude Code
- Review output files (`.code-gauntlet/`) are stored in your project directory on your machine
- All code analysis happens locally via your existing Claude Code session
- No data is sent to any external server, API, or analytics service operated by this plugin
- No telemetry, no tracking, no cookies

## Third-Party Services

This plugin does not connect to any third-party services of its own. It operates within your existing Claude Code environment and uses only the tools and model access you have already configured (e.g., the Anthropic API via Claude Code). Any data handling by Claude Code itself is governed by Anthropic's privacy policy, not this plugin.

## Data Storage

The only files created in the working tree are within the review output directory (`.code-gauntlet/` by default, configurable via `$CODE_GAUNTLET_OUTPUT_DIR`). These contain review context, findings, and intermediate pipeline artifacts. You control these files entirely — you can read, edit, delete, or ignore them at any time. When the output directory is inside the repo, it is ignored by default via `.git/info/exclude` (never by editing the tracked `.gitignore`). The sole default write outside that directory is the exclude entry under the git dir, which does not appear in `git status`.

## Contact

If you have questions about this privacy policy, open an issue at https://github.com/liatrio-labs/claude-code-gauntlet/issues.
