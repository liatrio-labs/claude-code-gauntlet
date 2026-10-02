"""Unified diff event and facts boundary proofs."""

import argparse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Literal, cast

import pytest
from gauntlet import numstat, patches
from gauntlet.delivery import post
from gauntlet.diff import (
    DiffCounts,
    DiffEvent,
    DiffFacts,
    DiffPathPolicy,
    HeaderEvent,
    HunkEvent,
    LineEvent,
    LineKey,
    PostingPathPolicy,
    diff_counts,
    diff_path_spelling,
    is_line_valid,
    is_new_file,
    old_line_for,
    old_path_for,
    parse_diff,
    patch_report_policy,
    path_is_ambiguous,
    posting_policy,
    range_is_valid,
    span_texts,
    valid_lines_for_file,
    walk_diff,
)
from gauntlet.verify import decide


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", [], id="empty"),
        pytest.param(
            "--- a/src/app.py\n+++ b/src/app.py\n",
            [
                HeaderEvent("old_path", "a/src/app.py"),
                HeaderEvent("new_path", "b/src/app.py"),
            ],
            id="prefixed-headers",
        ),
        pytest.param(
            "--- src/app.py\n+++ src/app.py\n",
            [
                HeaderEvent("old_path", "src/app.py"),
                HeaderEvent("new_path", "src/app.py"),
            ],
            id="plain-headers",
        ),
        pytest.param(
            "--- /dev/null\n+++ b/added.py\n",
            [
                HeaderEvent("old_path", "/dev/null"),
                HeaderEvent("new_path", "b/added.py"),
            ],
            id="null-header",
        ),
        pytest.param(
            "diff --git a/logo.png b/logo.png\nold mode 100644\nnew mode 100755\nindex 1111111..2222222\nsimilarity index 94%\nBinary files a/logo.png and b/logo.png differ\n",
            [HeaderEvent("git_header", "a/logo.png b/logo.png")],
            id="git-and-noise",
        ),
        pytest.param(
            'diff --git "a/caf\\303\\251.py" b/my file.py\tx\n',
            [HeaderEvent("git_header", '"a/caf\\303\\251.py" b/my file.py\tx')],
            id="raw-git-header",
        ),
        pytest.param(
            "@@ -10,2 +20,3 @@ def handler():\n ctx\n+added\n tail\n",
            [
                HunkEvent(10, 20, 2, 3),
                LineEvent(10, 20, "ctx"),
                LineEvent(None, 21, "added"),
                LineEvent(11, 22, "tail"),
            ],
            id="explicit-counts",
        ),
        pytest.param(
            "@@ -0,0 +1 @@\n+only\n",
            [HunkEvent(0, 1, 0, 1), LineEvent(None, 1, "only")],
            id="omitted-counts",
        ),
        pytest.param(
            "+++ b/multi.py\n@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -50,2 +51,3 @@\n d\n+e\n f\n",
            [
                HeaderEvent("new_path", "b/multi.py"),
                HunkEvent(1, 1, 2, 3),
                LineEvent(1, 1, "a"),
                LineEvent(None, 2, "b"),
                LineEvent(2, 3, "c"),
                HunkEvent(50, 51, 2, 3),
                LineEvent(50, 51, "d"),
                LineEvent(None, 52, "e"),
                LineEvent(51, 53, "f"),
            ],
            id="second-hunk",
        ),
        pytest.param(
            "@@ -7,3 +7,3 @@\n ctx\n-gone\n+fresh\n tail\n",
            [
                HunkEvent(7, 7, 3, 3),
                LineEvent(7, 7, "ctx"),
                LineEvent(8, None, "gone"),
                LineEvent(None, 8, "fresh"),
                LineEvent(9, 9, "tail"),
            ],
            id="sides-and-text",
        ),
        pytest.param(
            "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b2\n",
            [
                HunkEvent(1, 1, 2, 2),
                LineEvent(1, 1, "a"),
                LineEvent(2, None, "b"),
                LineEvent(None, 2, "b2"),
            ],
            id="newline-marker",
        ),
        pytest.param(
            "--- a/schema.sql\n+++ b/schema.sql\n@@ -1,3 +1,2 @@\n CREATE TABLE t (\n--- deprecated: drop me\n );\n",
            [
                HeaderEvent("old_path", "a/schema.sql"),
                HeaderEvent("new_path", "b/schema.sql"),
                HunkEvent(1, 1, 3, 2),
                LineEvent(1, 1, "CREATE TABLE t ("),
                LineEvent(2, None, "-- deprecated: drop me"),
                LineEvent(3, 2, ");"),
            ],
            id="minus-header-body",
        ),
        pytest.param(
            "--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1,3 @@\n intro\n+++ x marks a diff-of-a-diff\n outro\n",
            [
                HeaderEvent("old_path", "a/notes.md"),
                HeaderEvent("new_path", "b/notes.md"),
                HunkEvent(1, 1, 2, 3),
                LineEvent(1, 1, "intro"),
                LineEvent(None, 2, "++ x marks a diff-of-a-diff"),
                LineEvent(2, 3, "outro"),
            ],
            id="plus-header-body",
        ),
        pytest.param(
            "@@ -1,4 +1,3 @@\n head\n-alpha\x0cbeta\n+gamma\n middle\n-omega\n",
            [
                HunkEvent(1, 1, 4, 3),
                LineEvent(1, 1, "head"),
                LineEvent(2, None, "alpha\x0cbeta"),
                LineEvent(None, 2, "gamma"),
                LineEvent(3, 3, "middle"),
                LineEvent(4, None, "omega"),
            ],
            id="formfeed-content",
        ),
        pytest.param(
            "--- a/f.py\n+++ b/f.py\n@@ -1,4 +1,4 @@\n ctx\n+added\n",
            [
                HeaderEvent("old_path", "a/f.py"),
                HeaderEvent("new_path", "b/f.py"),
                HunkEvent(1, 1, 4, 4),
                LineEvent(1, 1, "ctx"),
                LineEvent(None, 2, "added"),
            ],
            id="truncated-hunk",
        ),
        pytest.param(
            "@@ -1,2 +1,2 @@\n ctx\n+added",
            [
                HunkEvent(1, 1, 2, 2),
                LineEvent(1, 1, "ctx"),
                LineEvent(None, 2, "added"),
            ],
            id="unterminated-last-line",
        ),
        pytest.param(
            "@@ -1,3 +1,3 @@\n a\n\n+b\n",
            [
                HunkEvent(1, 1, 3, 3),
                LineEvent(1, 1, "a"),
                LineEvent(2, 2, ""),
                LineEvent(None, 3, "b"),
            ],
            id="bare-empty-context",
        ),
        pytest.param(
            "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-alpha\n-beta\n-gamma\ndiff --git a/next.py b/next.py\n--- a/next.py\n+++ b/next.py\n@@ -10,1 +10,2 @@\n ctx\n+added\n",
            [
                HeaderEvent("git_header", "a/gone.py b/gone.py"),
                HeaderEvent("old_path", "a/gone.py"),
                HeaderEvent("new_path", "/dev/null"),
                HunkEvent(1, 0, 3, 0),
                LineEvent(1, None, "alpha"),
                LineEvent(2, None, "beta"),
                LineEvent(3, None, "gamma"),
                HeaderEvent("git_header", "a/next.py b/next.py"),
                HeaderEvent("old_path", "a/next.py"),
                HeaderEvent("new_path", "b/next.py"),
                HunkEvent(10, 10, 1, 2),
                LineEvent(10, 10, "ctx"),
                LineEvent(None, 11, "added"),
            ],
            id="deleted-body-drains",
        ),
        pytest.param(
            "@@ -0,0 +1 @@\n+hello\n",
            [HunkEvent(0, 1, 0, 1), LineEvent(None, 1, "hello")],
            id="added-marker",
        ),
        pytest.param(
            "@@ -1 +0,0 @@\n-hello\n",
            [HunkEvent(1, 0, 1, 0), LineEvent(1, None, "hello")],
            id="removed-marker",
        ),
        pytest.param(
            "@@ -1 +1 @@\n  indented\n",
            [HunkEvent(1, 1, 1, 1), LineEvent(1, 1, " indented")],
            id="marker-indent",
        ),
        pytest.param(
            "@@ -1 +1 @@\n \n",
            [HunkEvent(1, 1, 1, 1), LineEvent(1, 1, "")],
            id="blank-space-context",
        ),
        pytest.param(
            "@@ -1,2 +1,2 @@\n\n ctx\n",
            [HunkEvent(1, 1, 2, 2), LineEvent(1, 1, ""), LineEvent(2, 2, "ctx")],
            id="bare-empty-drains",
        ),
        pytest.param(
            "@@ -1,2 +1,2 @@\nbare_ctx\n ctx\n",
            [
                HunkEvent(1, 1, 2, 2),
                LineEvent(1, 1, "bare_ctx"),
                LineEvent(2, 2, "ctx"),
            ],
            id="bare-context-content",
        ),
        pytest.param(
            "@@ -0,0 +1 @@\n+    indented_add\n",
            [HunkEvent(0, 1, 0, 1), LineEvent(None, 1, "    indented_add")],
            id="leading-whitespace",
        ),
        pytest.param(
            "@@ -0,0 +1 @@\n+a\x0cb\n",
            [HunkEvent(0, 1, 0, 1), LineEvent(None, 1, "a\x0cb")],
            id="formfeed-text",
        ),
        pytest.param(
            "@@ -1,2 +1,2 @@\n a\n\\ No newline at end of file\n b\n",
            [HunkEvent(1, 1, 2, 2), LineEvent(1, 1, "a"), LineEvent(2, 2, "b")],
            id="marker-skipped",
        ),
    ],
)
def test_walk_diff(diff_text: str, expected: list[DiffEvent]) -> None:
    assert list(walk_diff(diff_text)) == expected


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param(
            "--- a/My Docs/read me.md\t\n+++ b/My Docs/read me.md\t\n",
            [
                HeaderEvent("old_path", "a/My Docs/read me.md"),
                HeaderEvent("new_path", "b/My Docs/read me.md"),
            ],
            id="tab-terminated-space",
        ),
        pytest.param(
            '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n',
            [
                HeaderEvent("old_path", "a/café.py"),
                HeaderEvent("new_path", "b/café.py"),
            ],
            id="octal-utf8",
        ),
        pytest.param(
            '+++ "b/tab\\there.txt"\n+++ "b/quo\\"te.txt"\n+++ "b/back\\\\slash.txt"\n',
            [
                HeaderEvent("new_path", "b/tab\there.txt"),
                HeaderEvent("new_path", 'b/quo"te.txt'),
                HeaderEvent("new_path", "b/back\\slash.txt"),
            ],
            id="escaped-tab-quote-backslash",
        ),
        pytest.param(
            '+++ "b/caf\\303\\251 space.py"\t\n',
            [HeaderEvent("new_path", "b/café space.py")],
            id="quoted-tab-terminator",
        ),
        pytest.param(
            '--- "a/bad\\q.py"\n+++ "b/\\377.py"\n+++ "b/\\400.py"\n+++ "b/trailing\\"\n',
            [
                HeaderEvent("old_path", '"a/bad\\q.py"'),
                HeaderEvent("new_path", '"b/\\377.py"'),
                HeaderEvent("new_path", '"b/\\400.py"'),
                HeaderEvent("new_path", '"b/trailing\\"'),
            ],
            id="undecodable-fields",
        ),
        pytest.param(
            '+++ say"hi".py\n+++ "quoted"/app.py\n',
            [
                HeaderEvent("new_path", 'say"hi".py'),
                HeaderEvent("new_path", '"quoted"/app.py'),
            ],
            id="embedded-quotes",
        ),
    ],
)
def test_header_decoding(diff_text: str, expected: list[DiffEvent]) -> None:
    assert list(walk_diff(diff_text)) == expected


