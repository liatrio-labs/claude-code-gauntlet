"""Posting tables over the command boundary."""

from dataclasses import dataclass, field

import pytest
from gauntlet.delivery import compose, fold, post
from gauntlet.forge import (
    ForgeUnavailable,
    JsonFetch,
    PostResult,
    ReviewTarget,
    parse_remote,
)
from gauntlet.marker import find_finding_markers, find_marker

from tests.support.delivery import finding
from tests.support.posting import (
    ADDED,
    ADDED_KEY,
    CONTEXT,
    CONTEXT_KEY,
    CONTRACT_DIFF,
    ERRORS,
    FOOTER,
    GL_FIXTURES,
    HEADER,
    NEW,
    SHA,
    UNKNOWN_WARNING,
    invoke_posting,
    review,
)
from tests.support.prior import prior

TARGET_ARGS = ("--owner", "o", "--repo", "r", "--pr-number", "5")

GH_ARGS = (*TARGET_ARGS, "--platform", "github", "--sha", SHA)

TRAILER = "\n\n\u2694\ufe0f *Code Gauntlet*"

SUMMARY = HEADER + "Summary" + FOOTER.replace("{count}", "1")

EMPTY_SUMMARY = HEADER + "Summary" + FOOTER.replace("{count}", "0")

BASIC_BODY = "**\U0001f7e0 [HIGH] T**\n\nb" + TRAILER

RANGE_PROSE = (
    "**\U0001f7e0 [HIGH] Range bug**\n\nBody\n\n**Suggested fix:**\nReturn "
    "two instead." + TRAILER
)

RANGE_GH = (
    "**\U0001f7e0 [HIGH] Range bug**\n\nBody\n\n**Suggested fix:**\nReturn "
    "two instead.\n\n```suggestion\n    return 2\n    # done\n```" + TRAILER
)

CONTEXT_BODY = "**\U0001f7e0 [HIGH] Context-line finding**\n\nBody one" + TRAILER

ADDED_BODY = "**\U0001f7e1 [MEDIUM] Added-line finding**\n\nBody two" + TRAILER

NEW_BODY = "**\U0001f4a1 [LOW] New-file finding**\n\nBody three" + TRAILER

NEW_KEY = "a9cb7253f6710b82"

RANGE_KEY = "1422e1e3b48521d1"

RENAME_DIFF = (GL_FIXTURES / "rename.diff").read_text(encoding="utf-8")

GH_DIFF = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 "
    "+1,6 @@\n def f():\n+    line2\n+    line3\n+    line4\n+    line5\n+ "
    "   line6\n"
)

GL_DIFF = GH_DIFF.split("--- a/foo.py", 1)[1]

GL_DIFF = "--- foo.py" + GL_DIFF.replace("+++ b/foo.py", "+++ foo.py")

GH_INDENTED = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 "
    "+1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)

GL_INDENTED = (
    "--- foo.py\n+++ foo.py\n@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)

GH_COLLISION = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 "
    "+1,3 @@\n line1\n+TOP_V1\n+TOP_V2\ndiff --git a/b/foo.py b/b/foo.py\n"
    "--- a/b/foo.py\n+++ b/b/foo.py\n@@ -1,1 +1,2 @@\n line1\n+SUB_V1\n"
)

GL_COLLISION = (
    "--- a/x.py\n+++ a/x.py\n@@ -1,1 +1,2 @@\n line1\n+SUB_V1\n--- x.py\n"
    "+++ x.py\n@@ -1,1 +1,2 @@\n line1\n+TOP_V1\n"
)

GH_RECALL = (
    "diff --git a/src/edited.py b/src/edited.py\n--- a/src/edited.py\n+++ "
    "b/src/edited.py\n@@ -1,1 +1,2 @@\n line1\n+CURRENT\n"
)

GL_RECALL = "--- src/edited.py\n+++ src/edited.py\n@@ -1,1 +1,2 @@\n line1\n+CURRENT\n"

GL_REAL_A = (
    "diff --git a/a/foo.py b/a/foo.py\n--- a/foo.py\n+++ a/foo.py\n@@ -1,2 "
    "+1,2 @@\n ctx\n-x\n+y\n"
)

DIFF_WARNING = (
    "WARNING: Could not fetch diff (exit 128): denied. Skipping line "
    "validation \u2014 all findings will be posted.\n"
)


def off_diff(title, *, line=999, path="foo.py", valid=range(1, 7), members=""):
    message = (
        f"Skipping finding '{title}' at {path}:{line} — line not found in diff. "
        f"Valid lines for this file: {list(valid)}"
    )
    return message + (f" [group members: {members}]" if members else "")


def marker(key, sha=SHA):
    return (
        '\n\n<!-- code-gauntlet-finding-key: {"sha":"'
        + sha
        + '","key":"'
        + key
        + '"} -->'
    )


def fix(**values):
    return finding(
        **{
            "title": "Range bug",
            "body": "Body",
            "suggestion": "Return two instead.",
            "suggested_fix_code": "    return 2\n    # done",
            **values,
        }
    )


def grouped(member, *, primary=False, key="k1", **values):
    return {
        **member,
        "consolidation_key": key,
        "consolidation_primary": primary,
        **values,
    }


def gh_anchor(*, path="foo.py", line=2, start=None):
    anchor = {"path": path, "line": line, "side": "RIGHT"}
    if start is not None:
        anchor.update(start_line=start, start_side="RIGHT")
    return anchor


def position(path="foo.py", line=2, **values):
    return {
        "position_type": "text",
        "base_sha": "base1",
        "head_sha": "head1",
        "start_sha": "start1",
        "new_path": path,
        "new_line": line,
        **values,
    }


@dataclass(frozen=True, slots=True)
class Expected:
    anchors: tuple = ()
    bodies: tuple | None = None
    summary: str | None = None
    summary_has: tuple[str, ...] = ()
    summary_lacks: tuple[str, ...] = ()
    body_has: tuple[tuple[str, ...], ...] = ()
    body_lacks: tuple[tuple[str, ...], ...] = ()
    skipped: tuple[str, ...] = ()
    before_skips: str = ""
    err: str = ""
    counts: tuple[str, ...] = ()

    surfaces: tuple[str, ...] | None = None

    head_calls: int = 0
    code: int = 0
    out_lines: tuple[str, ...] = ()
    summary_ordered: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Row:
    id: str
    data: object
    expected: Expected
    options: dict = field(default_factory=dict)
    limits: tuple[tuple[str, str, int], ...] = ()
    composed: str | None = None


@pytest.fixture
def posting(tmp_path, monkeypatch, capsys, forge_factory):
    def run(data, **options):
        return invoke_posting(
            tmp_path, monkeypatch, capsys, forge_factory, data, **options
        )

    return run


