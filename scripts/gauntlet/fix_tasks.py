#!/usr/bin/env python3
"""Render persisted review findings as deterministic FIX-task payloads."""

from __future__ import annotations

import argparse
import difflib
import ntpath
import os
import posixpath
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import NoReturn, TypedDict

from gauntlet import proc
from gauntlet.cli import CliError, Command, Parser, warn
from gauntlet.fs import JsonReadError, confined, read_json
from gauntlet.jsjson import write_result
from gauntlet.markdown import code_span, fence_run
from gauntlet.registry import (
    DETAIL_FIELDS_BY_DIMENSION as _DETAIL_FIELDS_BY_DIMENSION,
)
from gauntlet.registry import (
    RULE_SOURCE_LABEL_FALLBACK,
    RULE_SOURCE_LABELS,
    SEVERITY_EMOJI,
)
from gauntlet.text import neutralize_comment_openers, normalize_report_severity

REQUIRED_FIELDS = (
    "id",
    "file",
    "line_start",
    "title",
    "description",
    "severity",
    "confidence",
    "dimension",
)
ALIASES = {"line_start": "line", "description": "body"}
OPTIONAL_ALIASES = {"line_end": "end_line"}
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
HEADING_RE = re.compile(r"(?m)^([ ]{0,3})(#{1,6})(?= |$)")
MAX_CONFIG_BYTES = 1024 * 1024


class FixTaskWire(TypedDict):
    subject: str
    description: str
    metadata: dict[str, object]


def _one_line(value: object) -> str:
    text = str(value)
    text = CONTROL_RE.sub(" ", text)
    text = re.sub(r" +", " ", text).strip()
    return neutralize_comment_openers(text)


def _safe_prose(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character
        for character in text
        if character == "\n"
        or not (ord(character) < 32 or 0x7F <= ord(character) <= 0x9F)
    )
    text = neutralize_comment_openers(text)
    text = HEADING_RE.sub(r"\1\\\2", text)
    return re.sub(r"(?m)^([ ]{0,3})(=+|-+)[ \t]*$", r"\1\\\2", text)


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _field(
    finding: Mapping[str, object], canonical: str, optional: bool = False
) -> tuple[object, bool]:
    if canonical in finding:
        return finding[canonical], True
    alias = OPTIONAL_ALIASES.get(canonical) if optional else ALIASES.get(canonical)
    if alias and alias in finding:
        return finding[alias], True
    return None, False


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {value}")


def _load_findings(path: str) -> list[dict[str, object]]:
    try:
        data = read_json(path, parse_constant=_reject_json_constant)
    except JsonReadError as exc:
        raise CliError(f"could not read valid JSON from {path}: {exc.cause}") from exc

    if isinstance(data, list):
        findings = data
    elif isinstance(data, dict) and isinstance(data.get("findings"), list):
        findings = data["findings"]
    else:
        raise CliError(
            "post-review artifact must be a findings array or an object with a findings array"
        )

    normalized_findings = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise CliError(f"finding at index {index} is not an object")
        normalized = dict(finding)
        for field in (*REQUIRED_FIELDS, "line_end"):
            value, present = _field(finding, field, optional=field == "line_end")
            if present and field not in normalized:
                normalized[field] = value
        for field in REQUIRED_FIELDS:
            if field not in normalized:
                raise CliError(f"finding at index {index} lacks required key {field}")
        normalized_findings.append(normalized)
    return normalized_findings


def _path_info(value: object, root: str) -> tuple[bool, str | None]:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False, None
    if os.path.isabs(value) or ntpath.isabs(value) or ntpath.splitdrive(value)[0]:
        return False, None
    try:
        target = os.path.realpath(os.path.join(root, value))
    except (OSError, ValueError):
        return False, None
    if not confined(target, root):
        return False, None
    return True, target


def _valid_cross_file_refs(
    finding: Mapping[str, object], root: str
) -> tuple[list[str], int]:
    raw, present = _field(finding, "cross_file_refs", optional=True)
    if not present or not isinstance(raw, list):
        return [], 0
    accepted = []
    rejected = 0
    for value in raw:
        ok, _ = _path_info(value, root)
        if ok:
            assert isinstance(value, str)
            accepted.append(value)
        else:
            rejected += 1
    return accepted, rejected