@pytest.mark.parametrize(
    "event, expected_fields",
    [
        (HeaderEvent("old_path", "a/f.py"), ("kind", "value")),
        (
            HunkEvent(1, 1, 1, 1),
            ("old_line", "new_line", "old_count", "new_count", "kind"),
        ),
        (LineEvent(1, 1, "ctx"), ("old_line", "new_line", "text", "kind")),
    ],
    ids=["header", "hunk", "line"],
)
def test_diff_event_shape(event: DiffEvent, expected_fields: tuple[str, ...]) -> None:
    assert tuple(field.name for field in fields(event)) == expected_fields
    assert not hasattr(event, "__dict__")


NULL_ORDER_CASES = [
    pytest.param(
        "git-prefixed",
        "--- a/f.py\n+++ b//dev/null\n",
        {},
        set(),
        {},
        {},
        id="git-prefixed-new-null",
    ),
    pytest.param(
        "git-prefixed",
        "--- a//dev/null\n+++ b/f.py\n",
        {("f.py", 1): 1},
        {"f.py"},
        {},
        {("f.py", 1): "ctx"},
        id="git-prefixed-old-null",
    ),
    pytest.param(
        "glab-verbatim",
        "--- a/f.py\n+++ b//dev/null\n",
        {("b//dev/null", 1): 1},
        set(),
        {"b//dev/null": "a/f.py"},
        {("b//dev/null", 1): "ctx"},
        id="glab-verbatim-new-null",
    ),
    pytest.param(
        "glab-verbatim",
        "--- a//dev/null\n+++ b/f.py\n",
        {("b/f.py", 1): 1},
        set(),
        {"b/f.py": "a//dev/null"},
        {("b/f.py", 1): "ctx"},
        id="glab-verbatim-old-null",
    ),
    pytest.param(
        "verify-both-spellings",
        "--- a/f.py\n+++ b//dev/null\n",
        {("b//dev/null", 1): 1, ("/dev/null", 1): 1},
        set(),
        {},
        {("b//dev/null", 1): "ctx", ("/dev/null", 1): "ctx"},
        id="verify-both-spellings-new-null",
    ),
    pytest.param(
        "verify-both-spellings",
        "--- a//dev/null\n+++ b/f.py\n",
        {("b/f.py", 1): 1, ("f.py", 1): 1},
        set(),
        {},
        {("b/f.py", 1): "ctx", ("f.py", 1): "ctx"},
        id="verify-both-spellings-old-null",
    ),
]


@pytest.mark.parametrize(
    "policy, headers, valid, new_files, old_paths, texts", NULL_ORDER_CASES
)
def test_current_null_order(
    policy: DiffPathPolicy,
    headers: str,
    valid: Mapping[LineKey, int | None],
    new_files: set[str],
    old_paths: Mapping[str, str],
    texts: Mapping[LineKey, str],
) -> None:
    diff_text = headers + "@@ -1 +1 @@\n ctx\n"
    if policy == "verify-both-spellings":
        assert decide.parse_diff_lines(diff_text) == set(valid)
    else:
        platform = "github" if policy == "git-prefixed" else "gitlab"
        assert post.parse_diff_text(platform, diff_text) == (
            valid,
            new_files,
            old_paths,
            texts,
        )


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "glab_diff" / name).read_text(
        encoding="utf-8"
    )