def check(row, posting, monkeypatch):
    for platform, surface, size in row.limits:
        monkeypatch.setitem(
            fold.PLATFORM_BODY_LIMITS[platform]["surfaces"][surface], "bytes", size
        )
    if row.composed is not None:
        monkeypatch.setattr(
            compose,
            "compose_review_body",
            lambda *a, **k: compose.ComposedBody(row.composed, 0, 0, 0, ()),
        )
    run = posting(row.data, **row.options)
    expected = row.expected
    assert run.code == expected.code
    assert run.err == (
        expected.before_skips
        + "".join("WARNING: " + msg + "\n" for msg in expected.skipped)
        + expected.err
    )
    assert len(run.head_calls) == expected.head_calls
    assert all(
        call.target is None or call.target == ReviewTarget("o", "r", 5)
        for call in run.fake.calls
    )
    assert all(request.method == "POST" for request in run.requests)
    assert all(
        request.endpoint
        in (
            "projects/o%2Fr/merge_requests/5/notes",
            "projects/o%2Fr/merge_requests/5/discussions",
        )
        if request.platform == "gitlab"
        else request.endpoint == "repos/o/r/pulls/5/reviews"
        for request in run.requests
    )
    if run.payload is not None:
        assert run.requests == ()
        assert run.payload["skipped"] == list(expected.skipped)
        if run.payload["platform"] == "github":
            assert (run.payload["endpoint"], run.payload["method"]) == (
                "repos/o/r/pulls/5/reviews",
                "POST",
            )
            assert run.payload["payload"]["event"] == "COMMENT"
            comments = run.payload["payload"]["comments"]
            summary = run.payload["payload"]["body"]
        else:
            comments = run.payload["discussions"]
            summary = run.payload["summary"].get("body", "")

    else:
        if row.data.get("platform") == "gitlab":
            comments = [
                dict(request.payload)
                for request in run.requests
                if request.endpoint.endswith("/discussions")
                or find_finding_markers(request.payload["body"])
            ]
            notes = [
                request.payload
                for request in run.requests
                if request.endpoint.endswith("/notes")
                and not find_finding_markers(request.payload["body"])
            ]
            summary = notes[0]["body"] if notes else ""
        else:
            comments = run.requests[0].payload["comments"] if run.requests else []
            summary = run.requests[0].payload["body"] if run.requests else ""
    anchors = tuple(
        comment.get(
            "position", {key: value for key, value in comment.items() if key != "body"}
        )
        for comment in comments
    )
    assert anchors == expected.anchors
    if expected.bodies is not None:
        assert tuple(comment["body"] for comment in comments) == expected.bodies
    if expected.summary is not None:
        assert summary == expected.summary
    summary_position = 0
    for value in expected.summary_ordered:
        summary_position = summary.index(value, summary_position) + len(value)
    for value in expected.summary_has:
        assert value in summary
    for value in expected.summary_lacks:
        assert value not in summary
    for index, fragments in enumerate(expected.body_has):
        comment = comments[index]
        for value in fragments:
            assert value in comment["body"]
    for index, fragments in enumerate(expected.body_lacks):
        comment = comments[index]
        for value in fragments:
            assert value not in comment["body"]
    assert (
        tuple(line for line in run.out.splitlines() if "suggested fix(es)" in line)
        == expected.counts
    )
    if expected.out_lines:
        assert (
            tuple(line for line in run.out.splitlines() if line in expected.out_lines)
            == expected.out_lines
        )

    if expected.surfaces is not None:
        assert (
            tuple(request.endpoint.rsplit("/", 1)[-1] for request in run.requests)
            == expected.surfaces
        )
        assert all(request.method == "POST" for request in run.requests)

    return run


GH_ANCHOR = gh_anchor()

GH_RANGE = gh_anchor(line=3, start=2)

GL_ANCHOR = position(old_path="foo.py")


def fix_counts(kept, downgraded):
    return (
        f"  {kept} suggested fix(es) passed the apply-check.",
        f"  {downgraded} suggested fix(es) downgraded to prose.",
    )


KEPT = fix_counts(1, 0)

DOWN = fix_counts(0, 1)

BOTH = fix_counts(1, 1)


def params(rows):
    return [pytest.param(row, id=row.id) for row in rows]


SITE = [
    Row(
        "SITE-rename-old-path",
        review("gitlab", [finding(file="new_name.py", line=3, omit=("end_line",))]),
        Expected(
            anchors=(position("new_name.py", 3, old_line=3, old_path="old_name.py"),),
            bodies=(BASIC_BODY,),
        ),
        {"diff": RENAME_DIFF},
    ),
]


@pytest.mark.parametrize("row", params(SITE))
def test_position_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)


ANCHOR = [
    Row(
        "ANCHOR-gl-unique-prefixed-recall",
        review(
            "gitlab",
            [
                fix(
                    file="b/src/edited.py",
                    line=2,
                    end_line=2,
                    suggested_fix_code="CHANGED",
                )
            ],
        ),
        Expected(
            anchors=(position("src/edited.py", 2, old_path="src/edited.py"),),
            bodies=(
                RANGE_PROSE.removesuffix(TRAILER)
                + "\n\n```suggestion\nCHANGED\n```"
                + TRAILER
                + marker("20aad88a122950a0"),
            ),
            counts=KEPT,
        ),
        {"diff": GL_RECALL, "dry_run": False},
    ),
    Row(
        "ANCHOR-gl-ambiguous-raw-spelling",
        review(
            "gitlab",
            [fix(file="a/x.py", line=2, end_line=2, suggested_fix_code="CHANGED")],
        ),
        Expected(
            anchors=(position("a/x.py", 2, old_path="a/x.py"),),
            bodies=(RANGE_PROSE + marker("e9a9d3f40fddd4fe"),),
            skipped=("suggested-fix downgraded: a/x.py:2 (no_diff_oracle)",),
            counts=DOWN,
        ),
        {"diff": GL_COLLISION, "dry_run": False},
    ),
    Row(
        "ANCHOR-gl-live-without-oracle-posts-all",
        review("gitlab", [CONTEXT, ADDED, NEW], review_body="MR review"),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_path="src/edited.py"),
                position("src/edited.py", 62, old_path="src/edited.py"),
                position(
                    "src/app/clients/api/__init__.py",
                    1,
                    old_path="src/app/clients/api/__init__.py",
                ),
            ),
            bodies=(
                CONTEXT_BODY + marker(CONTEXT_KEY),
                ADDED_BODY + marker(ADDED_KEY),
                NEW_BODY + marker(NEW_KEY),
            ),
            summary=HEADER + "MR review" + FOOTER.replace("{count}", "3"),
            summary_lacks=("could not be anchored inline",),
            err=DIFF_WARNING,
            surfaces=("notes", "discussions", "discussions", "discussions"),
            out_lines=("  3 inline discussion(s) posted.",),
        ),
        {"diff_status": 128, "diff_error": "denied", "dry_run": False},
    ),
    Row(
        "ANCHOR-gh-ambiguous-raw-spelling",
        review(
            findings=[
                fix(file="b/foo.py", line=2, end_line=2, suggested_fix_code="CHANGED"),
                fix(file="b/foo.py", line=3, end_line=3, suggested_fix_code="CHANGED"),
            ]
        ),
        Expected(
            anchors=(
                gh_anchor(path="b/foo.py"),
                gh_anchor(line=3),
            ),
            bodies=(RANGE_PROSE, RANGE_PROSE),
            skipped=(
                "suggested-fix downgraded: b/foo.py:2 (no_diff_oracle)",
                "suggested-fix downgraded: b/foo.py:3 (no_diff_oracle)",
            ),
            counts=fix_counts(0, 2),
        ),
        {"diff": GH_COLLISION},
    ),
    Row(
        "ANCHOR-gh-unique-prefixed-recall",
        review(
            findings=[
                fix(
                    file="b/src/edited.py",
                    line=2,
                    end_line=2,
                    suggested_fix_code="CHANGED",
                )
            ]
        ),
        Expected(
            anchors=(gh_anchor(path="src/edited.py"),),
            bodies=(
                RANGE_PROSE.removesuffix(TRAILER)
                + "\n\n```suggestion\nCHANGED\n```"
                + TRAILER,
            ),
            counts=KEPT,
        ),
        {"diff": GH_RECALL},
    ),
    Row(
        "ANCHOR-resolved-path-cannot-join-files",
        review(findings=[finding(file="b/foo.py", line=1, end_line=3)]),
        Expected(
            anchors=(gh_anchor(line=1),),
            bodies=(BASIC_BODY,),
        ),
        {
            "diff": (
                "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
                "@@ -1,1 +1,2 @@\n line0\n+FOO_L1\ndiff --git a/b/foo.py "
                "b/b/foo.py\n--- a/b/foo.py\n+++ b/b/foo.py\n@@ -3,1 +3,2 "
                "@@\n line2\n+SUB_L4\n"
            )
        },
    ),
    Row(
        "ANCHOR-literal-a-directory-keeps-old-side",
        review("gitlab", [finding(file="a/foo.py", line=1, omit=("end_line",))]),
        Expected(
            anchors=(position("a/foo.py", 1, old_line=1, old_path="a/foo.py"),),
            bodies=(BASIC_BODY,),
        ),
        {"diff": GL_REAL_A + "--- foo.py\n+++ foo.py\n@@ -0,0 +1,1 @@\n+new\n"},
    ),
]


