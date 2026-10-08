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
BLAMED = "abcdef0 (First Author 2024-01-02 00:00:00 +0000 1) code"
OLD = "1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old"
DIFF = "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(row[1:], id=row[0])
        for row in json.loads(r"""[
        ["BLAME-cross-critical", {"cross_file_refs": ["other"], "severity": "critical"}, null, null, "surfaced", "high", null, null, ""],
        ["BLAME-cross-low", {"cross_file_refs": ["other"], "severity": "low"}, null, null, "surfaced", "low", null, null, ""],
        ["BLAME-cross-unknown", {"cross_file_refs": ["other"], "severity": "strange"}, null, null, "surfaced", "strange", null, null, ""],
        ["BLAME-missing-file", {"file": "missing"}, null, null, "new", "high", null, null, "classify_blame: file not found 'missing' \u2014 classifying as 'new' (conservative)."],
        ["BLAME-log-failure", {}, ["", " log bad \n", 1], null, "new", "high", null, null, "classify_blame: git log failed for base 'base': log bad \u2014 classifying as 'new' (conservative)."],
        ["BLAME-log-empty-stderr", {}, ["", "", 1], null, "new", "high", null, null, "classify_blame: git log failed for base 'base':  \u2014 classifying as 'new' (conservative)."],
        ["BLAME-blame-failure", {}, null, ["", " blame bad \n", 1], "new", "high", null, null, "classify_blame: git blame failed for 'source': blame bad \u2014 classifying as 'new' (conservative)."],
        ["BLAME-binary", {}, null, ["", "BINARY data", 1], "new", "high", null, null, "classify_blame: binary file 'source' \u2014 classifying as 'new' (conservative)."],
        ["BLAME-unparseable", {}, null, ["no blame", "", 0], "new", "high", null, null, "classify_blame: could not parse blame output for 'source' lines 1-2 \u2014 classifying as 'new' (conservative)."],
        ["BLAME-short-prefix-new", {}, null, null, "new", "high", "First Author", "2024-01-02", ""],
        ["BLAME-reverse-prefix-not-new", {}, ["abcdef0\n", "", 0], ["abcdef0123456789 (First Author 2024-01-02 00:00:00 +0000 1) code", "", 0], "surfaced", "medium", "First Author", "2024-01-02", ""],
        ["BLAME-mixed-new-old", {}, null, ["1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old\nabcdef0 (First Author 2024-01-02 00:00:00 +0000 1) code", "", 0], "new", "high", "Old Author", "2020-03-04", ""],
        ["BLAME-surfaced-first-author", {}, ["\n", "", 0], ["^1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old\nabcdef0 (First Author 2024-01-02 00:00:00 +0000 1) code", "", 0], "surfaced", "medium", "Old Author", "2020-03-04", ""],
        ["BLAME-surfaced-unknown", {"severity": "strange"}, ["", "", 0], ["1234567 (Old Author 2020-03-04 00:00:00 +0000 2) old", "", 0], "surfaced", "strange", "Old Author", "2020-03-04", ""]
    ]""")
    ],
)
def test_blame(case, tmp_path, monkeypatch, verify_git, capsys):
    patch, log, blame, origin, severity, author, date, warning = case
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
    json.loads(
        '[["```\\nblock_symbol\\n``` `inline_symbol`","",["block_symbol","inline_symbol"]],["`os.path.join` grantTypeShortcut.equals(substring(3, 5)) foo.bar(baz.qux(nested.value))","",["bar","baz","equals","foo","grantTypeShortcut","join","nested","path","qux","substring","value"]],["obj.method() Foo::bar other->value thing[slot] hash#tag get_user_data","",["Foo","bar","get_user_data","hash","method","obj","other","slot","tag","thing","value"]],["Concrete Between However Implementation Additionally Response","",[]],["`MyClass`\\n```\\nMyHandler\\n```","",["MyClass","MyHandler"]],["`self` `None` `print` `x` `ab`","",[]],["","`important_func`",["important_func"]],[null,null,[]]]'
    ),
    ids=json.loads(
        '["SYMBOL-block-inline","SYMBOL-dotted-chained-nested","SYMBOL-bare-punctuation-snake","SYMBOL-English-CamelCase","SYMBOL-quoted-CamelCase","SYMBOL-stopwords-short","SYMBOL-evidence-only","SYMBOL-empty-None"]'
    ),
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
    assert [cmd[4] for cmd, _ in verify_git[0]] == symbols
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
    "case",
    [
        pytest.param(row[1:], id=row[0])
        for row in json.loads(r"""[
        ["FACT-no-line", {"line_start": null}, "normal", true, 80, {"verified": true, "reason": "no line reference \u2014 verification skipped", "code_at_lines": null}, [], ""],
        ["FACT-missing-path", {"file": "missing"}, "normal", false, 0, {"verified": false, "reason": "file not found: 'missing'", "code_at_lines": null}, [], ""],
        ["FACT-read-OSError", {}, "read-error", false, 0, {"verified": false, "reason": "could not read file 'source': read denied", "code_at_lines": null}, [], ""],
        ["FACT-binary", {}, "binary", true, 80, {"verified": true, "reason": "binary file \u2014 verification skipped", "code_at_lines": null}, [], "WARNING: verify_factual: binary file 'source' \u2014 skipping factual check.\n"],
        ["FACT-start-before", {"line_start": -1}, "normal", false, 0, {"verified": false, "reason": "line_start -1 out of range (file has 2 line(s))", "code_at_lines": null}, [], ""],
        ["FACT-start-after", {"line_start": 3}, "normal", false, 0, {"verified": false, "reason": "line_start 3 out of range (file has 2 line(s))", "code_at_lines": null}, [], ""],
        ["FACT-end-clamped", {"line_end": 99}, "normal", true, 80, {"verified": true, "reason": "no extractable symbols \u2014 verification skipped", "code_at_lines": "prefix_local_symbol_suffix\ntail"}, [], ""],
        ["FACT-local-substring", {"description": "`local_symbol`"}, "normal", true, 80, {"verified": true, "reason": "file content and symbols verified", "code_at_lines": "prefix_local_symbol_suffix"}, [], ""],
        ["FACT-proportional-mixed", {"description": "`alpha` `beta` `gamma` `local_symbol`"}, "mixed", true, 62, {"verified": false, "reason": "referenced symbol(s) not found in codebase: beta", "code_at_lines": "prefix_local_symbol_suffix", "original_confidence": 80, "symbols_checked": 4, "symbols_missing": 1}, ["alpha", "beta", "gamma"], "WARNING: verify_factual: git grep error (rc=2) for symbol 'gamma': bad \u2014 skipping.\n"],
        ["FACT-penalty-base-70", {"description": "`alpha` `beta` `gamma` `local_symbol`", "confidence": 100}, "missing", true, 48, {"verified": false, "reason": "referenced symbol(s) not found in codebase: alpha, beta, gamma", "code_at_lines": "prefix_local_symbol_suffix", "original_confidence": 100, "symbols_checked": 4, "symbols_missing": 3}, ["alpha", "beta", "gamma"], ""],
        ["FACT-floor-30", {"description": "`alpha`", "confidence": 40}, "missing", true, 30, {"verified": false, "reason": "referenced symbol(s) not found in codebase: alpha", "code_at_lines": "prefix_local_symbol_suffix", "original_confidence": 40, "symbols_checked": 1, "symbols_missing": 1}, ["alpha"], ""],
        ["FACT-default-confidence", {"description": "`alpha`", "confidence": null}, "default", true, 30, {"verified": false, "reason": "referenced symbol(s) not found in codebase: alpha", "code_at_lines": "prefix_local_symbol_suffix", "original_confidence": 100, "symbols_checked": 1, "symbols_missing": 1}, ["alpha"], ""],
        ["FACT-rc1-empty-rc0", {"description": "`alpha` `beta`"}, "empty", true, 30, {"verified": false, "reason": "referenced symbol(s) not found in codebase: alpha, beta", "code_at_lines": "prefix_local_symbol_suffix", "original_confidence": 80, "symbols_checked": 2, "symbols_missing": 2}, ["alpha", "beta"], ""],
        ["FACT-timeout", {"description": "`alpha`"}, "timeout", true, 80, {"verified": true, "reason": "file content and symbols verified", "code_at_lines": "prefix_local_symbol_suffix"}, ["alpha"], "WARNING: verify_factual: symbol search timed out for 'alpha' \u2014 skipping (Phase 5 will validate).\n"],
        ["FACT-fatal", {"description": "`alpha`"}, "fatal", true, 80, {"verified": true, "reason": "file content and symbols verified", "code_at_lines": "prefix_local_symbol_suffix"}, ["alpha"], "WARNING: verify_factual: git grep error (rc=128) for symbol 'alpha': fatal \u2014 skipping.\n"]
    ]""")
    ],
)
def test_fact(case, tmp_path, monkeypatch, verify_git, capsys):
    patch, mode, keep, confidence, metadata, attempted, warnings = case
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_bytes(
        b"\xff" if mode == "binary" else b"prefix_local_symbol_suffix\ntail\n"
    )
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
        replies["grep"] = lambda argv: json.loads(
            '{"alpha":["found","",0],"beta":["","",1],"gamma":[""," bad \\n",2]}'
        )[argv[4]]
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
    "case",
    [
        pytest.param(row[1:], id=row[0])
        for row in json.loads(r"""[
        ["DIFF-skipped", {}, null, {"in_diff": null, "reason": "diff validation skipped"}, "new", "high"],
        ["DIFF-no-line", {"line_start": null}, "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n", {"in_diff": true, "reason": "no line reference \u2014 validation skipped"}, "new", "high"],
        ["DIFF-out-downgrade", {"line_start": 50, "line_end": 55}, "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n", {"in_diff": false, "reason": "lines 50-55 of 'source' not found in diff \u2014 tagged as surfaced (was: new)"}, "surfaced", "medium"],
        ["DIFF-partial-overlap", {"line_start": 2, "line_end": 5}, "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n", {"in_diff": true, "reason": "line 2 found in diff"}, "new", "high"],
        ["DIFF-blame-surfaced-no-double", {"line_start": 50, "blame_metadata": {"classification": "surfaced"}}, "--- a/source\n+++ b/source\n@@ -1,1 +1,2 @@\n code\n+added\n", {"in_diff": false, "reason": "lines 50-50 of 'source' not found in diff \u2014 tagged as surfaced (was: new)"}, "surfaced", "high"],
        ["DIFF-space-nonascii", {"file": "My Docs/caf\u00e9.md", "line_start": 2}, "--- a/My Docs/caf\u00e9.md\t\n+++ b/My Docs/caf\u00e9.md\t\n@@ -1,1 +1,2 @@\n intro\n+added\n", {"in_diff": true, "reason": "line 2 found in diff"}, "new", "high"]
    ]""")
    ],
)
def test_diff(case):
    patch, diff, validation, origin, severity = case
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
    "case",
    [
        pytest.param(row[1:], id=row[0])
        for row in json.loads(r"""[
        ["SOURCE-provided", "file", "patch", "Diff source: --diff-file (patch), 5 bytes\n", []],
        ["SOURCE-missing-no-git", "missing", null, "WARNING: Could not read diff file 'missing': [Errno 2] No such file or directory: 'missing'\n", []],
        ["SOURCE-three-dot", "three", "patch", "Diff source: git diff base...HEAD (three-dot), 5 bytes\n", [["git", "diff", "--end-of-options", "base...HEAD"]]],
        ["SOURCE-two-dot", "two", "patch", "WARNING: git diff base...HEAD failed (exit 1): no merge. Falling back to git diff base HEAD (two-dot).\nDiff source: git diff base HEAD (two-dot fallback), 5 bytes\n", [["git", "diff", "--end-of-options", "base...HEAD"], ["git", "diff", "--end-of-options", "base", "HEAD"]]],
        ["SOURCE-both-fail", "fail", null, "WARNING: git diff base...HEAD failed (exit 1): no merge. Falling back to git diff base HEAD (two-dot).\nWARNING: git diff base HEAD also failed (exit 2): bad base. Diff validation will be skipped.\n", [["git", "diff", "--end-of-options", "base...HEAD"], ["git", "diff", "--end-of-options", "base", "HEAD"]]],
        ["SOURCE-empty-present", "empty", "", "Diff source: git diff base...HEAD (three-dot), 0 bytes\n", [["git", "diff", "--end-of-options", "base...HEAD"]]]
    ]""")
    ],
)
def test_source(case, tmp_path, monkeypatch, verify_git, capsys):
    mode, expected, stderr, commands = case
    monkeypatch.chdir(tmp_path)
    (tmp_path / "patch").write_text("patch", encoding="utf-8")
    calls, replies = verify_git
    replies["diff"] = lambda argv: (
        ("", " no merge \n", 1)
        if len(argv) == 4 and mode in ("two", "fail")
        else ("", " bad base \n", 2)
        if mode == "fail"
        else ("", "", 0)
        if mode == "empty"
        else ("patch", "", 0)
    )
    path = "patch" if mode == "file" else "missing" if mode == "missing" else None
    assert (
        verify.get_diff(verify.VerifyContext(str(tmp_path), "base"), path) == expected
    )
    assert calls == [(cmd, {}) for cmd in commands]
    assert capsys.readouterr().err == stderr


