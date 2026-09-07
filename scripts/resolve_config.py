#!/usr/bin/env python3
"""Resolve the code-gauntlet configuration and print its receipt.

Usage:
    python3 scripts/resolve_config.py [--target pr|mr|local] [--cwd DIR] [--plugin-root DIR]

``--target`` is optional.  Without it, target-dependent validation is skipped and the
success JSON contains ``"target": null``.  ``--cwd`` defaults to the process directory.
``--plugin-root`` defaults to two levels above this script and, when supplied, must name
that same directory.  stdin is unused.

On success, exactly one JSON object is returned on stdout with the rendered block on
stderr.  Every non-zero result has empty stdout.  Exit 0 is successful resolution.  Exit
1 is an invalid pin, invalid ``REVIEW.md`` value, or target-incompatible delivery.  Exit
2 is argument usage, not a git repository, an unreadable or missing bundle version, or
another resolver setup failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast

MAX_SAFE_INTEGER = 9007199254740991
_SCRIPT_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))
_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f]")
_CANDIDATE_RE = re.compile(r"^[a-z_]+(,[a-z_]+)*$")
_PIPELINE_VERSION_RE = re.compile(
    r"(?m)^[ \t]*const[ \t]+PIPELINE_VERSION[ \t]*=[ \t]*"
    r"(['\"])([^'\"\r\n]+)\1[ \t]*;?[ \t]*(?://[^\r\n]*)?$"
)

# generated-from-registry-identity:knob_registry — do not edit; run scripts/generate_contract_requirements.py
KNOB_REGISTRY = [
    {
        "key": "model_tier",
        "modes": [
            "headless",
            "interactive",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
            "interactive": [
                "fixed",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "optimized",
            ],
        },
        "env": "CODE_GAUNTLET_MODEL_TIER",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "optimized",
                "default",
            ],
            "interactive": [
                "optimized",
                "fixed",
            ],
        },
        "type": "string",
        "waistPath": None,
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "delivery",
        "modes": [
            "headless",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "review_md",
                "default",
            ],
        },
        "rule": {
            "kind": "csv_subset",
            "values": [
                "chat",
                "pr_comments",
                "markdown",
            ],
        },
        "env": "CODE_GAUNTLET_DELIVERY",
        "reviewMdKey": "default_delivery",
        "defaults": {
            "headless": [
                "markdown",
                "default",
            ],
        },
        "type": "csv_list",
        "waistPath": None,
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "post_mode",
        "modes": [
            "headless",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "dry-run",
                "live",
            ],
        },
        "env": "CODE_GAUNTLET_POST_MODE",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "dry-run",
                "default",
            ],
        },
        "type": "string",
        "waistPath": None,
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "pr_comment_cap",
        "modes": [
            "headless",
            "interactive",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
            "interactive": [
                "env",
                "default",
            ],
        },
        "rule": {
            "headless": {
                "kind": "positive_digits",
            },
            "interactive": {
                "kind": "digits_or_null",
            },
        },
        "env": "CODE_GAUNTLET_PR_COMMENT_CAP",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "6",
                "default",
            ],
            "interactive": [
                "null",
                "default",
            ],
        },
        "type": "int_or_null",
        "waistPath": "limits.deliveryCap",
        "derivedFrom": None,
        "nullReceipt": [
            "interactive",
        ],
    },
    {
        "key": "delivery_tier",
        "modes": [
            "headless",
            "interactive",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
            "interactive": [
                "env",
                "default",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "all",
                "main_only",
            ],
        },
        "env": "CODE_GAUNTLET_DELIVERY_TIER",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "all",
                "default",
            ],
            "interactive": [
                "all",
                "default",
            ],
        },
        "type": "string",
        "waistPath": "delivery.tier",
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "draft_policy",
        "modes": [
            "headless",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "review",
                "skip",
            ],
        },
        "env": "CODE_GAUNTLET_DRAFT_POLICY",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "review",
                "default",
            ],
        },
        "type": "string",
        "waistPath": None,
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "reviewed_policy",
        "modes": [
            "headless",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "incremental",
                "full",
                "skip",
            ],
        },
        "env": "CODE_GAUNTLET_REVIEWED_POLICY",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "full",
                "default",
            ],
        },
        "type": "string",
        "waistPath": None,
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "pr_not_found_policy",
        "modes": [
            "headless",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "local",
                "error",
            ],
        },
        "env": "CODE_GAUNTLET_PR_NOT_FOUND_POLICY",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "error",
                "default",
            ],
        },
        "type": "string",
        "waistPath": None,
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "trivial_scope",
        "modes": [
            "headless",
        ],
        "allowedSources": {
            "headless": [
                "env",
                "default",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "light",
                "full",
            ],
        },
        "env": "CODE_GAUNTLET_TRIVIAL_SCOPE",
        "reviewMdKey": None,
        "defaults": {
            "headless": [
                "full",
                "default",
            ],
        },
        "type": "string",
        "waistPath": "scopeAnswer",
        "derivedFrom": None,
        "nullReceipt": [],
    },
    {
        "key": "review_md",
        "modes": [
            "interactive",
        ],
        "allowedSources": {
            "interactive": [
                "discovery",
            ],
        },
        "rule": {
            "kind": "enum",
            "values": [
                "present",
                "absent",
            ],
        },
        "env": None,
        "reviewMdKey": None,
        "defaults": {
            "interactive": [
                "absent",
                "discovery",
            ],
        },
        "type": "string",
        "waistPath": None,
        "derivedFrom": "reviewConfigPath",
        "nullReceipt": [],
    },
]
# /generated-from-registry-identity:knob_registry


class ResolverError(Exception):
    """An invalid configuration value that belongs to exit code 1."""


class ResolverSetupError(Exception):
    """A repository, CLI, or bundle setup failure that belongs to exit code 2."""


def _selected_rule(rule: Any, mode: str) -> Any:
    if not isinstance(rule, dict):
        return None
    if isinstance(rule.get("kind"), str):
        return rule
    return rule.get(mode)


def _safe_integer(value: str) -> bool:
    try:
        number = int(value)
        return 0 <= number <= MAX_SAFE_INTEGER
    except (TypeError, ValueError):
        return False


def matches_rule(rule: Any, value: Any, mode: str) -> bool:
    """Return whether a string satisfies a registry rule for ``mode``."""
    selected = _selected_rule(rule, mode)
    if not isinstance(value, str) or not isinstance(selected, dict):
        return False
    kind = selected.get("kind")
    values = selected.get("values")
    if kind == "enum":
        return isinstance(values, list) and value in values
    if kind == "csv_subset":
        if not isinstance(values, list) or not value:
            return False
        parts = value.split(",")
        return (
            all(part in values for part in parts)
            and len(set(parts)) == len(parts)
            and all(part != "" for part in parts)
        )
    if kind == "positive_digits":
        return bool(
            re.fullmatch(r"[1-9][0-9]*", value, flags=re.ASCII)
        ) and _safe_integer(value)
    if kind == "digits_or_null":
        return value == "null" or (
            bool(re.fullmatch(r"(?:0|[1-9][0-9]*)", value, flags=re.ASCII))
            and _safe_integer(value)
        )
    return False


def _rule_display(
    rule: Any, mode: str, values_override: list[str] | None = None
) -> str:
    selected = _selected_rule(rule, mode)
    if not isinstance(selected, dict):
        return "valid value"
    kind = selected.get("kind")
    if kind == "enum" or kind == "csv_subset":
        values = (
            values_override
            if values_override is not None
            else selected.get("values", [])
        )
        if isinstance(values, list):
            return "{" + ",".join(str(value) for value in values) + "}"
    if kind == "positive_digits":
        return "{positive integer}"
    if kind == "digits_or_null":
        return "{digits or null}"
    return "valid value"


def _error_value(value: Any) -> str:
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            return json.dumps(value, ensure_ascii=False)
        return value
    return str(value)


def _invalid_message(
    row: Mapping[str, Any],
    value: Any,
    mode: str,
    *,
    review_value: bool = False,
    values_override: list[str] | None = None,
) -> str:
    if review_value:
        name = "REVIEW.md default_delivery"
    else:
        name = str(row.get("env") or row.get("key") or "configuration")
    return (
        f"HEADLESS CONFIG ERROR: {name}={_error_value(value)} not in "
        f"{_rule_display(row.get('rule'), mode, values_override)}"
    )


def _default_for(row: Mapping[str, Any], mode: str) -> tuple[Any, Any] | None:
    defaults = row.get("defaults")
    if not isinstance(defaults, dict):
        return None
    value = defaults.get(mode)
    if not isinstance(value, list) or len(value) != 2:
        return None
    return value[0], value[1]


def _typed_value(row: Mapping[str, Any], value: str) -> Any:
    kind = row.get("type")
    if kind == "csv_list":
        return value.split(",")
    if kind == "int_or_null":
        return None if value == "null" else int(value)
    return value


def _registry_rows(
    registry: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    if registry is not None:
        return list(registry)
    return cast(list[Mapping[str, Any]], KNOB_REGISTRY)


def serialize_receipt(
    mode: str,
    config_echo: Mapping[str, Any],
    *,
    registry: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Serialize the mode's receipt in registry order."""
    rows = _registry_rows(registry)
    receipt = {}
    for row in rows:
        key = row.get("key")
        if mode not in row.get("modes", []) or not isinstance(key, str):
            continue
        if key in config_echo:
            receipt[key] = config_echo[key]
    return json.dumps(receipt, indent=4, ensure_ascii=False)