class SiblingIndex:
    """Cache one root git listing for all findings."""

    def __init__(self, root: str) -> None:
        self.paths: list[str] | None = None
        self.error: str | None = None
        try:
            result = proc.run_bytes(["git", "ls-files", "-z"], cwd=root, timeout=10)
        except (OSError, proc.TimeoutExpired) as exc:
            self.error = f"could not list tracked files: {type(exc).__name__}"
            return
        if result.returncode != 0:
            self.error = "could not list tracked files: git ls-files failed"
            return
        self.paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

    def siblings(self, file_path: str, delivered: Sequence[str]) -> list[str]:
        if self.paths is None:
            return []
        normalized = posixpath.normpath(file_path)
        directory = posixpath.dirname(normalized)
        basename = posixpath.basename(normalized)
        extension = posixpath.splitext(basename)[1].lower()
        delivered_normalized = {posixpath.normpath(path) for path in delivered}
        candidates = []
        for candidate in self.paths:
            candidate_normalized = posixpath.normpath(candidate)
            if posixpath.dirname(candidate_normalized) != directory:
                continue
            if (
                candidate_normalized in delivered_normalized
                or candidate_normalized == normalized
            ):
                continue
            candidate_base = posixpath.basename(candidate_normalized)
            same_extension = posixpath.splitext(candidate_base)[1].lower() == extension
            ratio = difflib.SequenceMatcher(None, candidate_base, basename).ratio()
            candidates.append((not same_extension, -ratio, candidate))
        candidates.sort()
        return [item[2] for item in candidates[:2]]


def _candidate_names(root: str) -> list[str]:
    names = ["package.json", "Cargo.toml", "go.mod", "pyproject.toml", "Makefile"]
    with suppress(OSError):
        names.extend(
            name
            for name in sorted(os.listdir(root))
            if name.endswith((".csproj", ".sln"))
        )
    return names


def _safe_candidate(root: str, name: str, notes: list[str]) -> str | None:
    path = os.path.join(root, name)
    if not os.path.lexists(path):
        return None
    try:
        real = os.path.realpath(path)
        if not confined(real, root):
            notes.append(f"toolchain candidate {name} rejected: outside repo root")
            return None
        info = os.stat(real)
        if not stat.S_ISREG(info.st_mode):
            notes.append(f"toolchain candidate {name} rejected: not a regular file")
            return None
        if info.st_size > MAX_CONFIG_BYTES:
            notes.append(f"toolchain candidate {name} rejected: larger than 1 MiB")
            return None
        with open(real, "rb") as handle:
            handle.read(1)
    except (OSError, ValueError):
        notes.append(f"toolchain candidate {name} rejected: unreadable")
        return None
    return real


def _package_command(scripts: Mapping[str, object], prefix: str, default: str) -> str:
    valid = sorted(
        name
        for name in scripts
        if isinstance(name, str) and SCRIPT_NAME_RE.fullmatch(name)
    )
    if prefix == "test" and "test" in valid:
        return "npm test"
    if prefix in ("lint", "build") and prefix in valid:
        return f"npm run {prefix}"
    matching = [name for name in valid if name.startswith(prefix)]
    if matching:
        return f"npm run {matching[0]}"
    return default


def _package_toolchain(path: str) -> dict[str, str]:
    defaults = {"test": "npm test", "lint": "npm run lint", "build": "npm run build"}
    try:
        data = read_json(path)
    except JsonReadError:
        return defaults
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return defaults
    return {
        "test": _package_command(scripts, "test", defaults["test"]),
        "lint": _package_command(scripts, "lint", defaults["lint"]),
        "build": _package_command(scripts, "build", defaults["build"]),
    }


