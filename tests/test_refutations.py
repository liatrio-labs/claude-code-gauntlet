"""Refutation checks at the command boundary use captured source and change ownership."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "#include <stdbool.h>\nint use(int value) {\n  if (!value) return 0;\n  // intentional: ignore this failure\n  return added_guard(value);\n}\n"
PATCH = "diff --git a/source b/source\n--- a/source\n+++ b/source\n@@ -1,5 +1,6 @@\n #include <stdbool.h>\n int use(int value) {\n   if (!value) return 0;\n   // intentional: ignore this failure\n+  return added_guard(value);\n }\n"


def citation(start=3, end=3, anchor="if (!value) return 0;", file="source"):
    return dict(file=file, line_start=start, line_end=end, anchor=anchor)


def check(
    tmp_path,
    citations,
    *,
    text=SOURCE,
    diff=PATCH,
    file="source",
    head="abc1234",
    kind="local",
    extra=(),
    env=None,
):
    path = tmp_path / file
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    patch = tmp_path / "diff.patch"
    patch.write_text(diff, encoding="utf-8")
    request = json.dumps(
        {
            "citations": [
                dict(id=str(i), refutation=value) for i, value in enumerate(citations)
            ]
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_refutations.py"),
            "--request",
            request,
            "--repo-root",
            str(tmp_path),
            "--head-sha",
            head,
            "--target-kind",
            kind,
            "--diff-file",
            str(patch),
            *extra,
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = json.loads(result.stdout)
    assert output["request"] == request
    return output["checks"]


@pytest.mark.parametrize(
    "value,reason",
    [
        (citation(), "resolved_preexisting_code"),
        (citation(2, 4), "resolved_preexisting_code"),
        (citation(4, 4, "// intentional: ignore this failure"), "comment_only"),
        (citation(3, 5, "return added_guard(value);"), "added_evidence"),
        (citation(1, 2), "quote_mismatch"),
        (citation(3, 3, "fictional guard"), "quote_mismatch"),
        (citation(20, 20), "unavailable_source"),
        (citation(1, 9), "invalid_span"),
        (citation(3, 3, "return " + "x" * 401 + ";"), "quote_mismatch"),
        (citation(file="../source"), "unavailable_source"),
    ],
)
def test_refutation_resolves_real_preexisting_executable_lines(tmp_path, value, reason):
    text, diff = SOURCE, PATCH
    if value["line_end"] == 9:
        text += "int unchanged;\n" * 4
    if len(value["anchor"]) > 400:
        text = text.replace("if (!value) return 0;", value["anchor"])
        diff = diff.replace("if (!value) return 0;", value["anchor"])
    assert check(tmp_path, [value], text=text, diff=diff) == [
        dict(id="0", verified=reason == "resolved_preexisting_code", reason=reason)
    ]


@pytest.mark.parametrize(
    "file,text",
    [
        ("docs/source", "This return value is intentional.\n"),
        ("README.md", "The system is intentionally designed this way.\n"),
        ("data/record", '{"return": "guarded"}\n'),
        ("vendor/source", SOURCE),
    ],
)
def test_non_source_quotes_never_refute(tmp_path, file, text):
    value = citation(1, 1, text.splitlines()[0], file)
    assert (
        check(tmp_path, [value], file=file, text=text)[0]["reason"]
        == "non_source_evidence"
    )


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def commit(root):
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "fixture",
    )
    return git(root, "rev-parse", "HEAD")


def test_captured_head_wins_over_worktree_and_incremental_uses_full_local_range(
    tmp_path,
):
    git(tmp_path, "init", "-q")
    source = tmp_path / "source"
    source.write_text(SOURCE.replace("  if (!value) return 0;\n", ""), encoding="utf-8")
    base = commit(tmp_path)
    source.write_text(SOURCE, encoding="utf-8")
    first = commit(tmp_path)
    (tmp_path / "other").write_text("unrelated\n", encoding="utf-8")
    head = commit(tmp_path)
    partial = git(tmp_path, "diff", first, head) + "\n"
    source.write_text("forged guard\n", encoding="utf-8")
    assert check(tmp_path, [citation()], text=None, diff=partial, head=head, kind="pr")[
        0
    ]["verified"]
    assert (
        check(
            tmp_path,
            [citation()],
            text=None,
            diff=partial,
            head=head,
            kind="pr",
            extra=("--incremental", "--base-ref", base),
        )[0]["reason"]
        == "added_evidence"
    )
    assert (
        check(
            tmp_path,
            [citation()],
            text=None,
            diff=partial,
            head=head,
            kind="pr",
            extra=("--incremental", "--base-ref", "missing"),
        )[0]["reason"]
        == "full_pr_diff_unavailable"
    )


def test_verbatim_gitlab_path_keeps_its_real_b_directory(tmp_path):
    diff = PATCH.replace(
        "diff --git a/source b/source", "diff --git b/source b/source"
    ).replace("--- a/source", "--- b/source")
    assert (
        check(
            tmp_path,
            [citation(5, 5, "return added_guard(value);", "b/source")],
            file="b/source",
            diff=diff,
        )[0]["reason"]
        == "added_evidence"
    )


def test_command_builds_change_ownership_once_for_multiple_citations(
    tmp_path, monkeypatch, capsys
):
    from gauntlet import refutations

    (tmp_path / "source").write_text(SOURCE, encoding="utf-8")
    patch = tmp_path / "diff.patch"
    patch.write_text(PATCH, encoding="utf-8")
    walks = []
    original = refutations.walk_diff

    def counted(*args, **kwargs):
        walks.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(refutations, "walk_diff", counted)
    request = json.dumps(
        {"citations": [dict(id=str(i), refutation=citation()) for i in range(2)]}
    )
    assert (
        refutations.CLI.invoke(
            [
                "--request",
                request,
                "--repo-root",
                str(tmp_path),
                "--head-sha",
                "abc1234",
                "--target-kind",
                "local",
                "--diff-file",
                str(patch),
            ]
        )
        == 0
    )
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert all(row["verified"] for row in checks)
    assert len(walks) == 1


def test_refutations_need_no_content_classifier_binary(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "git").symlink_to(shutil.which("git"))
    env = {**os.environ, "PATH": str(tools)}
    assert check(tmp_path, [citation()], env=env)[0]["verified"]
    assert check(
        tmp_path,
        [citation(1, 1, "return value;", "src/unfamiliar.language")],
        file="src/unfamiliar.language",
        text="return value;\n",
        env=env,
    )[0]["verified"]
    assert (
        check(
            tmp_path,
            [citation(1, 1, "return value;", "docs/unfamiliar.language")],
            file="docs/unfamiliar.language",
            text="return value;\n",
            env=env,
        )[0]["reason"]
        == "non_source_evidence"
    )
