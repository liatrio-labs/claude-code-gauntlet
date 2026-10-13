"""The narrow YAML reader fails closed so unreadable forms cannot pass vacuously.

Stdlib only: CI installs pytest and nothing else.
"""

import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, TypeAlias
from unittest.mock import patch

import pytest
from gauntlet import contract_gen as contract_gen

from tests.support import generator_inputs
from tests.support.repository import copy_generator_tree

REPO = Path(__file__).resolve().parents[1]
FORMS = REPO / ".github" / "ISSUE_TEMPLATE"
LABELS_DIFF = REPO / ".github" / "labels_diff.py"
BIOME_ASSETS = {
    "biome-darwin-arm64",
    "biome-darwin-x64",
    "biome-linux-arm64",
    "biome-linux-arm64-musl",
    "biome-linux-x64",
    "biome-linux-x64-musl",
    "biome-win32-arm64.exe",
    "biome-win32-x64.exe",
}
ADVISORY_URL = (
    "https://github.com/liatrio-labs/claude-code-gauntlet/security/advisories/new"
)
REQUIRED_TRIAGE_LABELS = {"needs-triage", "work-queue"}
REQUIRED_TOPIC_LABELS = {
    "determinism",
    "benchmarking",
    "reliability",
    "bench",
    "latency",
    "policy",
    "tooling",
    "process",
    "verify-boundary",
}
REQUIRED_AREA_LABELS = {
    "area:workflows",
    "area:agents",
    "area:scripts",
    "area:skills",
    "area:bench",
    "area:docs",
    "area:ci",
}
LABEL_CATEGORIES = {"type", "state", "resolution", "outreach", "topic", "area"}
SINGULAR_AREA_DRIFT = re.compile(r"area:(workflow|skill|script|doc|agent)(?![a-z])")
FORM_TOP_LEVEL_KEYS = {
    "name",
    "description",
    "title",
    "labels",
    "assignees",
    "projects",
    "type",
    "body",
}
FORM_FIELD_TYPES = {"markdown", "input", "textarea", "dropdown", "checkboxes"}
FORM_ITEM_KEYS = {"type", "id", "attributes", "validations"}
FORM_TOP_LEVEL_REQUIRED = {"name", "description", "body"}
# Unknown per-type attributes make forms fail validation rather than being ignored.
FORM_ATTRIBUTES = {
    "markdown": {"value"},
    "input": {"label", "description", "placeholder", "value"},
    "textarea": {"label", "description", "placeholder", "value", "render"},
    "dropdown": {"label", "description", "multiple", "options", "default"},
    "checkboxes": {"label", "description", "options"},
}
FORM_TYPES_WITHOUT_VALIDATIONS = {"markdown"}
FORM_TYPES_WITH_OPTIONS = {"dropdown", "checkboxes"}
FORM_TYPES_WITH_MAPPING_OPTIONS = {"checkboxes"}
# GitHub rejects empty, boolean-like and reserved None dropdown options.
RESERVED_OPTIONS = {"none", "true", "false", "yes", "no", "on", "off"}


def _read(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def _pre_commit_hook_span(text: str, hook_id: str) -> tuple[int, int]:
    marker = re.compile(rf"(?m)^      - id: {re.escape(hook_id)}\s*$")
    matches = list(marker.finditer(text))
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {hook_id} hook block, found {len(matches)}"
        )
    start = matches[0].start()
    following = re.search(r"(?m)^      - id: [^\n]+$", text[matches[0].end() :])
    end = matches[0].end() + following.start() if following else len(text)
    return start, end


def _pre_commit_hook_block(text: str, hook_id: str) -> str:
    start, end = _pre_commit_hook_span(text, hook_id)
    return text[start:end]


def _pre_commit_repo_block(text: str, hook_id: str) -> str:
    hook_start, _ = _pre_commit_hook_span(text, hook_id)
    repo_markers = list(re.finditer(r"(?m)^  - repo:\s*[^\n]*$", text))
    enclosing = []
    for index, marker in enumerate(repo_markers):
        end = (
            repo_markers[index + 1].start()
            if index + 1 < len(repo_markers)
            else len(text)
        )
        if marker.start() < hook_start < end:
            enclosing.append(text[marker.start() : end])
    if len(enclosing) != 1:
        raise AssertionError(
            f"expected exactly one enclosing repo block for {hook_id}, found {len(enclosing)}"
        )
    return enclosing[0]


def _pre_commit_hook_value(block: str, key: str) -> str:
    matches = re.findall(rf"(?m)^        {re.escape(key)}:\s*(.*?)\s*$", block)
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one {key}: key in hook block, found {len(matches)}"
        )
    return _unquote(matches[0])


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _form_files() -> list[Path]:
    return sorted(
        p
        for p in list(FORMS.glob("*.yml")) + list(FORMS.glob("*.yaml"))
        if p.name not in {"config.yml", "config.yaml"}
    )


def _form_id(path: Path) -> str:
    return path.stem


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _sequence_after(lines: list[str], start: int, indent: int) -> list[str]:
    # Mapping continuation keys must not truncate option sequences silently.
    items = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) > indent:
            continue
        if _indent(line) < indent or not line.lstrip(" ").startswith("- "):
            break
        items.append(_unquote(line.lstrip(" ")[2:]))
    return items


def _form_labels(text: str) -> list[str]:
    # An unreadable label sequence must fail closed; an empty result would make
    # membership checks vacuous.
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("labels:"):
            continue
        inline = line[len("labels:") :].strip()
        if inline.startswith("[") and inline.endswith("]"):
            return [_unquote(part) for part in inline[1:-1].split(",") if part.strip()]
        if inline:
            raise ValueError(f"unsupported labels: scalar {inline!r}")
        items = _sequence_after(lines, index + 1, indent=2)
        if not items:
            raise ValueError("labels: block sequence is empty or unreadable")
        return items
    return []


def _single_key(block: list[str], name: str) -> str | None:
    # GitHub permits key reordering, but duplicate keys must fail closed.
    found = [line for line in block if re.match(rf"^    {name}:", line)]
    if len(found) > 1:
        raise ValueError(f"duplicate {name}: keys in one body item: {found}")
    return _unquote(found[0].split(":", 1)[1]) if found else None


