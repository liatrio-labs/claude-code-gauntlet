"""Injected roots replace host discovery and default policy stays pure."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet import tasks

from tests.support.artifacts import task_file

_REAL_DEFAULT_ROOTS = tasks._default_roots


@pytest.mark.parametrize(
    "tmpdir,uid,system_temp,expected",
    [
        (
            None,
            42,
            None,
            (os.path.join("base-a", "claude-42"), os.path.join("base-b", "claude-42")),
        ),
        (
            "extra" + os.sep * 3,
            42,
            None,
            (
                os.path.join("base-a", "claude-42"),
                os.path.join("base-b", "claude-42"),
                os.path.join("extra", "claude-42"),
            ),
        ),
        (
            os.sep,
            42,
            None,
            (
                os.path.join("base-a", "claude-42"),
                os.path.join("base-b", "claude-42"),
                os.path.join(os.sep, "claude-42"),
            ),
        ),
        (
            "",
            42,
            "unused",
            (os.path.join("base-a", "claude-42"), os.path.join("base-b", "claude-42")),
        ),
        (
            None,
            None,
            "system",
            (
                os.path.join("base-a", "claude"),
                os.path.join("base-b", "claude"),
                os.path.join("system", "claude"),
            ),
        ),
        (
            "extra",
            None,
            "system",
            (
                os.path.join("base-a", "claude"),
                os.path.join("base-b", "claude"),
                os.path.join("extra", "claude"),
                os.path.join("system", "claude"),
            ),
        ),
    ],
)
def test_default_root_candidate_policy_is_pure(
    tmpdir, uid, system_temp, expected, monkeypatch
):
    monkeypatch.setattr(
        os.path, "realpath", lambda *_: pytest.fail("pure policy resolved a path")
    )
    monkeypatch.setattr(
        os.path, "isdir", lambda *_: pytest.fail("pure policy probed a path")
    )
    result = tasks._root_candidates(("base-a", "base-b"), tmpdir, uid, system_temp)
    # Native separators keep this a policy test on Windows too.
    assert result == expected


@pytest.mark.parametrize(
    "uid,tmpdir,expected,realpath_inputs",
    [
        (
            42,
            None,
            (
                os.path.join("/tmp", "claude-42"),
                os.path.join("/private/tmp", "claude-42"),
            ),
            (
                os.path.join("/tmp", "claude-42"),
                os.path.join("/private/tmp", "claude-42"),
            ),
        ),
        (
            None,
            None,
            (
                os.path.join("/tmp", "claude"),
                os.path.join("/private/tmp", "claude"),
                os.path.join("fixture-system", "claude"),
            ),
            (
                os.path.join("/tmp", "claude"),
                os.path.join("/private/tmp", "claude"),
                os.path.join("fixture-system", "claude"),
            ),
        ),
        (
            None,
            "fixture-extra" + os.sep * 3,
            (
                os.path.join("/tmp", "claude"),
                os.path.join("/private/tmp", "claude"),
                os.path.join("fixture-extra", "claude"),
            ),
            (
                os.path.join("/tmp", "claude"),
                os.path.join("/private/tmp", "claude"),
                os.path.join("fixture-extra", "claude"),
                os.path.join("fixture-system", "claude"),
            ),
        ),
    ],
    ids=["uid-present", "no-getuid-system-temp", "TMPDIR-set"],
)
def test_real_default_discovery_preserves_order_and_deduplicates(
    uid, tmpdir, expected, realpath_inputs, monkeypatch
):
    resolved = []
    temp_calls = []

    def realpath(path):
        resolved.append(path)
        if tmpdir and path == os.path.join("fixture-system", "claude"):
            return os.path.join("fixture-extra", "claude")
        return path

    def system_temp():
        temp_calls.append(True)
        return "fixture-system"

    if uid is None:
        monkeypatch.delattr(os, "getuid", raising=False)
    else:
        monkeypatch.setattr(os, "getuid", lambda: uid, raising=False)
    monkeypatch.setattr(tasks.tempfile, "gettempdir", system_temp)
    monkeypatch.setattr(os.path, "realpath", realpath)
    monkeypatch.setattr(
        os.path, "isdir", lambda *_: pytest.fail("default discovery probed a host path")
    )
    monkeypatch.setattr(tasks, "_default_roots", _REAL_DEFAULT_ROOTS)
    environ = {} if tmpdir is None else {"TMPDIR": tmpdir}
    assert tasks.roots_from_environment(environ) == tasks.TaskRoots(expected, None)
    assert tuple(resolved) == realpath_inputs
    assert temp_calls == ([] if uid is not None else [True])


@pytest.mark.parametrize(
    "raw", [None, "", os.pathsep * 2], ids=["absent", "empty", "separators-only"]
)
def test_absent_or_empty_roots_select_default_branch(raw, monkeypatch):
    calls = []

    def supplied_default(tmpdir):
        calls.append(tmpdir)
        return ("fixture-default",)

    # Replace the branch with a pure provider; never execute host discovery.
    monkeypatch.setattr(tasks, "_default_roots", supplied_default)
    environ = {"TMPDIR": "fixture-temp", tasks.TASKS_DIR_ENV: "direct-override"}
    if raw is not None:
        environ[tasks.TASK_ROOTS_ENV] = raw
    roots = tasks.roots_from_environment(environ)
    assert roots == tasks.TaskRoots(("fixture-default",), "direct-override")
    assert calls == ["fixture-temp"]


@pytest.mark.parametrize(
    "parts,expected",
    [
        (("first", "", "second", "first"), ("first", "second")),
        (("[g]", "relative", "missing"), ("[g]", "relative", "missing")),
    ],
)
def test_explicit_roots_drop_empty_items_and_preserve_priority(parts, expected):
    roots = tasks.roots_from_environment({tasks.TASK_ROOTS_ENV: os.pathsep.join(parts)})
    assert roots == tasks.TaskRoots(expected, None)


def test_explicit_roots_realpath_deduplication_preserves_first_spelling(
    tmp_path, symlink_or_skip
):
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    roots = tasks.roots_from_environment(
        {
            tasks.TASK_ROOTS_ENV: os.pathsep.join(
                (str(alias), str(root), str(tmp_path / "missing"))
            ),
            tasks.TASKS_DIR_ENV: "override-spelling",
        }
    )
    assert roots == tasks.TaskRoots(
        (str(alias), str(tmp_path / "missing")), "override-spelling"
    )


def test_guard_fails_at_teardown_even_when_discovery_exception_is_swallowed(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_text(
        (repo / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    test = tmp_path / "test_guard.py"
    test.write_text(
        "import os\nfrom gauntlet import awaiting\n"
        "def test_swallowed(monkeypatch):\n"
        "    monkeypatch.delenv('CODE_GAUNTLET_TASK_ROOTS', raising=False)\n"
        "    assert awaiting.CLI.invoke(['bare-id', '--timeout-seconds', '0']) == 4\n",
        encoding="utf-8",
    )
    environ = os.environ.copy()
    environ["PYTHONPATH"] = str(repo / "scripts")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", test.name, "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "default task-root discovery was reached during the test" in result.stdout
    assert "1 passed, 1 error" in result.stdout


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("literal.output", id="RESOLVE-output-name"),
        pytest.param("dir/task", id="RESOLVE-separator"),
        pytest.param("dir!task", id="RESOLVE-altsep"),
    ],
)
def test_path_targets_bypass_roots(target, monkeypatch):
    monkeypatch.setattr(os, "altsep", "!")
    monkeypatch.setattr(os.path, "isdir", lambda *_: pytest.fail("path probed roots"))
    assert tasks.resolve_target(target, tasks.TaskRoots(("unused",), "unused")) == (
        target,
        [],
    )


@pytest.mark.parametrize(
    "mode,winner",
    [
        pytest.param("override", "direct", id="RESOLVE-direct-override"),
        pytest.param("miss", "a-new", id="RESOLVE-override-miss"),
        pytest.param("priority", "a-new", id="RESOLVE-first-root-newest"),
        pytest.param("tie", "a-old", id="RESOLVE-stable-tie"),
        pytest.param("vanished", "a-old", id="RESOLVE-vanished"),
        pytest.param("all-vanished", "a-old", id="RESOLVE-all-stat-fail"),
    ],
)
def test_resolution_priority(mode, winner, tmp_path, monkeypatch):
    a, b, override = (tmp_path / name for name in ("a[g]", "b", "direct"))
    old = task_file(a, mtime=10)
    # Both hits must have the same root-relative nesting depth.
    new = a / "zzz" / "session" / "tasks" / "task.output"
    new.parent.mkdir(parents=True)
    new.write_text("", encoding="utf-8")
    os.utime(new, (20, 20))
    later = task_file(b, mtime=30)
    override.mkdir()
    direct = override / "task.output"
    if mode == "override":
        direct.write_text("", encoding="utf-8")
    hits = {str(a): [str(old), str(new)], str(b): [str(later)]}
    monkeypatch.setattr(tasks, "glob_under", lambda root, _: hits[root])
    getmtime = os.path.getmtime

    def mtime(path):
        if mode == "all-vanished" or (mode == "vanished" and path == str(new)):
            raise FileNotFoundError("gone")
        return 10 if mode == "tie" else getmtime(path)

    monkeypatch.setattr(os.path, "getmtime", mtime)
    roots = tasks.TaskRoots(
        (str(a), str(b)), str(override) if mode in ("override", "miss") else None
    )
    path, searched = tasks.resolve_target("task", roots)
    assert path == str({"direct": direct, "a-old": old, "a-new": new}[winner])
    expected = [str(override / "task.output")] if roots.override else []
    if mode != "override":
        expected.append(os.path.join(str(a), "*", "*", "tasks", "task.output"))
    assert searched == expected


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("w[g]", id="RESOLVE-brackets"),
        pytest.param("w?", id="RESOLVE-question"),
        pytest.param("*", id="RESOLVE-star"),
    ],
)
def test_literal_task_ids(name, tmp_path):
    root = tmp_path / "root[g]"
    ordinary = task_file(root, "wg")
    roots = tasks.TaskRoots((str(root),), None)
    assert tasks.resolve_target("wg", roots)[0] == str(ordinary)
    assert tasks.resolve_target(name, roots)[0] is None
    if os.name != "nt" or name == "w[g]":
        literal = task_file(root, name)
        assert tasks.resolve_target(name, roots)[0] == str(literal)


def test_unresolved_target_discloses_nonexistent_roots(tmp_path):
    root = tmp_path / "missing"
    override = tmp_path / "absent"
    assert tasks.resolve_target(
        "task", tasks.TaskRoots((str(root),), str(override))
    ) == (
        None,
        [
            str(override / "task.output"),
            os.path.join(str(root), "*", "*", "tasks", "task.output"),
        ],
    )


def test_resolve_target_uses_literal_task_root(tmp_path, monkeypatch):
    root = tmp_path / "claude-[g]"
    target = root / "slug" / "session" / "tasks" / "w123.output"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    assert tasks.resolve_target("w123", tasks.TaskRoots((str(root),), None))[0] == str(
        target
    )


@pytest.mark.parametrize(
    "mode,expected_text,expected_bytes",
    [
        pytest.param("missing", "", None, id="READ-missing"),
        pytest.param("denied", "", 1, id="READ-unreadable"),
        pytest.param("value-error", "", 1, id="READ-invalid-open"),
        pytest.param("vanished", "", None, id="READ-vanished"),
        pytest.param("directory", "", 37, id="READ-directory-size"),
        pytest.param(
            "decoded",
            '\ufffd\n{"ok":true,"stats":{}}\n',
            30,
            id="READ-bom-replacement-crlf",
        ),
    ],
)
def test_observation_read_policy(
    mode, expected_text, expected_bytes, tmp_path, monkeypatch
):
    target = tmp_path / "task.output"
    if mode == "directory":
        target.mkdir()
        monkeypatch.setattr(os.path, "getsize", lambda _: 37)
    elif mode == "decoded":
        target.write_bytes(b'\xef\xbb\xbf\xff\r\n{"ok":true,"stats":{}}\r\n')
    elif mode != "missing":
        target.write_text("x", encoding="utf-8")
    if mode in ("denied", "value-error", "vanished"):

        def fail(*args, **kwargs):
            if mode == "vanished":
                target.unlink(missing_ok=True)
            raise ValueError("invalid") if mode == "value-error" else OSError("denied")

        monkeypatch.setattr(Path, "open", fail)
    observation = tasks.observe(str(target), tasks.TaskRoots((), None))
    assert tasks.read_task(str(target)) == expected_text
    # The first size query precedes the read; a later observation sees disappearance.
    if mode == "vanished":
        observation = tasks.observe(str(target), tasks.TaskRoots((), None))
    assert observation.file_bytes == expected_bytes
    assert observation.resolved_path == str(target)
    assert observation.terminal == (
        {"ok": True, "stats": {}} if mode == "decoded" else None
    )
    assert observation.searched == ()
    assert observation.scan_stop_reason is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation unavailable")
def test_fifo_observation_does_not_open(tmp_path, monkeypatch):
    target = tmp_path / "fifo.output"
    os.mkfifo(target)
    monkeypatch.setattr(Path, "open", lambda *_a, **_k: pytest.fail("opened FIFO"))
    observation = tasks.observe(str(target), tasks.TaskRoots((), None))
    assert observation.file_bytes == 0
    assert observation.terminal is None


@pytest.mark.parametrize(
    "text,expected,bare",
    [
        pytest.param(
            '{"ok":true,"phaseReached":null}',
            {"ok": True, "phaseReached": None},
            False,
            id="TERMINAL-phase",
        ),
        pytest.param(
            '{"ok":true,"stats":null}',
            {"ok": True, "stats": None},
            False,
            id="TERMINAL-stats",
        ),
        pytest.param(
            '{"ok":true,"artifactPaths":null}',
            {"ok": True, "artifactPaths": None},
            False,
            id="TERMINAL-artifacts",
        ),
        pytest.param(
            '{"ok":true,"checkpoints":null}',
            {"ok": True, "checkpoints": None},
            False,
            id="TERMINAL-checkpoints",
        ),
        pytest.param(
            '{"ok":true,"resolvedPolicy":null}',
            {"ok": True, "resolvedPolicy": None},
            False,
            id="TERMINAL-policy",
        ),
        pytest.param(
            '{"ok":true,"gaps":null}',
            {"ok": True, "gaps": None},
            False,
            id="TERMINAL-gaps",
        ),
        pytest.param(
            '{"ok":false,"failingPhase":"args","error":"boom"}',
            {"ok": False, "failingPhase": "args", "error": "boom"},
            False,
            id="TERMINAL-failure",
        ),
        pytest.param('{"ok":1,"stats":{}}', None, False, id="TERMINAL-nonboolean"),
        pytest.param("[]", None, False, id="TERMINAL-nonobject"),
        pytest.param(
            '{"ok":false,"error":"boom","planVersion":2,"verified":[],"written":[]}',
            None,
            True,
            id="TERMINAL-receipt-bare",
        ),
        pytest.param(
            '{"status":"ok","receipt":{},"result":{"verified":[]}}',
            None,
            False,
            id="TERMINAL-verify-receipt",
        ),
        pytest.param(
            '{"ok":true,"stats":{},"result":{"ok":false,"gaps":[]}}',
            {"ok": False, "gaps": []},
            False,
            id="TERMINAL-result-precedence",
        ),
        pytest.param(
            '{"result":"{\\"ok\\":true,\\"stats\\":{}}"}',
            {"ok": True, "stats": {}},
            False,
            id="TERMINAL-string-result",
        ),
        pytest.param('{"result":{"ok":true}}', None, True, id="TERMINAL-result-bare"),
        pytest.param(
            '{"result":"a prose summary"}', None, False, id="TERMINAL-prose-result"
        ),
        pytest.param(
            '{"result":"{\\"ok\\":true}"}', None, True, id="TERMINAL-string-bare"
        ),
        pytest.param(
            '{"result":{"result":{"ok":true,"stats":{}}}}',
            None,
            False,
            id="TERMINAL-no-recursive-promotion",
        ),
    ],
)
def test_terminal_shapes(text, expected, bare):
    assert tasks.find_terminal(text) == (expected, bare, None)


@pytest.mark.parametrize(
    "text,expected,bare",
    [
        pytest.param("", None, False, id="DOCUMENT-empty"),
        pytest.param(" \n\t", None, False, id="DOCUMENT-whitespace"),
        pytest.param(
            'log\n{\n  "result": {"ok":true,"stats":{}}\n}\ntrailer',
            {"ok": True, "stats": {}},
            False,
            id="DOCUMENT-pretty-envelope",
        ),
        pytest.param(
            'log\r\n{"ok":true,"stats":{}}\r\ntrailer',
            {"ok": True, "stats": {}},
            False,
            id="DOCUMENT-crlf",
        ),
        pytest.param(
            '{"ok":true}\n{"ok":false,"gaps":[]}\n{"ok":true,"stats":{}}',
            {"ok": True, "stats": {}},
            True,
            id="DOCUMENT-last-return-no-lf",
        ),
        pytest.param(
            'log\n  {"ok":true,"stats":{}}', None, False, id="DOCUMENT-indented"
        ),
        pytest.param(
            '{"result":{"ok":true,"stats":{}}', None, False, id="DOCUMENT-torn-envelope"
        ),
        pytest.param(
            '{\n "progress": [\n  {"ok":true,"stats":{}}',
            None,
            False,
            id="DOCUMENT-torn-pretty",
        ),
        pytest.param(
            '{"preview":"{\\"ok\\":true,\\"stats\\":{}}"}',
            None,
            False,
            id="DOCUMENT-escaped-preview",
        ),
        pytest.param(
            '{"a":[\n{"ok":true,"stats":{}}\nBROKEN',
            {"ok": True, "stats": {}},
            False,
            id="DOCUMENT-later-after-decode-error",
        ),
        pytest.param(
            '\ufeff{"ok":true,"stats":{}}',
            None,
            False,
            id="DOCUMENT-bom-text-is-not-read",
        ),
        pytest.param(
            '{"a":' + "[" * 60000 + "1" + "]" * 60000 + '}\n{"ok":true,"stats":{}}',
            {"ok": True, "stats": {}},
            False,
            id="DOCUMENT-later-after-recursion",
        ),
    ],
)
def test_document_scan(text, expected, bare):
    assert tasks.find_terminal(text) == (expected, bare, None)


@pytest.mark.parametrize(
    "kind,count,terminal,expected,reason",
    [
        pytest.param(
            "chars",
            8_000_000,
            True,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-chars-exact",
        ),
        pytest.param(
            "chars", 8_000_001, True, None, "max_chars", id="LIMIT-chars-over"
        ),
        pytest.param(
            "candidates",
            499,
            True,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-candidates-exact",
        ),
        pytest.param(
            "candidates", 500, True, None, "max_candidates", id="LIMIT-candidates-over"
        ),
        pytest.param(
            "probes",
            1999,
            True,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-probes-exact",
        ),
        pytest.param("probes", 2000, True, None, "max_probes", id="LIMIT-probes-over"),
        pytest.param(
            "deep", 7, True, {"ok": True, "stats": {}}, None, id="LIMIT-deep-under"
        ),
        pytest.param(
            "deep", 8, True, None, "max_deep_candidates", id="LIMIT-deep-exact"
        ),
        pytest.param(
            "post-terminal",
            501,
            False,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-later-bound-suppressed",
        ),
        pytest.param(
            "whole",
            8_000_010,
            True,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-whole-document-exception",
        ),
    ],
)
def test_scan_limits(kind, count, terminal, expected, reason):
    returned = '{"ok":true,"stats":{}}'
    if kind == "chars":
        text = "x" * (count - len(returned) - 1) + "\n" + returned
    elif kind == "whole":
        text = '{"summary":"' + "x" * count + '","result":' + returned + "}"
    elif kind == "post-terminal":
        text = returned + "\n" + "{}\n" * count
    else:
        line = {
            "candidates": "{}",
            "probes": '{"x":',
            "deep": '{"a":' + "[" * 60000 + "1" + "]" * 60000 + "}",
        }[kind]
        text = (line + "\n") * count + (returned if terminal else "")
    assert tasks.find_terminal(text) == (expected, False, reason)


@pytest.mark.parametrize(
    "text,expected,seconds",
    [
        pytest.param("{" * 200000, None, 1, id="LIMIT-dense-braces"),
        pytest.param('{"' * 200000, None, 1, id="LIMIT-dense-quotes"),
        pytest.param('\n{"a":' * 50000, None, 2, id="LIMIT-many-fragments"),
        pytest.param(
            "{" * 50000 + '\n{"ok":true,"stats":{}}',
            {"ok": True, "stats": {}},
            1,
            id="LIMIT-noise-then-return",
        ),
    ],
)
def test_scan_cost(text, expected, seconds):
    import time

    started = time.monotonic()
    assert tasks.find_terminal(text)[0] == expected
    assert time.monotonic() - started < seconds


def test_sweep_uses_literal_task_root(tmp_path, monkeypatch):
    root = tmp_path / "claude-[g]"
    target = root / "slug" / "session" / "tasks" / "w123.output"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    assert list(tasks.sweep_paths(tasks.TaskRoots((str(root),), None))) == [str(target)]


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("order", id="SWEEP-global-newest-stable-tie"),
        pytest.param("vanished", id="SWEEP-vanished"),
        pytest.param("cap", id="SWEEP-exact-200"),
    ],
)
def test_sweep_policy(mode, tmp_path, monkeypatch):
    root = tmp_path / "root[g]"
    old = task_file(root, "old", mtime=1)
    new = task_file(root, "new", mtime=3)
    direct = tmp_path / "direct[g]"
    direct.mkdir()
    override = direct / "override.output"
    override.write_text("", encoding="utf-8")
    os.utime(override, (3, 3))
    roots = tasks.TaskRoots((str(root),), str(direct))
    if mode == "cap":
        for index in range(201):
            task_file(root, f"cap{index:03}", mtime=10 + index)
        result = tasks.sweep_paths(roots)
        assert len(result) == 200
        assert result[0].endswith("cap200.output")
        assert result[-1].endswith("cap001.output")
    else:
        if mode == "vanished":
            real = os.path.getmtime

            def stat(path):
                if path == str(new):
                    raise OSError("gone")
                return real(path)

            monkeypatch.setattr(os.path, "getmtime", stat)
        assert tasks.sweep_paths(roots) == tuple(
            str(p)
            for p in ([override, new, old] if mode == "order" else [override, old])
        )
