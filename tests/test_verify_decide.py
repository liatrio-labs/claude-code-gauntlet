"""Repository decisions through real files and the process leaf."""

import ast
import builtins
import copy
import importlib
import json
from pathlib import Path

import pytest
from gauntlet import proc
from gauntlet.diff import parse_diff
from gauntlet.registry import VERIFY_SLICE_FIELDS, VerifySliceFinding
from gauntlet.verify import decide as verify
from gauntlet.verify import wire

from tests.test_verify_wire import receipt

DASH = "\u2014"
DIFF = "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n"


@pytest.mark.parametrize(
    ("patch", "log", "blame", "origin", "severity", "author", "date", "warning"),
    [
        pytest.param(
            {"file": "missing"},
            None,
            None,
            "new",
            "high",
            None,
            None,
            "classify_blame: file not found 'missing' \u2014 classifying as 'new' "
            "(conservative).",
            id="BLAME-missing-file",
        ),
        pytest.param(
            {},
            ["", "", 1],
            None,
            "new",
            "high",
            None,
            None,
            "classify_blame: git log failed for base 'base':  \u2014 classifying as 'new' "
            "(conservative).",
            id="BLAME-log-empty-stderr",
        ),
        pytest.param(
            {},
            None,
            ["", " blame bad \n", 1],
            "new",
            "high",
            None,
            None,
            "classify_blame: git blame failed for 'source': blame bad \u2014 classifying as "
            "'new' (conservative).",
            id="BLAME-blame-failure",
        ),
        pytest.param(
            {},
            None,
            ["", "BINARY data", 1],
            "new",
            "high",
            None,
            None,
            "classify_blame: binary file 'source' \u2014 classifying as 'new' (conservative).",
            id="BLAME-binary",
        ),
        pytest.param(
            {},
            None,
            ["no blame", "", 0],
            "new",
            "high",
            None,
            None,
            "classify_blame: could not parse blame output for 'source' lines 1-2 \u2014 "
            "classifying as 'new' (conservative).",
            id="BLAME-unparseable",
        ),
        pytest.param(
            {},
            ["abcdef0\n", "", 0],
            ["abcdef0123456789 (First Author 2024-01-02 00:00:00 +0000 1) code", "", 0],
            "surfaced",
            "medium",
            "First Author",
            "2024-01-02",
            "",
            id="BLAME-reverse-prefix-not-new",
        ),
        pytest.param(
            {},
            None,
            [
                "1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old\n"
                "abcdef0 (First Author 2024-01-02 00:00:00 +0000 1) code",
                "",
                0,
            ],
            "new",
            "high",
            "Old Author",
            "2020-03-04",
            "",
            id="BLAME-mixed-new-old",
        ),
        pytest.param(
            {"severity": "medium"},
            ("", "", 0),
            None,
            "surfaced",
            "low",
            "First Author",
            "2024-01-02",
            "",
            id="BLAME-medium-downgrade",
        ),
        pytest.param(
            {"cross_file_refs": ["other"], "severity": "strange"},
            None,
            None,
            "surfaced",
            "strange",
            None,
            None,
            "",
            id="BLAME-cross-unknown",
        ),
        pytest.param(
            {},
            ["", " log bad \n", 1],
            None,
            "new",
            "high",
            None,
            None,
            "classify_blame: git log failed for base 'base': log bad "
            "\u2014 classifying as 'new' (conservative).",
            id="BLAME-log-failure",
        ),
        pytest.param(
            {},
            ["\n", "", 0],
            [
                "^1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old\n"
                "abcdef0 (First Author 2024-01-02 00:00:00 +0000 1) code",
                "",
                0,
            ],
            "surfaced",
            "medium",
            "Old Author",
            "2020-03-04",
            "",
            id="BLAME-surfaced-first-author",
        ),
        pytest.param(
            {"severity": "strange"},
            ("", "", 0),
            ["1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old", "", 0],
            "surfaced",
            "strange",
            "Old Author",
            "2020-03-04",
            "",
            id="BLAME-surfaced-unknown",
        ),
    ],
)
def test_blame(
    patch,
    log,
    blame,
    origin,
    severity,
    author,
    date,
    warning,
    tmp_path,
    monkeypatch,
    verify_git,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\nadded\n", encoding="utf-8")
    finding = {
        "file": "source",
        "line_start": 1,
        "line_end": 2,
        "severity": "high",
        **patch,
    }
    original_severity = finding["severity"]
    calls, replies = verify_git
    if log is not None:
        replies["log"] = log
    if blame is not None:
        replies["blame"] = blame
    assert (
        verify.classify_blame(finding, verify.VerifyContext(str(tmp_path), "base"))
        == origin
    )
    assert finding["severity"] == severity
    assert finding["blame_metadata"] == {
        "classification": origin,
        "author": author,
        "date": date,
        "original_severity": original_severity,
    }
    expected_commands = (
        []
        if patch.get("cross_file_refs") or patch.get("file") == "missing"
        else [["git", "log", "--format=%H", "--end-of-options", "base..HEAD"]]
    )
    if expected_commands and (log is None or log[2] == 0):
        expected_commands.append(["git", "blame", "-L1,2", "--", "source"])
    assert calls == [(cmd, {}) for cmd in expected_commands]
    assert capsys.readouterr().err == (f"WARNING: {warning}\n" if warning else "")


@pytest.mark.parametrize(
    ("description", "evidence", "symbols"),
    [
        pytest.param(
            "```\nblock_symbol\n``` `inline_symbol`",
            "",
            ["block_symbol", "inline_symbol"],
            id="SYMBOL-block-inline",
        ),
        pytest.param(
            "`os.path.join` grantTypeShortcut.equals(substring(3, 5)) "
            "foo.bar(baz.qux(nested.value))",
            "",
            [
                "bar",
                "baz",
                "equals",
                "foo",
                "grantTypeShortcut",
                "join",
                "nested",
                "path",
                "qux",
                "substring",
                "value",
            ],
            id="SYMBOL-dotted-chained-nested",
        ),
        pytest.param(
            "", "`important_func`", ["important_func"], id="SYMBOL-evidence-only"
        ),
        pytest.param(
            "`9abc` `foo-bar` good_name.9abc",
            "",
            ["foobar", "good_name"],
            id="SYMBOL-invalid-identifiers-and-cleanup",
        ),
        pytest.param(
            "obj.method() Foo::bar other->value thing[slot] hash#tag get_user_data",
            "",
            [
                "Foo",
                "bar",
                "get_user_data",
                "hash",
                "method",
                "obj",
                "other",
                "slot",
                "tag",
                "thing",
                "value",
            ],
            id="SYMBOL-bare-punctuation-snake",
        ),
        pytest.param(
            "`self` `None` `print` `x` `ab`", "", [], id="SYMBOL-stopwords-short"
        ),
        pytest.param(
            "```lang\nAlphaType BetaType\n```",
            "",
            ["AlphaType", "BetaType", "langAlphaTypeBetaType"],
            id="SYMBOL-fenced-identifiers",
        ),
        pytest.param(
            "`Left",
            "Right`",
            ["LeftRight"],
            id="SYMBOL-cross-field-separator",
        ),
        pytest.param(
            "`XhandlerX`",
            "",
            ["XhandlerX"],
            id="SYMBOL-quoted-X-preserved",
        ),
        pytest.param("`_private`", "", ["private"], id="SYMBOL-underscore-backtick"),
        pytest.param(None, None, [], id="SYMBOL-empty-None"),
    ],
)
def test_symbol(description, evidence, symbols, tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("plain\n", encoding="utf-8")
    finding = {
        "file": "source",
        "line_start": 1,
        "description": description,
        "evidence": evidence,
        "confidence": 80,
    }
    assert (
        verify.verify_factual(finding, verify.VerifyContext(str(tmp_path), "base"))
        is True
    )
    assert finding["confidence"] == 80
    assert verify_git[0] == [
        (["git", "grep", "-l", "-e", symbol], {"timeout": 3, "cwd": str(tmp_path)})
        for symbol in symbols
    ]
    assert finding["factual_verification"] == {
        "verified": True,
        "reason": "file content and symbols verified"
        if symbols
        else f"no extractable symbols {DASH} verification skipped",
        "code_at_lines": "plain",
    }


@pytest.mark.parametrize(
    (
        "patch",
        "mode",
        "content",
        "keep",
        "confidence",
        "metadata",
        "attempted",
        "warnings",
    ),
    [
        pytest.param(
            {"line_start": None},
            "normal",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            80,
            {
                "verified": True,
                "reason": "no line reference \u2014 verification skipped",
                "code_at_lines": None,
            },
            [],
            "",
            id="FACT-no-line",
        ),
        pytest.param(
            {"file": "missing"},
            "normal",
            b"prefix_local_symbol_suffix\ntail\n",
            False,
            0,
            {
                "verified": False,
                "reason": "file not found: 'missing'",
                "code_at_lines": None,
            },
            [],
            "",
            id="FACT-missing-path",
        ),
        pytest.param(
            {},
            "read-error",
            b"prefix_local_symbol_suffix\ntail\n",
            False,
            0,
            {
                "verified": False,
                "reason": "could not read file 'source': read denied",
                "code_at_lines": None,
            },
            [],
            "",
            id="FACT-read-OSError",
        ),
        pytest.param(
            {},
            "binary",
            b"\xff",
            True,
            80,
            {
                "verified": True,
                "reason": "binary file \u2014 verification skipped",
                "code_at_lines": None,
            },
            [],
            "WARNING: verify_factual: binary file 'source' \u2014 skipping factual check.\n",
            id="FACT-binary",
        ),
        pytest.param(
            {"line_start": 3},
            "normal",
            b"prefix_local_symbol_suffix\ntail\n",
            False,
            0,
            {
                "verified": False,
                "reason": "line_start 3 out of range (file has 2 line(s))",
                "code_at_lines": None,
            },
            [],
            "",
            id="FACT-start-after",
        ),
        pytest.param(
            {"line_start": -1},
            "normal",
            b"prefix_local_symbol_suffix\ntail\n",
            False,
            0,
            {
                "verified": False,
                "reason": "line_start -1 out of range (file has 2 line(s))",
                "code_at_lines": None,
            },
            [],
            "",
            id="FACT-start-before",
        ),
        pytest.param(
            {"line_end": 99},
            "normal",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            80,
            {
                "verified": True,
                "reason": "no extractable symbols \u2014 verification skipped",
                "code_at_lines": "prefix_local_symbol_suffix\ntail",
            },
            [],
            "",
            id="FACT-end-clamped",
        ),
        pytest.param(
            {"description": "`alpha` `beta` `gamma` `local_symbol`"},
            "mixed",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            62,
            {
                "verified": False,
                "reason": "referenced symbol(s) not found in codebase: beta",
                "code_at_lines": "prefix_local_symbol_suffix",
                "original_confidence": 80,
                "symbols_checked": 4,
                "symbols_missing": 1,
            },
            ["alpha", "beta", "gamma"],
            "WARNING: verify_factual: git grep error (rc=2) for symbol 'gamma': bad \u2014 "
            "skipping.\n",
            id="FACT-proportional-mixed",
        ),
        pytest.param(
            {"description": "`alpha` `beta` `gamma` `local_symbol`", "confidence": 100},
            "missing",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            48,
            {
                "verified": False,
                "reason": "referenced symbol(s) not found in codebase: alpha, beta, gamma",
                "code_at_lines": "prefix_local_symbol_suffix",
                "original_confidence": 100,
                "symbols_checked": 4,
                "symbols_missing": 3,
            },
            ["alpha", "beta", "gamma"],
            "",
            id="FACT-penalty-base-70",
        ),
        pytest.param(
            {"description": "`alpha`", "confidence": None},
            "default",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            30,
            {
                "verified": False,
                "reason": "referenced symbol(s) not found in codebase: alpha",
                "code_at_lines": "prefix_local_symbol_suffix",
                "original_confidence": 100,
                "symbols_checked": 1,
                "symbols_missing": 1,
            },
            ["alpha"],
            "",
            id="FACT-default-confidence",
        ),
        pytest.param(
            {"description": "`alpha` `beta`"},
            "empty",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            30,
            {
                "verified": False,
                "reason": "referenced symbol(s) not found in codebase: alpha, beta",
                "code_at_lines": "prefix_local_symbol_suffix",
                "original_confidence": 80,
                "symbols_checked": 2,
                "symbols_missing": 2,
            },
            ["alpha", "beta"],
            "",
            id="FACT-rc1-empty-rc0",
        ),
        pytest.param(
            {"description": "`alpha`"},
            "timeout",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            80,
            {
                "verified": True,
                "reason": "file content and symbols verified",
                "code_at_lines": "prefix_local_symbol_suffix",
            },
            ["alpha"],
            "WARNING: verify_factual: symbol search timed out for 'alpha' \u2014 skipping "
            "(Phase 5 will validate).\n",
            id="FACT-timeout",
        ),
        pytest.param(
            {"description": "`alpha`"},
            "fatal",
            b"prefix_local_symbol_suffix\ntail\n",
            True,
            80,
            {
                "verified": True,
                "reason": "file content and symbols verified",
                "code_at_lines": "prefix_local_symbol_suffix",
            },
            ["alpha"],
            "WARNING: verify_factual: git grep error (rc=128) for symbol 'alpha': fatal \u2014 "
            "skipping.\n",
            id="FACT-fatal",
        ),
        pytest.param(
            {"file": ""},
            "normal",
            b"prefix_local_symbol_suffix\ntail\n",
            False,
            0,
            {"verified": False, "reason": "file not found: ''", "code_at_lines": None},
            [],
            "",
            id="FACT-empty-path-diagnostic",
        ),
        pytest.param(
            {},
            "trailing-X",
            b"codeX\n",
            True,
            80,
            {
                "verified": True,
                "reason": "no extractable symbols \u2014 verification skipped",
                "code_at_lines": "codeX",
            },
            [],
            "",
            id="FACT-preserve-trailing-X",
        ),
    ],
)
def test_fact(
    patch,
    mode,
    content,
    keep,
    confidence,
    metadata,
    attempted,
    warnings,
    tmp_path,
    monkeypatch,
    verify_git,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_bytes(content)
    finding = {"file": "source", "line_start": 1, "confidence": 80, **patch}
    calls, replies = verify_git
    if mode == "read-error":
        original = builtins.open

        def open_file(path, *args, **kwargs):
            if path == "source":
                raise OSError("read denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", open_file)
    if mode in ("missing", "default"):
        replies["grep"] = ("", "", 1)
        if mode == "default":
            del finding["confidence"]
    elif mode == "mixed":
        replies["grep"] = lambda argv: {
            "alpha": ["found", "", 0],
            "beta": ["", "", 1],
            "gamma": ["", " bad \n", 2],
        }[argv[4]]
    elif mode == "empty":
        replies["grep"] = lambda argv: ("", "", 0 if argv[4] == "beta" else 1)
    elif mode == "timeout":
        replies["grep"] = proc.TimeoutExpired(["git", "grep"], 3)
    elif mode == "fatal":
        replies["grep"] = ("", "fatal", 128)
    assert (
        verify.verify_factual(finding, verify.VerifyContext(str(tmp_path), "base"))
        is keep
    )
    assert finding["confidence"] == confidence
    assert finding["factual_verification"] == metadata
    assert calls == [
        (["git", "grep", "-l", "-e", symbol], {"timeout": 3, "cwd": str(tmp_path)})
        for symbol in attempted
    ]
    assert capsys.readouterr().err == warnings


@pytest.mark.parametrize(
    ("patch", "diff", "validation", "origin", "severity"),
    [
        pytest.param(
            {},
            None,
            {"in_diff": None, "reason": "diff validation skipped"},
            "new",
            "high",
            id="DIFF-skipped",
        ),
        pytest.param(
            {"line_start": None},
            "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n",
            {"in_diff": True, "reason": "no line reference \u2014 validation skipped"},
            "new",
            "high",
            id="DIFF-no-line",
        ),
        pytest.param(
            {"line_start": 50, "line_end": 55},
            "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n",
            {
                "in_diff": False,
                "reason": "lines 50-55 of 'source' not found in diff \u2014 tagged as surfaced "
                "(was: new)",
            },
            "surfaced",
            "medium",
            id="DIFF-out-downgrade",
        ),
        pytest.param(
            {"line_start": 2, "line_end": 5},
            "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n",
            {"in_diff": True, "reason": "line 2 found in diff"},
            "new",
            "high",
            id="DIFF-partial-overlap",
        ),
        pytest.param(
            {"line_start": 50, "blame_metadata": {"classification": "surfaced"}},
            "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n",
            {
                "in_diff": False,
                "reason": "lines 50-50 of 'source' not found in diff \u2014 tagged as surfaced "
                "(was: new)",
            },
            "surfaced",
            "high",
            id="DIFF-blame-surfaced-no-double",
        ),
    ],
)
def test_diff(patch, diff, validation, origin, severity):
    finding = {
        "file": "source",
        "line_start": 1,
        "origin": "new",
        "severity": "high",
        **patch,
    }
    facts = None if diff is None else parse_diff(diff, policy="verify-both-spellings")
    assert verify.validate_diff_lines(finding, facts) is True
    assert finding["diff_validation"] == validation
    assert (finding["origin"], finding["severity"]) == (origin, severity)


@pytest.mark.parametrize(
    ("path", "reply", "expected", "stderr", "commands"),
    [
        pytest.param(
            "patch",
            ("patch", "", 0),
            "patch",
            "Diff source: --diff-file (patch), 5 bytes\n",
            [],
            id="SOURCE-provided",
        ),
        pytest.param(
            "missing",
            ("patch", "", 0),
            None,
            "WARNING: Could not read diff file 'missing': [Errno 2] No such file or "
            "directory: 'missing'\n",
            [],
            id="SOURCE-missing-no-git",
        ),
        pytest.param(
            None,
            ("patch", "", 0),
            "patch",
            "Diff source: git diff base...HEAD (three-dot), 5 bytes\n",
            [["git", "diff", "--end-of-options", "base...HEAD"]],
            id="SOURCE-three-dot",
        ),
        pytest.param(
            None,
            lambda argv: (
                ("", " no merge \n", 1) if len(argv) == 4 else ("patch", "", 0)
            ),
            "patch",
            "WARNING: git diff base...HEAD failed (exit 1): no merge. Falling back to git "
            "diff base HEAD (two-dot).\n"
            "Diff source: git diff base HEAD (two-dot fallback), 5 bytes\n",
            [
                ["git", "diff", "--end-of-options", "base...HEAD"],
                ["git", "diff", "--end-of-options", "base", "HEAD"],
            ],
            id="SOURCE-two-dot",
        ),
        pytest.param(
            None,
            lambda argv: (
                ("", " no merge \n", 1) if len(argv) == 4 else ("", " bad base \n", 2)
            ),
            None,
            "WARNING: git diff base...HEAD failed (exit 1): no merge. Falling back to git "
            "diff base HEAD (two-dot).\n"
            "WARNING: git diff base HEAD also failed (exit 2): bad base. Diff validation "
            "will be skipped.\n",
            [
                ["git", "diff", "--end-of-options", "base...HEAD"],
                ["git", "diff", "--end-of-options", "base", "HEAD"],
            ],
            id="SOURCE-both-fail",
        ),
    ],
)
def test_source(
    path, reply, expected, stderr, commands, tmp_path, monkeypatch, verify_git, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "patch").write_text("patch", encoding="utf-8")
    calls, replies = verify_git
    replies["diff"] = reply
    assert (
        verify.get_diff(verify.VerifyContext(str(tmp_path), "base"), path) == expected
    )
    assert calls == [(cmd, {}) for cmd in commands]
    assert capsys.readouterr().err == stderr


def test_pipe(tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\nadded\n", encoding="utf-8")
    finding = {
        "id": "a",
        "file": "source",
        "line_start": 1,
        "severity": "high",
        "confidence": 80,
    }
    verify_git[1]["diff"] = ("", "bad", 1)
    result = verify.run_verification(
        [finding], verify.VerifyContext(str(tmp_path), "base")
    )
    assert result["stats"] == {"total": 1, "new": 1, "surfaced": 0, "eliminated": 0}
    assert list(result["stats"]) == ["total", "new", "surfaced", "eliminated"]
    assert result["verified"] == [finding]
    assert finding["severity"] == "high"
    assert finding["diff_validation"] == {
        "in_diff": None,
        "reason": "diff validation skipped",
    }
    assert [cmd for cmd, _kwargs in verify_git[0] if cmd[1] == "diff"] == [
        ["git", "diff", "--end-of-options", "base...HEAD"],
        ["git", "diff", "--end-of-options", "base", "HEAD"],
    ]


def test_pipe_projection(tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\nadded\n", encoding="utf-8")
    (tmp_path / "patch").write_text("", encoding="utf-8")
    findings = [
        {
            "id": "cross",
            "file": "source",
            "line_start": 1,
            "severity": "critical",
            "confidence": 80,
            "cross_file_refs": ["other"],
        },
        {
            "id": "symbol",
            "file": "source",
            "line_start": 1,
            "severity": "high",
            "confidence": 80,
            "description": "`absent_symbol`",
        },
        {
            "id": "range",
            "file": "source",
            "line_start": 3,
            "line_end": 4,
            "severity": "medium",
            "confidence": 80,
        },
        {
            "id": "missing",
            "file": "missing",
            "line_start": 1,
            "severity": "low",
            "confidence": 80,
        },
        {
            "id": "clean",
            "file": "source",
            "line_start": 1,
            "line_end": 2,
            "severity": "low",
            "confidence": 55,
            "evidence": "`code`",
        },
    ]
    verify_git[1]["grep"] = ("", "", 1)
    extra = [
        {
            **copy.deepcopy(f),
            "title": "extra",
            "dimension": "bug",
            "agent": "reader",
            "suggestion": "unused",
            "criticality": 7,
        }
        for f in findings
    ]
    for inputs in (findings, extra):
        result = verify.run_verification(
            inputs, verify.VerifyContext(str(tmp_path), "base"), "patch"
        )
        assert result["stats"] == {"total": 5, "new": 0, "surfaced": 3, "eliminated": 2}
        assert list(result["stats"]) == ["total", "new", "surfaced", "eliminated"]
        assert all(
            kept is inputs[index]
            for kept, index in zip(result["verified"], (0, 1, 4), strict=True)
        )
        assert all(
            gone is inputs[index]
            for gone, index in zip(result["eliminated"], (2, 3), strict=True)
        )
        assert [f["id"] for f in result["verified"]] == ["cross", "symbol", "clean"]
        assert wire.build_deltas(inputs, result["verified"]) == [
            {
                "id": "cross",
                "verified": True,
                "origin": "surfaced",
                "severity": "high",
                "confidence": 80,
            },
            {
                "id": "symbol",
                "verified": True,
                "origin": "surfaced",
                "severity": "medium",
                "confidence": 30,
            },
            {
                "id": "range",
                "verified": False,
                "origin": "new",
                "severity": "medium",
                "confidence": 0,
                "elimination_reason": "evidence does not match file content",
            },
            {
                "id": "missing",
                "verified": False,
                "origin": "new",
                "severity": "low",
                "confidence": 0,
                "elimination_reason": "evidence does not match file content",
            },
            {
                "id": "clean",
                "verified": True,
                "origin": "surfaced",
                "severity": "low",
                "confidence": 55,
            },
        ]


@pytest.mark.parametrize(
    "root",
    [
        pytest.param("reviewed", id="CTX-reviewed-repo-cwd"),
        pytest.param("empty", id="CTX-empty-root-fallback"),
    ],
)
def test_context_root(root, verify_git, tmp_path):
    calls, replies = verify_git
    replies["rev-parse"] = (
        (str(tmp_path) + "\n", "", 0) if root == "reviewed" else ("", "", 0)
    )
    expected = (
        str(tmp_path)
        if root == "reviewed"
        else str(Path(__file__).resolve().parents[1] / "scripts")
    )
    assert verify.resolve_repo_root() == expected
    assert calls == [(["git", "rev-parse", "--show-toplevel"], {})]


def test_context_log_queries(invoke, tmp_path, verify_git):
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    (tmp_path / "patch").write_text(DIFF, encoding="utf-8")
    calls, replies = verify_git
    token = '{"findings":[{"file":"source","line_start":1,"description":"%60remote_symbol%60"},{"file":"source","line_start":1}]}'
    warnings = f"WARNING: classify_blame: git log failed for base 'base': bad {DASH} classifying as 'new' (conservative).\n"
    for root in ["repo-one", "repo-two"]:
        replies["rev-parse"] = (str(tmp_path / root), "", 0)
        replies["log"] = ("", "bad", 1)
        result = receipt(
            invoke, tmp_path, token, ["--base-branch", "base", "--diff-file", "patch"]
        )
        env = json.loads(result.stdout)
        assert env["status"] == "ok"
        assert env["result"]["stats"] == {
            "total": 2,
            "new": 2,
            "surfaced": 0,
            "eliminated": 0,
        }
        assert (
            result.stderr.decode()
            == warnings * 2 + "Diff source: --diff-file (patch), 55 bytes\n"
        )
    assert [cmd for cmd, _ in calls if cmd[1] == "log"] == [
        ["git", "log", "--format=%H", "--end-of-options", "base..HEAD"]
    ] * 2
    assert [cmd for cmd, _ in calls if cmd[1] == "rev-parse"] == [
        ["git", "rev-parse", "--show-toplevel"]
    ] * 2
    assert [kwargs for cmd, kwargs in calls if cmd[1] == "grep"] == [
        {"timeout": 3, "cwd": str(tmp_path / "repo-one")},
        {"timeout": 3, "cwd": str(tmp_path / "repo-two")},
    ]


def test_context_import_help_git(monkeypatch, capsys):
    calls = []

    def output(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("import/help must not run git")

    monkeypatch.setattr(proc, "output", output)
    monkeypatch.setattr(proc, "run", output)
    package = importlib.import_module("gauntlet.verify")
    for module in (package, verify, wire):
        importlib.reload(module)
    assert calls == []
    with pytest.raises(SystemExit) as caught:
        wire.CLI.invoke(["--help"])
    assert caught.value.code == 0
    assert calls == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "token,stats,deltas,ids",
    [
        pytest.param(
            '{"findings":[],"base_branch":"%uDCFF"}',
            {"total": 0, "new": 0, "surfaced": 0, "eliminated": 0},
            [],
            [],
            id="CTX-empty-surrogate-base",
        ),
        pytest.param(
            '{"findings":[{"id":"cross","severity":"critical","cross_file_refs":["other"]}]}',
            {"total": 1, "new": 0, "surfaced": 1, "eliminated": 0},
            [
                {
                    "id": "cross",
                    "verified": True,
                    "origin": "surfaced",
                    "severity": "high",
                }
            ],
            ["cross"],
            id="CTX-cross-only-no-log",
        ),
    ],
)
def test_context_lazy_regression(
    token, stats, deltas, ids, invoke, tmp_path, verify_git
):
    (tmp_path / "patch").write_text("", encoding="utf-8")
    result = receipt(invoke, tmp_path, token, ["--diff-file", "patch"])
    assert result.returncode == 0
    env = json.loads(result.stdout)
    assert env["status"] == "ok"
    assert not [cmd for cmd, _ in verify_git[0] if cmd[1] == "log"]
    assert env["result"]["stats"] == stats
    assert env["result"]["deltas"] == deltas
    assert [f["id"] for f in env["result"]["verified"]] == ids
    assert env["result"]["eliminated"] == []


def foreign_reads(source):
    tree = ast.parse(source)
    allowed = set(VERIFY_SLICE_FIELDS) | (
        verify.FindingWire.__annotations__.keys()
        - VerifySliceFinding.__annotations__.keys()
        - {"line", "end_line"}
    )
    document_sites = {
        "validate_input_shape": {"findings"},
        "validate_diff_lines": {"classification"},
        "run_receipt": {"findings", "base_branch", "verified"},
    }
    bad = []
    sites = {
        id(node): function.name
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(function)
    }
    for node in ast.walk(tree):
        site = sites.get(id(node), "<module>")
        key = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if isinstance(node.value, ast.Name) and node.value.id == "Literal":
                continue
            key = node.slice
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            key = node.args[0]
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if key.value in allowed or key.value in document_sites.get(site, set()):
            continue
        bad.append((site, key.value))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_NUMERIC_FIELDS"
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Tuple)
            bad.extend(
                ("_NUMERIC_FIELDS", item.value)
                for item in node.value.elts
                if isinstance(item, ast.Constant)
                and item.value not in allowed | {"line", "end_line"}
            )
    return bad


def test_field_guard_live():
    for path in Path(verify.__file__).parent.glob("*.py"):
        assert foreign_reads(path.read_text(encoding="utf-8")) == [], str(path)


def test_field_guard_added_unprojected_read():
    source = """def verify_factual(renamed):
    renamed["unknown_write"] = 1
    if enabled:
        return renamed.get("title"), renamed["line"], renamed.get("end_line"), finding.get("classification")
"""
    assert foreign_reads(source) == [
        ("verify_factual", "title"),
        ("verify_factual", "line"),
        ("verify_factual", "end_line"),
        ("verify_factual", "classification"),
    ]


@pytest.mark.parametrize(
    "mechanism",
    [
        pytest.param("log", id="ARGV-log-terminator"),
        pytest.param("diff", id="ARGV-diff-terminator"),
        pytest.param("grep", id="ARGV-grep-pattern-option"),
    ],
)
def test_leading_dash_argv(mechanism, tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    calls, replies = verify_git
    context = verify.VerifyContext(str(tmp_path), "-base")
    finding = {"file": "source", "line_start": 1}
    if mechanism == "log":
        assert verify.classify_blame(finding, context) == "new"
        assert calls == [
            (["git", "log", "--format=%H", "--end-of-options", "-base..HEAD"], {}),
            (["git", "blame", "-L1,1", "--", "source"], {}),
        ]
    elif mechanism == "diff":
        replies["diff"] = lambda argv: (
            ("", "bad", 1) if len(argv) == 4 else ("patch", "", 0)
        )
        assert verify.get_diff(context) == "patch"
        assert calls == [
            (["git", "diff", "--end-of-options", "-base...HEAD"], {}),
            (["git", "diff", "--end-of-options", "-base", "HEAD"], {}),
        ]
    else:
        monkeypatch.setattr(verify, "_extract_symbols", lambda *_: {"-symbol"})
        assert verify.verify_factual(finding, context) is True
        assert calls == [
            (
                ["git", "grep", "-l", "-e", "-symbol"],
                {"timeout": 3, "cwd": str(tmp_path)},
            )
        ]


def test_null_line_end_and_diff_policy(tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\nadded\n", encoding="utf-8")
    (tmp_path / "patch").write_text(
        "--- source\n+++ source\n@@ -1 +1 @@\n-code\n+code\n", encoding="utf-8"
    )
    finding = {"id": "a", "file": "b/source", "line_start": 1, "line_end": None}
    (tmp_path / "b").mkdir()
    (tmp_path / "b/source").write_text("code\nadded\n", encoding="utf-8")
    policies = []
    original = verify.parse_diff

    def parse(text, *, policy):
        policies.append(policy)
        return original(text, policy=policy)

    monkeypatch.setattr(verify, "parse_diff", parse)
    result = verify.run_verification(
        [finding], verify.VerifyContext(str(tmp_path), "base"), "patch"
    )
    assert policies == ["verify-both-spellings"]
    assert verify_git[0] == [
        (["git", "log", "--format=%H", "--end-of-options", "base..HEAD"], {}),
        (["git", "blame", "-L1,1", "--", "b/source"], {}),
    ]
    assert finding["factual_verification"] == {
        "verified": True,
        "reason": "no extractable symbols \u2014 verification skipped",
        "code_at_lines": "code",
    }
    assert finding["diff_validation"] == {
        "in_diff": True,
        "reason": "line 1 found in diff",
    }
    assert result["stats"] == {"total": 1, "new": 1, "surfaced": 0, "eliminated": 0}


def test_blame_default_line(tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    finding = {"file": "source"}
    assert (
        verify.classify_blame(finding, verify.VerifyContext(str(tmp_path), "base"))
        == "new"
    )
    assert finding["blame_metadata"] == {
        "classification": "new",
        "author": "First Author",
        "date": "2024-01-02",
        "original_severity": "",
    }
    assert verify_git[0] == [
        (["git", "log", "--format=%H", "--end-of-options", "base..HEAD"], {}),
        (["git", "blame", "-L1,1", "--", "source"], {}),
    ]


def test_diff_default_origin_and_range_end():
    finding = {"file": "source", "line_start": 1, "line_end": 1, "severity": "high"}
    facts = parse_diff(
        "--- a/source\n+++ b/source\n@@ -2 +2 @@\n-old\n+new\n",
        policy="verify-both-spellings",
    )
    assert verify.validate_diff_lines(finding, facts) is True
    assert finding == {
        "file": "source",
        "line_start": 1,
        "line_end": 1,
        "severity": "medium",
        "origin": "surfaced",
        "diff_validation": {
            "in_diff": False,
            "reason": "lines 1-1 of 'source' not found in diff \u2014 tagged as surfaced (was: new)",
        },
    }


def test_pipe_factual_warning_before_diff(tmp_path, monkeypatch, verify_git, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    (tmp_path / "patch").write_text("", encoding="utf-8")
    verify_git[1]["grep"] = ("", "fatal", 128)
    result = verify.run_verification(
        [{"file": "source", "line_start": 1, "description": "`alpha`"}],
        verify.VerifyContext(str(tmp_path), "base"),
        "patch",
    )
    assert result["stats"] == {"total": 1, "new": 0, "surfaced": 1, "eliminated": 0}
    assert capsys.readouterr().err == (
        "WARNING: verify_factual: git grep error (rc=128) for symbol 'alpha': fatal \u2014 skipping.\n"
        "Diff source: --diff-file (patch), 0 bytes\n"
    )
