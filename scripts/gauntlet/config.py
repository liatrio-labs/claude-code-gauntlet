#!/usr/bin/env python3
"""Resolve configuration and preserve the machine-parsed receipt blocks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser
from gauntlet.fs import read_text
from gauntlet.jsjson import JS_MAX_SAFE_INTEGER
from gauntlet.paths import PLUGIN_ROOT
from gauntlet.registry import JS_TRIM_CHARS, KNOB_REGISTRY


class ConfigEchoEntry(TypedDict):
    value: str
    source: str


_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f]")
_CANDIDATE_RE = re.compile(r"^[a-z_]+(,[a-z_]+)*$")
_PIPELINE_VERSION_RE = re.compile(
    r"(?m)^[ \t]*const[ \t]+PIPELINE_VERSION[ \t]*=[ \t]*"
    r"(['\"])([^'\"\r\n]+)\1[ \t]*;?[ \t]*(?://[^\r\n]*)?$"
)


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
        return 0 <= number <= JS_MAX_SAFE_INTEGER
    except (TypeError, ValueError):
        return False


def matches_rule(rule: Any, value: object, mode: str) -> bool:
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


def _error_value(value: object) -> str:
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            return json.dumps(value, ensure_ascii=False)
        return value
    return str(value)


def _invalid_message(
    row: Mapping[str, Any],
    value: object,
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


def _typed_value(row: Mapping[str, Any], value: str) -> object:
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
    return list(KNOB_REGISTRY)


def serialize_receipt(
    mode: str,
    config_echo: Mapping[str, object],
    *,
    registry: Sequence[Mapping[str, Any]] | None = None,
) -> str:
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
    rows = _registry_rows(registry)
    if mode not in {"headless", "interactive"}:
        raise ResolverSetupError(f"unsupported mode: {mode}")
    if target not in {None, "pr", "mr", "local"}:
        raise ResolverSetupError(f"unsupported target: {target}")

    review_value = parse_default_delivery(review_md_text)
    config_echo: dict[str, ConfigEchoEntry] = {}
    resolved: dict[str, object] = {}
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
        if row.get("resolvedKey"):
            resolved[key] = _typed_value(row, str(value))
    return {
        "configEcho": config_echo,
        "waist": {"configEcho": config_echo},
        "resolved": resolved,
    }


def _receipt_as_text(value: object) -> str:
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


def one_line(value: object) -> str:
    text = _receipt_as_text(value)
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r" +", " ", text)
    return text.strip(JS_TRIM_CHARS)


def receipt_safe(value: object) -> str:
    text = one_line(value).replace("`", "")
    return text or "unknown"


def render_block(
    mode: str,
    config_echo: Mapping[str, object],
    identity: Mapping[str, object],
    *,
    registry: Sequence[Mapping[str, Any]] | None = None,
) -> str:
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
    if not isinstance(text, str):
        return None
    body = _default_delivery_body(re.split(r"\r\n|\r|\n", text))
    if body is None:
        return None
    cleaned = re.sub(r"<!--.*?-->", "", "\n".join(body), flags=re.DOTALL)
    candidate = next(
        (line.strip() for line in re.split(r"\r\n|\r|\n", cleaned) if line.strip()),
        None,
    )
    if candidate is None or not _CANDIDATE_RE.fullmatch(candidate):
        return None
    return candidate


def _default_delivery_body(lines: Sequence[str]) -> list[str] | None:
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
    return body


def _git_repo_root(cwd: str) -> str:
    try:
        result = proc.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
        )
    except OSError as exc:
        raise ResolverSetupError(f"git repository probe failed: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ResolverSetupError("not a git repository")
    return os.path.realpath(result.stdout.strip())


def read_pipeline_version(plugin_root: str) -> str:
    path = os.path.join(plugin_root, "workflows", "pipeline.js")
    try:
        text = read_text(path)
    except OSError as exc:
        raise ResolverSetupError(f"cannot read pipeline bundle: {exc}") from exc
    match = _PIPELINE_VERSION_RE.search(text)
    if match is None:
        raise ResolverSetupError("PIPELINE_VERSION not found in workflow bundle")
    return match.group(2)


def probe_review_md(repo_root: str) -> str | None:
    path = os.path.join(repo_root, "REVIEW.md")
    if not os.path.isfile(path):
        return None
    try:
        return read_text(path)
    except OSError as exc:
        raise ResolverSetupError(f"cannot read root REVIEW.md: {exc}") from exc


def build_parser() -> Parser:
    parser = Parser(
        prog="resolve_config",
        add_help=False,
        description="Resolve the code-gauntlet configuration.",
    )
    parser.add_argument("--target", choices=("pr", "mr", "local"), default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--plugin-root", default=None)
    return parser


def _handle(args: argparse.Namespace) -> int:
    try:
        cwd = os.path.realpath(args.cwd or os.getcwd())
        plugin_root = os.path.realpath(args.plugin_root or PLUGIN_ROOT)
        if args.plugin_root is not None and plugin_root != PLUGIN_ROOT:
            raise ResolverSetupError("plugin root mismatch")
        repo_root = _git_repo_root(cwd)
        version = read_pipeline_version(plugin_root)
        review_text = probe_review_md(repo_root)
        mode = (
            "headless"
            if os.environ.get("CODE_GAUNTLET_HEADLESS") == "1"
            else "interactive"
        )
        result = resolve(mode, os.environ, review_text, args.target)
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
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        sys.stderr.write(block + "\n")
        return 0
    except ResolverError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except ResolverSetupError as exc:
        raise CliError(str(exc), 2) from exc


CLI = Command(parser=build_parser(), main=_handle)
