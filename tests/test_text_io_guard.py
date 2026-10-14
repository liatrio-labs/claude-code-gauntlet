from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, TypeGuard

import pytest

from tests.support.repository import tracked_paths

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (
    "scripts/",
    "tests/",
    "bench/",
    ".github/",
    "workflows/test/tools/",
    "conftest.py",
)
EXCLUDED_ROOTS = ("bench/vendor/", "bench/workspace/", "tests/fixtures/")
NEWLINE_ROOTS = ("scripts/", "workflows/test/tools/", "tests/tools/")
NON_TEXT_OPEN_RECEIVERS = frozenset({"os", "tarfile", "zipfile", "webbrowser"})
GUARDED_STAR_IMPORTS = frozenset(
    {
        "subprocess",
        "io",
        "os",
        "tempfile",
        "codecs",
        "pathlib",
        "builtins",
        "gzip",
        "bz2",
        "lzma",
        "logging",
    }
)
_MISSING = object()


def _rule(name: str, **settings: Any) -> dict[str, Any]:
    shape: dict[str, Any] = (
        {"kind": "attribute"}
        if "attribute" in settings
        else {"kind": "exact", "names": (name,)}
    )
    return {"name": name, **shape, **settings}


# Policy and test oracles stay separate so deleting a rule cannot delete its proof.
CALL_RULES: tuple[dict[str, Any], ...] = (
    _rule(
        "open, builtins.open and io.open",
        names=("open", "builtins.open", "io.open"),
        mode_index=1,
        default_mode="r",
    ),
    *(
        _rule(f"{module}.open", mode_index=1, default_mode="rb", text_mode_marker="t")
        for module in ("gzip", "bz2", "lzma")
    ),
    _rule("os.fdopen", mode_index=1, default_mode="r"),
    _rule(
        "Path.open",
        attribute="open",
        exclude_receivers=NON_TEXT_OPEN_RECEIVERS,
        mode_index=0,
        default_mode="r",
    ),
    _rule("Path.read_text", attribute="read_text", always_text=True, encoding_index=0),
    _rule(
        "Path.write_text", attribute="write_text", always_text=True, encoding_index=1
    ),
    *(
        _rule(
            f"tempfile.{name}",
            names=(f"tempfile.{name}", name),
            mode_index=index,
            default_mode="w+b",
        )
        for name, index in (
            ("NamedTemporaryFile", 0),
            ("TemporaryFile", 0),
            ("SpooledTemporaryFile", 1),
        )
    ),
    _rule(
        "io.TextIOWrapper",
        names=("io.TextIOWrapper", "TextIOWrapper"),
        always_text=True,
        encoding_index=1,
    ),
    _rule(
        "logging.FileHandler",
        mode_index=1,
        default_mode="a",
        always_text=True,
        encoding_index=2,
    ),
    _rule(
        "subprocess text-capable calls",
        names=tuple(
            f"subprocess.{name}"
            for name in ("run", "Popen", "check_output", "call", "check_call")
        ),
        subprocess_text=True,
    ),
    _rule("codecs.open", mode_index=1, default_mode="r"),
    _rule(
        "tempfile.mkstemp(text=True) (forbidden)",
        names=("tempfile.mkstemp",),
        forbidden_text_flag=True,
    ),
    *(
        _rule(f"{name} (forbidden)", names=(name,), forbidden=True)
        for name in ("os.popen", "subprocess.getoutput", "subprocess.getstatusoutput")
    ),
)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.asname:
                    aliases[imported.asname] = imported.name
                else:
                    aliases[imported.name.split(".", 1)[0]] = imported.name.split(
                        ".", 1
                    )[0]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for imported in node.names:
                aliases[imported.asname or imported.name] = ".".join(
                    part for part in (module, imported.name) if part
                )
    return aliases


def _canonical_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    dotted = _dotted_name(node)
    if dotted is None:
        return None
    head, separator, tail = dotted.partition(".")
    canonical_head = aliases.get(head, head)
    return canonical_head + (separator + tail if separator else "")


def _matched_rule(call: ast.Call, aliases: dict[str, str]) -> dict | None:
    canonical = _canonical_name(call.func, aliases)
    for rule in CALL_RULES:
        if rule["kind"] == "exact" and canonical in rule["names"]:
            return rule
        if rule["kind"] == "attribute" and isinstance(call.func, ast.Attribute):
            if call.func.attr != rule["attribute"]:
                continue
            receiver = _canonical_name(call.func.value, aliases)
            if receiver and receiver.split(".", 1)[0] in rule.get(
                "exclude_receivers", ()
            ):
                continue
            if rule["attribute"] == "open":
                if any(keyword.arg == "name" for keyword in call.keywords):
                    continue
                if call.args:
                    first = call.args[0]
                    # A ZipFile member named "r" remains a fail-closed false positive.
                    if (
                        isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and not _valid_open_mode(first.value)
                    ):
                        continue
            return rule
    return None