def _form_fields(text: str) -> list[dict[str, Any]]:
    # Checkbox mappings must yield label text so empty and reserved values remain
    # detectable.
    # Only sibling validations count; nested validations and description prose cannot
    # establish a requirement.
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^  - \w+:", line)]
    fields = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        block[0] = "    " + block[0].lstrip(" ")[2:]
        item_keys: list[str] = []
        attributes: dict[str, str | None] = {}
        options: list[str] | None = None
        validations: dict[str, str] = {}
        section: str | None = None
        for index, line in enumerate(block):
            if not line.strip():
                continue
            key_match = re.match(r"^    (\w+):(.*)$", line)
            if key_match:
                item_keys.append(key_match.group(1))
                section = key_match.group(1)
                continue
            sub_match = re.match(r"^      (\w+):(.*)$", line)
            if sub_match and section == "attributes":
                name, inline = sub_match.group(1), sub_match.group(2).strip()
                attributes[name] = _unquote(inline) if inline else None
                if name == "options" and not inline:
                    options = _sequence_after(block, index + 1, indent=8)
            elif sub_match and section == "validations":
                validations[sub_match.group(1)] = sub_match.group(2).strip()
        field_type = _single_key(block, "type")
        if field_type in FORM_TYPES_WITH_MAPPING_OPTIONS and options is not None:
            options = [re.sub(r"^label:\s*", "", option) for option in options]
        fields.append(
            {
                "type": field_type,
                "id": _single_key(block, "id"),
                "keys": item_keys,
                "attributes": attributes,
                "options": options,
                "required": validations.get("required") == "true",
            }
        )
    return fields


def _field(form_name: str, field_id: str) -> dict[str, Any]:
    form = next(path for path in _form_files() if path.stem == Path(form_name).stem)
    for field in _form_fields(form.read_text(encoding="utf-8")):
        if field["id"] == field_id:
            return field
    raise AssertionError(f"missing {field_id} in {form_name}")


def _contact_links(text: str) -> list[tuple[str, str]]:
    links: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if re.match(r"^  - name:", line):
            current = {"name": _unquote(line.split(":", 1)[1])}
            links.append(current)
        elif current is not None:
            match = re.match(r"^    (\w+):(.*)$", line)
            if match:
                current[match.group(1)] = _unquote(match.group(2))
    return [(link.get("name", ""), link.get("url", "")) for link in links]


def _contributing_phases() -> list[str]:
    architecture = (
        _read("CONTRIBUTING.md").split("\n## Architecture", 1)[1].split("\n## ", 1)[0]
    )
    phases = [
        f"Phase {number} — {name}"
        for number, name in re.findall(r"^(\d+)\. ([^:\n]+):", architecture, re.M)
    ]
    if not phases:
        raise ValueError(
            "CONTRIBUTING Architecture section no longer lists phases as `N. Name:`"
        )
    return phases


def test_bug_form_phase_options_match_contributing_architecture() -> None:
    options = _field("bug_report.yml", "phase")["options"]
    assert [
        option for option in options if option.startswith("Phase ")
    ] == _contributing_phases()


def _labels() -> list[dict[str, str]]:
    labels: list[dict[str, str]] = json.loads(_read(".github/labels.json"))["labels"]
    return labels


def test_taxonomy_covers_the_required_label_set() -> None:
    labels = _labels()
    names = {label["name"] for label in labels}
    missing = (
        REQUIRED_TRIAGE_LABELS | REQUIRED_TOPIC_LABELS | REQUIRED_AREA_LABELS
    ) - names
    assert missing == set(), f"labels.json is missing {sorted(missing)}"
    area = {label["name"] for label in labels if label["category"] == "area"}
    assert area == REQUIRED_AREA_LABELS


def test_taxonomy_entries_are_well_formed_and_sorted() -> None:
    # The labels API rejects descriptions over 100 characters instead of truncating
    # them.
    # A leading dash would be parsed as a flag by the sync recipe.
    labels = _labels()
    names = [label["name"] for label in labels]
    assert names == sorted(names), "labels.json entries must be name-sorted"
    assert len(names) == len(set(names)), "duplicate label names in labels.json"
    for label in labels:
        assert set(label) == {"name", "color", "description", "category"}
        assert re.search(r"^[0-9a-f]{6}$", label["color"]), (
            "color must be 6-digit lowercase hex, no leading '#'"
        )
        assert label["description"].strip(), "description must not be empty"
        assert len(label["description"]) <= 100
        assert label["category"] in LABEL_CATEGORIES
        assert not label["name"].startswith("-"), "label name must not start with '-'"
        assert not label["description"].startswith("-")


def test_no_singular_area_label_drift_in_contribution_surface() -> None:
    offenders = {}
    candidates = [
        path
        for path in list((REPO / ".github").rglob("*"))
        + list((REPO / "docs").rglob("*"))
        if path.is_file() and path.suffix in {".md", ".yml", ".yaml", ".json"}
    ] + [
        REPO / name
        for name in ("CONTRIBUTING.md", "README.md", "SECURITY.md", "AGENTS.md")
    ]
    for path in candidates:
        hits = sorted(
            set(SINGULAR_AREA_DRIFT.findall(path.read_text(encoding="utf-8")))
        )
        if hits:
            offenders[str(path.relative_to(REPO))] = hits
    assert offenders == {}, f"singular area-label drift: {offenders}"


def _as_live(labels: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "name": label["name"],
            "color": label["color"].upper(),
            "description": label["description"],
        }
        for label in labels
    ]