@pytest.mark.parametrize(
    "mode,stats,severity,validation",
    json.loads(
        '[["siblings",{"total":2,"new":1,"surfaced":0,"eliminated":1},"high",{"in_diff":true,"reason":"line 1 found in diff"}],["absent",{"total":1,"new":1,"surfaced":0,"eliminated":0},"high",{"in_diff":null,"reason":"diff validation skipped"}],["empty",{"total":1,"new":0,"surfaced":1,"eliminated":0},"medium",{"in_diff":false,"reason":"lines 1-1 of \'source\' not found in diff \\u2014 tagged as surfaced (was: new)"}]]'
    ),
)
def test_pipe(mode, stats, severity, validation, tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\nadded\n", encoding="utf-8")
    (tmp_path / "patch").write_text(
        DIFF if mode == "siblings" else "", encoding="utf-8"
    )
    findings = [
        json.loads(
            '{"id":"a","file":"source","line_start":1,"severity":"high","confidence":80}'
        )
    ]
    if mode == "siblings":
        findings.append(
            json.loads(
                '{"id":"b","file":"missing","line_start":1,"severity":"high","confidence":80}'
            )
        )
    if mode == "absent":
        verify_git[1]["diff"] = ("", "bad", 1)
    result = verify.run_verification(
        findings,
        verify.VerifyContext(str(tmp_path), "base"),
        None if mode in ("absent", "empty") else "patch",
    )
    assert result["stats"] == stats
    assert list(result["stats"]) == ["total", "new", "surfaced", "eliminated"]
    assert result["verified"][0] is findings[0]
    assert findings[0]["severity"] == severity
    assert findings[0]["diff_validation"] == validation
    if mode == "siblings":
        assert result["eliminated"][0] is findings[1]
        assert wire.build_deltas(findings, result["verified"]) == json.loads(
            '[{"id":"a","verified":true,"origin":"new","severity":"high","confidence":80},{"id":"b","verified":false,"origin":"new","severity":"high","confidence":0,"elimination_reason":"evidence does not match file content"}]'
        )


def test_pipe_projection(tmp_path, monkeypatch, verify_git):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "source").write_text("code\nadded\n", encoding="utf-8")
    (tmp_path / "patch").write_text("", encoding="utf-8")
    findings = json.loads(
        '[{"id":"cross","file":"source","line_start":1,"severity":"critical","confidence":80,"cross_file_refs":["other"]},{"id":"symbol","file":"source","line_start":1,"severity":"high","confidence":80,"description":"`absent_symbol`"},{"id":"range","file":"source","line_start":3,"line_end":4,"severity":"medium","confidence":80},{"id":"missing","file":"missing","line_start":1,"severity":"low","confidence":80},{"id":"clean","file":"source","line_start":1,"line_end":2,"severity":"low","confidence":55,"evidence":"`code`"}]'
    )
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
        assert [f["id"] for f in result["verified"]] == ["cross", "symbol", "clean"]
        assert wire.build_deltas(inputs, result["verified"]) == json.loads(
            '[{"id":"cross","verified":true,"origin":"surfaced","severity":"high","confidence":80},{"id":"symbol","verified":true,"origin":"surfaced","severity":"medium","confidence":30},{"id":"range","verified":false,"origin":"new","severity":"medium","confidence":0,"elimination_reason":"evidence does not match file content"},{"id":"missing","verified":false,"origin":"new","severity":"low","confidence":0,"elimination_reason":"evidence does not match file content"},{"id":"clean","verified":true,"origin":"surfaced","severity":"low","confidence":55}]'
        )