def _argument(call: ast.Call, keyword: str, position: int | None) -> ast.expr | object:
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return _MISSING


def _literal_string(node: ast.expr | object) -> TypeGuard[ast.Constant]:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _valid_open_mode(mode: str) -> bool:
    return (
        bool(mode)
        and set(mode) <= set("rwxabt+")
        and sum(mode.count(char) for char in "rwxa") == 1
    )


def _encoding_state(call: ast.Call, rule: dict) -> tuple[bool, bool]:
    encoding = _argument(call, "encoding", rule.get("encoding_index"))
    if encoding is _MISSING:
        return False, False
    valid = (
        isinstance(encoding, ast.Constant)
        and isinstance(encoding.value, str)
        and encoding.value.lower() != "locale"
    )
    return valid, not valid


def _explicit_keyword_encoding(call: ast.Call) -> bool:
    return any(item.arg == "encoding" for item in call.keywords)


def _text_mode(call: ast.Call, rule: dict) -> tuple[bool, bool]:
    if rule.get("always_text"):
        return True, False
    if rule.get("subprocess_text"):
        is_text = False
        dynamic = False
        for name in ("text", "universal_newlines"):
            value = _argument(call, name, None)
            if value is _MISSING:
                continue
            if not isinstance(value, ast.Constant):
                dynamic = True
            elif bool(value.value):
                is_text = True
        if any(item.arg == "errors" for item in call.keywords):
            is_text = True
        return is_text, dynamic

    mode = _argument(call, "mode", rule.get("mode_index"))
    if mode is _MISSING:
        mode = rule["default_mode"]
    if isinstance(mode, str):
        marker = rule.get("text_mode_marker")
        return (marker in mode if marker else "b" not in mode), False
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return False, True
    marker = rule.get("text_mode_marker")
    return (marker in mode.value if marker else "b" not in mode.value), False


def _has_expansion(call: ast.Call) -> bool:
    return any(item.arg is None for item in call.keywords)


def _is_script(path: str) -> bool:
    return path.startswith(NEWLINE_ROOTS)


def _call_offender(call: ast.Call, rule: dict, path: str, source: str) -> str | None:
    name = ast.get_source_segment(source, call) or "<call>"
    name = " ".join(name.split())
    if rule.get("forbidden"):
        return f"{path}:{call.lineno} {name} (forbidden text I/O API)"
    if rule.get("forbidden_text_flag"):
        text_flag = _argument(call, "text", 3)
        if _has_expansion(call):
            return (
                f"{path}:{call.lineno} {name} (tempfile.mkstemp text mode is forbidden)"
            )
        if text_flag is _MISSING or (
            isinstance(text_flag, ast.Constant) and text_flag.value is False
        ):
            return None
        return f"{path}:{call.lineno} {name} (tempfile.mkstemp text mode is forbidden)"

    valid_encoding, invalid_encoding = _encoding_state(call, rule)
    text_mode, dynamic_text = _text_mode(call, rule)
    reasons = []
    if invalid_encoding:
        reasons.append("encoding must be a non-None string literal")
    if dynamic_text:
        reasons.append("mode/text flags must be literals")
    if text_mode and not valid_encoding:
        reasons.append("text mode requires an explicit encoding")
    if _has_expansion(call) and not (
        _explicit_keyword_encoding(call) and valid_encoding
    ):
        reasons.append("**kwargs requires explicit literal encoding")

    mode = _argument(call, "mode", rule.get("mode_index"))
    if mode is _MISSING:
        mode = rule.get("default_mode")
    mode_value = mode.value if isinstance(mode, ast.Constant) else mode
    writes = isinstance(mode_value, str) and any(char in mode_value for char in "wax+")
    is_write_text = rule.get("name") == "Path.write_text"
    if _is_script(path) and text_mode and (writes or is_write_text):
        newline = _argument(call, "newline", None)
        if not _literal_string(newline) or newline.value != "":
            reasons.append('text writes require newline=""')

    if not reasons:
        return None
    return f"{path}:{call.lineno} {name} ({'; '.join(reasons)})"