def _run_labels(
    *args: str, live: list[dict[str, str]] | None = None
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        argv = [sys.executable, str(LABELS_DIFF), *args]
        if live is not None:
            path = Path(tmp) / "live.json"
            path.write_text(json.dumps(live), encoding="utf-8")
            argv += ["--live", str(path)]
        return subprocess.run(
            argv, cwd=REPO, capture_output=True, text=True, encoding="utf-8"
        )


@pytest.mark.parametrize(
    "repo",
    [
        pytest.param(None, id="infer-repository"),
        pytest.param("acme/widgets", id="explicit-repository"),
    ],
)
def test_labels_commands(repo: str | None) -> None:
    args = ["--commands"]
    if repo is not None:
        args += ["--repo", repo]
    result = _run_labels(*args)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    labels = _labels()
    assert len(lines) == len(labels)
    emitted = {}
    for line in lines:
        argv = shlex.split(line)
        assert argv[:3] == ["gh", "label", "create"]
        assert "--force" in argv
        if repo is None:
            assert "--repo" not in argv
        else:
            assert "--repo acme/widgets" in line
            assert argv[argv.index("--repo") + 1] == "acme/widgets"
        emitted[argv[3]] = argv[argv.index("--description") + 1]
    assert emitted == {label["name"]: label["description"] for label in labels}


@pytest.mark.parametrize(
    "live,status,expected,absent,commands",
    [
        pytest.param(
            _as_live(_labels()),
            0,
            ("in sync",),
            (),
            None,
            id="uppercase-colors-are-in-sync",
        ),
        pytest.param(
            [
                _as_live(_labels())[1] | {"description": "stale description"},
                *_as_live(_labels())[2:],
                {
                    "name": "hand-made",
                    "color": "ffffff",
                    "description": "not managed here",
                },
            ],
            1,
            ("missing: area:agents", "diverging: area:bench", "unmanaged", "hand-made"),
            (),
            {"area:agents", "area:bench"},
            id="missing-and-diverging-with-unmanaged",
        ),
        pytest.param(
            [
                *_as_live(_labels()),
                {
                    "name": "hand-made",
                    "color": "ffffff",
                    "description": "not managed here",
                },
            ],
            0,
            ("unmanaged", "hand-made", "in sync"),
            ("missing:", "diverging:"),
            None,
            id="unmanaged-only-is-informational",
        ),
    ],
)
def test_labels_drift(
    live: list[dict[str, str]],
    status: int,
    expected: tuple[str, ...],
    absent: tuple[str, ...],
    commands: set[str] | None,
) -> None:
    result = _run_labels(live=live)
    assert result.returncode == status, result.stdout + result.stderr
    for fragment in expected:
        assert fragment in result.stdout
    for fragment in absent:
        assert fragment not in result.stdout
    if commands is not None:
        narrowed = _run_labels("--commands", live=live).stdout.strip().splitlines()
        assert {shlex.split(line)[3] for line in narrowed} == commands


def _live_pages() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    live = _as_live(_labels())
    half = len(live) // 2
    return live[:half], live[half:]


@pytest.mark.parametrize(
    "payload,status,stdout_fragments,stderr_fragment",
    [
        pytest.param(
            b"\n".join(json.dumps(page).encode("utf-8") for page in _live_pages()),
            0,
            (b"in sync",),
            b"",
            id="concatenated-pagination",
        ),
        pytest.param(
            json.dumps(_live_pages()).encode("utf-8"),
            0,
            (b"in sync",),
            b"",
            id="nested-pagination",
        ),
        pytest.param(
            json.dumps(
                [
                    _as_live(_labels())[0]
                    | {"description": "crème brûlée — live label"},
                    *_as_live(_labels())[1:],
                ],
                ensure_ascii=False,
            ).encode("utf-8"),
            1,
            (b"diverging: area:agents", "crème brûlée".encode()),
            "—".encode(),
            id="utf8-cli-with-cp1252-stdio",
        ),
    ],
)
def test_labels_accepted_input(
    payload: bytes,
    status: int,
    stdout_fragments: tuple[bytes, ...],
    stderr_fragment: bytes,
) -> None:
    result = subprocess.run(
        [sys.executable, str(LABELS_DIFF), "--live", "-"],
        cwd=REPO,
        env=dict(os.environ, PYTHONIOENCODING="cp1252"),
        input=payload,
        capture_output=True,
    )
    assert result.returncode == status, result.stdout + result.stderr
    for fragment in stdout_fragments:
        assert fragment in result.stdout
    assert stderr_fragment in result.stderr


def test_labels_loader_reads_utf8_bytes_with_cp1252_text_wrapper() -> None:
    spec = importlib.util.spec_from_file_location("labels_diff_test", LABELS_DIFF)
    assert spec is not None and spec.loader is not None
    labels_diff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(labels_diff)
    payload = '[{"name":"review","color":"ffffff","description":"café"}]'.encode()
    stream = io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252")
    with patch.object(labels_diff.sys, "stdin", stream):
        labels = labels_diff.load_live("-")
    assert labels[0]["description"] == "café"


RefusedInputRow: TypeAlias = (
    tuple[str, tuple[str, ...], bytes | None, bytes | None]
    | tuple[str, tuple[str, ...], bytes | None, bytes | None, str, bool]
)
manifest_commands = ("--manifest", "{path}", "--commands")
stdin_response = ("--live", "-")
REFUSED_LABEL_INPUTS: tuple[RefusedInputRow, ...] = (
    *(
        (f"manifest-{name}", manifest_commands, None, payload)
        for name, payload in (
            ("not-json", b"{oops"),
            ("no-labels-key", b'{"tags": []}'),
            ("labels-is-mapping", b'{"labels": {"bug": "d73a4a"}}'),
            ("empty-labels", b'{"labels": []}'),
            ("string-entry", b'{"labels": ["bug"]}'),
            ("missing-color", b'{"labels": [{"name": "bug", "description": "x"}]}'),
        )
    ),
    (
        "manifest-live-not-json",
        ("--manifest", "{path}", "--live", "{path}"),
        None,
        b"{oops",
    ),
    ("live-required", (), None, None),
    ("empty-response", stdin_response, b"", None),
    (
        "api-error-object",
        stdin_response,
        b'{"message": "Not Found", "status": "404"}',
        None,
    ),
    ("names-only", stdin_response, b'["bug", "enhancement"]', None),
    ("non-json-response", stdin_response, b"gh: command not found", None),
    (
        "missing-manifest-file",
        ("--manifest", "/nonexistent/labels.json", "--commands"),
        None,
        None,
    ),
    ("missing-live-file", ("--live", "/nonexistent/live.json"), None, None),
    (
        "non-utf8-stdin",
        stdin_response,
        b'[{"name": "caf\xe9"}]',
        None,
        "not UTF-8",
        False,
    ),
    (
        "non-utf8-live-file",
        ("--live", "{path}"),
        None,
        b'[{"name": "caf\xe9"}]',
        "not UTF-8",
        False,
    ),
    (
        "non-utf8-manifest",
        manifest_commands,
        None,
        b'[{"name": "caf\xe9"}]',
        "not UTF-8",
        False,
    ),
    (
        "name-is-flag",
        manifest_commands,
        None,
        b'{"labels":[{"name":"-wip","color":"ffffff",'
        b'"description":"leading dash in name"}]}',
        "",
        True,
    ),
    (
        "description-is-flag",
        manifest_commands,
        None,
        b'{"labels":[{"name":"ok","color":"ffffff",'
        b'"description":"-leading dash in description"}]}',
        "",
        True,
    ),
    (
        "color-injection",
        manifest_commands,
        None,
        b'{"labels":[{"name":"ok","color":"fff; echo pwned","description":"not hex"}]}',
        "",
        True,
    ),
)


def _refused_input(
    row: RefusedInputRow,
) -> tuple[tuple[str, ...], bytes | None, bytes | None, str, bool]:
    if len(row) == 4:
        return row[1], row[2], row[3], "", False
    return row[1], row[2], row[3], row[4], row[5]


# Exit 1 means drift; corrupt manifests must exit 2 rather than emit a false report.
# Each reader needs its own UTF-8 refusal so a decode traceback cannot read as drift.
@pytest.mark.parametrize(
    "argv,stdin,file_bytes,diagnostic,empty_output",
    [pytest.param(*_refused_input(row), id=row[0]) for row in REFUSED_LABEL_INPUTS],
)
def test_labels_refused_input(
    tmp_path: Path,
    argv: tuple[str, ...],
    stdin: bytes | None,
    file_bytes: bytes | None,
    diagnostic: str,
    empty_output: bool,
) -> None:
    path = tmp_path / "input.json"
    if file_bytes is not None:
        path.write_bytes(file_bytes)
    args = [arg.format(path=path) for arg in argv]
    result = subprocess.run(
        [sys.executable, str(LABELS_DIFF), *args],
        cwd=REPO,
        input=stdin,
        capture_output=True,
    )
    stderr = result.stderr.decode("utf-8")
    assert result.returncode == 2, result.stdout
    assert "Traceback" not in stderr
    assert stderr.strip(), "the refusal must say what was wrong"
    if diagnostic:
        assert diagnostic in stderr
    if empty_output:
        assert result.stdout == b"", "nothing may be emitted into the shell recipe"


@pytest.mark.parametrize("form", _form_files(), ids=_form_id)
def test_form_schema(form: Path) -> None:
    text = form.read_text(encoding="utf-8")
    keys = {
        line.split(":", 1)[0] for line in text.splitlines() if re.match(r"^\w+:", line)
    }
    assert keys <= FORM_TOP_LEVEL_KEYS
    assert keys >= FORM_TOP_LEVEL_REQUIRED
    fields = _form_fields(text)
    assert fields, f"{form.name} has no body items"
    ids = [field["id"] for field in fields if field["id"]]
    assert len(ids) == len(set(ids)), f"duplicate field ids in {form.name}"
    labels = [
        field["attributes"].get("label")
        for field in fields
        if field["type"] != "markdown"
    ]
    assert len(labels) == len(set(labels)), f"duplicate field labels in {form.name}"
    for field in fields:
        assert field["type"] in FORM_FIELD_TYPES
        assert set(field["keys"]) <= FORM_ITEM_KEYS
        assert set(field["attributes"]) <= FORM_ATTRIBUTES[field["type"]], (
            "unknown attribute for this field type"
        )
        if field["type"] in FORM_TYPES_WITHOUT_VALIDATIONS:
            assert "validations" not in field["keys"], (
                "a markdown block cannot be required"
            )
            assert field["id"] is None, "a markdown block takes no id"
            assert "value" in field["attributes"]
        else:
            assert re.search(r"^[A-Za-z0-9_-]+$", field["id"] or "")
            assert field["attributes"].get("label")
        if field["type"] in FORM_TYPES_WITH_OPTIONS:
            options = field["options"]
            assert options, f"a {field['type']} must declare options"
            assert len(options) == len(set(options))
            for option in options:
                assert option.strip()
                assert option.strip().lower() not in RESERVED_OPTIONS


def _declared_options(text: str) -> int:
    lines = text.splitlines()
    total = 0
    for index, line in enumerate(lines):
        if line.rstrip() != "      options:":
            continue
        for candidate in lines[index + 1 :]:
            if not candidate.strip() or candidate.lstrip().startswith("#"):
                continue
            if _indent(candidate) > 8:
                continue
            if _indent(candidate) < 8 or not candidate.lstrip().startswith("- "):
                break
            total += 1
    return total


@pytest.mark.parametrize("form", _form_files(), ids=_form_id)
def test_the_reader_reads_every_declared_option(form: Path) -> None:
    # An independent count detects truncation after YAML comments or mapping
    # continuation keys.
    text = form.read_text(encoding="utf-8")
    declared = _declared_options(text)
    parsed = sum(
        len(field["options"] or [])
        for field in _form_fields(text)
        if field["type"] in FORM_TYPES_WITH_OPTIONS
    )
    assert parsed == declared


@pytest.mark.parametrize("form", _form_files(), ids=_form_id)
def test_every_auto_applied_label_exists_in_the_taxonomy(form: Path) -> None:
    # GitHub silently drops form labels that do not already exist.
    known = {label["name"] for label in _labels()}
    labels = _form_labels(form.read_text(encoding="utf-8"))
    assert labels, f"{form.name} auto-applies no labels"
    assert "needs-triage" in labels
    for label in labels:
        assert label in known


def test_bug_form_components_include_workflows_and_bench() -> None:
    joined = "\n".join(_field("bug_report.yml", "phase")["options"])
    assert "workflows/" in joined
    assert "bench/" in joined


def test_bug_form_requires_install_method() -> None:
    field = _field("bug_report.yml", "install_method")
    assert field["type"] == "dropdown"
    assert "validations" in field["keys"], (
        "`validations` must be a sibling of `attributes`, not nested inside it"
    )
    assert field["required"], "install method is a first-order diagnostic"
    options = " ".join(field["options"]).lower()
    for expected in ("marketplace", "--plugin-dir", "clone"):
        assert expected in options


def test_bug_form_plugin_version_placeholder_is_3x() -> None:
    # Either a pinned example or the 3.x line is accepted; the release tool does not
    # update this form, so pinned examples age on release.
    placeholder = _field("bug_report.yml", "plugin_version")["attributes"][
        "placeholder"
    ]
    assert placeholder is not None
    assert re.search(r"3\.(?:x|\d+\.\d+)", placeholder)
    assert not re.search(r"2\.(?:x|\d+\.\d+)", placeholder)
    assert ".claude-plugin/plugin.json" in placeholder


def test_feature_form_area_options_include_workflows_and_bench() -> None:
    options = "\n".join(_field("feature_request.yml", "area")["options"])
    assert "workflows/" in options
    assert "bench/" in options


def test_no_form_points_at_disabled_discussions() -> None:
    # Discussions are disabled for this repository, so forms must not send users there.
    # Revisit the owner decision and this test together if Discussions are enabled.
    for form in _form_files():
        assert not re.search(r"(?i)discussion", form.read_text(encoding="utf-8"))


def test_config_links_the_security_advisory_form() -> None:
    links = _contact_links(_read(".github/ISSUE_TEMPLATE/config.yml"))
    matching = [
        name
        for name, url in links
        if url == ADVISORY_URL and re.search(r"(?i)security", name)
    ]
    assert matching, f"no security contact link among {links}"


def test_security_md_exists_with_scope_and_supported_versions() -> None:
    # The retired architecture must not be advertised as supported.
    text = _read("SECURITY.md")
    assert ADVISORY_URL in text, "must point at the enabled private advisory form"
    for heading in ("reporting", "supported versions", "scope"):
        assert re.search(rf"(?im)^#{{2,3}} .*{heading}", text)
    major = json.loads(_read(".claude-plugin/plugin.json"))["version"].split(".")[0]
    active_rows = re.findall(r"(?im)^\|\s*([^|]+?)\s*\|\s*Yes\s*\|$", text)
    assert len(active_rows) == 1, "exactly one version row must receive security fixes"
    supported_major = re.search(r"\b(\d+)\.x\b", active_rows[0])
    assert supported_major is not None
    assert supported_major.group(1) == major, (
        "supported versions must name the shipped major line"
    )
    assert re.search(r"(?im)^\|\s*2\.x[^|]*\|\s*No", text)


def test_contributing_describes_the_v3_js_pipeline_areas() -> None:
    text = _read("CONTRIBUTING.md")
    assert "workflows/src/" in text
    assert "`bench/`" in text
    assert "docs/maintainer-issues.md" in text


def test_maintainer_issue_standard_has_the_required_sections() -> None:
    text = _read("docs/maintainer-issues.md")
    for section in (
        "Problem / Goal",
        "Requirements",
        "Evidence",
        "Candidate directions",
        "Out of scope",
        "Verification",
    ):
        assert re.search(rf"(?m)^#{{2,4}} {re.escape(section)}", text)


def test_maintainer_issue_standard_defers_tier_definitions_to_the_runbook() -> None:
    # Costs, tier invocations and prose tier tables would duplicate the canonical
    # measurement ladder.
    text = _read("docs/maintainer-issues.md")
    for tier in ("suites-only", "smoke", "paired-mini"):
        assert tier in text
    assert not re.search(r"\$\d", text), (
        "tier costs belong only in bench/MEASUREMENT.md"
    )
    assert "--tier" not in text, "tier invocations belong only in bench/MEASUREMENT.md"
    ladder_rows = [
        line
        for line in text.splitlines()
        if line.lstrip().startswith("|")
        and re.search(r"(?i)\b(tier|trigger|smoke|mini-subset|holdout)\b", line)
    ]
    assert ladder_rows == [], (
        "a tier table here is the ladder restated; link the runbook instead"
    )
    link = re.search(r"\[`bench/MEASUREMENT\.md`\]\((\.\./[^)]+)\)", text)
    assert link is not None, "the canonical runbook must be linked, not just named"
    assert (REPO / "docs" / link.group(1)).resolve().is_file()


def test_every_tier_slug_the_standard_mandates_resolves_in_the_runbook() -> None:
    # Read the standard vocabulary so a new normative slug cannot silently point to a
    # missing runbook entry.
    standard = _read("docs/maintainer-issues.md")
    vocabulary = standard.split("names exactly one measurement tier", 1)[1].split(
        "\n\n", 2
    )[1]
    slugs = re.findall(r"^- `([a-z-]+)`", vocabulary, re.M)
    assert slugs, "the standard must list the tier slugs as a bullet list"
    runbook_slugs = set(
        re.findall(r"^\| [^|]+ \| `([a-z-]+)` \|", _read("bench/MEASUREMENT.md"), re.M)
    )
    for slug in slugs:
        assert slug in runbook_slugs, (
            "a slug the standard mandates must appear in the runbook's Slug column, "
            f"which holds {sorted(runbook_slugs)}"
        )


def test_cspell_scope_includes_the_new_public_docs() -> None:
    hooks = _read(".pre-commit-config.yaml")
    cspell_block = hooks.split("- id: cspell", 1)
    assert len(cspell_block) == 2, "cspell hook missing from .pre-commit-config.yaml"
    block = cspell_block[1].split("\n  - repo:", 1)[0]
    files_pattern = re.search(r"^        files: \^\((.*)\)\$", block, re.M)
    assert files_pattern is not None, (
        "the cspell hook must keep an explicit files: scope"
    )
    scope = re.compile(rf"^({files_pattern.group(1)})$")
    for path in (
        "README.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/maintainer-issues.md",
    ):
        assert scope.match(path), f"cspell scope does not cover {path}"


def test_contributing_python_lint_hook_ids_match_pre_commit_config() -> None:
    expected = {"ruff-check", "ruff-format", "mypy"}
    contributing = _read("CONTRIBUTING.md")
    hooks = _read(".pre-commit-config.yaml")
    declared = set(re.findall(r"^\s+- id: ([^\s#]+)", hooks, re.M))
    documented = set(
        re.findall(
            r"pre-commit run (ruff-check|ruff-format|mypy) --all-files", contributing
        )
    )
    assert documented == expected, (
        "CONTRIBUTING.md must document exactly the Python lint/format/type hooks"
    )
    assert expected.issubset(declared), (
        f"hooks {sorted(expected - declared)} missing from .pre-commit-config.yaml"
    )


def test_pin_file_has_all_known_assets_and_valid_digests() -> None:
    pin = json.loads(_read("workflows/biome-pin.json"))
    assert isinstance(pin, dict)
    assert re.search(r"^\d+\.\d+\.\d+$", pin["version"])
    assert set(pin["sha256"]) == BIOME_ASSETS
    for asset, digest in pin["sha256"].items():
        assert asset in BIOME_ASSETS
        assert re.search(r"^[0-9a-f]{64}$", digest)


def test_ci_js_lint_uses_runner_without_another_biome_pin() -> None:
    ci = _read(".github/workflows/ci.yml")
    js_lint = ci.split("  js-lint:\n", 1)[1].split("\n  workflow-tests:", 1)[0]
    assert "python3 workflows/test/tools/biome_check.py" in js_lint
    for forbidden in (
        "BIOME_VERSION",
        "BIOME_SHA256",
        "sha256sum",
        "releases/download/@biomejs",
    ):
        assert forbidden not in ci


def test_operational_biome_version_lives_only_in_pin_file() -> None:
    version = json.loads(_read("workflows/biome-pin.json"))["version"]
    paths = (
        ".github/workflows/ci.yml",
        "workflows/AGENTS.md",
        "CONTRIBUTING.md",
        "README.md",
        ".github/pull_request_template.md",
    )
    for path in paths:
        assert version not in _read(path)


# Required check-run names must change with the live branch-protection ruleset.
# GitHub matches check-run names rather than workflow titles alone.
REQUIRED_PR_CHECK_CONTEXTS = (
    "Run Linting",
    "Run Tests (3.10)",
    "Run Tests (3.11)",
    "Run Tests (3.12)",
    "Run Workflow JS Lint",
    "Run Workflow JS Tests",
    "Run Bench Self-Tests (3.11)",
    "Run Bench Self-Tests (3.12)",
    "Run Tests on Windows (3.12)",
    "Run Tests on Windows (3.14)",
    "Validate plugin.json",
    "lint-pr-title",
)
# Add each new required check here so its docs question cannot be skipped.
LOCAL_COMMAND_FOR_REQUIRED_CHECK = {
    "Run Linting": "pre-commit run --all-files",
    "Run Tests (3.10)": "python -m pytest tests/ -q",
    "Run Tests (3.11)": "python -m pytest tests/ -q",
    "Run Tests (3.12)": "python -m pytest tests/ -q",
    "Run Workflow JS Lint": "python3 workflows/test/tools/biome_check.py",
    "Run Workflow JS Tests": "node --test workflows/test/*.test.js",
    "Run Bench Self-Tests (3.11)": "python -m pytest bench/tests/ -q",
    "Run Bench Self-Tests (3.12)": "python -m pytest bench/tests/ -q",
    "Run Tests on Windows (3.12)": "python -m pytest tests/ -q",
    "Run Tests on Windows (3.14)": "python -m pytest tests/ -q",
    "Validate plugin.json": "claude plugin validate .",
    "lint-pr-title": None,  # Needs the PR title; nothing local to run.
}


def test_ci_only_required_checks_are_documented() -> None:
    ci_only = [
        context
        for context, command in LOCAL_COMMAND_FOR_REQUIRED_CHECK.items()
        if command is None
    ]
    assert ci_only
    for context in ci_only:
        assert context in _read("CONTRIBUTING.md")


REQUIRED_PR_CHECK_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/validate.yml",
    ".github/workflows/pr-title-lint.yml",
)


