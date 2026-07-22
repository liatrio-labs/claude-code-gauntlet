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

The only files created are within the `.code-gauntlet/` directory in your local project (configurable via `$CODE_GAUNTLET_OUTPUT_DIR`). These contain review context, findings (NDJSON), and intermediate pipeline artifacts. You control these files entirely — you can read, edit, delete, or gitignore them at any time. The directory is gitignored by default.

## Contact

If you have questions about this privacy policy, open an issue at https://github.com/liatrio-labs/claude-code-gauntlet/issues.