DIFF_CASES = [
    (
        "plain-unprefixed-with-git-header",
        "diff --git a/src/app.py b/src/app.py\n--- src/app.py\n+++ src/app.py\n"
        "@@ -1,1 +1,2 @@\n ctx\n+added\n",
        ("glab-verbatim",),
        DiffFacts(
            {("src/app.py", 1): 1, ("src/app.py", 2): None},
            frozenset(),
            {"src/app.py": "src/app.py"},
            {("src/app.py", 1): "ctx", ("src/app.py", 2): "added"},
        ),
    ),
    (
        "plain-modified",
        _fixture("modified.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("src/edited.py", 61): 50,
                ("src/edited.py", 62): None,
                ("src/edited.py", 63): 52,
            },
            frozenset(),
            {"src/edited.py": "src/edited.py"},
            {
                ("src/edited.py", 61): "unchanged_ctx",
                ("src/edited.py", 62): "added",
                ("src/edited.py", 63): "tail_ctx",
            },
        ),
    ),
    (
        "plain-added",
        _fixture("added.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("src/app/clients/api/__init__.py", 1): None,
                ("src/app/clients/api/__init__.py", 2): None,
                ("src/app/clients/api/__init__.py", 3): None,
                ("src/app/clients/api/__init__.py", 4): None,
                ("src/app/clients/api/__init__.py", 5): None,
                ("src/app/clients/api/__init__.py", 6): None,
                ("src/app/clients/api/__init__.py", 7): None,
                ("src/app/clients/api/__init__.py", 8): None,
                ("src/app/clients/api/__init__.py", 9): None,
                ("src/app/clients/api/__init__.py", 10): None,
                ("src/app/clients/api/__init__.py", 11): None,
                ("src/app/clients/api/__init__.py", 12): None,
                ("src/app/clients/api/__init__.py", 13): None,
                ("src/app/clients/api/__init__.py", 14): None,
                ("src/app/clients/api/__init__.py", 15): None,
                ("src/app/clients/api/__init__.py", 16): None,
            },
            frozenset({"src/app/clients/api/__init__.py"}),
            {"src/app/clients/api/__init__.py": "src/app/clients/api/__init__.py"},
            {
                ("src/app/clients/api/__init__.py", 1): "added_01",
                ("src/app/clients/api/__init__.py", 2): "added_02",
                ("src/app/clients/api/__init__.py", 3): "",
                ("src/app/clients/api/__init__.py", 4): "added_04",
                ("src/app/clients/api/__init__.py", 5): "added_05",
                ("src/app/clients/api/__init__.py", 6): "added_06",
                ("src/app/clients/api/__init__.py", 7): "added_07",
                ("src/app/clients/api/__init__.py", 8): "added_08",
                ("src/app/clients/api/__init__.py", 9): "added_09",
                ("src/app/clients/api/__init__.py", 10): "added_10",
                ("src/app/clients/api/__init__.py", 11): "added_11",
                ("src/app/clients/api/__init__.py", 12): "added_12",
                ("src/app/clients/api/__init__.py", 13): "added_13",
                ("src/app/clients/api/__init__.py", 14): "added_14",
                ("src/app/clients/api/__init__.py", 15): "added_15",
                ("src/app/clients/api/__init__.py", 16): "added_16",
            },
        ),
    ),
    (
        "plain-deleted",
        _fixture("deleted.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts({}, frozenset(), {"src/removed.py": "src/removed.py"}, {}),
    ),
    (
        "plain-rename",
        _fixture("rename.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("new_name.py", 3): 3,
                ("new_name.py", 4): None,
                ("new_name.py", 5): 5,
                ("new_name.py", 6): 6,
            },
            frozenset(),
            {"new_name.py": "old_name.py"},
            {
                ("new_name.py", 3): "ctx",
                ("new_name.py", 4): "y",
                ("new_name.py", 5): "ctx2",
                ("new_name.py", 6): "",
            },
        ),
    ),
    (
        "plain-contract",
        _fixture("modified.diff") + _fixture("added.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("src/edited.py", 61): 50,
                ("src/edited.py", 62): None,
                ("src/edited.py", 63): 52,
                ("src/app/clients/api/__init__.py", 1): None,
                ("src/app/clients/api/__init__.py", 2): None,
                ("src/app/clients/api/__init__.py", 3): None,
                ("src/app/clients/api/__init__.py", 4): None,
                ("src/app/clients/api/__init__.py", 5): None,
                ("src/app/clients/api/__init__.py", 6): None,
                ("src/app/clients/api/__init__.py", 7): None,
                ("src/app/clients/api/__init__.py", 8): None,
                ("src/app/clients/api/__init__.py", 9): None,
                ("src/app/clients/api/__init__.py", 10): None,
                ("src/app/clients/api/__init__.py", 11): None,
                ("src/app/clients/api/__init__.py", 12): None,
                ("src/app/clients/api/__init__.py", 13): None,
                ("src/app/clients/api/__init__.py", 14): None,
                ("src/app/clients/api/__init__.py", 15): None,
                ("src/app/clients/api/__init__.py", 16): None,
            },
            frozenset({"src/app/clients/api/__init__.py"}),
            {
                "src/edited.py": "src/edited.py",
                "src/app/clients/api/__init__.py": "src/app/clients/api/__init__.py",
            },
            {
                ("src/edited.py", 61): "unchanged_ctx",
                ("src/edited.py", 62): "added",
                ("src/edited.py", 63): "tail_ctx",
                ("src/app/clients/api/__init__.py", 1): "added_01",
                ("src/app/clients/api/__init__.py", 2): "added_02",
                ("src/app/clients/api/__init__.py", 3): "",
                ("src/app/clients/api/__init__.py", 4): "added_04",
                ("src/app/clients/api/__init__.py", 5): "added_05",
                ("src/app/clients/api/__init__.py", 6): "added_06",
                ("src/app/clients/api/__init__.py", 7): "added_07",
                ("src/app/clients/api/__init__.py", 8): "added_08",
                ("src/app/clients/api/__init__.py", 9): "added_09",
                ("src/app/clients/api/__init__.py", 10): "added_10",
                ("src/app/clients/api/__init__.py", 11): "added_11",
                ("src/app/clients/api/__init__.py", 12): "added_12",
                ("src/app/clients/api/__init__.py", 13): "added_13",
                ("src/app/clients/api/__init__.py", 14): "added_14",
                ("src/app/clients/api/__init__.py", 15): "added_15",
                ("src/app/clients/api/__init__.py", 16): "added_16",
            },
        ),
    ),
    (
        "deleted-body-drains",
        _fixture("deleted.diff") + _fixture("modified.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("src/edited.py", 61): 50,
                ("src/edited.py", 62): None,
                ("src/edited.py", 63): 52,
            },
            frozenset(),
            {"src/removed.py": "src/removed.py", "src/edited.py": "src/edited.py"},
            {
                ("src/edited.py", 61): "unchanged_ctx",
                ("src/edited.py", 62): "added",
                ("src/edited.py", 63): "tail_ctx",
            },
        ),
    ),
    (
        "captured-git",
        _fixture("git_style.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("b/inner.py", 2): 2,
                ("b/inner.py", 3): 3,
                ("b/inner.py", 4): 4,
                ("b/inner.py", 5): None,
                ("b/inner.py", 6): 6,
                ("b/inner.py", 7): 7,
                ("b/inner.py", 8): 8,
                ("src/edited.py", 2): 2,
                ("src/edited.py", 3): 3,
                ("src/edited.py", 4): 4,
                ("src/edited.py", 5): None,
                ("src/edited.py", 6): 6,
                ("src/edited.py", 7): 7,
                ("src/edited.py", 8): 8,
                ("src/added.py", 1): None,
                ("src/added.py", 2): None,
                ("new_name.py", 1): 1,
                ("new_name.py", 2): None,
                ("new_name.py", 3): 3,
                ("new_name.py", 4): 4,
            },
            frozenset({"src/added.py"}),
            {
                "b/inner.py": "b/inner.py",
                "src/edited.py": "src/edited.py",
                "new_name.py": "old_name.py",
            },
            {
                ("b/inner.py", 2): "line_02",
                ("b/inner.py", 3): "line_03",
                ("b/inner.py", 4): "line_04",
                ("b/inner.py", 5): "changed_05",
                ("b/inner.py", 6): "line_06",
                ("b/inner.py", 7): "line_07",
                ("b/inner.py", 8): "line_08",
                ("src/edited.py", 2): "line_02",
                ("src/edited.py", 3): "line_03",
                ("src/edited.py", 4): "line_04",
                ("src/edited.py", 5): "changed_05",
                ("src/edited.py", 6): "line_06",
                ("src/edited.py", 7): "line_07",
                ("src/edited.py", 8): "line_08",
                ("src/added.py", 1): "added_01",
                ("src/added.py", 2): "added_02",
                ("new_name.py", 1): "ctx",
                ("new_name.py", 2): "y",
                ("new_name.py", 3): "ctx2",
                ("new_name.py", 4): "",
            },
        ),
    ),
    (
        "captured-git-spaces",
        _fixture("git_style_spaces.diff"),
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("docs/user guide/x.md", 1): 1,
                ("docs/user guide/x.md", 2): None,
                ("café.py", 1): None,
                ("my file.py", 1): None,
                ("new name.py", 1): 1,
                ("new name.py", 2): None,
            },
            frozenset({"my file.py", "café.py"}),
            {
                "docs/user guide/x.md": "docs/user guide/x.md",
                "new name.py": "old name.py",
            },
            {
                ("docs/user guide/x.md", 1): "ctx",
                ("docs/user guide/x.md", 2): "new",
                ("café.py", 1): "a",
                ("my file.py", 1): "added_01",
                ("new name.py", 1): "ctx",
                ("new name.py", 2): "y",
            },
        ),
    ),
    (
        "git-prefix-contract",
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n ctx\n-x\n+y\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("src/app.py", 1): 1, ("src/app.py", 2): None},
            frozenset(),
            {"src/app.py": "src/app.py"},
            {("src/app.py", 1): "ctx", ("src/app.py", 2): "y"},
        ),
    ),
    (
        "plain-real-b",
        "--- b/inner.py\n+++ b/inner.py\n@@ -1 +1,2 @@\n ctx\n+added\n",
        ("glab-verbatim",),
        DiffFacts(
            {("b/inner.py", 1): 1, ("b/inner.py", 2): None},
            frozenset(),
            {"b/inner.py": "b/inner.py"},
            {("b/inner.py", 1): "ctx", ("b/inner.py", 2): "added"},
        ),
    ),
    (
        "plain-real-a-b",
        "--- a/real/x.py\n+++ a/real/x.py\n@@ -1 +1 @@\n-o\n+n\n--- b/real/y.py\n+++ b/real/y.py\n@@ -1 +1 @@\n-old\n+fresh\n",
        ("glab-verbatim",),
        DiffFacts(
            {("a/real/x.py", 1): None, ("b/real/y.py", 1): None},
            frozenset(),
            {"a/real/x.py": "a/real/x.py", "b/real/y.py": "b/real/y.py"},
            {("a/real/x.py", 1): "n", ("b/real/y.py", 1): "fresh"},
        ),
    ),
    (
        "side-specific",
        "--- b/old.py\n+++ a/new.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("git-prefixed",),
        DiffFacts(
            {("a/new.py", 1): None},
            frozenset(),
            {"a/new.py": "b/old.py"},
            {("a/new.py", 1): "n"},
        ),
    ),
    (
        "pair-proof:space-lookalike",
        "diff --git a/x b/y b/z\n--- a/x b/y\n+++ b/z\n@@ -1,1 +1,2 @@\n ctx\n+newline\n",
        ("glab-verbatim",),
        DiffFacts(
            {("z", 1): 1, ("z", 2): None},
            frozenset(),
            {"z": "x b/y"},
            {("z", 1): "ctx", ("z", 2): "newline"},
        ),
    ),
    (
        "pair-proof:old-unprefixed",
        "diff --git a/bar.py b/bar.py\n--- bar.py\n+++ b/bar.py\n@@ -1,1 +1,2 @@\n ctx\n+newline\n",
        ("glab-verbatim",),
        DiffFacts(
            {("b/bar.py", 1): 1, ("b/bar.py", 2): None},
            frozenset(),
            {"b/bar.py": "bar.py"},
            {("b/bar.py", 1): "ctx", ("b/bar.py", 2): "newline"},
        ),
    ),
    (
        "pair-proof:new-unprefixed",
        "diff --git a/bar.py b/bar.py\n--- a/bar.py\n+++ bar.py\n@@ -1,1 +1,2 @@\n ctx\n+newline\n",
        ("glab-verbatim",),
        DiffFacts(
            {("bar.py", 1): 1, ("bar.py", 2): None},
            frozenset(),
            {"bar.py": "a/bar.py"},
            {("bar.py", 1): "ctx", ("bar.py", 2): "newline"},
        ),
    ),
    (
        "pair-proof:old-mismatch",
        "diff --git a/bar.py b/bar.py\n--- a/other.py\n+++ b/bar.py\n@@ -1,1 +1,2 @@\n ctx\n+newline\n",
        ("glab-verbatim",),
        DiffFacts(
            {("b/bar.py", 1): 1, ("b/bar.py", 2): None},
            frozenset(),
            {"b/bar.py": "a/other.py"},
            {("b/bar.py", 1): "ctx", ("b/bar.py", 2): "newline"},
        ),
    ),
    (
        "pair-proof:both-mismatch",
        "diff --git a/bar.py b/bar.py\n--- a/other.py\n+++ b/other.py\n@@ -1,1 +1,2 @@\n ctx\n+newline\n",
        ("glab-verbatim",),
        DiffFacts(
            {("b/other.py", 1): 1, ("b/other.py", 2): None},
            frozenset(),
            {"b/other.py": "a/other.py"},
            {("b/other.py", 1): "ctx", ("b/other.py", 2): "newline"},
        ),
    ),
    (
        "pair-proof:quoted",
        'diff --git "a/old path.py" "b/new path.py"\n--- "a/old path.py"\n+++ "b/new path.py"\n@@ -1,1 +1,2 @@\n ctx\n+newline\n',
        ("glab-verbatim",),
        DiffFacts(
            {("b/new path.py", 1): 1, ("b/new path.py", 2): None},
            frozenset(),
            {"b/new path.py": "a/old path.py"},
            {("b/new path.py", 1): "ctx", ("b/new path.py", 2): "newline"},
        ),
    ),
    (
        "pair-proof:tab-cut",
        "diff --git a/t\tx.py b/t\tx.py\n--- a/t\tx.py\n+++ b/t\tx.py\n@@ -1,1 +1,2 @@\n ctx\n+newline\n",
        ("glab-verbatim",),
        DiffFacts(
            {("b/t", 1): 1, ("b/t", 2): None},
            frozenset(),
            {"b/t": "a/t"},
            {("b/t", 1): "ctx", ("b/t", 2): "newline"},
        ),
    ),
    (
        "one-pair-proof:plain-after-proven",
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("glab-verbatim",),
        DiffFacts(
            {("x.py", 1): None, ("b/x.py", 1): None},
            frozenset(),
            {"x.py": "x.py", "b/x.py": "a/x.py"},
            {("x.py", 1): "n", ("b/x.py", 1): "n"},
        ),
    ),
    (
        "one-pair-proof:failed-proof-consumed",
        "diff --git a/x.py b/x.py\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-o\n+n\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("glab-verbatim",),
        DiffFacts(
            {("b/y.py", 1): None, ("b/x.py", 1): None},
            frozenset(),
            {"b/y.py": "a/y.py", "b/x.py": "a/x.py"},
            {("b/y.py", 1): "n", ("b/x.py", 1): "n"},
        ),
    ),
    (
        "one-pair-proof:pending-old-cleared",
        "diff --git a/x.py b/x.py\n--- /dev/null\ndiff --git a/y.py b/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("glab-verbatim",),
        DiffFacts({("b/y.py", 1): None}, frozenset(), {}, {("b/y.py", 1): "n"}),
    ),
    (
        "empty-old:proven",
        "diff --git a/empty.py b/empty.py\n--- a/empty.py\n+++ b/empty.py\n@@ -0,0 +1 @@\n+first\n",
        ("glab-verbatim",),
        DiffFacts(
            {("empty.py", 1): None},
            frozenset(),
            {"empty.py": "empty.py"},
            {("empty.py", 1): "first"},
        ),
    ),
    (
        "empty-old:plain",
        "--- empty.py\n+++ empty.py\n@@ -0,0 +1 @@\n+first\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("empty.py", 1): None},
            frozenset({"empty.py"}),
            {"empty.py": "empty.py"},
            {("empty.py", 1): "first"},
        ),
    ),
    (
        "empty-old:plain-after-proven",
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n--- empty.py\n+++ empty.py\n@@ -0,0 +1 @@\n+first\n",
        ("glab-verbatim",),
        DiffFacts(
            {("x.py", 1): None, ("empty.py", 1): None},
            frozenset({"empty.py"}),
            {"x.py": "x.py", "empty.py": "empty.py"},
            {("x.py", 1): "n", ("empty.py", 1): "first"},
        ),
    ),
    (
        "empty-old:git-prefixed-named",
        "--- a/empty.py\n+++ b/empty.py\n@@ -0,0 +1,2 @@\n+first\n+second\n",
        ("git-prefixed",),
        DiffFacts(
            {("empty.py", 1): None, ("empty.py", 2): None},
            frozenset({"empty.py"}),
            {"empty.py": "empty.py"},
            {("empty.py", 1): "first", ("empty.py", 2): "second"},
        ),
    ),
    (
        "orphan-new",
        "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("git-prefixed",),
        DiffFacts({("x.py", 1): None}, frozenset(), {}, {("x.py", 1): "n"}),
    ),
    (
        "orphan-new",
        "diff --git a/x.py b/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("glab-verbatim",),
        DiffFacts({("b/x.py", 1): None}, frozenset(), {}, {("b/x.py", 1): "n"}),
    ),
    (
        "consumed-old",
        "--- /dev/null\n+++ b/x.py\n@@ -1 +1 @@\n-o\n+n\n+++ b/y.py\n@@ -1 +1 @@\n-o\n+n\n--- a/z.py\n+++ b/z.py\n@@ -1 +1 @@\n-o\n+n\n+++ b/w.py\n@@ -1 +1 @@\n-o\n+n\n",
        ("git-prefixed",),
        DiffFacts(
            {
                ("x.py", 1): None,
                ("y.py", 1): None,
                ("z.py", 1): None,
                ("w.py", 1): None,
            },
            frozenset({"x.py"}),
            {"z.py": "z.py"},
            {("x.py", 1): "n", ("y.py", 1): "n", ("z.py", 1): "n", ("w.py", 1): "n"},
        ),
    ),
    (
        "empty-new-no-hunk",
        "diff --git a/empty_new.py b/empty_new.py\nnew file mode 100644\nindex 0000000..e69de29\n--- /dev/null\n+++ b/empty_new.py\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts({}, frozenset({"empty_new.py"}), {}, {}),
    ),
    (
        "null-old",
        "diff --git a/added.py b/added.py\nnew file mode 100644\n--- /dev/null\n+++ b/added.py\n@@ -0,0 +1,1 @@\n+content\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("added.py", 1): None},
            frozenset({"added.py"}),
            {},
            {("added.py", 1): "content"},
        ),
    ),
    (
        "null-new",
        "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts({}, frozenset(), {}, {}),
    ),
    (
        "synthetic-null-new",
        "--- a/first.py\n+++ b/first.py\n@@ -1 +1 @@\n keep\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +1 @@\n-dropped\n stray\n",
        ("git-prefixed",),
        DiffFacts(
            {("first.py", 1): 1},
            frozenset(),
            {"first.py": "first.py"},
            {("first.py", 1): "keep"},
        ),
    ),
    (
        "orphan-body",
        "@@ -0,0 +1 @@\n+orphan\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts({}, frozenset(), {}, {}),
    ),
    (
        "decoded-git-space",
        "--- a/dir with space/x.py\t\n+++ b/dir with space/x.py\t\n@@ -1 +1 @@\n-old\n+new\n",
        ("git-prefixed",),
        DiffFacts(
            {("dir with space/x.py", 1): None},
            frozenset(),
            {"dir with space/x.py": "dir with space/x.py"},
            {("dir with space/x.py", 1): "new"},
        ),
    ),
    (
        "decoded-git-utf8",
        '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-old\n+new\n',
        ("git-prefixed",),
        DiffFacts(
            {("café.py", 1): None},
            frozenset(),
            {"café.py": "café.py"},
            {("café.py", 1): "new"},
        ),
    ),
    (
        "literal-quote-limitation",
        '--- "notes"\n+++ "notes"\n@@ -1 +1,2 @@\n c\n+z\n',
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("notes", 1): 1, ("notes", 2): None},
            frozenset(),
            {"notes": "notes"},
            {("notes", 1): "c", ("notes", 2): "z"},
        ),
    ),
    (
        "decoded-rename",
        'diff --git "a/caf\\303\\251 old.py" b/new.py\nsimilarity index 100%\nrename from "caf\\303\\251 old.py"\nrename to new.py\n--- "a/caf\\303\\251 old.py"\n+++ b/new.py\n@@ -1 +1 @@\n ctx\n',
        ("git-prefixed",),
        DiffFacts(
            {("new.py", 1): 1},
            frozenset(),
            {"new.py": "café old.py"},
            {("new.py", 1): "ctx"},
        ),
    ),
    (
        "omitted-counts",
        "--- oneline.txt\n+++ oneline.txt\n@@ -0,0 +1 @@\n+only\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("oneline.txt", 1): None},
            frozenset({"oneline.txt"}),
            {"oneline.txt": "oneline.txt"},
            {("oneline.txt", 1): "only"},
        ),
    ),
    (
        "formfeed-content",
        "--- ff.py\n+++ ff.py\n@@ -1,3 +1,3 @@\n p1\n \x0c\n-p2\n+p2X\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("ff.py", 1): 1, ("ff.py", 2): 2, ("ff.py", 3): None},
            frozenset(),
            {"ff.py": "ff.py"},
            {("ff.py", 1): "p1", ("ff.py", 2): "\x0c", ("ff.py", 3): "p2X"},
        ),
    ),
    (
        "minus-header-body",
        "diff --git a/db/schema.sql b/db/schema.sql\n--- db/schema.sql\n+++ db/schema.sql\n@@ -10,4 +10,3 @@\n CREATE TABLE t (\n--- deprecated: drop me\n   id INT,\n );\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {
                ("db/schema.sql", 10): 10,
                ("db/schema.sql", 11): 12,
                ("db/schema.sql", 12): 13,
            },
            frozenset(),
            {"db/schema.sql": "db/schema.sql"},
            {
                ("db/schema.sql", 10): "CREATE TABLE t (",
                ("db/schema.sql", 11): "  id INT,",
                ("db/schema.sql", 12): ");",
            },
        ),
    ),
    (
        "plus-header-body",
        "diff --git a/src/app.c b/src/app.c\n--- src/app.c\n+++ src/app.c\n@@ -20,2 +20,3 @@\n int i = 0;\n+++ x\n use(i);\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("src/app.c", 20): 20, ("src/app.c", 21): None, ("src/app.c", 22): 21},
            frozenset(),
            {"src/app.c": "src/app.c"},
            {
                ("src/app.c", 20): "int i = 0;",
                ("src/app.c", 21): "++ x",
                ("src/app.c", 22): "use(i);",
            },
        ),
    ),
    (
        "binary-noise",
        "diff --git a/img.png b/img.png\n--- a/img.png\n+++ b/img.png\nBinary files a/img.png and b/img.png differ\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts({}, frozenset(), {"img.png": "img.png"}, {}),
    ),
    (
        "newline-marker",
        "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n a\n\\ No newline at end of file\n b\n",
        ("git-prefixed",),
        DiffFacts(
            {("f.py", 1): 1, ("f.py", 2): 2},
            frozenset(),
            {"f.py": "f.py"},
            {("f.py", 1): "a", ("f.py", 2): "b"},
        ),
    ),
    (
        "truncated-hunk",
        "--- a/f.py\n+++ b/f.py\n@@ -1,4 +1,4 @@\n ctx\n+added\n",
        ("git-prefixed",),
        DiffFacts(
            {("f.py", 1): 1, ("f.py", 2): None},
            frozenset(),
            {"f.py": "f.py"},
            {("f.py", 1): "ctx", ("f.py", 2): "added"},
        ),
    ),
    (
        "last-write",
        "--- first\n+++ f\n@@ -1 +1 @@\n before\n--- last\n+++ f\n@@ -8 +1 @@\n after\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts({("f", 1): 8}, frozenset(), {"f": "last"}, {("f", 1): "after"}),
    ),
    (
        "replacement-character-content",
        "--- f.py\n+++ f.py\n@@ -1 +1 @@\n-old\n+repl�\n",
        ("git-prefixed", "glab-verbatim"),
        DiffFacts(
            {("f.py", 1): None},
            frozenset(),
            {"f.py": "f.py"},
            {("f.py", 1): "repl�"},
        ),
    ),
    (
        "leading-whitespace",
        "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 0\n+    return 1\n",
        ("git-prefixed",),
        DiffFacts(
            {("foo.py", 1): 1, ("foo.py", 2): None},
            frozenset(),
            {"foo.py": "foo.py"},
            {("foo.py", 1): "def f():", ("foo.py", 2): "    return 1"},
        ),
    ),
]


