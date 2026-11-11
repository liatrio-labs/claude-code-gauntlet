"""The command boundary checks real source, resolved quotes and added-line ownership."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "line,anchor,verified,reason",
    [
        (1, "if (!value) return;", True, "resolved_preexisting_code"),
        (2, "// intentional: ignore this failure", False, "comment_only"),
        (3, "return added_guard(value);", False, "added_evidence"),
        (1, "a fictional guard", False, "quote_mismatch"),
        (20, "missing", False, "unavailable_source"),
    ],
)
def test_refutation_command_resolves_existing_code_only(
    tmp_path, line, anchor, verified, reason
):
    (tmp_path / "source").write_text(
        "if (!value) return;\n// intentional: ignore this failure\nreturn added_guard(value);\n",
        encoding="utf-8",
    )
    diff = tmp_path / "diff.patch"
    diff.write_text(
        "diff --git a/source b/source\n--- a/source\n+++ b/source\n@@ -1,2 +1,3 @@\n if (!value) return;\n // intentional: ignore this failure\n+return added_guard(value);\n",
        encoding="utf-8",
    )
    request = json.dumps(
        {
            "citations": [
                {
                    "id": "f",
                    "refutation": {
                        "file": "source",
                        "line_start": line,
                        "line_end": line,
                        "anchor": anchor,
                    },
                }
            ]
        }
    )
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/check_refutations.py"),
            "--request",
            request,
            "--repo-root",
            str(tmp_path),
            "--head-sha",
            "abc1234",
            "--target-kind",
            "local",
            "--diff-file",
            str(diff),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {
        "request": request,
        "checks": [{"id": "f", "verified": verified, "reason": reason}],
    }


def test_refutation_uses_captured_head_and_rejects_escape_and_comment_padding(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "source").write_text(
        "/* intentional\nthis is safe\n*/\nreturn guarded;\n", encoding="utf-8"
    )
    git("add", "source")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    sha = git("rev-parse", "HEAD")
    (tmp_path / "source").write_text("return forged;\n", encoding="utf-8")
    diff = tmp_path / "diff.patch"
    diff.write_text(
        "diff --git a/other b/other\n--- a/other\n+++ b/other\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    citations = [
        dict(
            id="old",
            refutation=dict(
                file="source", line_start=4, line_end=4, anchor="return guarded;"
            ),
        ),
        dict(
            id="comment",
            refutation=dict(
                file="source", line_start=2, line_end=2, anchor="this is safe"
            ),
        ),
        dict(
            id="escape",
            refutation=dict(
                file="../source", line_start=1, line_end=1, anchor="return forged;"
            ),
        ),
    ]
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/check_refutations.py"),
            "--request",
            json.dumps(dict(citations=citations)),
            "--repo-root",
            str(tmp_path),
            "--head-sha",
            sha,
            "--target-kind",
            "pr",
            "--diff-file",
            str(diff),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert [row["verified"] for row in json.loads(result.stdout)["checks"]] == [
        True,
        False,
        False,
    ]


@pytest.mark.parametrize(
    "start,end,anchor,verified,reason",
    [
        (
            1,
            4,
            "if (!value) return;\n    return existing(value);",
            True,
            "resolved_preexisting_code",
        ),
        (3, 5, "return added_guard(value);", False, "added_evidence"),
        (1, 2, "return existing(value);", False, "quote_mismatch"),
        (
            1,
            4,
            "if (!value) return;\nreturn added_guard(value);",
            False,
            "quote_mismatch",
        ),
        (1, 4, "intentional", False, "quote_mismatch"),
        (1, 6, "// intentional: ignore this failure", False, "comment_only"),
        (1, 7, "}", False, "quote_mismatch"),
    ],
)
def test_refutation_command_resolves_only_unique_contiguous_lines_in_cited_span(
    tmp_path, start, end, anchor, verified, reason
):
    (tmp_path / "source").write_text(
        "function f() {\n    if (!value) return;\n    return existing(value);\n}\nreturn added_guard(value);\n// intentional: ignore this failure\n}\n",
        encoding="utf-8",
    )
    diff = tmp_path / "diff.patch"
    diff.write_text(
        "diff --git a/source b/source\n--- a/source\n+++ b/source\n@@ -1,6 +1,7 @@\n function f() {\n     if (!value) return;\n     return existing(value);\n }\n+return added_guard(value);\n // intentional: ignore this failure\n }\n",
        encoding="utf-8",
    )
    request = json.dumps(
        {
            "citations": [
                {
                    "id": "f",
                    "refutation": {
                        "file": "source",
                        "line_start": start,
                        "line_end": end,
                        "anchor": anchor,
                    },
                }
            ]
        }
    )
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/check_refutations.py"),
            "--request",
            request,
            "--repo-root",
            str(tmp_path),
            "--head-sha",
            "abc1234",
            "--target-kind",
            "local",
            "--diff-file",
            str(diff),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout)["checks"] == [
        {"id": "f", "verified": verified, "reason": reason}
    ]
