#!/usr/bin/env python3
"""Render persisted review findings as deterministic FIX-task payloads.

The input is the ``artifactPaths.postReview`` JSON artifact.  It may be the
persisted wrapper or the bare findings array.  The output is a JSON array for
the Phase 8 task-board flow.  Each finding becomes one object with ``subject``,
``description``, and ``metadata``.

Usage:
    python3 scripts/render_fix_tasks.py POST_REVIEW --repo-root REPO_ROOT

Non-goals
---------
This script does not create tasks, run a test, read source files, or infer
which findings are valid.  It only renders the delivered findings and selects
nearby tracked files as implementation patterns.  Its build-system table is
language-agnostic: it fills three command strings from fixed root-level config
precedence and degrades to placeholders when nothing is detected.  It never
filters, ranks, or reads findings by language.

Exit codes
----------
0 -- the payload was rendered and written to stdout.
1 -- a content, repository-root, or derived-data failure occurred; stdout is empty.
2 -- argparse rejected a malformed command.

No external Python dependencies are used.  Python 3.10 is supported, so this
module intentionally does not use ``tomllib`` or newer pattern-matching syntax.
"""

import argparse
import difflib
import json
import ntpath
import os
import posixpath
import re
import stat
import subprocess
import sys
from contextlib import suppress

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from script_io import write_result

# generated-from-registry-identity:detail_fields — do not edit; run scripts/generate_contract_requirements.py
_DETAIL_FIELDS_BY_DIMENSION = {
    "bug": ("hidden_errors",),
    "security": ("attack_vector",),
    "cross_file_impact": ("affected_consumers",),
    "test_coverage": (
        "criticality",
        "failure_scenario",
    ),
    "convention": ("claude_md_rule",),
    "intent": ("spec_text",),
    "comment_accuracy": (),
    "type_design": ("invalid_state_example",),
    "simplification": ("behavior_preserved",),
}
# /generated-from-registry-identity:detail_fields


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
COMMENT_RE = re.compile(r"<!--")
MAX_CONFIG_BYTES = 1024 * 1024


class ContentError(Exception):
    """A content failure that must not emit a stdout payload."""


def _one_line(value):
    """Normalize an untrusted structural value to one safe Markdown line."""
    text = str(value)
    text = CONTROL_RE.sub(" ", text)
    text = re.sub(r" +", " ", text).strip()
    return COMMENT_RE.sub("&lt;!--", text)


def _safe_prose(value):
    """Keep prose multiline while removing controls and forged structure."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character
        for character in text
        if character == "\n"
        or not (ord(character) < 32 or 0x7F <= ord(character) <= 0x9F)
    )
    text = COMMENT_RE.sub("&lt;!--", text)
    return HEADING_RE.sub(r"\1\\\2", text)


def _code_span(value):
    """Wrap value in a backtick run longer than every run inside it."""
    runs = re.findall(r"`+", value)
    fence = "`" * (max((len(run) for run in runs), default=0) + 1)
    padding = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def _fence(value):
    """Return a fenced evidence block with a safe delimiter length."""
    runs = re.findall(r"`+", value)
    fence = "`" * max(3, max((len(run) for run in runs), default=0) + 1)
    return f"{fence}\n{value}\n{fence}"


def _present(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _field(finding, canonical, optional=False):
    """Read canonical data first, then the persisted v2 alias."""
    if canonical in finding:
        return finding[canonical], True
    alias = OPTIONAL_ALIASES.get(canonical) if optional else ALIASES.get(canonical)
    if alias and alias in finding:
        return finding[alias], True
    return None, False


def _reject_json_constant(value):
    raise ValueError(f"non-standard JSON constant {value}")


def _load_findings(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle, parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContentError(f"could not read valid JSON from {path}: {exc}") from exc

    if isinstance(data, list):
        findings = data
    elif isinstance(data, dict) and isinstance(data.get("findings"), list):
        findings = data["findings"]
    else:
        raise ContentError(
            "post-review artifact must be a findings array or an object with a findings array"
        )

    normalized_findings = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ContentError(f"finding at index {index} is not an object")
        for field in REQUIRED_FIELDS:
            _, present = _field(finding, field)
            if not present:
                raise ContentError(
                    f"finding at index {index} lacks required key {field}"
                )
        normalized = dict(finding)
        for field in (*REQUIRED_FIELDS, "line_end"):
            value, present = _field(finding, field, optional=field == "line_end")
            if present and field not in normalized:
                normalized[field] = value
        normalized_findings.append(normalized)
    return normalized_findings


def _confined(target, root):
    return target == root or target.startswith(root + os.sep)


def _path_info(value, root):
    """Return (accepted, real path) for a repo-relative artifact path."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False, None
    if os.path.isabs(value) or ntpath.isabs(value) or ntpath.splitdrive(value)[0]:
        return False, None
    try:
        target = os.path.realpath(os.path.join(root, value))
    except (OSError, ValueError):
        return False, None
    if not _confined(target, root):
        return False, None
    return True, target