@pytest.mark.parametrize(
    "root",
    ["reviewed", "fallback", "empty"],
    ids=json.loads(
        '["CTX-reviewed-repo-cwd","CTX-fallback-scripts-root","CTX-empty-root-fallback"]'
    ),
)
def test_context_root(root, verify_git, tmp_path):
    calls, replies = verify_git
    replies["rev-parse"] = (
        (str(tmp_path) + "\n", "", 0)
        if root == "reviewed"
        else ("", "", 1 if root == "fallback" else 0)
    )
    expected = (
        str(tmp_path)
        if root == "reviewed"
        else str(Path(__file__).resolve().parents[1] / "scripts")
    )
    assert verify.resolve_repo_root() == expected
    assert calls == [(["git", "rev-parse", "--show-toplevel"], {})]


@pytest.mark.parametrize(
    "failed",
    [True, False],
    ids=["CTX-failed-log-per-blame", "CTX-successive-runs-fresh"],
)
def test_context_log_queries(failed, invoke, tmp_path, verify_git):
    (tmp_path / "source").write_text("code\n", encoding="utf-8")
    (tmp_path / "patch").write_text(DIFF, encoding="utf-8")
    calls, replies = verify_git
    token = '{"findings":[{"file":"source","line_start":1,"description":"%60remote_symbol%60"},{"file":"source","line_start":1}]}'
    warnings = f"WARNING: classify_blame: git log failed for base 'base': bad {DASH} classifying as 'new' (conservative).\n"
    for iteration, root in enumerate(["repo-one", "repo-two"]):
        replies["rev-parse"] = (str(tmp_path / root), "", 0)
        replies["log"] = (
            ("", "bad", 1)
            if failed
            else ("abcdef0123456789\n", "", 0)
            if iteration == 0
            else ("", "", 0)
        )
        result = receipt(
            invoke, tmp_path, token, ["--base-branch", "base", "--diff-file", "patch"]
        )
        env = json.loads(result.stdout)
        assert env["status"] == "ok"
        assert env["result"]["stats"] == (
            {"total": 2, "new": 2, "surfaced": 0, "eliminated": 0}
            if failed or iteration == 0
            else {"total": 2, "new": 0, "surfaced": 2, "eliminated": 0}
        )
        assert (
            result.stderr.decode()
            == (warnings * 2 if failed else "")
            + "Diff source: --diff-file (patch), 55 bytes\n"
        )
    assert [cmd for cmd, _ in calls if cmd[1] == "log"] == json.loads(
        '[["git","log","--format=%H","--end-of-options","base..HEAD"]]'
    ) * 2
    assert [cmd for cmd, _ in calls if cmd[1] == "rev-parse"] == json.loads(
        '[["git","rev-parse","--show-toplevel"]]'
    ) * 2
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
    json.loads(
        '[["{\\"findings\\":[],\\"base_branch\\":\\"%uDCFF\\"}",{"total":0,"new":0,"surfaced":0,"eliminated":0},[],[]],["{\\"findings\\":[{\\"id\\":\\"cross\\",\\"severity\\":\\"critical\\",\\"cross_file_refs\\":[\\"other\\"]}]}",{"total":1,"new":0,"surfaced":1,"eliminated":0},[{"id":"cross","verified":true,"origin":"surfaced","severity":"high"}],["cross"]]]'
    ),
    ids=["CTX-empty-surrogate-base", "CTX-cross-only-no-log"],
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


@pytest.mark.parametrize("mechanism", ["log", "diff", "grep"])
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
    assert finding["factual_verification"] == json.loads(
        '{"verified":true,"reason":"no extractable symbols \\u2014 verification skipped","code_at_lines":"code"}'
    )
    assert finding["diff_validation"] == {
        "in_diff": True,
        "reason": "line 1 found in diff",
    }
    assert result["stats"] == {"total": 1, "new": 1, "surfaced": 0, "eliminated": 0}
