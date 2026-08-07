#!/usr/bin/env bash
# Cursor Cloud Build install script (referenced from .cursor/environment.json).
#
# Fixes the two VM toolchain traps structurally instead of instructing agents
# around them (issue #98):
#   1. The VM's /exec-daemon/node (v22) shadows nvm's Node 24 on PATH, so JS
#      work fails on confusing feature/syntax errors rather than a clear
#      version mismatch.
#   2. pip --user console scripts (pytest, pre-commit) land in ~/.local/bin,
#      which is not on PATH by default.
#
# Runs at Build time on every Build, possibly over previously prepared disk
# state, so every step must be idempotent. The Build captures disk state, so
# PATH lines appended to the shell profiles persist into agent sessions.
set -euo pipefail

PROFILES=("$HOME/.bashrc" "$HOME/.profile")

append_once() {
  local line="$1" profile
  for profile in "${PROFILES[@]}"; do
    grep -qxF "$line" "$profile" 2>/dev/null || printf '%s\n' "$line" >>"$profile"
  done
}

# Node 24 (CI pins major 24 in .github/workflows/ci.yml) ahead of /exec-daemon/node.
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  # nvm.sh is not clean under `set -u`; relax around sourcing it.
  set +u
  . "$NVM_DIR/nvm.sh"
  nvm install 24
  nvm alias default 24
  NODE_BIN="$(dirname "$(nvm which default)")"
  set -u
  append_once "export PATH=\"$NODE_BIN:\$PATH\""
fi

# pip --user console scripts (pytest, pre-commit) live in ~/.local/bin.
append_once 'export PATH="$HOME/.local/bin:$PATH"'
python3 -m pip install --user --quiet pytest pytest-cov coverage pre-commit

# Fail the Build loudly if an agent shell would still resolve the wrong node.
resolved="$(bash -ic 'node --version' 2>/dev/null || true)"
case "$resolved" in
  v24.*) echo "node on agent PATH: $resolved" ;;
  *)
    echo "ERROR: node on agent PATH is '$resolved', expected v24.x" >&2
    exit 1
    ;;
esac