@pytest.mark.parametrize("row", params(ANCHOR))
def test_anchor_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)


GATE = [
    Row(
        "GATE-gh-kept-multiline",
        review(findings=[fix(future={"kept": [1]})]),
        Expected(anchors=(GH_RANGE,), bodies=(RANGE_GH,), skipped=(), counts=KEPT),
        {"diff": GH_INDENTED},
    ),
    Row(
        "GATE-gh-blocked-range",
        review(findings=[fix(end_line=940)]),
        Expected(
            anchors=(GH_ANCHOR,),
            bodies=(RANGE_PROSE,),
            skipped=("suggested-fix downgraded: foo.py:2 (range_not_in_diff)",),
            counts=DOWN,
        ),
        {"diff": GH_INDENTED},
    ),
    Row(
        "GATE-gh-degraded-group-members",
        review(
            findings=[
                grouped(finding(line=999, title="A", body="Body A"), primary=True),
                grouped(fix()),
            ]
        ),
        Expected(
            summary_has=(
                "could not be anchored inline",
                "Range bug",
                "Return two instead.",
            ),
            summary_lacks=("```suggestion",),
            skipped=(
                off_diff("A", valid=[1, 2, 3], members="A, Range bug"),
                "suggested-fix downgraded: foo.py:2 (anchor_mismatch)",
            ),
            counts=DOWN,
        ),
        {"diff": GH_INDENTED},
    ),
    Row(
        "GATE-gh-no-diff-oracle",
        review(findings=[fix()]),
        Expected(
            anchors=(GH_RANGE,),
            bodies=(RANGE_PROSE,),
            skipped=("suggested-fix downgraded: foo.py:2 (no_diff_oracle)",),
            before_skips=DIFF_WARNING,
            counts=DOWN,
        ),
        {"diff_status": 128, "diff_error": "denied"},
    ),
    Row(
        "GATE-gl-cap-downgrade",
        review("gitlab", [fix(end_line=103, suggested_fix_code="    patched")]),
        Expected(
            anchors=(position(),),
            bodies=(RANGE_PROSE,),
            skipped=("suggested-fix downgraded: foo.py:2 (span_exceeds_platform_cap)",),
            counts=DOWN,
        ),
        {
            "diff": "--- foo.py\n+++ foo.py\n@@ -0,0 +1,104 @@\n"
            + "".join("+    line" + str(n) + "\n" for n in range(1, 105))
        },
    ),
]


@pytest.mark.parametrize("row", params(GATE))
def test_fix_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)
    if row.id in ("GATE-gh-kept-multiline", "GATE-gh-blocked-range"):
        check(
            Row(row.id, row.data, row.expected, {**row.options, "dry_run": False}),
            posting,
            monkeypatch,
        )


FIRST = fix(
    title="First", line=2, end_line=4, suggested_fix_code="    a2\n    a3\n    a4"
)

SECOND = fix(
    title="Second", line=3, end_line=5, suggested_fix_code="    b3\n    b4\n    b5"
)

CORR = {
    "file": "foo.py",
    "line": 6,
    "severity": "medium",
    "title": "Corroborator",
    "body": "Body C",
    "agent": "bug-detector",
    "dimension": "correctness",
    "confidence": 70,
}

FIRST_KEY = "4e747ece3feb9629"

SECOND_KEY = "46708f208c1414bf"

CORR_KEY = "5e05d974eb959dd6"

OVERLAP_WARNING = "suggested-fix downgraded: foo.py:3 (overlaps_kept_fence)"

FIRST_BODY = RANGE_GH.replace("Range bug", "First").replace(
    "    return 2\n    # done", "    a2\n    a3\n    a4"
)

SECOND_PROSE = RANGE_PROSE.replace("Range bug", "Second")

OVERLAP = [
    Row(
        "OVERLAP-gh-fenceless-finding-does-not-block-fence",
        review(
            findings=[
                finding(title="No fence", body="No suggested fix", line=3),
                fix(
                    title="Fenced",
                    line=3,
                    end_line=5,
                    suggested_fix_code="    b3\n    b4\n    b5",
                ),
            ]
        ),
        Expected(
            anchors=(
                gh_anchor(line=3),
                gh_anchor(line=5, start=3),
            ),
            body_has=((), ("```suggestion\n    b3\n    b4\n    b5",)),
            counts=KEPT,
        ),
        {"diff": GH_DIFF},
    ),
    Row(
        "OVERLAP-gl-degraded-index",
        review(
            "gitlab",
            [
                finding(line=999, omit=("end_line",)),
                fix(line=3, end_line=940),
                FIRST,
                SECOND,
            ],
        ),
        Expected(
            anchors=(
                position(line=3, old_path="foo.py"),
                GL_ANCHOR,
                position(line=3, old_path="foo.py"),
            ),
            body_has=(("Range bug",), ("```suggestion:-0+2",), ("Second",)),
            body_lacks=(("```suggestion",), (), ("```suggestion",)),
            skipped=(
                off_diff("T"),
                "suggested-fix downgraded: foo.py:3 (range_not_in_diff)",
                OVERLAP_WARNING,
            ),
            counts=fix_counts(1, 2),
        ),
        {"diff": GL_DIFF},
    ),
    Row(
        "OVERLAP-gl-failed-winner-still-blocks-reactive",
        review("gitlab", [grouped(FIRST, primary=True), grouped(SECOND)]),
        Expected(
            anchors=(GL_ANCHOR, position(line=3, old_path="foo.py")),
            body_has=(("```suggestion:-0+2",), ("Second",)),
            body_lacks=((), ("```suggestion",)),
            err=(
                "WARNING: Skipping finding 'First' at foo.py:2 \u2014 "
                "GitLab rejected the inline discussion.\ndenied\nWARNING: "
            )
            + OVERLAP_WARNING
            + "\n",
            counts=BOTH,
        ),
        {
            "diff": GL_DIFF,
            "dry_run": False,
            "submissions": {
                "notes": [PostResult({}, None, None)],
                "discussions": [
                    PostResult(None, "denied", None),
                    PostResult({}, None, None),
                ],
            },
        },
    ),
]


@pytest.mark.parametrize("row", params(OVERLAP))
def test_overlap_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)