def _derive_required_pr_check_contexts() -> list[str]:
    # Update the frozen tuple and live ruleset together. Zero required approvals is
    # deliberate; checks, conversations and linear history are the merge gates.
    # Job-level names override job ids.
    contexts: list[str] = []
    for rel in REQUIRED_PR_CHECK_WORKFLOWS:
        for job_id, job in _ci_job_blocks(_read(rel)):
            names = re.findall(r"(?m)^    name:\s*(.+?)\s*$", job)
            label = _unquote(names[0]) if names else job_id
            matrices = re.findall(r"(?m)^        python-version:\s*\[(.*)\]\s*$", job)
            versions = [
                _unquote(part.strip())
                for matrix in matrices
                for part in matrix.split(",")
                if part.strip()
            ]
            if versions:
                contexts.extend(f"{label} ({version})" for version in versions)
            else:
                contexts.append(label)
    return contexts


def test_frozen_tuple_matches_workflow_derived_names() -> None:
    derived = tuple(sorted(_derive_required_pr_check_contexts()))
    frozen = tuple(sorted(REQUIRED_PR_CHECK_CONTEXTS))
    assert derived == frozen, (
        "workflow job names drifted from REQUIRED_PR_CHECK_CONTEXTS — "
        "update the tuple AND ruleset 16049246 together"
    )
    assert set(LOCAL_COMMAND_FOR_REQUIRED_CHECK) == set(REQUIRED_PR_CHECK_CONTEXTS)


