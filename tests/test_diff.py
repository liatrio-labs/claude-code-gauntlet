"""Unified diff event and facts boundary proofs."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

import pytest
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
    raw_hunks,
    span_texts,
    valid_lines_for_file,
    walk_diff,
)

from tests.conftest import Invocation
from tests.support.diff import diff_facts


def _fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "glab_diff" / name).read_text(
        encoding="utf-8"
    )


def _patch(old: str, new: str, body: str, *, hunk: str = "@@ -1 +1 @@") -> str:
    return f"--- {old}\n+++ {new}\n{hunk}\n{body}"


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", [], id="empty"),
        pytest.param(
            (
                "diff --git a/logo.png b/logo.png\n"
                "old mode 100644\n"
                "new mode 100755\n"
                "index 1111111..2222222\n"
                "similarity index 94%\n"
                "Binary files a/logo.png and b/logo.png differ\n"
            ),
            [HeaderEvent("git_header", "a/logo.png b/logo.png")],
            id="git-and-noise",
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
            (
                "--- a/schema.sql\n"
                "+++ b/schema.sql\n"
                "@@ -1,3 +1,2 @@\n"
                " CREATE TABLE t (\n"
                "--- deprecated: drop me\n"
                " );\n"
            ),
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
            (
                "--- a/notes.md\n"
                "+++ b/notes.md\n"
                "@@ -1,2 +1,3 @@\n"
                " intro\n"
                "+++ x marks a diff-of-a-diff\n"
                " outro\n"
            ),
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
            (
                "diff --git a/gone.py b/gone.py\n"
                "deleted file mode 100644\n"
                "--- a/gone.py\n"
                "+++ /dev/null\n"
                "@@ -1,3 +0,0 @@\n"
                "-alpha\n"
                "-beta\n"
                "-gamma\n"
                "diff --git a/next.py b/next.py\n"
                "--- a/next.py\n"
                "+++ b/next.py\n"
                "@@ -10,1 +10,2 @@\n"
                " ctx\n"
                "+added\n"
            ),
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
            "@@ -1 +1 @@\n  indented\n",
            [HunkEvent(1, 1, 1, 1), LineEvent(1, 1, " indented")],
            id="marker-indent",
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
            '+++ "b/\\377.py"\n',
            [HeaderEvent("new_path", '"b/\\377.py"')],
            id="invalid-utf8-octal-path",
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
    ],
)
def test_events(diff_text: str, expected: list[DiffEvent]) -> None:
    assert list(walk_diff(diff_text)) == expected


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", [], id="empty"),
        pytest.param(
            "+++ b/f\n@@ -1 +1 @@\n-x\n+y\n@@ -5 +5 @@\n-x\n+y",
            [
                ("b/f", HunkEvent(1, 1, 1, 1), "@@ -1 +1 @@\n-x\n+y\n"),
                ("b/f", HunkEvent(5, 5, 1, 1), "@@ -5 +5 @@\n-x\n+y"),
            ],
            id="unterminated-multiple-hunks",
        ),
        pytest.param(
            "+++ b/one.py\n@@ -1 +1 @@\n-old\n+new\n"
            "--- a/two.py\n+++ /dev/null\n@@ -2 +2 @@\n-old2\n+new2\n",
            [
                ("b/one.py", HunkEvent(1, 1, 1, 1), "@@ -1 +1 @@\n-old\n+new\n"),
                ("/dev/null", HunkEvent(2, 2, 1, 1), "@@ -2 +2 @@\n-old2\n+new2\n"),
            ],
            id="two-files",
        ),
        pytest.param(
            # Each lookalike ends its hunk, where dropping it would shorten the text.
            "+++ b/f.py\n@@ -1 +1,2 @@\n a\n+++ x\n"
            "@@ -5,2 +6,2 @@\n b\n@@ -9 +9 @@\n"
            "@@ -8,2 +9,2 @@\n c\ndiff --git a/x b/x\n",
            [
                ("b/f.py", HunkEvent(1, 1, 1, 2), "@@ -1 +1,2 @@\n a\n+++ x\n"),
                ("b/f.py", HunkEvent(5, 6, 2, 2), "@@ -5,2 +6,2 @@\n b\n@@ -9 +9 @@\n"),
                (
                    "b/f.py",
                    HunkEvent(8, 9, 2, 2),
                    "@@ -8,2 +9,2 @@\n c\ndiff --git a/x b/x\n",
                ),
            ],
            id="body-lines-shaped-like-headers",
        ),
        pytest.param(
            "+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n-- \n",
            [("b/f.py", HunkEvent(1, 1, 1, 1), "@@ -1 +1 @@\n-old\n+new\n")],
            id="header-zone-format-patch-trailer",
        ),
        pytest.param(
            "+++ b/f.py\n@@ -1,2 +1,2 @@\n+only\n",
            [("b/f.py", HunkEvent(1, 1, 2, 2), "@@ -1,2 +1,2 @@\n+only\n")],
            id="truncated-final-hunk",
        ),
        pytest.param(
            "+++  b/f.py \t\n@@ -1 +1 @@\n-old\r\n+new\r\n",
            [(" b/f.py ", HunkEvent(1, 1, 1, 1), "@@ -1 +1 @@\n-old\r\n+new\r\n")],
            id="lf-header-crlf-body",
        ),
        pytest.param(
            "+++ b/f.py\n@@ -1 +1 @@\n+last",
            [("b/f.py", HunkEvent(1, 1, 1, 1), "@@ -1 +1 @@\n+last")],
            id="unterminated-final-body-line",
        ),
        pytest.param(
            "+++ b/f.py\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n"
            "+new\n\\ No newline at end of file\n@@ -3 +3 @@\n+x\n",
            [
                (
                    "b/f.py",
                    HunkEvent(1, 1, 1, 1),
                    "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n"
                    "+new\n\\ No newline at end of file\n",
                ),
                ("b/f.py", HunkEvent(3, 3, 1, 1), "@@ -3 +3 @@\n+x\n"),
            ],
            id="markers-on-both-sides-before-next-hunk",
        ),
    ],
)
def test_raw_hunks(
    diff_text: str, expected: list[tuple[str | None, HunkEvent, str]]
) -> None:
    assert [
        (raw_hunk.new_path, raw_hunk.hunk, raw_hunk.text)
        for raw_hunk in raw_hunks(diff_text)
    ] == expected


@pytest.mark.parametrize(
    "diff_text, policies, expected, new_files, old_paths",
    [
        pytest.param(
            _fixture("modified.diff"),
            ("git-prefixed", "glab-verbatim"),
            {
                "src/edited.py": {
                    61: (50, "unchanged_ctx"),
                    62: (None, "added"),
                    63: (52, "tail_ctx"),
                }
            },
            set(),
            {"src/edited.py": "src/edited.py"},
            id="plain-modified",
        ),
        pytest.param(
            _fixture("added.diff"),
            ("git-prefixed", "glab-verbatim"),
            {
                "src/app/clients/api/__init__.py": {
                    1: (None, "added_01"),
                    2: (None, "added_02"),
                    3: (None, ""),
                    4: (None, "added_04"),
                    5: (None, "added_05"),
                    6: (None, "added_06"),
                    7: (None, "added_07"),
                    8: (None, "added_08"),
                    9: (None, "added_09"),
                    10: (None, "added_10"),
                    11: (None, "added_11"),
                    12: (None, "added_12"),
                    13: (None, "added_13"),
                    14: (None, "added_14"),
                    15: (None, "added_15"),
                    16: (None, "added_16"),
                }
            },
            {"src/app/clients/api/__init__.py"},
            {"src/app/clients/api/__init__.py": "src/app/clients/api/__init__.py"},
            id="plain-added",
        ),
        pytest.param(
            _fixture("rename.diff"),
            ("git-prefixed", "glab-verbatim"),
            {
                "new_name.py": {
                    3: (3, "ctx"),
                    4: (None, "y"),
                    5: (5, "ctx2"),
                    6: (6, ""),
                }
            },
            set(),
            {"new_name.py": "old_name.py"},
            id="plain-rename",
        ),
        pytest.param(
            _fixture("git_style.diff"),
            ("git-prefixed", "glab-verbatim"),
            {
                "b/inner.py": {
                    2: (2, "line_02"),
                    3: (3, "line_03"),
                    4: (4, "line_04"),
                    5: (None, "changed_05"),
                    6: (6, "line_06"),
                    7: (7, "line_07"),
                    8: (8, "line_08"),
                },
                "src/edited.py": {
                    2: (2, "line_02"),
                    3: (3, "line_03"),
                    4: (4, "line_04"),
                    5: (None, "changed_05"),
                    6: (6, "line_06"),
                    7: (7, "line_07"),
                    8: (8, "line_08"),
                },
                "src/added.py": {1: (None, "added_01"), 2: (None, "added_02")},
                "new_name.py": {
                    1: (1, "ctx"),
                    2: (None, "y"),
                    3: (3, "ctx2"),
                    4: (4, ""),
                },
            },
            {"src/added.py"},
            {
                "b/inner.py": "b/inner.py",
                "src/edited.py": "src/edited.py",
                "new_name.py": "old_name.py",
            },
            id="captured-git",
        ),
        pytest.param(
            _fixture("git_style_spaces.diff"),
            ("git-prefixed", "glab-verbatim"),
            {
                "docs/user guide/x.md": {1: (1, "ctx"), 2: (None, "new")},
                "café.py": {1: (None, "a")},
                "my file.py": {1: (None, "added_01")},
                "new name.py": {1: (1, "ctx"), 2: (None, "y")},
            },
            {"café.py", "my file.py"},
            {
                "docs/user guide/x.md": "docs/user guide/x.md",
                "new name.py": "old name.py",
            },
            id="captured-git-spaces",
        ),
        pytest.param(
            _patch("a/real/x.py", "a/real/x.py", "-o\n+n\n", hunk="@@ -1 +1 @@")
            + _patch(
                "b/real/y.py", "b/real/y.py", "-old\n+fresh\n", hunk="@@ -1 +1 @@"
            ),
            ("glab-verbatim",),
            {"a/real/x.py": {1: (None, "n")}, "b/real/y.py": {1: (None, "fresh")}},
            set(),
            {"a/real/x.py": "a/real/x.py", "b/real/y.py": "b/real/y.py"},
            id="plain-real-a-b",
        ),
        pytest.param(
            _patch("b/old.py", "a/new.py", "-o\n+n\n", hunk="@@ -1 +1 @@"),
            ("git-prefixed",),
            {"a/new.py": {1: (None, "n")}},
            set(),
            {"a/new.py": "b/old.py"},
            id="side-specific",
        ),
        pytest.param(
            "diff --git a/x b/y b/z\n"
            + _patch("a/x b/y", "b/z", " ctx\n+newline\n", hunk="@@ -1,1 +1,2 @@"),
            ("glab-verbatim",),
            {"z": {1: (1, "ctx"), 2: (None, "newline")}},
            set(),
            {"z": "x b/y"},
            id="pair-proof:space-lookalike",
        ),
        pytest.param(
            "diff --git a/bar.py b/bar.py\n"
            + _patch(
                "a/other.py", "b/bar.py", " ctx\n+newline\n", hunk="@@ -1,1 +1,2 @@"
            ),
            ("glab-verbatim",),
            {"b/bar.py": {1: (1, "ctx"), 2: (None, "newline")}},
            set(),
            {"b/bar.py": "a/other.py"},
            id="pair-proof:old-mismatch",
        ),
        pytest.param(
            'diff --git "a/old path.py" "b/new path.py"\n'
            + _patch(
                '"a/old path.py"',
                '"b/new path.py"',
                " ctx\n+newline\n",
                hunk="@@ -1,1 +1,2 @@",
            ),
            ("glab-verbatim",),
            {"b/new path.py": {1: (1, "ctx"), 2: (None, "newline")}},
            set(),
            {"b/new path.py": "a/old path.py"},
            id="pair-proof:quoted",
        ),
        pytest.param(
            "diff --git a/x.py b/x.py\n"
            + _patch("a/x.py", "b/x.py", "-o\n+n\n", hunk="@@ -1 +1 @@")
            + _patch("a/x.py", "b/x.py", "-o\n+n\n", hunk="@@ -1 +1 @@"),
            ("glab-verbatim",),
            {"x.py": {1: (None, "n")}, "b/x.py": {1: (None, "n")}},
            set(),
            {"x.py": "x.py", "b/x.py": "a/x.py"},
            id="one-pair-proof:plain-after-proven",
        ),
        pytest.param(
            "diff --git a/x.py b/x.py\n"
            + _patch("a/y.py", "b/y.py", "-o\n+n\n", hunk="@@ -1 +1 @@")
            + _patch("a/x.py", "b/x.py", "-o\n+n\n", hunk="@@ -1 +1 @@"),
            ("glab-verbatim",),
            {"b/y.py": {1: (None, "n")}, "b/x.py": {1: (None, "n")}},
            set(),
            {"b/y.py": "a/y.py", "b/x.py": "a/x.py"},
            id="one-pair-proof:failed-proof-consumed",
        ),
        pytest.param(
            "diff --git a/empty.py b/empty.py\n"
            + _patch("a/empty.py", "b/empty.py", "+first\n", hunk="@@ -0,0 +1 @@"),
            ("glab-verbatim",),
            {"empty.py": {1: (None, "first")}},
            set(),
            {"empty.py": "empty.py"},
            id="empty-old:proven",
        ),
        pytest.param(
            _patch("x", "x", "+first\n+second\n", hunk="@@ -5,0 +6,2 @@"),
            ("git-prefixed", "glab-verbatim"),
            {"x": {6: (None, "first"), 7: (None, "second")}},
            set(),
            {"x": "x"},
            id="insertion-at-nonzero-old",
        ),
        pytest.param(
            "diff --git a/x.py b/x.py\n"
            + _patch("a/x.py", "b/x.py", "-o\n+n\n", hunk="@@ -1 +1 @@")
            + _patch("empty.py", "empty.py", "+first\n", hunk="@@ -0,0 +1 @@"),
            ("glab-verbatim",),
            {"x.py": {1: (None, "n")}, "empty.py": {1: (None, "first")}},
            {"empty.py"},
            {"x.py": "x.py", "empty.py": "empty.py"},
            id="empty-old:plain-after-proven",
        ),
        pytest.param(
            _patch(
                "a/empty.py", "b/empty.py", "+first\n+second\n", hunk="@@ -0,0 +1,2 @@"
            ),
            ("git-prefixed",),
            {"empty.py": {1: (None, "first"), 2: (None, "second")}},
            {"empty.py"},
            {"empty.py": "empty.py"},
            id="empty-old:git-prefixed-named",
        ),
        pytest.param(
            _patch(
                "a/gone.py", "/dev/null", "-line1\n-line2\n", hunk="@@ -1,2 +0,0 @@"
            ),
            ("git-prefixed", "glab-verbatim"),
            {},
            set(),
            {},
            id="null-new",
        ),
        pytest.param(
            _patch(
                '"a/caf\\303\\251.py"',
                '"b/caf\\303\\251.py"',
                "-old\n+new\n",
                hunk="@@ -1 +1 @@",
            ),
            ("git-prefixed",),
            {"café.py": {1: (None, "new")}},
            set(),
            {"café.py": "café.py"},
            id="decoded-git-utf8",
        ),
        pytest.param(
            (
                'diff --git "a/caf\\303\\251 old.py" b/new.py\n'
                "similarity index 100%\n"
                'rename from "caf\\303\\251 old.py"\n'
                "rename to new.py\n"
            )
            + _patch(
                '"a/caf\\303\\251 old.py"', "b/new.py", " ctx\n", hunk="@@ -1 +1 @@"
            ),
            ("git-prefixed",),
            {"new.py": {1: (1, "ctx")}},
            set(),
            {"new.py": "café old.py"},
            id="decoded-rename",
        ),
    ],
)
def test_path_facts(
    diff_text: str,
    policies: tuple[DiffPathPolicy, ...],
    expected: Mapping[str, Mapping[int, tuple[int | None, str]]],
    new_files: set[str],
    old_paths: Mapping[str, str],
) -> None:
    for policy in policies:
        facts = parse_diff(diff_text, policy=policy)
        actual: dict[str, dict[int, tuple[int | None, str]]] = {}
        assert facts.valid_lines.keys() == facts.line_texts.keys()
        for (path, line), old in facts.valid_lines.items():
            actual.setdefault(path, {})[line] = (old, facts.line_texts[(path, line)])
        assert actual == expected
        assert facts.new_files == new_files
        assert facts.old_paths == old_paths


@pytest.mark.parametrize(
    "facts, path, line, expected",
    [
        pytest.param(None, "any.py", 999, (True, "any.py", None), id="skipped-valid"),
        pytest.param(
            diff_facts({("src/app.py", 42): 30}),
            "src/app.py",
            42,
            (True, "src/app.py", 30),
            id="exact-valid",
        ),
        pytest.param(
            diff_facts({("src/app.py", 42): 30}),
            "src/app.py",
            43,
            (False, "src/app.py", None),
            id="missing-valid",
        ),
        pytest.param(
            diff_facts({("src/app.py", 10): 4}),
            "a/src/app.py",
            10,
            (True, "src/app.py", 4),
            id="prefixed-valid-a",
        ),
        pytest.param(
            diff_facts({("src/app.py", 7): None}),
            "src/app.py",
            7,
            (True, "src/app.py", None),
            id="null-value",
        ),
        pytest.param(
            diff_facts({("b/x", 1): None, ("x", 1): 9}),
            "b/x",
            1,
            (True, "b/x", None),
            id="exact-null-before-stripped",
        ),
        pytest.param(
            diff_facts({("x", 1): 1}),
            "b/x",
            None,
            (False, "b/x", None),
            id="no-query-line",
        ),
    ],
)
def test_path_lookup(
    facts: DiffFacts | None,
    path: str,
    line: int | None,
    expected: tuple[bool, str, int | None],
) -> None:
    assert (
        is_line_valid(facts, path, line),
        diff_path_spelling(facts, path, line),
        old_line_for(facts, path, line),
    ) == expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        pytest.param(None, "foo.py", None, id="skipped-diagnostic"),
        pytest.param(
            diff_facts({("f.py", i): i for i in range(1, 21)}),
            "f.py",
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            id="ten-line-limit",
        ),
        pytest.param(
            diff_facts({("other.py", 1): None}), "missing.py", [], id="empty-diagnostic"
        ),
        pytest.param(
            diff_facts({("b/f", 3): None, ("b/f", 1): None, ("f", 1): 1, ("f", 2): 2}),
            "b/f",
            [1, 2, 3],
            id="sorted-deduplicated-union",
        ),
    ],
)
def test_path_diagnostics(
    facts: DiffFacts | None, path: str, expected: list[int] | None
) -> None:
    assert valid_lines_for_file(facts, path) == expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        pytest.param(None, "any.py", False, id="skipped-new-file"),
        pytest.param(
            diff_facts({}, new_files={"src/added.py"}),
            "src/added.py",
            True,
            id="exact-new-file",
        ),
        pytest.param(
            diff_facts({}, new_files={"src/added.py"}),
            "a/src/added.py",
            False,
            id="unresolved-prefix-a",
        ),
    ],
)
def test_path_new_file(facts: DiffFacts | None, path: str, expected: bool) -> None:
    assert is_new_file(facts, path) is expected


@pytest.mark.parametrize(
    "facts, path, start, end, expected",
    [
        pytest.param(None, "x", 1, 3, True, id="skipped-range"),
        pytest.param(
            diff_facts({("b/x", 1): 1, ("x", 2): None}),
            "b/x",
            1,
            2,
            True,
            id="per-line-resolution",
        ),
        pytest.param(
            diff_facts({("x", 1): 1, ("x", 3): None}),
            "x",
            1,
            3,
            False,
            id="all-lines-required",
        ),
    ],
)
def test_path_range(
    facts: DiffFacts | None, path: str, start: int, end: int, expected: bool
) -> None:
    assert range_is_valid(facts, path, start, end) is expected


@pytest.mark.parametrize(
    "facts, path, start, end, expected",
    [
        pytest.param(None, "x", 1, 1, None, id="skipped-span"),
        pytest.param(
            diff_facts({}, line_texts={("x", 1): "a", ("x", 2): ""}),
            "x",
            1,
            2,
            ["a", ""],
            id="exact-span",
        ),
        pytest.param(
            diff_facts({}, line_texts={("b/x", 1): "a", ("x", 2): "b"}),
            "b/x",
            1,
            2,
            None,
            id="no-union-span",
        ),
    ],
)
def test_path_span(
    facts: DiffFacts | None, path: str, start: int, end: int, expected: list[str] | None
) -> None:
    assert span_texts(facts, path, start, end) == expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        pytest.param(None, "b/x", False, id="skipped-ambiguity"),
        pytest.param(
            diff_facts({("src/foo.py", 9): 9, ("b/src/foo.py", 10): 10}),
            "src/foo.py",
            False,
            id="unprefixed-sibling",
        ),
        pytest.param(
            diff_facts({("b/x", 1): 1, ("x", 9): None}),
            "b/x",
            True,
            id="addressable-collision",
        ),
        pytest.param(
            diff_facts({("x", 1): 1}, old_paths={"b/x": "b/x"}, new_files={"b/x"}),
            "b/x",
            False,
            id="header-only-sibling",
        ),
    ],
)
def test_path_ambiguity(facts: DiffFacts | None, path: str, expected: bool) -> None:
    assert path_is_ambiguous(facts, path) is expected


@pytest.mark.parametrize(
    "facts, path, expected",
    [
        (None, "b/x", "b/x"),
        (diff_facts({}, old_paths={"x": "old"}), "x", "old"),
        (diff_facts({}, old_paths={"x": "old"}), "b/x", "b/x"),
    ],
)
def test_path_old_path(facts: DiffFacts | None, path: str, expected: str) -> None:
    assert old_path_for(facts, path) == expected


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", DiffCounts(0, 0, 0), id="empty"),
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
            (
                " Binary files a/x and b/x differ\n"
                "Binary files a/x and b/x differ!\n"
                "+Binary files a/x and b/x differ\n"
            ),
            DiffCounts(0, 0, 0),
            id="binary-exact-prose",
        ),
        pytest.param(
            "".join(
                _fixture(name)
                for name in (
                    "added.diff",
                    "deleted.diff",
                    "git_style.diff",
                    "git_style_spaces.diff",
                    "modified.diff",
                    "rename.diff",
                )
            ),
            DiffCounts(27, 11, 0),
            id="captured-glab-corpus",
        ),
    ],
)
def test_counts(diff_text: str, expected: DiffCounts) -> None:
    assert diff_counts(diff_text) == expected


def test_numstat_crlf_binary(tmp_path: Path, invoke: Callable[..., Invocation]) -> None:
    patch = (
        b"diff --git a/a.bin b/a.bin\r\nnew file mode 100644\r\n"
        b"Binary files /dev/null and b/a.bin differ\r\n"
        b"diff --git a/t.txt b/t.txt\r\n--- a/t.txt\r\n+++ b/t.txt\r\n"
        b"@@ -1 +1 @@\r\n-old\r\n+new\r\n"
    )
    patch_path = tmp_path / "crlf.patch"
    patch_path.write_bytes(patch)
    result = invoke("diff_numstat", [str(patch_path)], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"changed_lines=2\nbinary_files=1\n"


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("diff --git i/x w/x\n", "glab-verbatim", id="mnemonic"),
        pytest.param("diff --git a/x b/x\n", "git-prefixed", id="git-header"),
    ],
)
def test_report_policy(diff_text: str, expected: PostingPathPolicy) -> None:
    assert patch_report_policy(diff_text) == expected


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
    ],
)
def test_path_aliases(
    new_header: str, query: str, expected_keys: set[LineKey], accepted: bool
) -> None:
    diff_text = f"+++ {new_header}\n@@ -1 +1 @@\n ctx\n"
    facts = parse_diff(diff_text, policy="verify-both-spellings")
    assert set(facts.valid_lines) == expected_keys
    assert is_line_valid(facts, query, 1) is accepted


@pytest.mark.parametrize(
    "diff_text, expected",
    [
        pytest.param("", {}, id="empty"),
        pytest.param(
            "diff --git a/first.py b/first.py\nindex 1111111..2222222 100644\n"
            + _patch(
                "a/first.py",
                "b/first.py",
                " keep\n+added\n tail\n",
                hunk="@@ -1,2 +1,3 @@",
            )
            + (
                "diff --git a/logo.png b/logo.png\n"
                "index 3333333..4444444 100644\n"
                "Binary files a/logo.png and b/logo.png differ\n"
                "diff --git a/next.py b/next.py\n"
                "index 5555555..6666666 100644\n"
            )
            + _patch(
                "a/next.py", "b/next.py", " ctx\n+added2\n", hunk="@@ -10,1 +10,2 @@"
            ),
            {
                "b/first.py": (1, 2, 3),
                "b/next.py": (10, 11),
                "first.py": (1, 2, 3),
                "next.py": (10, 11),
            },
            id="binary-interlude",
        ),
        pytest.param(
            _patch("a/first.py", "b/first.py", " keep\n", hunk="@@ -1,1 +1,1 @@")
            + _patch(
                "a/gone.py", "/dev/null", "-dropped\n stray\n", hunk="@@ -1,2 +1,1 @@"
            ),
            {"b/first.py": (1,), "first.py": (1,)},
            id="synthetic-null-new",
        ),
        pytest.param(
            "diff --git a/src/app.py b/src/app.py\nindex 1111111..2222222 100644\n"
            + _patch(
                "src/app.py",
                "src/app.py",
                "     ctx\n+    added\n     tail\n",
                hunk="@@ -10,2 +10,3 @@ def handler():",
            ),
            {"src/app.py": (10, 11, 12)},
            id="plain-verbatim-plus-alias",
        ),
        pytest.param(
            _patch(
                "a/My Docs/read me.md\t",
                "b/My Docs/read me.md\t",
                " intro\n+added\n",
                hunk="@@ -1,1 +1,2 @@",
            )
            + _patch(
                '"a/caf\\303\\251.py"',
                '"b/caf\\303\\251.py"',
                " ctx\n+brewed\n",
                hunk="@@ -5,1 +5,2 @@",
            ),
            {
                "My Docs/read me.md": (1, 2),
                "b/My Docs/read me.md": (1, 2),
                "b/café.py": (5, 6),
                "café.py": (5, 6),
            },
            id="decoded-paths",
        ),
        pytest.param(
            "--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+first\n",
            {"b/x": (1,), "x": (1,)},
            id="empty-old-added-file-facts",
        ),
    ],
)
def test_path_membership(
    diff_text: str, expected: Mapping[str, tuple[int, ...]]
) -> None:
    facts = parse_diff(diff_text, policy="verify-both-spellings")
    actual: dict[str, list[int]] = {}
    for path, line in sorted(facts.valid_lines):
        actual.setdefault(path, []).append(line)
    assert {path: tuple(lines) for path, lines in actual.items()} == expected
    assert facts.valid_lines.keys() == facts.line_texts.keys()
    assert facts.new_files == (
        frozenset(expected) if "--- /dev/null\n" in diff_text else frozenset()
    )
    assert facts.old_paths == {}


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


def test_fixture_final_newline() -> None:
    fixtures = sorted((Path(__file__).parent / "fixtures/glab_diff").glob("*.diff"))
    assert fixtures
    for path in fixtures:
        data = path.read_bytes()
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), path.name