def _scan_source(source: str, path: str) -> list[str]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        line = error.lineno or 1
        return [f"{path}:{line} SyntaxError: {error.msg}"]

    aliases = _import_aliases(tree)
    offenders = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module in GUARDED_STAR_IMPORTS
            and any(imported.name == "*" for imported in node.names)
        ):
            offenders.append(
                f"{path}:{node.lineno} from {node.module} import * "
                "(star import hides guarded text I/O calls)"
            )
        if not isinstance(node, ast.Call):
            continue
        rule = _matched_rule(node, aliases)
        if rule is None:
            continue
        offender = _call_offender(node, rule, path, source)
        if offender:
            offenders.append(offender)
    return offenders


def _tracked_python_paths() -> list[str]:
    return sorted(
        path
        for path in tracked_paths(REPO_ROOT, ["*.py"])
        if path.startswith(SCANNED_ROOTS) and not path.startswith(EXCLUDED_ROOTS)
    )


def _tracked_offenders() -> list[str]:
    offenders = []
    for relative_path in _tracked_python_paths():
        path = REPO_ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        offenders.extend(_scan_source(source, relative_path))
    return offenders


def test_tracked_python_files_use_explicit_text_io_settings() -> None:
    offenders = _tracked_offenders()
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    ("snippet", "expected_count"),
    [
        ('open("file")', 1),
        ('gzip.open("file", "rt")', 1),
        ('Path("file").read_text()', 1),
        ("subprocess.run([], text=True)", 1),
        ('os.popen("command")', 1),
        ('open("file", mode=MODE, encoding="utf-8")', 1),
        ('subprocess.run([], text=TEXT, encoding="utf-8")', 1),
        ("subprocess.run([], text=False)", 0),
        ('open("file", **options, encoding="utf-8")', 0),
        ('Path("file").read_text("utf-8", **options)', 1),
        ('open("file", encoding=ENCODING)', 1),
        ('open("file", "rb")', 0),
        ('Path("file").open(encoding="utf-8")', 0),
        ('Path("file").write_text("x", "utf-8")', 0),
        ('from builtins import open as file_open\nfile_open("file", "rb")', 0),
        ('gzip.open("file")', 0),
        ("tempfile.mkstemp()", 0),
        ("tempfile.mkstemp(text=TEXT)", 1),
        ("tempfile.mkstemp(**options)", 1),
    ],
)
def test_call_rule_class_examples(snippet: str, expected_count: int) -> None:
    offenders = _scan_source(snippet, "tests/snippet.py")
    assert len(offenders) == expected_count, offenders


def test_locale_encoding_is_rejected() -> None:
    offenders = _scan_source('open("file", encoding="LoCaLe")', "tests/locale.py")
    assert len(offenders) == 1, offenders


def test_alias_resolution() -> None:
    snippet = "from io import open as text_open\ntext_open('file')"
    offenders = _scan_source(snippet, "tests/alias_snippet.py")
    assert len(offenders) == 1, offenders
    assert "tests/alias_snippet.py:2" in offenders[0]


def test_receiver_exemptions_do_not_hide_path_open() -> None:
    exempt = """\
import os
os.open("file", os.O_RDONLY)
"""
    assert _scan_source(exempt, "tests/exempt.py") == []
    offenders = _scan_source('Path("member").open()', "tests/archive.py")
    assert len(offenders) == 1


@pytest.mark.parametrize(
    ("snippet", "expected_count"),
    [
        ('zipfile.ZipFile("archive.zip").open("member.txt")', 0),
        ('Path("file").open("r")', 1),
    ],
)
def test_archive_members_do_not_look_like_path_open_modes(
    snippet: str, expected_count: int
) -> None:
    offenders = _scan_source(snippet, "tests/archive_members.py")
    assert len(offenders) == expected_count, offenders


def test_guarded_star_imports_fail_closed() -> None:
    offenders = _scan_source("from subprocess import *", "tests/star_import.py")
    assert len(offenders) == 1, offenders
    assert "star import hides guarded text I/O calls" in offenders[0]


def test_syntax_error_reports_the_scanned_path() -> None:
    offenders = _scan_source("def broken(:\n    pass\n", "tests/broken.py")
    assert len(offenders) == 1
    assert offenders[0].startswith("tests/broken.py:1 SyntaxError:")


@pytest.mark.parametrize(
    ("snippet", "expected_count"),
    [
        ('Path("file").write_text("x", encoding="utf-8")', 1),
        ('Path("file").write_text("x", encoding="utf-8", newline="")', 0),
    ],
)
def test_scripts_writes_require_literal_empty_newline(
    snippet: str, expected_count: int
) -> None:
    offenders = _scan_source(snippet, "scripts/snippet.py")
    assert len(offenders) == expected_count, offenders


def test_tests_writes_do_not_require_newline() -> None:
    assert _scan_source('open("file", "w", encoding="utf-8")', "tests/snippet.py") == []


def test_gitattributes_pins_lf_for_text_files() -> None:
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in attributes.splitlines()