GATE_MARKERS = ("--cov-fail-under=", "--test-coverage-lines=")


def _documented_coverage_gate_commands(text: str) -> set[str]:
    # Ignore trailing cosmetic blank lines while preserving interior command bytes.
    heading = re.search(r"(?m)^Coverage gates\b.*$", text)
    if not heading:
        raise AssertionError("CONTRIBUTING.md missing 'Coverage gates' paragraph")
    rest = text[heading.end() :]
    fence = re.search(r"```bash\n(.*?)```", rest, re.DOTALL)
    if not fence:
        raise AssertionError("CONTRIBUTING.md Coverage gates missing ```bash fence")
    body = fence.group(1)
    chunks = re.split(r"\n\s*\n", body.strip("\n"))
    commands = {c.rstrip("\n") for c in chunks if c.strip()}
    if len(commands) < 3:
        raise AssertionError(
            f"expected ≥3 gate commands in CONTRIBUTING.md, found {len(commands)}"
        )
    return commands


def _ci_run_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|-?\s*$", lines[i])
        if not m:
            i += 1
            continue
        base = len(m.group(1))
        i += 1
        content_indent = None
        collected = []
        while i < len(lines):
            line = lines[i]
            if line.strip() == "":
                collected.append("")
                i += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if content_indent is None:
                if indent <= base:
                    break
                content_indent = indent
            if indent < content_indent and line.strip():
                break
            collected.append(
                line[content_indent:] if content_indent is not None else line
            )
            i += 1
        blocks.append("\n".join(collected).rstrip("\n"))
    return blocks