PRIMARY_A = {
    "file": "foo.py",
    "line": 2,
    "severity": "high",
    "title": "A",
    "body": "Body A",
}

MEMBER_B = {
    "file": "foo.py",
    "line": 3,
    "severity": "medium",
    "title": "B",
    "body": "Body B",
    "agent": "bug-detector",
    "dimension": "correctness",
    "confidence": 70,
}

GROUP_BODY = (
    "**\U0001f7e0 [HIGH] A**\n\nBody A\n\n---\n\n**Corroborating finding "
    "\u2014 bug-detector (correctness, confidence 70):**\n\n**B**\n\nBody B" + TRAILER
)

GROUP = [
    Row(
        "GROUP-gh-corroborator-fence-suppressed",
        review(
            findings=[
                grouped(PRIMARY_A, primary=True),
                grouped(
                    {**MEMBER_B, "end_line": 3, "suggested_fix_code": "    replaced"}
                ),
            ]
        ),
        Expected(anchors=(GH_ANCHOR,), bodies=(GROUP_BODY,)),
        {"diff": GH_DIFF},
    ),
    Row(
        "GROUP-gh-no-line-primary-preserves-member-order",
        review(
            findings=[
                grouped(
                    {
                        "file": "foo.py",
                        "severity": "low",
                        "title": "Mystery",
                        "body": "Body A",
                    },
                    primary=True,
                ),
                grouped(MEMBER_B),
            ]
        ),
        Expected(
            summary_has=(
                "### \u26a0\ufe0f 2 findings could not be anchored inline",
                "Mystery",
                "Body A",
                "B",
                "Body B",
            ),
            summary_ordered=("Mystery", "Body A", "B", "Body B"),
            skipped=(
                "Finding 'Mystery' has no line number \u2014 skipping. "
                "[group members: Mystery, B]",
            ),
            out_lines=(
                "  2 finding(s) skipped inline (lines not in diff) "
                "\u2014 appended to review body.",
            ),
        ),
        {"diff": GH_DIFF},
    ),
    Row(
        "GROUP-gl-off-diff-primary-preserves-member-order",
        review(
            "gitlab",
            [
                grouped({**PRIMARY_A, "line": 999}, primary=True),
                grouped(MEMBER_B),
            ],
        ),
        Expected(
            summary_has=(
                "The following 2 findings reference lines outside this "
                "diff and are included here instead of as inline "
                "comments:",
                "### \u26a0\ufe0f 2 findings could not be anchored inline",
                "A",
                "Body A",
                "B",
                "Body B",
            ),
            summary_ordered=("A", "Body A", "B", "Body B"),
            skipped=(off_diff("A", members="A, B"),),
            out_lines=("  2 finding(s) skipped.",),
        ),
        {"diff": GL_DIFF},
    ),
]


@pytest.mark.parametrize("row", params(GROUP))
def test_group_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)


RERUN = [
    Row(
        "RERUN-partial-missing-anchored-member",
        review("gitlab", [grouped(CONTEXT, primary=True), grouped(ADDED)]),
        Expected(
            anchors=(position("src/edited.py", 62, old_path="src/edited.py"),),
            bodies=(ADDED_BODY + marker(ADDED_KEY),),
            surfaces=("discussions",),
            out_lines=("  1 inline discussion(s) posted.",),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
    ),
    Row(
        "RERUN-read-refusal-posts-everything",
        review("gitlab", [CONTEXT]),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            bodies=(CONTEXT_BODY + marker(CONTEXT_KEY),),
            summary=SUMMARY,
            err=(
                "WARNING: could not check for an existing summary note or "
                "already-delivered inline discussions (gitlab notes: fetch "
                "failed (exit 1): boom); posting them.\n"
            ),
            surfaces=("notes", "discussions"),
        ),
        {
            "diff": CONTRACT_DIFF,
            "dry_run": False,
            "entries": JsonFetch(None, "gitlab notes: fetch failed (exit 1): boom"),
        },
    ),
    Row(
        "RERUN-partial-missing-primary-single-body",
        review(
            "gitlab", [grouped(CONTEXT, primary=True), grouped(ADDED), grouped(NEW)]
        ),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            bodies=(CONTEXT_BODY + marker(CONTEXT_KEY),),
            surfaces=("discussions",),
            out_lines=(
                "  1 inline discussion(s) posted.",
                "  2 inline discussion(s) already on the MR from an "
                "earlier run \u2014 left alone.",
            ),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(ADDED_KEY, NEW_KEY)},
    ),
    Row(
        "RERUN-legacy-group-skips-render-and-count",
        review(
            "gitlab",
            [
                grouped(fix(), primary=True),
                grouped(finding(line=None, omit=("end_line",))),
            ],
        ),
        Expected(
            surfaces=(),
            out_lines=(
                "  0 inline discussion(s) posted.",
                "  2 inline discussion(s) already on the MR from an "
                "earlier run \u2014 left alone.",
            ),
        ),
        {
            "diff": GL_INDENTED,
            "dry_run": False,
            "entries": prior(RANGE_KEY, legacy=(RANGE_KEY,)),
        },
    ),
    Row(
        "RERUN-fix-and-provenance-independent",
        review(
            "gitlab",
            [
                {
                    **CONTEXT,
                    "end_line": 999,
                    "suggested_fix_code": "patched_ctx",
                    "rules": [{"id": "new"}],
                    "rule_source": "changed",
                }
            ],
        ),
        Expected(
            err=(
                "WARNING: suggested-fix downgraded: src/edited.py:61 "
                "(range_not_in_diff)\n"
            ),
            counts=DOWN,
            surfaces=(),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
    ),
    Row(
        "RERUN-resolved-path-key",
        review("gitlab", [{**CONTEXT, "file": "b/src/edited.py"}]),
        Expected(surfaces=()),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
    ),
    Row(
        "NOTICE-dedup-before-fold-notice",
        review("gitlab", [{**NEW, "body": "x" * 1000001}]),
        Expected(surfaces=()),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior("6471baa1c45bb13a")},
    ),
]


@pytest.mark.parametrize("row", params(RERUN))
def test_rerun_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)


UNANCHORED = {**ADDED, "line": None}

OFF_DIFF_MEMBER = {**ADDED, "line": 999}

