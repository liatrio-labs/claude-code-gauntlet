#!/usr/bin/env python3
"""
PreToolUse hook: validate_bash_subagent

Restricts Bash usage in subagents to the finding-emission printf-append pattern:
  printf '%s\\n' '...' >> <output_dir>/deep-review-*
  echo '...' >> <output_dir>/deep-review-*  (legacy, still accepted)

Security boundary — blocks arbitrary commands (grep, git, rm, etc.) while
allowing agents to append findings to their NDJSON file.

Reads hook input JSON from stdin. Checks:
- agent_id: if missing/null (orchestrator), allow all commands
- command: must match echo-append to a deep-review-* file

Emits JSON on stdout with hookSpecificOutput.permissionDecision ("allow"/"deny")
per the Claude Code PreToolUse hook protocol. Always exits 0.
"""

import json
import re
import sys

# Match: printf '%s\n' '<payload>' >> <path>/deep-review-<filename>
# Also accepts: echo '<payload>' >> <path>/deep-review-<filename> (legacy)
# Payload must be single-quoted or ANSI-C quoted (no double-quotes — expansion risk).
# Path can be relative (.deep-review/...) or absolute (/path/to/...).
# \Z blocks embedded newlines.
ECHO_APPEND_RE = re.compile(
    r"^\s*(?:printf\s+'%s\\n'\s+|echo\s+)"  # printf '%s\n' or echo (legacy)
    r"(?:'[^']*'|\$'(?:[^'\\]|\\.)*')"  # single-quoted or ANSI-C quoted payload
    r"\s+>>\s+"                           # append operator
    r"(\"?[^\s\"]+\"?)"                   # capture path (with optional quotes)
    r"\Z"
)

# Safe characters for file paths — no shell metacharacters
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_./:~-]+$")

FORBIDDEN_COMMANDS = ["grep", "cat", "git", "find"]


def validate_bash_command(hook_input):
    """
    Validate a bash command for subagent execution.

    Args:
        hook_input (dict): Hook input from Claude Code PreToolUse event.
            Expected schema: {
                "tool_input": {"command": "..."},
                "agent_id": "...",       # present for subagents
                "agent_type": "...",     # present for subagents
                ...
            }
            Falls back to top-level "command" for forward compatibility.

    Returns:
        tuple: (allowed: bool, message: str)
    """
    agent_id = hook_input.get("agent_id") or hook_input.get("agent_type")

    tool_input = hook_input.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = (tool_input.get("command") or hook_input.get("command", "")).strip()

    # Orchestrator (no agent_id) is allowed all commands
    if not agent_id:
        return True, "Orchestrator allowed"

    if not command:
        return False, "Empty command not allowed for subagents"

    # Reject forbidden commands (exact match on first token)
    first_token = command.split()[0] if command.split() else ""
    for forbidden in FORBIDDEN_COMMANDS:
        if forbidden == first_token:
            return False, f"Command '{forbidden}' not allowed in subagents"

    # Validate echo-append pattern
    m = ECHO_APPEND_RE.match(command)
    if m:
        raw_path = m.group(1).strip('"')

        if not SAFE_PATH_RE.match(raw_path):
            return False, f"Path contains unsafe characters: {raw_path}"

        if ".." in raw_path:
            return False, "Path traversal (..) not allowed in output path"

        filename = raw_path.rsplit("/", 1)[-1] if "/" in raw_path else raw_path
        if not filename.startswith("deep-review-"):
            return False, "Filename must start with deep-review-"

        return True, "Valid printf/echo-append pattern"

    # Command did not match echo-append — produce helpful diagnostics
    if "echo" not in command and "printf" not in command:
        return False, f"Command not allowed: {command}"

    # Reject shell operators
    for op in ["|", ";", "&&", "||"]:
        if op in command:
            return False, "Shell operators not allowed in subagents"

    # Reject overwrite (>) unless it's part of append (>>)
    if ">" in command.replace(">>", ""):
        return False, "echo command must use >> (append) not > (overwrite)"

    if "/deep-review-" not in command and "deep-review-" not in command:
        return False, "echo command must append to a deep-review-* file"

    return False, f"Command does not match valid echo-append pattern: {command}"


def main():
    """Read hook input from stdin and emit permissionDecision JSON on stdout."""
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, Exception) as e:
        json.dump({
            "hookSpecificOutput": {"permissionDecision": "deny"},
            "systemMessage": f"Invalid hook input: {e}"
        }, sys.stdout)
        sys.exit(0)

    allowed, message = validate_bash_command(hook_input)

    if allowed:
        json.dump({
            "hookSpecificOutput": {"permissionDecision": "allow"}
        }, sys.stdout)
    else:
        json.dump({
            "hookSpecificOutput": {"permissionDecision": "deny"},
            "systemMessage": message
        }, sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