def _ci_job_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    jobs_index = next(
        (index for index, line in enumerate(lines) if line == "jobs:"), None
    )
    if jobs_index is None:
        raise AssertionError("ci.yml is missing jobs:")
    starts = []
    for index in range(jobs_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" ") and not line.startswith("#"):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            starts.append((index, match.group(1)))
    jobs = []
    for position, (start, job_id) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        jobs.append((job_id, "\n".join(lines[start:end])))
    return jobs


def _ci_step_blocks(job: str) -> list[str]:
    lines = job.splitlines()
    starts = [index for index, line in enumerate(lines) if re.match(r"^      - ", line)]
    return [
        "\n".join(
            lines[
                start : starts[position + 1]
                if position + 1 < len(starts)
                else len(lines)
            ]
        )
        for position, start in enumerate(starts)
    ]


def _ci_step_run_bodies(step: str) -> list[str]:
    inline = re.findall(r"(?m)^\s+run:\s+(?!\|-?$)(.+?)\s*$", step)
    return inline + _ci_run_blocks(step)


def _ci_setup_node_steps(text: str) -> list[tuple[str, int, str]]:
    return [
        (job_id, index, step)
        for job_id, job in _ci_job_blocks(text)
        for index, step in enumerate(_ci_step_blocks(job))
        if "actions/setup-node@" in step
    ]