PROMOTE = [
    Row(
        "PROMOTE-partial-unanchored-positionless",
        review("gitlab", [grouped(CONTEXT, primary=True), grouped(UNANCHORED)]),
        Expected(
            anchors=({},),
            bodies=(ADDED_BODY + marker("8a2d596a9cd73766"),),
            surfaces=("notes",),
            out_lines=(
                "  1 inline discussion(s) posted.",
                "  1 inline discussion(s) already on the MR from an "
                "earlier run \u2014 left alone.",
            ),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
        (("gitlab", "note", 183),),
    ),
    Row(
        "PROMOTE-folded-positionless-note-reserves-marker",
        review(
            "gitlab",
            [
                grouped(CONTEXT, primary=True),
                grouped(
                    {"title": "Unanchored note", "severity": "low", "body": "x" * 1000}
                ),
            ],
        ),
        Expected(
            anchors=({},),
            body_has=(("_[folded:",),),
            surfaces=("notes",),
            err=(
                "WARNING: Inline body folded by 765 bytes at ?:None: this "
                "corroborator note reached the 500-byte GitLab body "
                "limit.\n"
            ),
            out_lines=(
                "  1 inline discussion(s) posted.",
                "  1 inline discussion(s) already on the MR from an "
                "earlier run \u2014 left alone.",
            ),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
        (("gitlab", "note", 500),),
    ),
    Row(
        "PROMOTE-late-invalid-does-not-make-note",
        review(
            "gitlab",
            [
                grouped(CONTEXT, primary=True),
                grouped(UNANCHORED),
                grouped(OFF_DIFF_MEMBER),
                NEW,
            ],
        ),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
                position("src/app/clients/api/__init__.py", 1),
            ),
            body_has=(
                ("Context-line finding", "Added-line finding"),
                ("New-file finding",),
            ),
            err=(
                "WARNING: Skipping finding 'Context-line finding' at "
                "src/edited.py:61 \u2014 GitLab rejected the inline "
                "discussion.\ndenied\nWARNING: Skipping corroborating "
                "finding 'Added-line finding' \u2014 no line number to "
                "anchor its own discussion on.\nWARNING: Skipping "
                "corroborating finding 'Added-line finding' at "
                "src/edited.py:999 \u2014 line not found in diff.\n"
            ),
            out_lines=(
                "  2 finding(s) had a malformed position (see warnings above).",
            ),
            surfaces=("notes", "discussions", "discussions"),
        ),
        {
            "diff": CONTRACT_DIFF,
            "dry_run": False,
            "submissions": {
                "notes": [PostResult({}, None, None)],
                "discussions": [
                    PostResult(None, "denied", None),
                    PostResult({}, None, None),
                ],
            },
        },
    ),
    Row(
        "PROMOTE-positionless-fence-gated",
        review(
            "gitlab",
            [
                grouped(PRIMARY_A, primary=True),
                grouped(
                    {**MEMBER_B, "line": None, "suggested_fix_code": "    replaced"}
                ),
            ],
        ),
        Expected(
            anchors=({},),
            body_has=(
                (
                    "B",
                    "Body B",
                    "\u2694\ufe0f *Code Gauntlet*\n\n<!-- code-gauntlet-finding-key:",
                ),
            ),
            body_lacks=(("```suggestion",),),
            counts=DOWN,
            err="WARNING: suggested-fix downgraded: foo.py:None (missing_end_line)\n",
            surfaces=("notes",),
        ),
        {"diff": GL_DIFF, "dry_run": False, "entries": prior("3c008a7625ca81b2")},
    ),
    Row(
        "PROMOTE-positionless-envelope-keeps-sibling",
        review("gitlab", [grouped(CONTEXT, primary=True), grouped(UNANCHORED), NEW]),
        Expected(
            anchors=(position("src/app/clients/api/__init__.py", 1),),
            bodies=(NEW_BODY + marker(NEW_KEY),),
            err=(
                "WARNING: The composed corroborator note is 227 bytes, "
                "over the 100-byte GitLab body limit; skipping this "
                "delivery.\n"
            ),
            surfaces=("discussions",),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
        (("gitlab", "note", 100),),
    ),
    Row(
        "PROMOTE-positionless-rejected-keeps-sibling",
        review("gitlab", [grouped(CONTEXT, primary=True), grouped(UNANCHORED), NEW]),
        Expected(
            anchors=({}, position("src/app/clients/api/__init__.py", 1)),
            bodies=(
                ADDED_BODY + marker("8a2d596a9cd73766"),
                NEW_BODY + marker(NEW_KEY),
            ),
            err=(
                "WARNING: Skipping corroborating finding 'Added-line "
                "finding' \u2014 GitLab rejected the position-less note.\n"
                "denied\n"
            ),
            surfaces=("notes", "discussions"),
        ),
        {
            "diff": CONTRACT_DIFF,
            "dry_run": False,
            "entries": prior(CONTEXT_KEY),
            "submissions": {
                "notes": [PostResult(None, "denied", None)],
                "discussions": [PostResult({}, None, None)],
            },
        },
    ),
]


@pytest.mark.parametrize("row", params(PROMOTE))
def test_promoted_delivery(row, posting, monkeypatch):
    run = check(row, posting, monkeypatch)
    if row.id == "PROMOTE-partial-unanchored-positionless":
        assert len(run.requests[0].payload["body"].encode("utf-8")) == 183
    if row.id == "PROMOTE-folded-positionless-note-reserves-marker":
        assert len(run.requests[0].payload["body"].encode("utf-8")) == 495


FAILURE = [
    Row(
        "FAILURE-gl-all-rejected",
        review("gitlab", [CONTEXT]),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            bodies=(CONTEXT_BODY + marker(CONTEXT_KEY),),
            err="WARNING: Skipping finding 'Context-line finding' at src/edited.py:61 "
            "\u2014 GitLab rejected the inline discussion.\ndenied\n"
            + ERRORS["all-rejected"].replace("all 2", "all 1"),
            out_lines=("  1 inline discussion(s) not delivered (see warnings above).",),
            surfaces=("notes", "discussions"),
            code=1,
        ),
        {
            "diff": CONTRACT_DIFF,
            "dry_run": False,
            "submissions": {
                "notes": [PostResult({}, None, None)],
                "discussions": [PostResult(None, "denied", None)],
            },
        },
    ),
    Row(
        "FAILURE-gh-summary-guard",
        review(),
        Expected(
            err=(
                "post_review: The composed review body is 65538 bytes, over "
                "the 65536-byte GitHub body limit; nothing was posted.\n"
            ),
            surfaces=(),
            code=1,
        ),
        {"dry_run": False},
        composed="\u754c" * 21846,
    ),
    Row(
        "FAILURE-gl-summary-utf8-guard-before-submit",
        review("gitlab"),
        Expected(err=ERRORS["gl-summary"], surfaces=(), code=1),
        {"dry_run": False},
        composed="\u754c" * 333334,
    ),
    Row(
        "FAILURE-gh-availability-before-planning",
        review(findings=[fix(line=999)]),
        Expected(
            err=ERRORS["gh-missing"],
            surfaces=(),
            code=1,
        ),
        {
            "dry_run": False,
            "availability": [
                ForgeUnavailable(
                    "'gh' CLI tool not found. Install it and ensure it is "
                    "authenticated before running this script."
                )
            ],
        },
    ),
    Row(
        "FAILURE-gh-inline-gate-before-fatal-before-sha",
        review(findings=[fix(end_line=940)], sha=None),
        Expected(
            err="WARNING: suggested-fix downgraded: foo.py:2 (range_not_in_diff)\n"
            + ERRORS["gh-inline"],
            counts=(),
            surfaces=(),
            code=1,
        ),
        {"diff": GH_INDENTED, "dry_run": False, "head_status": 1},
        (("github", "inline", 20),),
    ),
    Row(
        "FAILURE-gl-empty-versions-before-off-diff-diagnostics",
        review("gitlab", [fix(line=999)]),
        Expected(
            err=ERRORS["versions-empty"],
            surfaces=(),
            code=1,
        ),
        {"dry_run": False, "refs": JsonFetch([], None)},
    ),
    Row(
        "FAILURE-gl-summary-fatal-before-discussions",
        review("gitlab", [CONTEXT, ADDED]),
        Expected(
            summary=HEADER + "Summary" + FOOTER.replace("{count}", "2"),
            err="WARNING: Could not parse API response as JSON: text\n"
            + ERRORS["summary-submit"],
            surfaces=("notes",),
            code=1,
        ),
        {
            "diff": CONTRACT_DIFF,
            "dry_run": False,
            "submissions": [
                PostResult(
                    None,
                    "API call failed (exit 1).",
                    "Could not parse API response as JSON: text",
                )
            ],
        },
    ),
    Row(
        "FAILURE-gl-envelope-before-dedup-and-position",
        review("gitlab", [fix(end_line=940), CONTEXT]),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            bodies=(CONTEXT_BODY + marker(CONTEXT_KEY),),
            err=(
                "WARNING: suggested-fix downgraded: foo.py:2 "
                "(range_not_in_diff)\nWARNING: The composed inline "
                "discussion is 227 bytes, over the 200-byte GitLab body "
                "limit; skipping this delivery.\n"
            ),
            counts=DOWN,
            surfaces=("discussions",),
        ),
        {
            "diff": GL_INDENTED + CONTRACT_DIFF,
            "dry_run": False,
            "entries": prior(RANGE_KEY),
        },
        (("gitlab", "discussion", 200),),
    ),
]