def _toolchain(root: str, notes: list[str]) -> Mapping[str, str | None] | None:
    table: dict[str, dict[str, str | None]] = {
        "Cargo.toml": {
            "test": "cargo test",
            "lint": "cargo clippy",
            "build": "cargo build",
        },
        "go.mod": {
            "test": "go test ./...",
            "lint": "golangci-lint run",
            "build": "go build ./...",
        },
        "pyproject.toml": {"test": "pytest", "lint": "ruff check .", "build": None},
        "Makefile": {"test": "make test", "lint": "make lint", "build": "make build"},
    }
    for name in _candidate_names(root):
        path = _safe_candidate(root, name, notes)
        if path is None:
            continue
        if name == "package.json":
            return _package_toolchain(path)
        if name in table:
            return table[name]
        return {
            "test": "dotnet test",
            "lint": "dotnet format --verify-no-changes",
            "build": "dotnet build",
        }
    return None


def _commit_scope(file_path: str) -> str:
    normalized = posixpath.normpath(file_path)
    directory = posixpath.dirname(normalized)
    if directory:
        return posixpath.basename(directory)
    return posixpath.splitext(posixpath.basename(normalized))[0]


def _detail_value(value: object) -> str | None:
    if isinstance(value, list):
        if not value:
            return None
        return ", ".join(_one_line(item) for item in value)
    if isinstance(value, str):
        if not value.strip():
            return None
        return _one_line(value)
    if value is None:
        return None
    return _one_line(value)


def _details(finding: Mapping[str, object]) -> list[str]:
    dimension = finding.get("dimension")
    fields = (
        _DETAIL_FIELDS_BY_DIMENSION.get(dimension, ())
        if isinstance(dimension, str)
        else ()
    )
    lines = []
    for field in fields:
        if field not in finding:
            continue
        value = _detail_value(finding[field])
        if field == "rule_source":
            continue
        if value is None:
            continue
        if field == "claude_md_rule":
            source = finding.get("rule_source")
            label = (
                RULE_SOURCE_LABELS.get(source, RULE_SOURCE_LABEL_FALLBACK)
                if isinstance(source, str)
                else RULE_SOURCE_LABEL_FALLBACK
            )
        else:
            label = field.replace("_", " ")
        label = label[:1].upper() + label[1:]
        lines.append(f"**{label}:** {value}")
    return lines


def _render_description(
    finding: Mapping[str, object],
    file_path: object,
    rejected: bool,
    toolchain: Mapping[str, str | None] | None,
    severity: str,
) -> str:
    sections = [f"## Issue\n{_safe_prose(finding['description'])}"]
    if rejected:
        sections.append("## Location\n(path rejected: outside repo root)")
    else:
        line_start = finding["line_start"]
        line_end = finding.get("line_end")
        if line_end is None or line_end == line_start:
            location = f"{file_path}:{line_start}"
        else:
            location = f"{file_path}:{line_start}-{line_end}"
        sections.append(f"## Location\n{code_span(_one_line(location))}")

    evidence = finding.get("evidence")
    if _present(evidence):
        fence = fence_run(str(evidence))
        sections.append(f"## Evidence\n{fence}\n{evidence}\n{fence}")
    suggestion = finding.get("suggestion")
    if _present(suggestion):
        sections.append(f"## Suggested Fix\n{_safe_prose(suggestion)}")
    sections.append(f"## Category\n{severity} | {_one_line(finding['dimension'])}")
    detail_lines = _details(finding)
    if detail_lines:
        details = "\n".join(detail_lines)
        sections.append(f"## Details\n{details}")
    if toolchain is None:
        sections.append(
            "## Toolchain\n"
            "Not detected. Set the test, lint, and build commands before running this task."
        )
    return "\n\n".join(sections)