def _ci_gate_commands(text: str) -> set[str]:
    return {
        b for b in _ci_run_blocks(text) if any(marker in b for marker in GATE_MARKERS)
    }


def _ci_workflow_tests_node_version(text: str) -> str:
    matches = re.findall(
        r'(?m)^env:\n  NODE_VERSION:\s*["\']?([^"\'\s]+)["\']?\s*$', text
    )
    if len(matches) != 1:
        raise AssertionError(
            f"ci.yml must declare exactly one workflow NODE_VERSION, found {len(matches)}"
        )
    return str(matches[0])


def test_layout_hook_owns_pytest_and_keeps_markdown_scope() -> None:
    text = _read(".pre-commit-config.yaml")
    block = _pre_commit_hook_block(text, "agent-instruction-layout")
    assert (
        _pre_commit_hook_value(block, "name")
        == "agent instruction files stay small, unshadowed, and name real code"
    )
    assert (
        _pre_commit_hook_value(block, "entry")
        == "python -m pytest tests/test_agent_instruction_layout.py -q -p no:cacheprovider"
    )
    assert _pre_commit_hook_value(block, "language") == "python"
    assert (
        _pre_commit_hook_value(block, "files")
        == r"(^|/)(AGENTS|CLAUDE|CLAUDE\.local)\.md$|^REVIEW\.md$"
    )
    assert _pre_commit_hook_value(block, "pass_filenames") == "false"
    assert not re.search(r"(?m)^        (types|types_or|stages):", block)
    assert re.search(r"(?m)^default_stages: \[pre-commit\]$", text)
    assert re.search(r"pytest==[0-9][A-Za-z0-9.!+_-]*", block)


def test_pytest_hook_pins_match_dev_dependency() -> None:
    pins = re.findall(r'"(pytest==[^"\s]+)"', _read("pyproject.toml"))
    assert len(pins) == 1, f"expected one exact pytest dev pin, found {pins}"
    text = _read(".pre-commit-config.yaml")
    hook_pins = re.findall(r"pytest==[0-9][A-Za-z0-9.!+_-]*", text)
    assert hook_pins, "no pre-commit hook owns a pytest pin"
    mismatched = [pin for pin in hook_pins if pin != pins[0]]
    assert not mismatched, f"pytest hook pins differ from {pins[0]}: {mismatched}"


def test_contract_fence_hook_is_scoped_and_covers_derived_inputs() -> None:
    text = _read(".pre-commit-config.yaml")
    block = _pre_commit_hook_block(text, "contract-fences-current")
    repo_block = _pre_commit_repo_block(text, "contract-fences-current")
    repo_header = re.match(r"(?m)^  - repo:\s*(.*?)\s*$", repo_block)
    assert repo_header is not None
    assert _unquote(repo_header.group(1)) == "local"
    assert (
        _pre_commit_hook_value(block, "entry")
        == "python3 scripts/generate_contract_requirements.py --check"
    )
    assert _pre_commit_hook_value(block, "pass_filenames") == "false"
    assert _pre_commit_hook_value(block, "language") == "system"
    assert not re.search(r"(?m)^\s+always_run:", block)
    stage_values = re.findall(r"(?m)^        stages:\s*(.*?)\s*$", block)
    if stage_values:
        stages = _unquote(stage_values[0])
        assert re.search(r"\[\s*pre-commit(?:\s*,|\s*\])", stages)
    files_pattern = _pre_commit_hook_value(block, "files")
    assert files_pattern == "^(agents|skills/code-gauntlet|scripts|workflows)/"
    scope = re.compile(files_pattern)
    declared_inputs = generator_inputs.declared_inputs(str(REPO))
    assert {
        "scripts/gauntlet/delivery/compose.py",
        "scripts/gauntlet/delivery/gate.py",
        "scripts/gauntlet/config.py",
        "scripts/gauntlet/marker.py",
        "scripts/gauntlet/generate.py",
        "scripts/gauntlet/fs.py",
    }.issubset(declared_inputs), declared_inputs
    assert {
        "workflows/src/registry.js",
        "workflows/src/knobs.js",
        "workflows/src/renderReport.js",
    }.issubset(declared_inputs), declared_inputs
    assert {
        "workflows/src/filterFindings.js",
        "workflows/src/applyChallenges.js",
        "workflows/src/applyValidations.js",
    }.issubset(declared_inputs), declared_inputs
    registry = contract_gen.load_registry(str(REPO))
    paths = set(contract_gen.rendered_targets(str(REPO), registry, ""))
    paths.update(declared_inputs)
    paths.add("scripts/generate_contract_requirements.py")
    for path in sorted(paths):
        if path in declared_inputs:
            assert (REPO / path).is_file(), path
        assert scope.match(path) is not None, path