@pytest.mark.parametrize(
    "diff_text, policy, expected",
    [
        pytest.param(diff, policy, expected, id=f"{name}:{policy}")
        for name, diff, policies, expected in DIFF_CASES
        for policy in policies
    ],
)
def test_diff_facts(
    diff_text: str, policy: DiffPathPolicy, expected: DiffFacts
) -> None:
    assert parse_diff(diff_text, policy=policy) == expected


def test_deleted_git_body_facts() -> None:
    diff_text = (
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n-a\n-b\n-c\n"
        "diff --git a/next.py b/next.py\n--- a/next.py\n+++ b/next.py\n"
        "@@ -5,2 +5,2 @@\n keep\n-x\n+y\n"
    )
    assert parse_diff(diff_text, policy="git-prefixed") == DiffFacts(
        {("next.py", 5): 5, ("next.py", 6): None},
        frozenset(),
        {"next.py": "next.py"},
        {("next.py", 5): "keep", ("next.py", 6): "y"},
    )


@pytest.mark.parametrize(
    "fixture, path, line, expected_old",
    [
        pytest.param("git_style.diff", "src/edited.py", 2, 2, id="edited-file"),
        pytest.param("git_style.diff", "b/inner.py", 2, 2, id="real-b-directory"),
        pytest.param(
            "git_style_spaces.diff",
            "docs/user guide/x.md",
            1,
            1,
            id="directory-with-space",
        ),
        pytest.param(
            "git_style_spaces.diff", "my file.py", 1, None, id="added-file-with-space"
        ),
        pytest.param("git_style_spaces.diff", "café.py", 1, None, id="non-ascii"),
        pytest.param(
            "git_style_spaces.diff", "new name.py", 1, 1, id="renamed-file-with-space"
        ),
    ],
)
def test_captured_path_anchors(
    fixture: str, path: str, line: int, expected_old: int | None
) -> None:
    facts = parse_diff(_fixture(fixture), policy="glab-verbatim")
    assert facts is not None
    assert is_line_valid(facts, path, line)
    assert diff_path_spelling(facts, path, line) == path
    assert old_line_for(facts, path, line) == expected_old