def _valid_cross_file_refs(finding, root):
    raw, present = _field(finding, "cross_file_refs", optional=True)
    if (
        not present
        or not isinstance(raw, list)
        or not all(isinstance(value, str) for value in raw)
    ):
        return [], 0
    accepted = []
    rejected = 0
    for value in raw:
        ok, _ = _path_info(value, root)
        if ok:
            accepted.append(value)
        else:
            rejected += 1
    return accepted, rejected


class SiblingIndex:
    """One cached root-level git listing used for all findings."""

    def __init__(self, root):
        self.paths = None
        self.error = None
        try:
            result = subprocess.run(
                ["git", "ls-files", "-z"],
                cwd=root,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.error = f"could not list tracked files: {type(exc).__name__}"
            return
        if result.returncode != 0:
            self.error = "could not list tracked files: git ls-files failed"
            return
        self.paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

    def siblings(self, file_path, delivered):
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
            candidates.append(
                (not same_extension, -ratio, candidate, candidate_normalized)
            )
        candidates.sort(key=lambda item: item[:3])
        return [item[2] for item in candidates[:2]]


def _candidate_names(root):
    names = ["package.json", "Cargo.toml", "go.mod", "pyproject.toml", "Makefile"]
    with suppress(OSError):
        names.extend(
            name
            for name in sorted(os.listdir(root))
            if name.endswith((".csproj", ".sln"))
        )
    return names


def _safe_candidate(root, name, notes):
    path = os.path.join(root, name)
    if not os.path.lexists(path):
        return None
    try:
        real = os.path.realpath(path)
        if not _confined(real, root):
            notes.append(f"toolchain candidate {name} rejected: outside repo root")
            return None
        mode = os.stat(real).st_mode
        if not stat.S_ISREG(mode):
            notes.append(f"toolchain candidate {name} rejected: not a regular file")
            return None
        if os.stat(real).st_size > MAX_CONFIG_BYTES:
            notes.append(f"toolchain candidate {name} rejected: larger than 1 MiB")
            return None
        with open(real, "rb") as handle:
            handle.read(1)
    except (OSError, ValueError):
        notes.append(f"toolchain candidate {name} rejected: unreadable")
        return None
    return real


def _package_command(scripts, prefix, default):
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


def _package_toolchain(path):
    defaults = {"test": "npm test", "lint": "npm run lint", "build": "npm run build"}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return defaults
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return defaults
    return {
        "test": _package_command(scripts, "test", defaults["test"]),
        "lint": _package_command(scripts, "lint", defaults["lint"]),
        "build": _package_command(scripts, "build", defaults["build"]),
    }


def _toolchain(root, notes):
    table = {
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


def _commit_scope(file_path):
    normalized = posixpath.normpath(file_path)
    directory = posixpath.dirname(normalized)
    if directory:
        return posixpath.basename(directory)
    return posixpath.splitext(posixpath.basename(normalized))[0]


def _detail_value(value):
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


def _details(finding):
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
        if value is not None:
            label = field.replace("_", " ")
            label = label[:1].upper() + label[1:]
            lines.append(f"**{label}:** {value}")
    return lines


def _render_description(finding, file_path, rejected, toolchain):
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
        sections.append(f"## Location\n{_code_span(_one_line(location))}")

    evidence = finding.get("evidence")
    if _present(evidence):
        sections.append(f"## Evidence\n{_fence(str(evidence))}")
    suggestion = finding.get("suggestion")
    if _present(suggestion):
        sections.append(f"## Suggested Fix\n{_safe_prose(suggestion)}")
    sections.append(
        "## Category\n"
        f"{_one_line(finding['severity'])} | {_one_line(finding['dimension'])}"
    )
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


def _task(finding, root, toolchain, sibling_index, delivered, rejected_count):
    file_path = finding["file"]
    accepted, _ = _path_info(file_path, root)
    refs, rejected_refs = _valid_cross_file_refs(finding, root)
    rejected = not accepted
    rejected_count += int(rejected) + rejected_refs

    if rejected:
        files_to_modify = []
        patterns = []
        commit_scope = "fix"
    elif finding["dimension"] == "test_coverage":
        files_to_modify = refs
        pattern_target = refs[0] if refs else file_path
        patterns = sibling_index.siblings(pattern_target, delivered + files_to_modify)
        commit_scope = _commit_scope(file_path)
    else:
        files_to_modify = [file_path]
        patterns = sibling_index.siblings(file_path, delivered)
        commit_scope = _commit_scope(file_path)

    finding_id = _one_line(finding["id"])
    title = _one_line(finding["title"])
    severity = _one_line(finding["severity"])
    dimension = _one_line(finding["dimension"])
    complexity = "standard" if severity.lower() in ("critical", "high") else "trivial"
    if severity.lower() not in ("medium", "low"):
        complexity = "standard"
    metadata = {
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
        "proof_artifacts": [],
        "verification": {"pre": [], "post": []},
        "commit": {"template": f"fix({commit_scope}): {title}"},
        "review_context": {
            "finding_id": finding["id"],
            "dimension": finding["dimension"],
            "confidence": finding["confidence"],
            "evidence": evidence if (evidence := finding.get("evidence", "")) else "",
            "cross_file_refs": refs,
            "blame_classification": finding.get("origin")
            if isinstance(finding.get("origin"), str)
            else "unknown",
        },
    }
    if not rejected:
        metadata["proof_artifacts"].append({"type": "file", "path": file_path})
    if toolchain is not None:
        metadata["proof_artifacts"].insert(
            0, {"type": "test", "command": toolchain["test"], "expected": "All pass"}
        )
        metadata["verification"]["pre"] = [
            command
            for command in (toolchain["lint"], toolchain["build"])
            if command is not None
        ]
        metadata["verification"]["post"] = [toolchain["test"]]
    return {
        "subject": f"FIX: {title}",
        "description": _render_description(finding, file_path, rejected, toolchain),
        "metadata": metadata,
    }, rejected_count


def build_tasks(findings, root, notes):
    toolchain = _toolchain(root, notes)
    sibling_index = SiblingIndex(root)
    if sibling_index.error:
        notes.append(sibling_index.error)
    delivered = [
        finding["file"] for finding in findings if _path_info(finding["file"], root)[0]
    ]
    tasks = []
    rejected_count = 0
    for finding in findings:
        task, rejected_count = _task(
            finding, root, toolchain, sibling_index, delivered, rejected_count
        )
        tasks.append(task)
    return tasks, rejected_count


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("post_review", metavar="POST_REVIEW")
    parser.add_argument("--repo-root", metavar="REPO_ROOT")
    return parser


def main(argv=None):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.repo_root is None:
        print("ERROR: --repo-root is required", file=sys.stderr)
        return 1
    root = os.path.realpath(args.repo_root)
    if not os.path.isdir(root):
        print(
            f"ERROR: --repo-root is not a directory: {args.repo_root}", file=sys.stderr
        )
        return 1
    try:
        findings = _load_findings(args.post_review)
        notes = []
        tasks, rejected_count = build_tasks(findings, root, notes)
        write_result(None, tasks)
    except Exception as exc:  # noqa: BLE001 - no content failure may leak a payload
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for note in notes:
        print(f"WARNING: {note}", file=sys.stderr)
    print(
        f"Rendered {len(tasks)} FIX tasks from {args.post_review} ({rejected_count} paths rejected)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