def test_contract_fence_hook_rejects_stale_fence_without_writing() -> None:
    config = _read(".pre-commit-config.yaml")
    block = _pre_commit_hook_block(config, "contract-fences-current")
    entry = shlex.split(_pre_commit_hook_value(block, "entry"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        copy_generator_tree(
            REPO,
            root,
            contract_gen.rendered_targets(
                str(REPO), contract_gen.load_registry(str(REPO)), ""
            ),
        )
        target = root / "skills/code-gauntlet/references/phase1-preflight.md"
        clean_result = subprocess.run(
            entry, cwd=root, capture_output=True, text=True, encoding="utf-8"
        )
        assert clean_result.returncode == 0, clean_result.stdout + clean_result.stderr
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        open_index, close_index = contract_gen.find_identity_pairs(
            [line.rstrip("\n") for line in lines],
            "skills/code-gauntlet/references/phase1-preflight.md",
        )["derived_waist_fields"]
        body_index = next(
            index
            for index in range(open_index + 1, close_index)
            if lines[index].strip()
        )
        lines[body_index] = lines[body_index].rstrip("\n") + " stale\n"
        corrupted = "".join(lines).encode()
        target.write_bytes(corrupted)
        stale_result = subprocess.run(
            entry, cwd=root, capture_output=True, text=True, encoding="utf-8"
        )
        assert stale_result.returncode == 1
        assert (
            "skills/code-gauntlet/references/phase1-preflight.md" in stale_result.stderr
        )
        assert target.read_bytes() == corrupted


def test_windows_pytest_job_contract_is_pinned() -> None:
    text = _read(".github/workflows/ci.yml")
    windows_jobs = [
        job
        for _, job in _ci_job_blocks(text)
        if re.search(r"(?m)^    name: Run Tests on Windows\s*$", job)
    ]
    assert len(windows_jobs) == 1
    job = windows_jobs[0]
    assert re.search(r"(?m)^    runs-on:\s*windows-latest\s*$", job)
    assert re.search(r"(?m)^      matrix:\s*$", job)
    assert re.search(
        r'(?m)^        python-version:\s*\[[^\]]*["\']3\.12["\'][^\]]*\]\s*$',
        job,
    )
    assert re.search(r'(?m)^      PYTHONUTF8:\s*["\']?0["\']?\s*$', job)
    run_bodies = [
        body for step in _ci_step_blocks(job) for body in _ci_step_run_bodies(step)
    ]
    assert "python -m pytest tests/ -q" in run_bodies


def test_node_is_pinned_before_every_relevant_ci_job() -> None:
    text = _read(".github/workflows/ci.yml")
    version = _ci_workflow_tests_node_version(text)
    assert len(re.findall(r"(?m)^  NODE_VERSION:", text)) == 1
    setup_steps = _ci_setup_node_steps(text)
    assert setup_steps
    assert text.count("actions/setup-node@") == len(setup_steps)
    setup_node_uses = []
    for _job_id, _index, step in setup_steps:
        uses = re.search(
            r"(?m)^\s+uses:\s*actions/setup-node@([0-9a-f]{40})\s+#\s+v\S+$", step
        )
        assert uses is not None
        setup_node_uses.append(uses.group(1))
        assert re.findall(r"(?m)^\s+node-version:\s*(.*?)\s*$", step) == [
            "${{ env.NODE_VERSION }}"
        ]
    assert len(set(setup_node_uses)) == 1
    bare_node = re.compile(r"(?<![A-Za-z0-9_-])node ")
    consumer_markers = ("pre-commit run", "python -m pytest tests/")
    for _job_id, job in _ci_job_blocks(text):
        steps = _ci_step_blocks(job)
        consumer_indexes = [
            index
            for index, step in enumerate(steps)
            if any(
                marker in body or bare_node.search(body)
                for body in _ci_step_run_bodies(step)
                for marker in consumer_markers
            )
        ]
        if not consumer_indexes:
            continue
        setup_indexes = [
            index for index, step in enumerate(steps) if "actions/setup-node@" in step
        ]
        assert setup_indexes
        assert min(setup_indexes) < min(consumer_indexes)
    contributing = _read("CONTRIBUTING.md")
    assert f"CI pins Node `{version}` exactly" in contributing


def test_getting_started_declares_the_node_prerequisite_for_the_hook() -> None:
    text = _read("CONTRIBUTING.md")
    section = text.split("## Getting Started\n", 1)[1].split("\n## ", 1)[0]
    version = _ci_workflow_tests_node_version(_read(".github/workflows/ci.yml"))
    sentences = re.findall(r"(?m)^.*contract-fences-current.*\.$", section)
    assert len(sentences) == 1
    assert f"Node `{version}`" in sentences[0]
    assert section.index(sentences[0]) < section.index("pre-commit install")


def test_documented_and_ci_gate_commands_are_byte_identical_sets() -> None:
    documented = _documented_coverage_gate_commands(_read("CONTRIBUTING.md"))
    ci = _ci_gate_commands(_read(".github/workflows/ci.yml"))
    assert documented == ci
    assert (
        "bare directory form is not a valid `node --test` target on node 24"
        in _read("CONTRIBUTING.md")
    )
    assert (
        "Transform goldens updated with "
        "`UPDATE_GOLDENS=1 node --test workflows/test/goldens.test.js` "
        "in a reviewed diff; verify-wire fixtures updated with "
        "`workflows/test/tools/record_parity.py`"
    ) in _read(".github/pull_request_template.md")


def test_js_gate_includes_match_coverage_scope_json() -> None:
    scope = json.loads(_read("workflows/test/tools/coverage_scope.json"))
    documented = _documented_coverage_gate_commands(_read("CONTRIBUTING.md"))
    js_gates = [c for c in documented if "--test-coverage-lines=" in c]
    assert len(js_gates) == 1, js_gates
    flags = re.findall(r"--test-coverage-include='([^']+)'", js_gates[0])
    assert set(flags) == set(scope["includes"])


def test_no_sleep_poll_loop_survives_anywhere_under_skills() -> None:
    offenders = []
    for path in (REPO / "skills").rglob("*.md"):
        if re.search(r"\bsleep\s+\d+\b", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], "sleep-based poll loop still present"


def test_both_wait_protocol_copies_reference_the_awaiter() -> None:
    for relative in (
        "skills/code-gauntlet/SKILL.md",
        "skills/code-gauntlet/references/phase3-dispatch.md",
    ):
        assert "await_workflow.py" in (REPO / relative).read_text(encoding="utf-8")


def test_headless_reference_points_at_the_protocol() -> None:
    text = (REPO / "skills/code-gauntlet/references/headless-mode.md").read_text(
        encoding="utf-8"
    )
    assert "await_workflow.py" in text
    assert "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS" in text


@pytest.mark.parametrize(
    "command",
    sorted(
        {command for command in LOCAL_COMMAND_FOR_REQUIRED_CHECK.values() if command}
        | {
            "node workflows/build.js",
            "UPDATE_GOLDENS=1 node --test workflows/test/goldens.test.js",
            "workflows/test/tools/record_parity.py",
        }
    ),
)
def test_contributor_gate_commands(command: str) -> None:
    assert command in _read("CONTRIBUTING.md")
    assert command in _read(".github/pull_request_template.md")


@pytest.mark.parametrize(
    "heading,next_heading",
    [
        pytest.param(
            "### Common Commands",
            "## The v3 workflow pipeline (JS)",
            id="common-commands",
        ),
        pytest.param("## Testing", "## Branching and Commit Conventions", id="testing"),
    ],
)
def test_biome_command_in_contributor_bash_blocks(
    heading: str, next_heading: str
) -> None:
    section = (
        _read("CONTRIBUTING.md")
        .split(f"{heading}\n", 1)[1]
        .split(f"\n{next_heading}\n", 1)[0]
    )
    blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
    assert len(blocks) == 1
    assert "python3 workflows/test/tools/biome_check.py" in blocks[0]