def test_diff_facts_fields() -> None:
    assert tuple(field.name for field in fields(_facts())) == (
        "valid_lines",
        "new_files",
        "old_paths",
        "line_texts",
    )


@pytest.mark.parametrize(
    "policy", ["git-prefixed", "glab-verbatim", "verify-both-spellings"]
)
def test_diff_absence(policy: DiffPathPolicy) -> None:
    assert parse_diff(None, policy=policy) is None
    assert parse_diff("", policy=policy) == DiffFacts({}, frozenset(), {}, {})


@pytest.mark.parametrize(
    "policy, headers, valid, new_files, old_paths, texts", NULL_ORDER_CASES
)
def test_diff_null_order(
    policy: DiffPathPolicy,
    headers: str,
    valid: Mapping[LineKey, int | None],
    new_files: set[str],
    old_paths: Mapping[str, str],
    texts: Mapping[LineKey, str],
) -> None:
    assert parse_diff(headers + "@@ -1 +1 @@\n ctx\n", policy=policy) == DiffFacts(
        valid,
        frozenset(new_files),
        old_paths,
        texts,
    )


@pytest.mark.parametrize(
    "platform, expected",
    [
        ("github", "git-prefixed"),
        ("gitlab", "glab-verbatim"),
    ],
)
def test_posting_policy(
    platform: Literal["github", "gitlab"], expected: PostingPathPolicy
) -> None:
    assert posting_policy(platform) == expected


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param(
            "--- a/real/x.py\n+++ a/real/x.py\n", "glab-verbatim", id="verbatim"
        ),
        pytest.param(
            "--- b/real/x.py\n+++ b/real/x.py\n@@ -1,2 +1,3 @@\ndiff --git a/real/x.py b/real/x.py\n def f():\n+    original\n",
            "glab-verbatim",
            id="body-marker",
        ),
        pytest.param("diff --git a/x.py b/x.py\n", "git-prefixed", id="git-prefixed"),
        pytest.param("diff --git b/foo.py b/foo.py\n", "glab-verbatim", id="noprefix"),
        pytest.param(
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n',
            "git-prefixed",
            id="quoted-first",
        ),
        pytest.param("diff --git i/x w/x\n", "glab-verbatim", id="mnemonic"),
        pytest.param("\ndiff --git a/x b/x\n", "glab-verbatim", id="leading-newline"),
        pytest.param("", "glab-verbatim", id="empty"),
    ],
)
def test_patch_report_policy(diff_text: str, expected: PostingPathPolicy) -> None:
    assert patch_report_policy(diff_text) == expected