def _task(
    finding: Mapping[str, object],
    root: str,
    toolchain: Mapping[str, str | None] | None,
    sibling_index: SiblingIndex,
    delivered: Sequence[str],
    rejected_count: int,
) -> tuple[FixTaskWire, int]:
    file_path = finding["file"]
    accepted, _ = _path_info(file_path, root)
    refs, rejected_refs = _valid_cross_file_refs(finding, root)
    rejected = not accepted
    rejected_count += int(rejected) + rejected_refs

    if rejected:
        files_to_modify = []
        patterns = []
    else:
        assert isinstance(file_path, str)
        if finding["dimension"] == "test_coverage":
            files_to_modify = refs
            pattern_target = refs[0] if refs else file_path
            patterns = sibling_index.siblings(
                pattern_target, [*delivered, *files_to_modify]
            )
        else:
            files_to_modify = [file_path]
            patterns = sibling_index.siblings(file_path, delivered)
    commit_scope = "fix" if rejected else _commit_scope(str(file_path))

    finding_id = _one_line(finding["id"])
    title = _one_line(finding["title"])
    severity = normalize_report_severity(finding["severity"], SEVERITY_EMOJI)
    dimension = _one_line(finding["dimension"])
    complexity = "trivial" if severity in ("medium", "low") else "standard"
    proof_artifacts: list[dict[str, object]] = []
    verification: dict[str, list[str | None]] = {"pre": [], "post": []}
    metadata: dict[str, object] = {
        "task_type": "review-fix",
        "task_id": f"FIX-{finding_id}",
        "category": dimension,
        "severity": severity,
        "role": "implementer",
        "complexity": complexity,
        "model": "haiku" if complexity == "trivial" else "sonnet",
        "scope": {
            "files_to_create": [],
            "files_to_modify": files_to_modify,
            "patterns_to_follow": patterns,
        },
        "requirements": [
            {
                "id": f"R-{finding_id}.1",
                "text": finding["description"],
                "testable": True,
            }
        ],
        "proof_artifacts": proof_artifacts,
        "verification": verification,
        "commit": {"template": f"fix({commit_scope}): {title}"},
        "review_context": {
            "finding_id": finding["id"],
            "dimension": finding["dimension"],
            "confidence": finding["confidence"],
            "evidence": finding.get("evidence") or "",
            "cross_file_refs": refs,
            "blame_classification": finding.get("origin")
            if isinstance(finding.get("origin"), str)
            else "unknown",
        },
    }
    if not rejected:
        proof_artifacts.append({"type": "file", "path": file_path})
    if toolchain is not None:
        proof_artifacts.insert(
            0, {"type": "test", "command": toolchain["test"], "expected": "All pass"}
        )
        verification["pre"] = [
            command
            for command in (toolchain["lint"], toolchain["build"])
            if command is not None
        ]
        verification["post"] = [toolchain["test"]]
    return {
        "subject": f"FIX: {title}",
        "description": _render_description(
            finding, file_path, rejected, toolchain, severity
        ),
        "metadata": metadata,
    }, rejected_count


def build_tasks(
    findings: Sequence[Mapping[str, object]],
    root: str,
    notes: list[str],
) -> tuple[list[FixTaskWire], int]:
    toolchain = _toolchain(root, notes)
    sibling_index = SiblingIndex(root)
    if sibling_index.error:
        notes.append(sibling_index.error)
    delivered = []
    for finding in findings:
        file_path = finding["file"]
        if _path_info(file_path, root)[0]:
            assert isinstance(file_path, str)
            delivered.append(file_path)
    tasks = []
    rejected_count = 0
    for finding in findings:
        task, rejected_count = _task(
            finding, root, toolchain, sibling_index, delivered, rejected_count
        )
        tasks.append(task)
    return tasks, rejected_count


def build_parser() -> Parser:
    parser = Parser(
        prog="render_fix_tasks",
        description="Render persisted review findings as deterministic FIX-task payloads.",
    )
    parser.add_argument("post_review", metavar="POST_REVIEW")
    parser.add_argument("--repo-root", metavar="REPO_ROOT", required=True)
    return parser


def _handle(args: argparse.Namespace) -> int:
    root = os.path.realpath(args.repo_root)
    if not os.path.isdir(root):
        raise CliError(f"--repo-root is not a directory: {args.repo_root}")
    try:
        findings = _load_findings(args.post_review)
        notes: list[str] = []
        tasks, rejected_count = build_tasks(findings, root, notes)
        write_result(tasks)
    except Exception as exc:
        raise CliError(str(exc)) from exc
    for note in notes:
        warn(note)
    task_label = "task" if len(tasks) == 1 else "tasks"
    path_label = "path" if rejected_count == 1 else "paths"
    print(
        f"Rendered {len(tasks)} FIX {task_label} from {args.post_review} "
        f"({rejected_count} {path_label} rejected)",
        file=sys.stderr,
    )
    return 0


CLI = Command(parser=build_parser(), main=_handle)
