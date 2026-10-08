"""Injected roots replace host discovery and default policy stays pure."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from gauntlet import tasks

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