@pytest.mark.parametrize(
    "diff_text, expected_lines, expected_texts",
    [
        pytest.param(
            "--- b/real/x\n+++ b/real/x\n@@ -1 +1 @@\n ctx\n",
            {("b/real/x", 1): 1},
            {("b/real/x", 1): "ctx"},
            id="verbatim",
        ),
        pytest.param(
            "--- b/real/x\n+++ b/real/x\n@@ -1,2 +1,2 @@\ndiff --git a/x b/x\n ctx\n",
            {("b/real/x", 1): 1, ("b/real/x", 2): 2},
            {("b/real/x", 1): "diff --git a/x b/x", ("b/real/x", 2): "ctx"},
            id="body-marker",
        ),
        pytest.param(
            "diff --git a/x b/x\n--- a/other\n+++ b/other\n@@ -1 +1 @@\n ctx\n",
            {("other", 1): 1},
            {("other", 1): "ctx"},
            id="git-prefixed",
        ),
        pytest.param(
            "diff --git b/foo b/foo\n--- b/foo\n+++ b/foo\n@@ -1 +1 @@\n real\n"
            "diff --git foo foo\n--- foo\n+++ foo\n@@ -1 +1 @@\n sibling\n",
            {("b/foo", 1): 1, ("foo", 1): 1},
            {("b/foo", 1): "real", ("foo", 1): "sibling"},
            id="noprefix",
        ),
        pytest.param(
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n ctx\n',
            {("café.py", 1): 1},
            {("café.py", 1): "ctx"},
            id="quoted-first",
        ),
    ],
)
def test_patch_report_selection(
    diff_text: str,
    expected_lines: Mapping[LineKey, int | None],
    expected_texts: Mapping[LineKey, str],
) -> None:
    assert patches._diff_oracle(diff_text) == (expected_lines, expected_texts)
    facts = parse_diff(diff_text, policy=patch_report_policy(diff_text))
    assert facts is not None
    assert facts.valid_lines == expected_lines
    assert facts.line_texts == expected_texts


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", DiffCounts(0, 0, 0), id="empty"),
        pytest.param(
            "@@ -7,3 +7,3 @@\n ctx\n-gone\n+fresh\n tail\n",
            DiffCounts(1, 1, 0),
            id="pathless-body",
        ),
        pytest.param(
            "--- /dev/null\n+++ b/f.py\n@@ -0,0 +1 @@\n+new\n",
            DiffCounts(1, 0, 0),
            id="added",
        ),
        pytest.param(
            "--- a/f.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n",
            DiffCounts(0, 1, 0),
            id="removed",
        ),
        pytest.param(
            "@@ -1,3 +1,3 @@\n ctx\n--- x\n+++ y\n tail\n",
            DiffCounts(1, 1, 0),
            id="header-shaped-body",
        ),
        pytest.param(
            "Binary files a/x and b/x differ\r\nBinary files a/y and b/y differ\n",
            DiffCounts(0, 0, 2),
            id="crlf-binary",
        ),
        pytest.param(
            "@@ -1 +1 @@\nBinary files a/x and b/x differ\n",
            DiffCounts(0, 0, 1),
            id="body-binary",
        ),
        pytest.param(
            " Binary files a/x and b/x differ\nBinary files a/x and b/x differ!\n+Binary files a/x and b/x differ\n",
            DiffCounts(0, 0, 0),
            id="binary-exact-prose",
        ),
        pytest.param(
            _fixture("git_style.diff"), DiffCounts(5, 5, 0), id="captured-git"
        ),
        pytest.param(
            _fixture("git_style_spaces.diff"), DiffCounts(4, 1, 0), id="captured-spaces"
        ),
    ],
)
def test_diff_counts(diff_text: str, expected: DiffCounts) -> None:
    assert diff_counts(diff_text) == expected


@pytest.mark.parametrize(
    "diff_text, expected_stdout",
    [
        pytest.param(
            "@@ -7,3 +7,3 @@\n ctx\n-gone\n+fresh\n tail\n",
            "changed_lines=2\nbinary_files=0\n",
            id="pathless-body",
        ),
        pytest.param(
            "@@ -1 +1 @@\nBinary files a/x and b/x differ\n",
            "changed_lines=0\nbinary_files=1\n",
            id="body-binary",
        ),
    ],
)
def test_current_counts(
    diff_text: str,
    expected_stdout: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch = tmp_path / "capture.patch"
    patch.write_text(diff_text, encoding="utf-8")
    assert numstat._execute(argparse.Namespace(patch=str(patch))) == 0
    captured = capsys.readouterr()
    assert captured.out == expected_stdout
    assert captured.err == ""


def _facts(
    valid: Mapping[LineKey, int | None] | None = None,
    *,
    new_files: Iterable[str] = (),
    old_paths: Mapping[str, str] | None = None,
    texts: Mapping[LineKey, str] | None = None,
) -> DiffFacts:
    return DiffFacts(
        {} if valid is None else valid,
        frozenset(new_files),
        {} if old_paths is None else old_paths,
        {} if texts is None else texts,
    )


@pytest.mark.parametrize(
    "facts, path, line, expected",
    [
        pytest.param(None, "any.py", 999, (True, "any.py", None), id="skipped-valid"),
        pytest.param(
            _facts({("src/app.py", 42): 30}),
            "src/app.py",
            42,
            (True, "src/app.py", 30),
            id="exact-valid",
        ),
        pytest.param(
            _facts({("src/app.py", 42): 30}),
            "src/app.py",
            43,
            (False, "src/app.py", None),
            id="missing-valid",
        ),
        pytest.param(
            _facts({("src/app.py", 10): 4}),
            "a/src/app.py",
            10,
            (True, "src/app.py", 4),
            id="prefixed-valid-a",
        ),
        pytest.param(
            _facts({("src/app.py", 10): 4}),
            "b/src/app.py",
            10,
            (True, "src/app.py", 4),
            id="prefixed-valid-b",
        ),
        pytest.param(
            _facts({("src/app.py", 7): None}),
            "src/app.py",
            7,
            (True, "src/app.py", None),
            id="null-value",
        ),
        pytest.param(
            _facts({("src/app.py", 7): 5}),
            "src/app.py",
            7,
            (True, "src/app.py", 5),
            id="integer-value",
        ),
        pytest.param(
            _facts({("src/app.py", 61): 50}),
            "src/app.py",
            61,
            (True, "src/app.py", 50),
            id="exact-old-line",
        ),
        pytest.param(
            _facts({("src/app.py", 61): None}),
            "src/app.py",
            61,
            (True, "src/app.py", None),
            id="added-old-line",
        ),
        pytest.param(
            _facts({("src/app.py", 61): 50}),
            "a/src/app.py",
            61,
            (True, "src/app.py", 50),
            id="prefixed-old-line-a",
        ),
        pytest.param(
            _facts({("src/app.py", 61): 50}),
            "b/src/app.py",
            61,
            (True, "src/app.py", 50),
            id="prefixed-old-line-b",
        ),
        pytest.param(None, "f.py", 1, (True, "f.py", None), id="skipped-old-line"),
        pytest.param(
            _facts({("f.py", 1): 1}),
            "other.py",
            1,
            (False, "other.py", None),
            id="missing-old-line",
        ),
        pytest.param(_facts(), "f.py", 1, (False, "f.py", None), id="empty-oracle"),
        pytest.param(
            _facts({("b/x", 1): 0, ("x", 1): 9}),
            "b/x",
            1,
            (True, "b/x", 0),
            id="exact-before-stripped",
        ),
        pytest.param(
            _facts({("b/x", 1): None, ("x", 1): 9}),
            "b/x",
            1,
            (True, "b/x", None),
            id="exact-null-before-stripped",
        ),
        pytest.param(
            _facts({("x", 1): 0}), "x", True, (True, "x", 0), id="boolean-query"
        ),
        pytest.param(_facts({("x", 1): 0}), "x", 1.0, (True, "x", 0), id="float-query"),
        pytest.param(
            _facts({("x", 1): 1}), "b/x", None, (False, "b/x", None), id="no-query-line"
        ),
        pytest.param(
            _facts({("x", 1): 1}),
            "b/b/x",
            1,
            (False, "b/b/x", None),
            id="nonrecursive-query",
        ),
    ],
)
def test_diff_lookup(
    facts: DiffFacts | None,
    path: str,
    line: int | float | None,
    expected: tuple[bool, str, int | None],
) -> None:
    query = cast(int | None, line)
    assert (
        is_line_valid(facts, path, query),
        diff_path_spelling(facts, path, query),
        old_line_for(facts, path, query),
    ) == expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        pytest.param(None, "foo.py", None, id="skipped-diagnostic"),
        pytest.param(
            _facts(
                {
                    ("src/app.py", 10): None,
                    ("src/app.py", 3): 3,
                    ("src/app.py", 7): None,
                    ("other.py", 1): 1,
                }
            ),
            "src/app.py",
            [3, 7, 10],
            id="sorted-diagnostic",
        ),
        pytest.param(
            _facts({("f.py", i): i for i in range(1, 21)}),
            "f.py",
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            id="ten-line-limit",
        ),
        pytest.param(
            _facts({("src/app.py", 5): 2}),
            "a/src/app.py",
            [5],
            id="prefixed-diagnostic",
        ),
        pytest.param(
            _facts({("other.py", 1): None}), "missing.py", [], id="empty-diagnostic"
        ),
        pytest.param(
            _facts({("b/f", 3): None, ("b/f", 1): None, ("f", 1): 1, ("f", 2): 2}),
            "b/f",
            [1, 2, 3],
            id="sorted-deduplicated-union",
        ),
    ],
)
def test_diff_diagnostics(
    facts: DiffFacts | None, path: str, expected: list[int] | None
) -> None:
    assert valid_lines_for_file(facts, path) == expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        pytest.param(None, "any.py", False, id="skipped-new-file"),
        pytest.param(_facts(), "any.py", False, id="empty-new-file"),
        pytest.param(
            _facts(new_files={"src/added.py"}),
            "src/added.py",
            True,
            id="exact-new-file",
        ),
        pytest.param(
            _facts(new_files={"src/added.py"}),
            "a/src/added.py",
            False,
            id="unresolved-prefix-a",
        ),
        pytest.param(
            _facts(new_files={"src/added.py"}),
            "b/src/added.py",
            False,
            id="unresolved-prefix-b",
        ),
        pytest.param(
            _facts(new_files={"src/added.py"}),
            "src/other.py",
            False,
            id="missing-new-file",
        ),
        pytest.param(
            _facts(new_files={"foo.py"}), "a/foo.py", False, id="real-a-collision"
        ),
    ],
)
def test_diff_new_file(facts: DiffFacts | None, path: str, expected: bool) -> None:
    assert is_new_file(facts, path) is expected