@pytest.mark.parametrize("row", params(FAILURE))
def test_delivery_failure(row, posting, monkeypatch):
    check(row, posting, monkeypatch)


SUMMARY_ROWS = [
    Row(
        "SUMMARY-gh-exact-fit-posts-all-skipped-content",
        review(
            findings=[finding(line=99, title="Skipped", omit=("end_line",))],
            review_body="x" * 64892,
        ),
        Expected(
            summary_has=(
                "following 1 finding",
                "Skipped",
                FOOTER.replace("{count}", "1"),
            ),
            summary_lacks=("_[folded:", "not shown:"),
            skipped=(off_diff("Skipped", line=99, valid=[]),),
        ),
    ),
    Row(
        "SUMMARY-gl-omission-reported-after-capture",
        review(
            "gitlab",
            [finding(line=999, title="GitLab budget", omit=("end_line",))],
            review_body="x" * 1000000,
        ),
        Expected(
            summary_has=("_[folded:",),
            summary_lacks=("GitLab budget",),
            skipped=(off_diff("GitLab budget", valid=[]),),
            err=(
                "WARNING: Skipped finding 'GitLab budget' at foo.py:999 "
                "not shown: the summary note reached the 1000000-byte "
                "GitLab body limit.\n"
            ),
            out_lines=(
                "MR summary note captured (dry-run).",
                "  1 skipped finding(s) not shown: the summary note "
                "reached the 1000000-byte GitLab body limit.",
            ),
        ),
    ),
    Row(
        "SUMMARY-gh-no-line-and-placeholder-keep-sibling",
        review(
            findings=[
                {"file": "foo.py", "title": "No-line bug", "body": "Body C"},
                {"title": "Mystery bug", "body": "Body D"},
                finding(omit=("end_line",)),
            ]
        ),
        Expected(
            anchors=(GH_ANCHOR,),
            bodies=(BASIC_BODY,),
            summary_has=(
                "2 findings could not be anchored inline",
                "1 inline comment was posted",
                "No-line bug",
                "Mystery bug",
                "`foo.py`",
                "`?`",
            ),
            skipped=(
                "Finding 'No-line bug' has no line number \u2014 skipping.",
                "Finding 'Mystery bug' has no line number \u2014 skipping.",
            ),
        ),
        {"diff": GH_DIFF},
    ),
    Row(
        "SUMMARY-report-fold-closes-four-backtick-fence",
        [],
        Expected(summary_has=("\n````\n\n_[folded:", FOOTER.replace("{count}", "0"))),
        {"arguments": ()},
    ),
    Row(
        "SUMMARY-folded-prose-byte-count",
        review(review_body="X" * 70000),
        Expected(
            summary=HEADER
            + "X" * 65206
            + (
                "\n\n_[folded: 4794 more bytes; this review body reached "
                "the 65536-byte GitHub body limit]_"
            )
            + FOOTER.replace("{count}", "0"),
            out_lines=(
                "  review_body folded by 4794 bytes: the review body "
                "reached the 65536-byte GitHub body limit.",
            ),
        ),
    ),
    Row(
        "SUMMARY-whole-group-admission-and-omission-order",
        review(
            findings=[
                grouped({**PRIMARY_A, "line": 999}, primary=True),
                grouped({**MEMBER_B, "body": "x" * 65000}),
                finding(
                    line=999, title="Small unrelated", body="small", omit=("end_line",)
                ),
            ],
            review_body="",
        ),
        Expected(
            summary_has=(
                "following 1 finding",
                "Small unrelated",
                "corroborators included",
                "_2 of these 3 findings are not shown:",
            ),
            summary_lacks=("**A**", "**B**"),
            skipped=(
                off_diff("A", members="A, B"),
                off_diff("Small unrelated"),
            ),
            err=(
                "WARNING: Skipped finding 'A' at foo.py:999 not shown: the "
                "review body reached the 65536-byte GitHub body limit.\n"
                "WARNING: Skipped finding 'B' at foo.py:3 not shown: the "
                "review body reached the 65536-byte GitHub body limit.\n"
            ),
        ),
        {"diff": GH_DIFF},
    ),
    Row(
        "SUMMARY-existing-summary-suppresses-guard-and-notices",
        review(
            "gitlab", [finding(line=999, omit=("end_line",))], review_body="X" * 1100000
        ),
        Expected(
            summary="",
            err=(
                "WARNING: Skipping finding 'T' at foo.py:999 \u2014 line "
                "not found in diff. Valid lines for this file: []\n"
            ),
            surfaces=(),
        ),
        {"dry_run": False, "entries": prior()},
        composed="\u754c" * 333334,
    ),
]


@pytest.mark.parametrize("row", params(SUMMARY_ROWS))
def test_summary_delivery(row, posting, monkeypatch, tmp_path):
    if row.id == "SUMMARY-existing-summary-suppresses-guard-and-notices":
        composed = compose.ComposedBody(
            row.composed,
            0,
            1,
            100,
            (compose.OmittedEntry("foo.py:999", "T"),),
        )
        monkeypatch.setattr(compose, "compose_review_body", lambda *a, **k: composed)
        row = Row(row.id, row.data, row.expected, row.options, row.limits)
    if row.id == "SUMMARY-report-fold-closes-four-backtick-fence":
        source = "## Summary\n\n````python\n" + "x" * 70000 + "\n\n## Findings\n"
        report_path = tmp_path / "report.md"
        report_path.write_text(source, encoding="utf-8")
        row = Row(
            row.id,
            row.data,
            row.expected,
            {
                "arguments": (
                    "--report",
                    str(report_path),
                    "--owner",
                    "o",
                    "--repo",
                    "r",
                    "--pr-number",
                    "5",
                    "--platform",
                    "github",
                    "--sha",
                    SHA,
                )
            },
        )
    run = check(row, posting, monkeypatch)
    if row.id == "SUMMARY-gh-exact-fit-posts-all-skipped-content":
        assert len(run.payload["payload"]["body"].encode("utf-8")) == 65536
    if row.id == "SUMMARY-whole-group-admission-and-omission-order":
        assert (
            "  1 of 3 finding(s) skipped inline (lines not in diff) \u2014 "
            "appended to review body.\n"
        ) in run.out
        assert (
            "  2 skipped finding(s) not shown: the review body reached the "
            "65536-byte GitHub body limit.\n"
        ) in run.out
    if row.id == "SUMMARY-existing-summary-suppresses-guard-and-notices":
        assert "body limit" not in run.out


