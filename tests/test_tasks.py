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
    "uid", [42, None], ids=["uid-present", "no-getuid-system-temp"]
)
def test_real_default_discovery_preserves_order_and_deduplicates(uid, monkeypatch):
    resolved, temp_calls = [], []
    name = "claude" if uid is None else "claude-42"
    candidates = [
        os.path.join(base, name) for base in ("/tmp", "/private/tmp", "fixture-extra")
    ]

    def realpath(path):
        resolved.append(path)
        return candidates[-1] if path == os.path.join("fixture-system", name) else path

    def system_temp():
        temp_calls.append(True)
        return "fixture-system"

    if uid is None:
        monkeypatch.delattr(os, "getuid", raising=False)
    else:
        monkeypatch.setattr(os, "getuid", lambda: uid, raising=False)
    monkeypatch.setattr(tasks.tempfile, "gettempdir", system_temp)
    monkeypatch.setattr(tasks, "_default_roots", _REAL_DEFAULT_ROOTS)
    with monkeypatch.context() as patch:
        patch.setattr(os.path, "realpath", realpath)
        patch.setattr(os.path, "isdir", lambda *_: pytest.fail("probed host path"))
        roots = tasks.roots_from_environment({"TMPDIR": "fixture-extra" + os.sep * 3})
    assert roots == tasks.TaskRoots(tuple(candidates), None)
    assert resolved == candidates + (
        [os.path.join("fixture-system", name)] if uid is None else []
    )
    assert temp_calls == ([] if uid is not None else [True])


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
    "mode,winner",
    [
        pytest.param("override", "direct", id="RESOLVE-direct-override"),
        pytest.param("miss", "a-new", id="RESOLVE-override-miss"),
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

    roots = tasks.TaskRoots(
        (str(a), str(b)), str(override) if mode in ("override", "miss") else None
    )
    path, searched = tasks.resolve_target("task", roots)
    assert path == str({"direct": direct, "a-old": old, "a-new": new}[winner])
    expected = [str(override / "task.output")] if roots.override else []
    if mode != "override":
        expected.append(os.path.join(str(a), "*", "*", "tasks", "task.output"))
    assert searched == expected


def test_literal_task_ids(tmp_path):
    name = "w[g]"
    root = tmp_path / "root[g]"
    ordinary = task_file(root, "wg")
    roots = tasks.TaskRoots((str(root),), None)
    assert tasks.resolve_target("wg", roots)[0] == str(ordinary)
    assert tasks.resolve_target(name, roots)[0] is None
    literal = task_file(root, name)
    assert tasks.resolve_target(name, roots)[0] == str(literal)


@pytest.mark.parametrize(
    "mode,expected_text,expected_bytes",
    [
        pytest.param("missing", "", None, id="READ-missing"),
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
    if mode == "decoded":
        target.write_bytes(b'\xef\xbb\xbf\xff\r\n{"ok":true,"stats":{}}\r\n')
    observation = tasks.observe(str(target), tasks.TaskRoots((), None))
    assert tasks.read_task(str(target)) == expected_text
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
        pytest.param('{"ok":1,"stats":{}}', None, False, id="TERMINAL-nonboolean"),
        pytest.param(
            '{"ok":false,"error":"boom","planVersion":2,"verified":[],"written":[]}',
            None,
            True,
            id="TERMINAL-receipt-bare",
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
        pytest.param(
            '{"result":"a prose summary"}', None, False, id="TERMINAL-prose-result"
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
        pytest.param(
            'log\n{\n  "result": {"ok":true,"stats":{}}\n}\ntrailer',
            {"ok": True, "stats": {}},
            False,
            id="DOCUMENT-pretty-envelope",
        ),
        pytest.param(
            '{"ok":true}\n{"ok":false,"gaps":[]}\n{"ok":true,"stats":{}}',
            {"ok": True, "stats": {}},
            True,
            id="DOCUMENT-last-return-no-lf",
        ),
        pytest.param(
            '{\n "progress": [\n  {"ok":true,"stats":{}}',
            None,
            False,
            id="DOCUMENT-torn-pretty",
        ),
        pytest.param(
            '{"a":[\n{"ok":true,"stats":{}}\nBROKEN',
            {"ok": True, "stats": {}},
            False,
            id="DOCUMENT-later-after-decode-error",
        ),
    ],
)
def test_document_scan(text, expected, bare):
    assert tasks.find_terminal(text) == (expected, bare, None)


@pytest.mark.parametrize(
    "kind,count,expected,reason",
    [
        pytest.param("chars", 8_000_001, None, "max_chars", id="LIMIT-chars-over"),
        pytest.param(
            "candidates", 500, None, "max_candidates", id="LIMIT-candidates-over"
        ),
        pytest.param("probes", 2000, None, "max_probes", id="LIMIT-probes-over"),
        pytest.param(
            "post-terminal",
            501,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-later-bound-suppressed",
        ),
        pytest.param(
            "whole",
            8_000_010,
            {"ok": True, "stats": {}},
            None,
            id="LIMIT-whole-document-exception",
        ),
    ],
)
def test_scan_limits(kind, count, expected, reason):
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
            # Each invalid token stops decoding before the next line can nest.
            "probes": "{!",
        }[kind]
        text = (line + "\n") * count + returned
    assert tasks.find_terminal(text) == (expected, False, reason)


def test_sweep_policy(tmp_path):
    root = tmp_path / "root[g]"
    old = task_file(root, "old", mtime=1)
    new = task_file(root, "new", mtime=3)
    direct = tmp_path / "direct[g]"
    direct.mkdir()
    override = direct / "override.output"
    override.write_text("", encoding="utf-8")
    os.utime(override, (3, 3))
    roots = tasks.TaskRoots((str(root),), str(direct))
    assert tasks.sweep_paths(roots) == (str(override), str(new), str(old))
    assert tasks.sweep_paths(roots, limit=2) == (str(override), str(new))