def resolve(
    mode: str,
    environ: Mapping[str, str],
    review_md_text: str | None,
    target: str | None,
    *,
    registry: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve registry rows once, using env, REVIEW.md, then the mode default."""
    rows = _registry_rows(registry)
    if mode not in {"headless", "interactive"}:
        raise ResolverSetupError(f"unsupported mode: {mode}")
    if target not in {None, "pr", "mr", "local"}:
        raise ResolverSetupError(f"unsupported target: {target}")

    review_value = parse_default_delivery(review_md_text)
    config_echo: dict[str, dict[str, str]] = {}
    resolved: dict[str, Any] = {}
    for row in rows:
        if mode not in row.get("modes", []):
            continue
        key = row.get("key")
        if not isinstance(key, str):
            continue
        if row.get("derivedFrom") is not None:
            continue
        default = _default_for(row, mode)
        if default is None:
            continue
        value, source = default
        env_name = row.get("env")
        if isinstance(env_name, str) and env_name in environ:
            env_value = environ[env_name]
            if not matches_rule(row.get("rule"), env_value, mode):
                raise ResolverError(_invalid_message(row, env_value, mode))
            # An interactive model pin is a validation-only pin.  Its source remains
            # fixed because the row does not allow env as an interactive source.
            if "env" in row.get("allowedSources", {}).get(mode, []):
                value, source = env_value, "env"
        elif (
            review_value is not None
            and row.get("reviewMdKey") is not None
            and "review_md" in row.get("allowedSources", {}).get(mode, [])
        ):
            if not matches_rule(row.get("rule"), review_value, mode):
                raise ResolverError(
                    _invalid_message(row, review_value, mode, review_value=True)
                )
            value, source = review_value, "review_md"

        selected_rule = _selected_rule(row.get("rule"), mode)
        selected_values = (
            selected_rule.get("values", []) if isinstance(selected_rule, dict) else []
        )
        if (
            target == "local"
            and isinstance(value, str)
            and "pr_comments" in value.split(",")
            and isinstance(selected_values, list)
        ):
            allowed = [item for item in selected_values if item != "pr_comments"]
            raise ResolverError(
                _invalid_message(
                    row,
                    value,
                    mode,
                    review_value=source == "review_md",
                    values_override=allowed,
                )
            )
        config_echo[key] = {"value": str(value), "source": str(source)}
        if row.get("waistPath") is None:
            resolved[key] = _typed_value(row, str(value))
    return {
        "configEcho": config_echo,
        "waist": {"configEcho": config_echo},
        "resolved": resolved,
    }


def _receipt_as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(_receipt_as_text(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


_JS_TRIM_RE = re.compile(
    r"^[\t-\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
    r"|[\t-\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$"
)


def one_line(value: Any) -> str:
    """Apply the report renderer's one-line whitespace rule."""
    text = _receipt_as_text(value)
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r" +", " ", text)
    return _JS_TRIM_RE.sub("", text)