def test_capture_bare_array_and_wrapper_bytes(posting, monkeypatch):
    row = Row(
        "CAPTURE-bare-array-and-wrapper-bytes",
        [finding(omit=("end_line",))],
        Expected(
            anchors=(GH_ANCHOR,),
            bodies=(BASIC_BODY,),
            summary=HEADER.rstrip() + FOOTER.replace("{count}", "1"),
        ),
        {
            "diff": GH_DIFF,
            "arguments": GH_ARGS,
        },
    )
    run = check(row, posting, monkeypatch)
    wrapper = posting(review(findings=row.data, review_body=""), diff=GH_DIFF)
    assert run.raw == wrapper.raw


def test_capture_gl_dry_and_live_reserve_all_group_keys(posting, monkeypatch):
    row = Row(
        "CAPTURE-gl-dry-and-live-reserve-all-group-keys",
        review(
            "gitlab",
            [grouped({**CONTEXT, "body": "x" * 200}, primary=True), grouped(ADDED)],
        ),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            body_has=(("_[folded:",),),
            body_lacks=(("code-gauntlet-finding-key",),),
            err=(
                "WARNING: Inline body folded by 185 bytes at "
                "src/edited.py:61: this inline discussion reached the "
                "500-byte GitLab body limit.\n"
            ),
        ),
        {"diff": CONTRACT_DIFF},
        (("gitlab", "discussion", 500),),
    )
    run = check(row, posting, monkeypatch)
    # A dry run never reads prior delivery, so it captures every finding.
    assert [c.method for c in run.fake.calls] == [
        "diff",
        "ensure_available",
        "ensure_available",
        "diff_refs",
    ]
    live = posting(row.data, diff=CONTRACT_DIFF, dry_run=False)
    assert "2 inline discussion(s) posted." in live.out
    body = live.requests[1].payload["body"]
    assert body.startswith(run.payload["discussions"][0]["body"])
    assert len(body.encode("utf-8")) <= 500
    assert len(find_finding_markers(body)) == 2
    assert live.err == run.err


@pytest.mark.parametrize(
    "delta,key",
    [
        pytest.param(0, "d8544a43f683184d", id="CAPTURE-gl-multibyte-exact-fit"),
        pytest.param(1, "346f1085827a835e", id="CAPTURE-gl-multibyte-one-byte-over"),
    ],
)
def test_capture_gl_multibyte_boundary(delta, key, posting):
    data = review(
        "gitlab",
        [
            finding(
                file="bar.py",
                line=1,
                title="Boundary finding",
                body="\u754c seed" + "x" * (999821 + delta),
                omit=("end_line",),
            ),
            finding(
                file="bar.py",
                line=2,
                severity="low",
                title="Healthy sibling",
                body="short body",
                omit=("end_line",),
            ),
        ],
    )
    dry = posting(data, diff=GL_DIFF.replace("foo.py", "bar.py"))
    live = posting(data, diff=GL_DIFF.replace("foo.py", "bar.py"), dry_run=False)
    assert dry.code == live.code == 0
    assert dry.requests == ()
    assert live.payload is None
    assert tuple(request.endpoint.rsplit("/", 1)[-1] for request in live.requests) == (
        "notes",
        "discussions",
        "discussions",
    )

    assert dry.payload["platform"] == "gitlab"
    assert dry.payload["skipped"] == []
    discussions = dry.payload["discussions"]
    assert len(discussions) == 2
    assert tuple(d["position"] for d in discussions) == (
        position("bar.py", 1, old_line=1, old_path="bar.py"),
        position("bar.py", 2, old_path="bar.py"),
    )
    assert all(list(discussion) == ["body", "position"] for discussion in discussions)
    assert tuple(request.payload["position"] for request in live.requests[1:]) == (
        position("bar.py", 1, old_line=1, old_path="bar.py"),
        position("bar.py", 2, old_path="bar.py"),
    )
    body = discussions[0]["body"]
    suffix = marker(key)
    assert len(suffix.encode("utf-8")) == 113
    assert live.requests[1].payload["body"] == body + suffix
    assert len((body + suffix).encode("utf-8")) <= 1000000
    assert body.count(TRAILER) == 1
    if delta == 0:
        assert body == (
            "**\U0001f7e0 [HIGH] Boundary finding**\n\n\u754c seed"
            + "x" * 999821
            + TRAILER
        )
        assert len(live.requests[1].payload["body"].encode("utf-8")) == 1000000
        assert dry.err == live.err == ""
    else:
        assert "Boundary finding" in body
        assert "_[folded:" in body
        assert dry.err == live.err
        assert dry.err.startswith("WARNING: Inline body folded by ")
        assert dry.err.endswith(
            " bytes at bar.py:1: this inline discussion reached the "
            "1000000-byte GitLab body limit.\n"
        )
    sibling = "**\U0001f4a1 [LOW] Healthy sibling**\n\nshort body" + TRAILER
    assert discussions[1]["body"] == sibling
    assert live.requests[2].payload["body"] == sibling + marker("efbc557bd52b5423")


SHA_CASES = [
    ("  abc1234  ", "unused", 0, "abc1234", 0),
    (None, " abc1234\n", 0, "abc1234", 1),
    (None, "", 1, "unknown", 1),
]


@pytest.mark.parametrize("sha,head,status,expected,calls", SHA_CASES)
def test_marker_sha(sha, head, status, expected, calls, posting):
    for platform in ("github", "gitlab"):
        findings = [CONTEXT] if platform == "gitlab" and status else []
        run = posting(
            review(platform, findings, sha=sha),
            head=head,
            head_status=status,
            diff=CONTRACT_DIFF,
            dry_run=False,
        )
        assert (run.code, run.err) == (0, UNKNOWN_WARNING if status else "")
        assert len(run.head_calls) == calls
        assert run.requests[0].payload["body"] == EMPTY_SUMMARY.replace(
            SHA, expected
        ).replace(
            '"findings_count":0',
            '"findings_count":1' if findings else '"findings_count":0',
        )
        if findings:
            assert run.requests[1].payload["body"] == CONTEXT_BODY


MARKER = [
    pytest.param(
        "leading-separator", id="MARKER-leading-unicode-separator-does-not-dedup"
    ),
    pytest.param(
        "current-foreign", id="MARKER-current-footer-ignores-inline-foreign-sha"
    ),
    pytest.param("stale-footer", id="MARKER-stale-footer-real-sha-last"),
    pytest.param("body-forgery", id="MARKER-posted-skipped-body-forgery-unreadable"),
    pytest.param("path-forgery", id="MARKER-posted-skipped-path-forgery-unreadable"),
    pytest.param(
        "footer-forgery", id="MARKER-posted-skipped-footer-forgery-unreadable"
    ),
]