@pytest.mark.parametrize(
    "facts, path, start, end, expected",
    [
        pytest.param(None, "x", 1, 3, True, id="skipped-range"),
        pytest.param(_facts(), "x", 1, 1, False, id="empty-range-oracle"),
        pytest.param(
            _facts({("b/x", 1): 1, ("x", 2): None}),
            "b/x",
            1,
            2,
            True,
            id="per-line-resolution",
        ),
        pytest.param(
            _facts({("x", 1): 1, ("x", 3): None}),
            "x",
            1,
            3,
            False,
            id="all-lines-required",
        ),
        pytest.param(_facts(), "x", 2, 1, True, id="vacuous-range"),
        pytest.param(_facts(), "x", 1, 10**20, False, id="short-circuit"),
    ],
)
def test_diff_range(
    facts: DiffFacts | None, path: str, start: int, end: int, expected: bool
) -> None:
    assert range_is_valid(facts, path, start, end) is expected


@pytest.mark.parametrize(
    "facts, path, start, end, expected",
    [
        pytest.param(None, "x", 1, 1, None, id="skipped-span"),
        pytest.param(
            _facts(texts={("x", 1): "a", ("x", 2): ""}),
            "x",
            1,
            2,
            ["a", ""],
            id="exact-span",
        ),
        pytest.param(
            _facts({("x", 1): 1}, texts={}), "x", 1, 1, None, id="partial-content"
        ),
        pytest.param(
            _facts(texts={("b/x", 1): "a", ("x", 2): "b"}),
            "b/x",
            1,
            2,
            None,
            id="no-union-span",
        ),
        pytest.param(
            _facts(texts={("x", 1): "a"}), "b/x", 1, 1, None, id="no-strip-span"
        ),
        pytest.param(_facts(), "x", 2, 1, [], id="vacuous-span"),
    ],
)
def test_diff_span(
    facts: DiffFacts | None, path: str, start: int, end: int, expected: list[str] | None
) -> None:
    assert span_texts(facts, path, start, end) == expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        pytest.param(None, "b/x", False, id="skipped-ambiguity"),
        pytest.param(_facts(), "src/foo.py", False, id="unprefixed-empty"),
        pytest.param(
            _facts({("src/foo.py", 9): 9, ("b/src/foo.py", 10): 10}),
            "src/foo.py",
            False,
            id="unprefixed-sibling",
        ),
        pytest.param(
            _facts({("b/x", 1): 1, ("x", 9): None}),
            "b/x",
            True,
            id="addressable-collision",
        ),
        pytest.param(
            _facts({("x", 1): 1}, old_paths={"b/x": "b/x"}, new_files={"b/x"}),
            "b/x",
            False,
            id="header-only-sibling",
        ),
        pytest.param(
            _facts({("new", 1): 1, ("x", 1): 1}, old_paths={"new": "b/x"}),
            "b/x",
            False,
            id="old-rename-sibling",
        ),
    ],
)
def test_diff_ambiguity(facts: DiffFacts | None, path: str, expected: bool) -> None:
    assert path_is_ambiguous(facts, path) is expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        (None, "b/x", "b/x"),
        (_facts(), "b/x", "b/x"),
        (_facts(old_paths={"x": "old"}), "x", "old"),
        (_facts(old_paths={"x": "old"}), "b/x", "b/x"),
        (_facts(old_paths={"x": ""}), "x", ""),
    ],
)
def test_diff_old_path(facts: DiffFacts | None, path: str, expected: str) -> None:
    assert old_path_for(facts, path) == expected


def test_current_lookup_contract() -> None:
    valid = {("b/x", 1): 0, ("x", 1): 9}
    assert post.is_line_valid(valid, "b/x", 1)
    assert post.diff_path_spelling(valid, "b/x", 1) == "b/x"
    assert post.old_line_for(valid, "b/x", 1) == 0
    assert post.valid_lines_for_file(
        {("b/f", 3): None, ("b/f", 1): None, ("f", 1): 1, ("f", 2): 2}, "b/f"
    ) == [1, 2, 3]
    assert post._span_texts({("b/x", 1): "a", ("x", 2): "b"}, "b/x", 1, 2) is None


def test_current_last_write() -> None:
    diff_text = (
        "--- first\n+++ f\n@@ -1 +1 @@\n before\n--- last\n+++ f\n@@ -8 +1 @@\n after\n"
    )
    assert post.parse_diff_text("gitlab", diff_text) == (
        {("f", 1): 8},
        set(),
        {"f": "last"},
        {("f", 1): "after"},
    )


def test_current_side_specific() -> None:
    assert post.parse_diff_text(
        "github", "--- b/old.py\n+++ a/new.py\n@@ -1 +1 @@\n-o\n+n\n"
    ) == (
        {("a/new.py", 1): None},
        set(),
        {"a/new.py": "b/old.py"},
        {("a/new.py", 1): "n"},
    )


def test_parse_diff_requires_policy() -> None:
    with pytest.raises(TypeError, match="policy"):
        without_policy = cast(Callable[[str], DiffFacts | None], parse_diff)
        without_policy("")