def receipt_safe(value: Any) -> str:
    """Apply the report receipt's one-line, backtick-safe rendering rule."""
    text = one_line(value).replace("`", "")
    return text or "unknown"


def render_block(
    mode: str,
    config_echo: Mapping[str, Any],
    identity: Mapping[str, Any],
    *,
    registry: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Render the resolver receipt in registry order without a trailing newline."""
    rows = _registry_rows(registry)
    lines = ["Headless config:" if mode == "headless" else "Resolved config:"]
    for row in rows:
        if mode not in row.get("modes", []):
            continue
        key = row.get("key")
        if not isinstance(key, str):
            continue
        if row.get("derivedFrom") is not None and key not in config_echo:
            continue
        entry = config_echo.get(key) if isinstance(config_echo, Mapping) else None
        entry_map = entry if isinstance(entry, Mapping) else None
        valid = (
            entry_map is not None
            and isinstance(entry_map.get("value"), str)
            and isinstance(entry_map.get("source"), str)
        )
        value = entry_map.get("value") if valid and entry_map else "unknown"
        source = entry_map.get("source") if valid and entry_map else "unknown"
        lines.append(f"  {key}={receipt_safe(value)} ({receipt_safe(source)})")
    lines.append(
        f"  pipeline_version={receipt_safe(identity.get('pipeline_version'))} (bundle)"
    )
    lines.append(
        f"  plugin_root={receipt_safe(identity.get('plugin_root'))} (resolved)"
    )
    return "\n".join(lines)


def parse_default_delivery(text: str | None) -> str | None:
    """Return the root Default Delivery candidate, or ``None`` when it is unset."""
    if not isinstance(text, str):
        return None
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"## Default Delivery[ \t]*", line)
        ),
        None,
    )
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^#{1,6}[ \t]", line) or re.match(
            r"^[ \t]{0,3}(?:`{3,}|~{3,})", line
        ):
            break
        body.append(line)
    cleaned = re.sub(r"<!--.*?-->", "", "\n".join(body), flags=re.DOTALL)
    candidate = next(
        (line.strip() for line in cleaned.splitlines() if line.strip()), None
    )
    if candidate is None or not _CANDIDATE_RE.fullmatch(candidate):
        return None
    return candidate


def _git_repo_root(cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ResolverSetupError(f"git repository probe failed: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ResolverSetupError("not a git repository")
    return os.path.realpath(proc.stdout.strip())


def read_pipeline_version(plugin_root: str) -> str:
    """Read the bundle's non-empty PIPELINE_VERSION declaration."""
    path = os.path.join(plugin_root, "workflows", "pipeline.js")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ResolverSetupError(f"cannot read pipeline bundle: {exc}") from exc
    match = _PIPELINE_VERSION_RE.search(text)
    if match is None:
        raise ResolverSetupError("PIPELINE_VERSION not found in workflow bundle")
    return match.group(2)


def probe_review_md(repo_root: str) -> tuple[bool, str | None]:
    """Inspect only the repository-root REVIEW.md for presence and text."""
    path = os.path.join(repo_root, "REVIEW.md")
    if not os.path.isfile(path):
        return False, None
    try:
        with open(path, encoding="utf-8", newline=None) as handle:
            return True, handle.read()
    except OSError as exc:
        raise ResolverSetupError(f"cannot read root REVIEW.md: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Resolve the code-gauntlet configuration.",
        exit_on_error=False,
    )


def run(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run the resolver and return ``(exit_code, stdout, stderr)``."""
    parser = _parser()
    parser.add_argument("--target", choices=("pr", "mr", "local"), default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--plugin-root", default=None)
    try:
        args = parser.parse_args([] if argv is None else argv)
    except (argparse.ArgumentError, SystemExit):
        return 2, "", ""

    env = environ if environ is not None else os.environ
    try:
        cwd = os.path.realpath(args.cwd or os.getcwd())
        plugin_root = os.path.realpath(args.plugin_root or _SCRIPT_ROOT)
        if args.plugin_root is not None and plugin_root != _SCRIPT_ROOT:
            raise ResolverSetupError("plugin root mismatch")
        repo_root = _git_repo_root(cwd)
        version = read_pipeline_version(plugin_root)
        _, review_text = probe_review_md(repo_root)
        mode = "headless" if env.get("CODE_GAUNTLET_HEADLESS") == "1" else "interactive"
        result = resolve(mode, env, review_text, args.target)
        identity = {"pipeline_version": version, "plugin_root": plugin_root}
        block = render_block(mode, result["configEcho"], identity)
        payload = {
            "mode": mode,
            "target": args.target,
            "block": block,
            "waist": result["waist"],
            "resolved": result["resolved"],
            "identity": identity,
        }
        stdout = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        return 0, stdout, block + "\n"
    except ResolverError as exc:
        return 1, "", f"{exc}\n"
    except ResolverSetupError as exc:
        return 2, "", f"RESOLVER SETUP ERROR: {exc}\n"


def main() -> int:
    """Run the CLI, writing only the resolver payload to stdout."""
    code, stdout, stderr = run(sys.argv[1:], os.environ)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