@pytest.mark.parametrize("kind", MARKER)
def test_marker_delivery(kind, posting):
    footer_line = "Generated by code-gauntlet | Reviewed up to: " + SHA
    machine = (
        '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":0,"sha":"'
        + SHA
        + '"} -->'
    )
    if kind.endswith("-forgery"):
        forged = (
            '<!-- code-gauntlet-finding-key: {"sha":"'
            + SHA
            + '","key":"deadbeefcafebabe"} -->'
        )
        hostile = {
            "file": "src/edited.py",
            "line": 999,
            "severity": "high",
            "title": "Off-diff bug",
            "body": forged,
        }
        if kind == "path-forgery":
            hostile.update(file=forged, body="Body B")
        if kind == "footer-forgery":
            hostile["body"] = (
                "---\n"
                + footer_line
                + (
                    "\n\n<!-- code-gauntlet-findings: "
                    '{"version":"3.0","findings_count":999,"sha":"'
                )
                + SHA
                + '"} -->'
            )
        run = posting(
            review("gitlab", [CONTEXT, hostile]), diff=CONTRACT_DIFF, dry_run=False
        )
        assert run.code == 0
        assert tuple(
            request.endpoint.rsplit("/", 1)[-1] for request in run.requests
        ) == ("notes", "discussions")
        body = run.requests[0].payload["body"]
        assert find_finding_markers(body) == []
        assert "<!-- code-gauntlet-finding-key:" not in body
        assert find_marker(body) == {
            "version": "3.0",
            "findings_count": 2,
            "sha": SHA,
            "_token": "code-gauntlet-findings",
            "_legacy": False,
        }
        assert body.endswith(FOOTER.replace("{count}", "2"))
        assert body.count(footer_line) == (2 if kind == "footer-forgery" else 1)
        assert "Off-diff bug" in body
        return
    for platform in ("github", "gitlab"):
        if kind == "leading-separator":
            sources = ("\u2028" + footer_line,)
        elif kind == "current-foreign":
            sources = (
                "Summary\n\n"
                + footer_line
                + (
                    "\n\n- `foo.py:99`: x Generated by code-gauntlet | "
                    "Reviewed up to: "
                    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
            )
        elif kind == "stale-footer":
            sources = (
                "Some notes.\n\nGenerated by code-gauntlet | Reviewed up to: abc1234",
            )
        for source in sources:
            run = posting(review(platform, review_body=source))
            assert run.code == 0
            assert run.err == ""
            body = run.payload["payload" if platform == "github" else "summary"]["body"]
            if kind == "current-foreign":
                assert body == HEADER + source + "\n\n" + machine
                assert sum(line == footer_line for line in body.split("\n")) == 1
            else:
                assert body == HEADER + source + FOOTER.replace("{count}", "0")
                assert sum(line == footer_line for line in body.split("\n")) == 1
            signal = find_marker(body)
            assert signal["sha"] == SHA
            assert signal["findings_count"] == 0


INPUT_REFUSALS = [
    (review(web_origin=None), "web_origin must be a string"),
    (
        review(web_origin="https://git.example.com/path"),
        "web_origin: web_origin must be an http(s) origin without a path, query or fragment",
    ),
    (7, "Findings JSON must be an object or an array."),
    ({"repo": "r"}, "Missing required field in findings JSON: 'owner'"),
    (review(owner=7), "owner must be a string"),
    (
        review(platform="BitBucket"),
        "Unsupported platform: 'bitbucket'. Use 'github' or 'gitlab'.",
    ),
    ({**review(), "findings": None}, "findings must be an array"),
    (review(findings=[None]), "findings[0] must be an object"),
]


@pytest.mark.parametrize("data,error", INPUT_REFUSALS)
def test_invalid_review_input(
    data, error, tmp_path, monkeypatch, capsys, forge_factory
):
    artifact = tmp_path / "post-review-payload.json"
    artifact.write_bytes(b"sentinel artifact\n")
    run = invoke_posting(
        tmp_path,
        monkeypatch,
        capsys,
        forge_factory,
        data,
        dry_run=False,
        keep_existing_artifact=True,
    )
    assert (run.code, run.out, run.err) == (1, "", "post_review: " + error + "\n")
    assert run.forge_factory_calls == run.head_calls == run.requests == ()
    assert run.raw == b"sentinel artifact\n"


@pytest.mark.parametrize("platform", ["github", "gitlab"])
def test_review_input_defaults_and_unknown_fields(platform, posting):
    data = review(platform)
    del data["findings"]
    run = posting(data)
    assert (run.code, run.err) == (0, "")
    assert (
        run.payload["payload" if platform == "github" else "summary"]["body"]
        == EMPTY_SUMMARY
    )
    data = review(platform, [finding(future={"nested": [1]})], future_wrapper=7)
    run = posting(data, diff=GH_DIFF if platform == "github" else GL_DIFF)
    assert (run.code, run.err) == (0, "")
    comments = (
        run.payload["payload"]["comments"]
        if platform == "github"
        else run.payload["discussions"]
    )
    assert comments[0]["body"] == BASIC_BODY


@pytest.mark.parametrize(
    "data",
    [
        [],
        review(owner="other", repo="other", pr_number=9, sha="bbbbbbb", review_body=""),
    ],
)
def test_review_input_overrides_and_report(data, posting, tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Review\n\n## Summary\n\nFrom report\n\n## Findings\n", encoding="utf-8"
    )
    run = posting(
        data,
        arguments=(*GH_ARGS, "--report", str(report)),
    )
    assert (run.code, run.err, run.head_calls) == (0, "", ())
    assert run.payload["endpoint"] == "repos/o/r/pulls/5/reviews"
    assert run.payload["payload"]["body"] == HEADER + "From report" + FOOTER.replace(
        "{count}", "0"
    )
    existing = posting(review(), arguments=("--report", "missing-report.md"))
    assert (existing.code, existing.err) == (0, "")
    assert existing.payload["payload"]["body"] == EMPTY_SUMMARY


def test_string_pr_number_and_explicit_group_primary(posting):
    run = posting(
        review(
            pr_number="5",
            findings=[
                grouped(PRIMARY_A),
                grouped(MEMBER_B, primary=True),
            ],
        ),
        diff=GH_DIFF,
    )
    assert (run.code, run.err) == (0, "")
    assert run.payload["endpoint"] == "repos/o/r/pulls/5/reviews"
    comments = run.payload["payload"]["comments"]
    assert len(comments) == 1
    assert comments[0]["body"].startswith("**\U0001f7e1 [MEDIUM] B**\n\nBody B")
    assert "Body A" in comments[0]["body"]


def test_detected_platform(posting, monkeypatch):
    monkeypatch.setattr(
        post, "origin_remote", lambda: parse_remote("git@github.com:o/r")
    )
    data = review()
    del data["platform"]
    run = posting(data)
    assert (run.code, run.err) == (0, "")
    assert run.out.startswith("Detected platform: github (from git remote: github.com)")
    assert run.payload["platform"] == "github"


def test_gitlab_identity_failure_warns_and_posts_without_dedup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gauntlet.forge import Poster
    from gauntlet.prior_review import PriorDelivery

    from tests.support.forge import FakeGitLab

    failure = (
        "could not resolve posting identity: /user denied; set CODE_GAUNTLET_POSTER"
    )
    fake = FakeGitLab(
        entries=[JsonFetch([], "gitlab notes denied")],
        posters=[Poster(None, failure)],
    )
    state = post.gitlab_prior_delivery(
        "o", "r", 5, SHA, forge=fake, session=post.DeliverySession(dry_run=False)
    )
    assert state == PriorDelivery(
        False, frozenset(), frozenset(), f"gitlab notes denied; {failure}"
    )
    warning = capsys.readouterr().err
    assert failure in warning and "posting them" in warning


@pytest.mark.parametrize(
    "origin, arguments, host",
    [
        (None, (), None),
        ("https://Git.Example.COM:8443/", (), "git.example.com"),
        (
            "invalid",
            ("--web-origin", "http://Override.Example:8080"),
            "override.example",
        ),
    ],
)
def test_posting_origin(origin, arguments, host, posting):
    data = review()
    if origin is not None:
        data["web_origin"] = origin
    run = posting(data, arguments=arguments)
    assert (run.code, run.err) == (0, "")
    assert run.forge_factory_calls == (("github", host),)