@pytest.mark.parametrize("query", [None, 1, 1.0, True, []])
def test_diff_lookup_wrong_path(query: object) -> None:
    with pytest.raises(TypeError):
        is_line_valid(_facts(), cast(str, query), 1)


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", set(), id="empty"),
        pytest.param(None, None, id="skipped"),
        pytest.param(
            "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,3 +1,4 @@\n line1\n+added_line\n line2\n line3\n",
            {
                ("b/foo.py", 4),
                ("foo.py", 4),
                ("b/foo.py", 3),
                ("foo.py", 3),
                ("b/foo.py", 2),
                ("foo.py", 2),
                ("b/foo.py", 1),
                ("foo.py", 1),
            },
            id="added",
        ),
        pytest.param(
            "diff --git a/bar.py b/bar.py\n--- a/bar.py\n+++ b/bar.py\n@@ -1,4 +1,3 @@\n line1\n-removed\n line2\n line3\n",
            {
                ("bar.py", 1),
                ("b/bar.py", 2),
                ("bar.py", 3),
                ("b/bar.py", 1),
                ("bar.py", 2),
                ("b/bar.py", 3),
            },
            id="removed-offset",
        ),
        pytest.param(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n ctx\n+new_a\n ctx2\ndiff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -10,2 +10,3 @@\n ctx\n+new_b\n ctx2\n",
            {
                ("b.py", 11),
                ("b/b.py", 12),
                ("b/a.py", 2),
                ("a.py", 3),
                ("b.py", 10),
                ("b/a.py", 1),
                ("b/b.py", 11),
                ("b.py", 12),
                ("a.py", 2),
                ("b/a.py", 3),
                ("b/b.py", 10),
                ("a.py", 1),
            },
            id="multiple-files",
        ),
        pytest.param(
            "+++ b/module.ts\n@@ -100,3 +200,4 @@\n existing\n+inserted\n existing2\n existing3\n",
            {
                ("module.ts", 201),
                ("b/module.ts", 200),
                ("b/module.ts", 203),
                ("module.ts", 200),
                ("module.ts", 203),
                ("b/module.ts", 202),
                ("module.ts", 202),
                ("b/module.ts", 201),
            },
            id="offset-hunk",
        ),
        pytest.param(
            "+++ b/f.py\n@@ -1,2 +1,2 @@\n line1\n-old\n+new\n\\ No newline at end of file\n",
            {("f.py", 1), ("f.py", 2), ("b/f.py", 1), ("b/f.py", 2)},
            id="newline-marker",
        ),
        pytest.param(
            "+++ b/multi.py\n@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -50,2 +51,3 @@\n d\n+e\n f\n",
            {
                ("b/multi.py", 3),
                ("multi.py", 3),
                ("b/multi.py", 51),
                ("b/multi.py", 2),
                ("multi.py", 51),
                ("multi.py", 2),
                ("b/multi.py", 53),
                ("multi.py", 53),
                ("b/multi.py", 1),
                ("b/multi.py", 52),
                ("multi.py", 1),
                ("multi.py", 52),
            },
            id="multiple-hunks",
        ),
        pytest.param(
            "diff --git a/first.py b/first.py\nindex 1111111..2222222 100644\n--- a/first.py\n+++ b/first.py\n@@ -1,2 +1,3 @@\n keep\n+added\n tail\ndiff --git a/logo.png b/logo.png\nindex 3333333..4444444 100644\nBinary files a/logo.png and b/logo.png differ\ndiff --git a/next.py b/next.py\nindex 5555555..6666666 100644\n--- a/next.py\n+++ b/next.py\n@@ -10,1 +10,2 @@\n ctx\n+added2\n",
            {
                ("next.py", 11),
                ("first.py", 3),
                ("next.py", 10),
                ("b/first.py", 2),
                ("first.py", 2),
                ("b/next.py", 11),
                ("b/first.py", 1),
                ("first.py", 1),
                ("b/next.py", 10),
                ("b/first.py", 3),
            },
            id="binary-interlude",
        ),
        pytest.param(
            "diff --git a/first.py b/first.py\n--- a/first.py\n+++ b/first.py\n@@ -1,2 +1,3 @@\n keep\n+added\n tail\ndiff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-alpha\n-beta\n-gamma\ndiff --git a/next.py b/next.py\n--- a/next.py\n+++ b/next.py\n@@ -10,1 +10,2 @@\n ctx\n+added2\n",
            {
                ("next.py", 11),
                ("first.py", 3),
                ("next.py", 10),
                ("b/first.py", 2),
                ("first.py", 2),
                ("b/next.py", 11),
                ("b/first.py", 1),
                ("first.py", 1),
                ("b/next.py", 10),
                ("b/first.py", 3),
            },
            id="deleted-body-drains",
        ),
        pytest.param(
            "--- a/first.py\n+++ b/first.py\n@@ -1,1 +1,1 @@\n keep\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +1,1 @@\n-dropped\n stray\n",
            {("b/first.py", 1), ("first.py", 1)},
            id="synthetic-null-new",
        ),
        pytest.param(
            "diff --git a/schema.sql b/schema.sql\n--- a/schema.sql\n+++ b/schema.sql\n@@ -1,3 +1,2 @@\n CREATE TABLE t (\n--- deprecated: drop me\n );\n",
            {
                ("schema.sql", 1),
                ("schema.sql", 2),
                ("b/schema.sql", 1),
                ("b/schema.sql", 2),
            },
            id="minus-header-body",
        ),
        pytest.param(
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1,4 +1,3 @@\n head\n-alpha\x0cbeta\n+gamma\n middle\n-omega\n",
            {
                ("f.py", 2),
                ("b/f.py", 2),
                ("b/f.py", 1),
                ("f.py", 1),
                ("b/f.py", 3),
                ("f.py", 3),
            },
            id="formfeed-content",
        ),
        pytest.param(
            "diff --git a/src/app.py b/src/app.py\nindex 1111111..2222222 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -10,2 +10,3 @@ def handler():\n     ctx\n+    added\n     tail\n",
            {
                ("b/src/app.py", 11),
                ("b/src/app.py", 10),
                ("src/app.py", 11),
                ("src/app.py", 10),
                ("b/src/app.py", 12),
                ("src/app.py", 12),
            },
            id="git-both-spellings",
        ),
        pytest.param(
            "diff --git a/src/app.py b/src/app.py\nindex 1111111..2222222 100644\n--- src/app.py\n+++ src/app.py\n@@ -10,2 +10,3 @@ def handler():\n     ctx\n+    added\n     tail\n",
            {("src/app.py", 10), ("src/app.py", 11), ("src/app.py", 12)},
            id="plain-verbatim-plus-alias",
        ),
        pytest.param(
            '--- a/My Docs/read me.md\t\n+++ b/My Docs/read me.md\t\n@@ -1,1 +1,2 @@\n intro\n+added\n--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -5,1 +5,2 @@\n ctx\n+brewed\n',
            {
                ("café.py", 5),
                ("My Docs/read me.md", 2),
                ("b/My Docs/read me.md", 1),
                ("b/café.py", 5),
                ("My Docs/read me.md", 1),
                ("café.py", 6),
                ("b/My Docs/read me.md", 2),
                ("b/café.py", 6),
            },
            id="decoded-paths",
        ),
        pytest.param(
            "--- a/f.py\n+++ b/f.py\n@@ -1,4 +1,4 @@\n ctx\n+added\n",
            {("f.py", 1), ("f.py", 2), ("b/f.py", 1), ("b/f.py", 2)},
            id="truncated-hunk",
        ),
    ],
)
def test_verify_membership(
    diff_text: str | None, expected: set[LineKey] | None
) -> None:
    facts = parse_diff(diff_text, policy="verify-both-spellings")
    if expected is None:
        assert facts is None
    else:
        assert facts is not None
        assert set(facts.valid_lines) == expected
        assert set(facts.line_texts) == expected
        assert facts.new_files == frozenset()
        assert facts.old_paths == {}


@pytest.mark.parametrize(
    "policy, left, right, path, query, answers",
    [
        pytest.param(
            "glab-verbatim",
            "--- b/inner.py\n+++ b/inner.py\n",
            "diff --git a/b/inner.py b/b/inner.py\n--- a/b/inner.py\n+++ b/b/inner.py\n",
            "b/inner.py",
            "inner.py",
            (True, False),
            id="glab-canonical-collision",
        ),
        pytest.param(
            "git-prefixed",
            "--- a/x\n+++ x\n",
            "--- a/x\n+++ b/x\n",
            "x",
            "b/b/x",
            (False, True),
            id="git-canonical-collision",
        ),
    ],
)
def test_verify_policy_counterexamples(
    policy: PostingPathPolicy,
    left: str,
    right: str,
    path: str,
    query: str,
    answers: tuple[bool, bool],
) -> None:
    body = "@@ -1 +1 @@\n ctx\n"
    expected = DiffFacts({(path, 1): 1}, frozenset(), {path: path}, {(path, 1): "ctx"})
    assert parse_diff(left + body, policy=policy) == expected
    assert parse_diff(right + body, policy=policy) == expected
    for headers, answer in zip((left, right), answers, strict=True):
        old = decide.parse_diff_lines(headers + body)
        assert decide.is_line_in_diff(old, query, 1) is answer
        facts = parse_diff(headers + body, policy="verify-both-spellings")
        assert is_line_valid(facts, query, 1) is answer


@pytest.mark.parametrize(
    "new_header, query, expected_keys, accepted",
    [
        pytest.param(
            "b/x", "a/b/x", {("b/x", 1), ("x", 1)}, True, id="raw-alias-query"
        ),
        pytest.param(
            "b/b/x",
            "x",
            {("b/b/x", 1), ("b/x", 1)},
            False,
            id="no-recursive-header-strip",
        ),
        pytest.param("x", "b/b/x", {("x", 1)}, False, id="no-recursive-query-strip"),
        pytest.param(
            "a/real/x",
            "real/x",
            {("a/real/x", 1), ("real/x", 1)},
            True,
            id="real-a-directory",
        ),
    ],
)
def test_verify_aliases(
    new_header: str, query: str, expected_keys: set[LineKey], accepted: bool
) -> None:
    diff_text = f"+++ {new_header}\n@@ -1 +1 @@\n ctx\n"
    old = decide.parse_diff_lines(diff_text)
    assert old == expected_keys
    assert decide.is_line_in_diff(old, query, 1) is accepted
    facts = parse_diff(diff_text, policy="verify-both-spellings")
    assert facts is not None
    assert set(facts.valid_lines) == expected_keys
    assert is_line_valid(facts, query, 1) is accepted
