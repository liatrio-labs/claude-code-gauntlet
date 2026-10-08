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
from gauntlet.marker import find_finding_marker, find_finding_markers, find_marker

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
    prior,
    review,
)

TRAILER = "\n\n\u2694\ufe0f *Code Gauntlet*"
SUMMARY = HEADER + "Summary" + FOOTER.replace("{count}", "1")
EMPTY_SUMMARY = HEADER + "Summary" + FOOTER.replace("{count}", "0")
BASIC_BODY = "**\U0001f7e0 [HIGH] T**\n\nb" + TRAILER
RANGE_PROSE = (
    "**\U0001f7e0 [HIGH] Range bug**\n\nBody\n\n"
    "**Suggested fix:**\nReturn two instead." + TRAILER
)
RANGE_GH = (
    "**\U0001f7e0 [HIGH] Range bug**\n\nBody\n\n"
    "**Suggested fix:**\nReturn two instead.\n\n"
    "```suggestion\n    return 2\n    # done\n```" + TRAILER
)
CONTEXT_BODY = "**\U0001f7e0 [HIGH] Context-line finding**\n\nBody one" + TRAILER
ADDED_BODY = "**\U0001f7e1 [MEDIUM] Added-line finding**\n\nBody two" + TRAILER
NEW_BODY = "**\U0001f4a1 [LOW] New-file finding**\n\nBody three" + TRAILER
NEW_KEY = "a9cb7253f6710b82"
RANGE_KEY = "1422e1e3b48521d1"
RENAME_DIFF = (GL_FIXTURES / "rename.diff").read_text(encoding="utf-8")
GH_DIFF = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
    "@@ -1,1 +1,6 @@\n def f():\n+    line2\n+    line3\n"
    "+    line4\n+    line5\n+    line6\n"
)
GL_DIFF = GH_DIFF.split("--- a/foo.py", 1)[1]
GL_DIFF = "--- foo.py" + GL_DIFF.replace("+++ b/foo.py", "+++ foo.py")
GH_INDENTED = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)
GL_INDENTED = (
    "--- foo.py\n+++ foo.py\n@@ -1,1 +1,3 @@\n def f():\n+    return 1\n+    # tail\n"
)
GH_COLLISION = (
    "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"
    "@@ -1,1 +1,3 @@\n line1\n+TOP_V1\n+TOP_V2\n"
    "diff --git a/b/foo.py b/b/foo.py\n--- a/b/foo.py\n+++ b/b/foo.py\n"
    "@@ -1,1 +1,2 @@\n line1\n+SUB_V1\n"
)
GL_COLLISION = (
    "--- a/x.py\n+++ a/x.py\n@@ -1,1 +1,2 @@\n line1\n+SUB_V1\n"
    "--- x.py\n+++ x.py\n@@ -1,1 +1,2 @@\n line1\n+TOP_V1\n"
)
GH_RECALL = (
    "diff --git a/src/edited.py b/src/edited.py\n--- a/src/edited.py\n"
    "+++ b/src/edited.py\n@@ -1,1 +1,2 @@\n line1\n+CURRENT\n"
)
GL_RECALL = "--- src/edited.py\n+++ src/edited.py\n@@ -1,1 +1,2 @@\n line1\n+CURRENT\n"
GL_REAL_A = (
    "diff --git a/a/foo.py b/a/foo.py\n--- a/foo.py\n+++ a/foo.py\n"
    "@@ -1,2 +1,2 @@\n ctx\n-x\n+y\n"
)
DIFF_WARNING = (
    "WARNING: Could not fetch diff (exit 128): denied. Skipping line validation "
    "\u2014 all findings will be posted.\n"
)


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
    err: str = ""
    counts: tuple[str, ...] = ()
    methods: tuple[str, ...] | None = None
    surfaces: tuple[str, ...] | None = None
    keys: tuple[tuple[str, ...], ...] | None = None
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
    assert run.err == expected.err
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
            assert list(run.payload) == [
                "platform",
                "endpoint",
                "method",
                "payload",
                "skipped",
            ]
            assert (run.payload["endpoint"], run.payload["method"]) == (
                "repos/o/r/pulls/5/reviews",
                "POST",
            )
            assert list(run.payload["payload"]) == ["body", "event", "comments"]
            assert run.payload["payload"]["event"] == "COMMENT"
            comments = run.payload["payload"]["comments"]
            summary = run.payload["payload"]["body"]
        else:
            assert list(run.payload) == [
                "platform",
                "summary",
                "discussions",
                "skipped",
            ]
            comments = run.payload["discussions"]
            summary = run.payload["summary"].get("body", "")
        assert run.raw is not None and not run.raw.endswith(b"\n")
    else:
        if row.data.get("platform") == "gitlab":
            comments = [
                dict(request.payload)
                for request in run.requests
                if request.endpoint.endswith("/discussions")
                or find_finding_marker(request.payload["body"])
            ]
            notes = [
                request.payload
                for request in run.requests
                if request.endpoint.endswith("/notes")
                and not find_finding_marker(request.payload["body"])
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
    if expected.methods is not None:
        assert tuple(call.method for call in run.fake.calls) == expected.methods
    if expected.surfaces is not None:
        assert (
            tuple(request.endpoint.rsplit("/", 1)[-1] for request in run.requests)
            == expected.surfaces
        )
        assert all(request.method == "POST" for request in run.requests)
    if expected.keys is not None:
        assert (
            tuple(
                tuple(value["key"] for value in find_finding_markers(comment["body"]))
                for comment in comments
            )
            == expected.keys
        )
    return run


GH_ANCHOR = {"path": "foo.py", "line": 2, "side": "RIGHT"}
GH_RANGE = {
    "path": "foo.py",
    "line": 3,
    "side": "RIGHT",
    "start_line": 2,
    "start_side": "RIGHT",
}
GL_ANCHOR = position(old_path="foo.py")
KEPT = (
    "  1 suggested fix(es) passed the apply-check.",
    "  0 suggested fix(es) downgraded to prose.",
)
DOWN = (
    "  0 suggested fix(es) passed the apply-check.",
    "  1 suggested fix(es) downgraded to prose.",
)
BOTH = (
    "  1 suggested fix(es) passed the apply-check.",
    "  1 suggested fix(es) downgraded to prose.",
)


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
    Row(
        "SITE-raw-path-without-oracle",
        review("gitlab", [finding(file="b/x.py", line=3, omit=("end_line",))]),
        Expected(
            anchors=(position("b/x.py", 3, old_path="b/x.py"),),
            bodies=(BASIC_BODY,),
            err=DIFF_WARNING,
        ),
        {"diff_status": 128, "diff_error": "denied"},
    ),
    Row(
        "SITE-zero-line-is-accepted",
        review(findings=[finding(line=0, title="A", body="Body A")]),
        Expected(
            summary_has=("foo.py:0",),
            skipped=(
                "Skipping finding 'A' at foo.py:0 — line not found in diff. Valid lines for this file: []",
            ),
            err="WARNING: Skipping finding 'A' at foo.py:0 — line not found in diff. Valid lines for this file: []\n",
        ),
    ),
    Row(
        "SITE-negative-line-is-accepted",
        review(findings=[finding(line=-1, title="A", body="Body A")]),
        Expected(
            summary_has=("foo.py:-1",),
            skipped=(
                "Skipping finding 'A' at foo.py:-1 — line not found in diff. Valid lines for this file: []",
            ),
            err="WARNING: Skipping finding 'A' at foo.py:-1 — line not found in diff. Valid lines for this file: []\n",
        ),
    ),
    Row(
        "SITE-empty-file-is-accepted",
        review(findings=[finding(file="", title="A", body="Body A")]),
        Expected(
            summary_has=(":2",),
            skipped=(
                "Skipping finding 'A' at :2 — line not found in diff. Valid lines for this file: []",
            ),
            err="WARNING: Skipping finding 'A' at :2 — line not found in diff. Valid lines for this file: []\n",
        ),
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
            keys=(("20aad88a122950a0",),),
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
            err="WARNING: suggested-fix downgraded: a/x.py:2 (no_diff_oracle)\n",
            counts=DOWN,
            keys=(("e9a9d3f40fddd4fe",),),
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
            keys=((CONTEXT_KEY,), (ADDED_KEY,), (NEW_KEY,)),
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
                {"path": "b/foo.py", "line": 2, "side": "RIGHT"},
                {"path": "foo.py", "line": 3, "side": "RIGHT"},
            ),
            bodies=(RANGE_PROSE, RANGE_PROSE),
            skipped=(
                "suggested-fix downgraded: b/foo.py:2 (no_diff_oracle)",
                "suggested-fix downgraded: b/foo.py:3 (no_diff_oracle)",
            ),
            err="WARNING: suggested-fix downgraded: b/foo.py:2 (no_diff_oracle)\nWARNING: suggested-fix downgraded: b/foo.py:3 (no_diff_oracle)\n",
            counts=(
                "  0 suggested fix(es) passed the apply-check.",
                "  2 suggested fix(es) downgraded to prose.",
            ),
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
            anchors=({"path": "src/edited.py", "line": 2, "side": "RIGHT"},),
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
            anchors=({"path": "foo.py", "line": 1, "side": "RIGHT"},),
            bodies=(BASIC_BODY,),
        ),
        {
            "diff": "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,2 @@\n line0\n+FOO_L1\ndiff --git a/b/foo.py b/b/foo.py\n--- a/b/foo.py\n+++ b/b/foo.py\n@@ -3,1 +3,2 @@\n line2\n+SUB_L4\n"
        },
    ),
    Row(
        "ANCHOR-gh-fetch-failure-keeps-raw-range",
        review(findings=[finding(file="b/foo.py", end_line=9999)]),
        Expected(
            anchors=(
                {
                    "path": "b/foo.py",
                    "line": 9999,
                    "side": "RIGHT",
                    "start_line": 2,
                    "start_side": "RIGHT",
                },
            ),
            bodies=(BASIC_BODY,),
            err=DIFF_WARNING,
        ),
        {"diff_status": 128, "diff_error": "denied"},
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
            err="WARNING: suggested-fix downgraded: foo.py:2 (range_not_in_diff)\n",
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
                "Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3] [group members: A, Range bug]",
                "suggested-fix downgraded: foo.py:2 (anchor_mismatch)",
            ),
            err="WARNING: Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3] [group members: A, Range bug]\nWARNING: suggested-fix downgraded: foo.py:2 (anchor_mismatch)\n",
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
            err=DIFF_WARNING
            + "WARNING: suggested-fix downgraded: foo.py:2 (no_diff_oracle)\n",
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
            err="WARNING: suggested-fix downgraded: foo.py:2 (span_exceeds_platform_cap)\n",
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
    read_json = post.read_json
    loaded = []

    def observe_input(path):
        data = read_json(path)
        loaded.append(data)
        return data

    monkeypatch.setattr(post, "read_json", observe_input)
    check(row, posting, monkeypatch)
    assert loaded == [row.data]
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
        "OVERLAP-gh-lineless-end-line-keeps-sibling-fence",
        review(
            findings=[
                {
                    "file": "foo.py",
                    "severity": "low",
                    "title": "No line",
                    "body": "No line number at all.",
                    "suggested_fix_code": "x",
                    "end_line": 5,
                },
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
                {
                    "path": "foo.py",
                    "line": 5,
                    "side": "RIGHT",
                    "start_line": 3,
                    "start_side": "RIGHT",
                },
            ),
            bodies=(
                RANGE_GH.replace("Range bug", "Fenced").replace(
                    "    return 2\n    # done", "    b3\n    b4\n    b5"
                ),
            ),
            summary_has=("No line", "could not be anchored inline"),
            summary_lacks=("```suggestion",),
            skipped=(
                "Finding 'No line' has no line number \u2014 skipping.",
                "suggested-fix downgraded: foo.py:None (invalid_range)",
            ),
            err="WARNING: Finding 'No line' has no line number \u2014 skipping.\n"
            "WARNING: suggested-fix downgraded: foo.py:None (invalid_range)\n",
            counts=BOTH,
        ),
        {"diff": GH_DIFF},
    ),
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
                {"path": "foo.py", "line": 3, "side": "RIGHT"},
                {
                    "path": "foo.py",
                    "line": 5,
                    "side": "RIGHT",
                    "start_line": 3,
                    "start_side": "RIGHT",
                },
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
                "Skipping finding 'T' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6]",
                "suggested-fix downgraded: foo.py:3 (range_not_in_diff)",
                OVERLAP_WARNING,
            ),
            err="WARNING: Skipping finding 'T' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6]\nWARNING: suggested-fix downgraded: foo.py:3 (range_not_in_diff)\nWARNING: "
            + OVERLAP_WARNING
            + "\n",
            counts=(
                "  1 suggested fix(es) passed the apply-check.",
                "  2 suggested fix(es) downgraded to prose.",
            ),
        ),
        {"diff": GL_DIFF},
    ),
    Row(
        "OVERLAP-gl-loser-occupies-nothing",
        review(
            "gitlab",
            [
                FIRST,
                grouped(SECOND, primary=True),
                grouped(
                    fix(
                        title="Corroborator",
                        line=5,
                        end_line=5,
                        suggested_fix_code="    fixed5",
                    )
                ),
            ],
        ),
        Expected(
            anchors=(GL_ANCHOR, position(line=5, old_path="foo.py")),
            body_has=(("```suggestion:-0+2",), ("```suggestion\n    fixed5",)),
            counts=(
                "  2 suggested fix(es) passed the apply-check.",
                "  0 suggested fix(es) downgraded to prose.",
            ),
        ),
        {"diff": GL_DIFF, "dry_run": False, "entries": prior(SECOND_KEY)},
    ),
    Row(
        "OVERLAP-gl-failed-winner-still-blocks-reactive",
        review("gitlab", [grouped(FIRST, primary=True), grouped(SECOND)]),
        Expected(
            anchors=(GL_ANCHOR, position(line=3, old_path="foo.py")),
            body_has=(("```suggestion:-0+2",), ("Second",)),
            body_lacks=((), ("```suggestion",)),
            err="WARNING: Skipping finding 'First' at foo.py:2 \u2014 GitLab rejected the inline discussion.\ndenied\nWARNING: "
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
    Row(
        "OVERLAP-gl-reactive-sites-do-not-claim",
        review(
            "gitlab",
            [
                grouped(finding(omit=("end_line",)), primary=True),
                grouped(FIRST),
                grouped(SECOND),
            ],
        ),
        Expected(
            anchors=(GL_ANCHOR, GL_ANCHOR, position(line=3, old_path="foo.py")),
            body_has=(
                ("Corroborating finding",),
                ("```suggestion:-0+2",),
                ("```suggestion:-0+2",),
            ),
            err="WARNING: Skipping finding 'T' at foo.py:2 \u2014 GitLab rejected the inline discussion.\ndenied\n",
            counts=(
                "  2 suggested fix(es) passed the apply-check.",
                "  0 suggested fix(es) downgraded to prose.",
            ),
        ),
        {
            "diff": GL_DIFF,
            "dry_run": False,
            "submissions": {
                "notes": [PostResult({}, None, None)],
                "discussions": [
                    PostResult(None, "denied", None),
                    PostResult({}, None, None),
                    PostResult({}, None, None),
                ],
            },
        },
    ),
    Row(
        "OVERLAP-gl-losing-primary-partial-rerun",
        review("gitlab", [FIRST, grouped(SECOND, primary=True), grouped(CORR)]),
        Expected(
            anchors=(GL_ANCHOR, position(line=3, old_path="foo.py")),
            bodies=(
                FIRST_BODY.replace("```suggestion\n", "```suggestion:-0+2\n")
                + marker(FIRST_KEY),
                SECOND_PROSE + marker(SECOND_KEY),
            ),
            keys=((FIRST_KEY,), (SECOND_KEY,)),
            err="WARNING: " + OVERLAP_WARNING + "\n",
            counts=BOTH,
        ),
        {"diff": GL_DIFF, "dry_run": False, "entries": prior(CORR_KEY)},
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
    "**\U0001f7e0 [HIGH] A**\n\nBody A\n\n---\n\n"
    "**Corroborating finding \u2014 bug-detector (correctness, confidence 70):**\n\n"
    "**B**\n\nBody B" + TRAILER
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
        "GROUP-whole-group-degrades-with-zero-id",
        review(
            findings=[
                grouped({**PRIMARY_A, "line": 999, "id": 0}, primary=True),
                grouped(MEMBER_B),
            ]
        ),
        Expected(
            summary_has=(
                "2 findings could not be anchored inline",
                "A",
                "B",
                "Body A",
                "Body B",
                "corroborators included",
            ),
            skipped=(
                "Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6] [group members: 0, B]",
            ),
            err="WARNING: Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6] [group members: 0, B]\n",
        ),
        {"diff": GH_DIFF},
    ),
    Row(
        "GROUP-no-line-placeholder-and-member-labels",
        review(
            "gitlab",
            [
                grouped({"title": "Mystery", "body": "Body D"}, primary=True),
                grouped({**MEMBER_B, "id": "finding-2"}),
            ],
        ),
        Expected(
            summary_has=(
                "### \u26a0\ufe0f 2 findings could not be anchored inline",
                "`?`",
                "Mystery",
                "B",
                "Body D",
                "Body B",
            ),
            summary_ordered=("Mystery", "Body D", "B", "Body B"),
            skipped=(
                "Finding 'Mystery' has no line number \u2014 skipping. [group members: Mystery, finding-2]",
            ),
            err="WARNING: Finding 'Mystery' has no line number \u2014 skipping. [group members: Mystery, finding-2]\n",
            out_lines=("  2 finding(s) skipped.",),
        ),
        {"diff": GL_DIFF},
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
                "Finding 'Mystery' has no line number \u2014 skipping. [group members: Mystery, B]",
            ),
            err="WARNING: Finding 'Mystery' has no line number \u2014 skipping. [group members: Mystery, B]\n",
            out_lines=(
                "  2 finding(s) skipped inline (lines not in diff) \u2014 appended to review body.",
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
                "The following 2 findings reference lines outside this diff and are included here instead of as inline comments:",
                "### \u26a0\ufe0f 2 findings could not be anchored inline",
                "A",
                "Body A",
                "B",
                "Body B",
            ),
            summary_ordered=("A", "Body A", "B", "Body B"),
            skipped=(
                "Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6] [group members: A, B]",
            ),
            err="WARNING: Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6] [group members: A, B]\n",
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
        "RERUN-read-refusal-posts-everything",
        review("gitlab", [CONTEXT]),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            bodies=(CONTEXT_BODY + marker(CONTEXT_KEY),),
            summary=SUMMARY,
            err="WARNING: could not check for an existing summary note or already-delivered inline discussions (gitlab notes: fetch failed (exit 1): boom); posting them.\n",
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
            keys=((CONTEXT_KEY,),),
            surfaces=("discussions",),
            out_lines=(
                "  1 inline discussion(s) posted.",
                "  2 inline discussion(s) already on the MR from an earlier run — left alone.",
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
            methods=(
                "diff",
                "ensure_available",
                "ensure_available",
                "diff_refs",
                "review_entries",
            ),
            out_lines=(
                "  0 inline discussion(s) posted.",
                "  2 inline discussion(s) already on the MR from an earlier run — left alone.",
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
            err="WARNING: suggested-fix downgraded: src/edited.py:61 (range_not_in_diff)\n",
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
]


@pytest.mark.parametrize("row", params(RERUN))
def test_rerun_delivery(row, posting, monkeypatch):
    check(row, posting, monkeypatch)
    if row.id == "RERUN-fix-and-provenance-independent":
        kept = posting(
            review(
                "gitlab",
                [{**CONTEXT, "end_line": 61, "suggested_fix_code": "patched_ctx"}],
            ),
            diff=CONTRACT_DIFF,
            dry_run=False,
        )
        assert kept.code == 0
        assert kept.err == ""
        assert kept.requests[1].payload["body"] == (
            "**\U0001f7e0 [HIGH] Context-line finding**\n\nBody one\n\n"
            "```suggestion\npatched_ctx\n```" + TRAILER + marker(CONTEXT_KEY)
        )


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
            keys=(("8a2d596a9cd73766",),),
            out_lines=(
                "  1 inline discussion(s) posted.",
                "  1 inline discussion(s) already on the MR from an earlier run — left alone.",
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
            err="WARNING: Inline body folded by 765 bytes at ?:None: this corroborator note reached the 500-byte GitLab body limit.\n",
            out_lines=(
                "  1 inline discussion(s) posted.",
                "  1 inline discussion(s) already on the MR from an earlier run — left alone.",
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
            err="WARNING: Skipping finding 'Context-line finding' at src/edited.py:61 \u2014 GitLab rejected the inline discussion.\ndenied\nWARNING: Skipping corroborating finding 'Added-line finding' \u2014 no line number to anchor its own discussion on.\nWARNING: Skipping corroborating finding 'Added-line finding' at src/edited.py:999 \u2014 line not found in diff.\n",
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
            err="WARNING: The composed corroborator note is 227 bytes, over the 100-byte GitLab body limit; skipping this delivery.\n",
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
            err="WARNING: Skipping corroborating finding 'Added-line finding' \u2014 GitLab rejected the position-less note.\ndenied\n",
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
    Row(
        "PROMOTE-partial-off-diff-positionless",
        review("gitlab", [grouped(CONTEXT, primary=True), grouped(OFF_DIFF_MEMBER)]),
        Expected(
            anchors=({},),
            bodies=(ADDED_BODY + marker("df28db457d5734f5"),),
            surfaces=("notes",),
            keys=(("df28db457d5734f5",),),
        ),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior(CONTEXT_KEY)},
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
            methods=("diff", "ensure_available"),
            surfaces=(),
            code=1,
        ),
        {
            "dry_run": False,
            "availability": [
                ForgeUnavailable(
                    "'gh' CLI tool not found. Install it and ensure it is authenticated before running this script."
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
            methods=("diff", "ensure_available", "ensure_available", "diff_refs"),
            surfaces=(),
            code=1,
        ),
        {"dry_run": False, "refs": JsonFetch([], None)},
    ),
    Row(
        "FAILURE-gl-blank-sha-before-planning",
        review("gitlab", [fix(line=999)]),
        Expected(
            err=ERRORS["blank-head"],
            methods=("diff", "ensure_available", "ensure_available", "diff_refs"),
            surfaces=(),
            code=1,
        ),
        {
            "dry_run": False,
            "refs": JsonFetch(
                [
                    {
                        "base_commit_sha": "base1",
                        "head_commit_sha": "",
                        "start_commit_sha": "start1",
                    }
                ],
                None,
            ),
        },
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
        "FAILURE-gl-dry-budget-unanchored-members-keeps-sibling-status",
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
            anchors=(position("src/app/clients/api/__init__.py", 1),),
            bodies=(NEW_BODY,),
            code=1,
            err="WARNING: The composed inline discussion is 452 bytes, over the 200-byte GitLab body limit; skipping this delivery.\nWARNING: Skipping corroborating finding 'Added-line finding' \u2014 no line number to anchor its own discussion on.\nWARNING: Skipping corroborating finding 'Added-line finding' at src/edited.py:999 \u2014 line not found in diff.\n",
            out_lines=(
                "  1 inline discussion(s) captured.",
                "  2 finding(s) had a malformed position (see warnings above).",
                "  1 inline discussion(s) not delivered (see warnings above).",
            ),
            skipped=(
                "Skipping corroborating finding 'Added-line finding' \u2014 no line number to anchor its own discussion on.",
                "Skipping corroborating finding 'Added-line finding' at src/edited.py:999 \u2014 line not found in diff.",
            ),
        ),
        {"diff": CONTRACT_DIFF},
        (("gitlab", "discussion", 200),),
    ),
    Row(
        "FAILURE-gl-envelope-before-dedup-and-position",
        review("gitlab", [fix(end_line=940), CONTEXT]),
        Expected(
            anchors=(
                position("src/edited.py", 61, old_line=50, old_path="src/edited.py"),
            ),
            bodies=(CONTEXT_BODY + marker(CONTEXT_KEY),),
            err="WARNING: suggested-fix downgraded: foo.py:2 (range_not_in_diff)\nWARNING: The composed inline discussion is 227 bytes, over the 200-byte GitLab body limit; skipping this delivery.\n",
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


def test_github_summary_sha_before_composition_and_guard(posting, monkeypatch, capsys):
    def compose_after_sha(*args, **kwargs):
        assert capsys.readouterr().err == UNKNOWN_WARNING
        return compose.ComposedBody("\u754c" * 21846, 0, 0, 0, ())

    monkeypatch.setattr(compose, "compose_review_body", compose_after_sha)
    run = posting(review(sha=None), dry_run=False, head_status=1)
    assert run.code == 1
    assert run.err == (
        "post_review: The composed review body is 65538 bytes, over the "
        "65536-byte GitHub body limit; nothing was posted.\n"
    )
    assert run.out == ""
    assert run.head_calls == (("git", "rev-parse", "HEAD"),)
    assert run.requests == ()
    assert run.payload is None


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
            skipped=(
                "Skipping finding 'Skipped' at foo.py:99 \u2014 line not found in diff. Valid lines for this file: []",
            ),
            err="WARNING: Skipping finding 'Skipped' at foo.py:99 \u2014 line not found in diff. Valid lines for this file: []\n",
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
            skipped=(
                "Skipping finding 'GitLab budget' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: []",
            ),
            err="WARNING: Skipping finding 'GitLab budget' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: []\n"
            "WARNING: Skipped finding 'GitLab budget' at foo.py:999 not shown: the summary note reached the 1000000-byte GitLab body limit.\n",
            out_lines=(
                "MR summary note captured (dry-run).",
                "  1 skipped finding(s) not shown: the summary note reached the 1000000-byte GitLab body limit.",
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
            err="WARNING: Finding 'No-line bug' has no line number \u2014 skipping.\nWARNING: Finding 'Mystery bug' has no line number \u2014 skipping.\n",
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
            + "\n\n_[folded: 4794 more bytes; this review body reached the 65536-byte GitHub body limit]_"
            + FOOTER.replace("{count}", "0"),
            out_lines=(
                "  review_body folded by 4794 bytes: the review body reached the 65536-byte GitHub body limit.",
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
                "Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6] [group members: A, B]",
                "Skipping finding 'Small unrelated' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6]",
            ),
            err="WARNING: Skipping finding 'A' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6] [group members: A, B]\nWARNING: Skipping finding 'Small unrelated' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3, 4, 5, 6]\nWARNING: Skipped finding 'A' at foo.py:999 not shown: the review body reached the 65536-byte GitHub body limit.\nWARNING: Skipped finding 'B' at foo.py:3 not shown: the review body reached the 65536-byte GitHub body limit.\n",
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
            err="WARNING: Skipping finding 'T' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: []\n",
            surfaces=(),
        ),
        {"dry_run": False, "entries": prior()},
        composed="\u754c" * 333334,
    ),
    Row(
        "SUMMARY-cjk-omission-uses-bytes",
        review(
            findings=[
                finding(
                    line=999,
                    title="CJK skipped",
                    body="\u754c" * 2000,
                    omit=("end_line",),
                )
            ],
            review_body="\u754c" * 21000,
        ),
        Expected(
            summary_has=(
                "following 0 findings",
                "_1 of these 1 finding is not shown:",
                "\u754c" * 10,
            ),
            summary_lacks=("CJK skipped",),
            skipped=(
                "Skipping finding 'CJK skipped' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: []",
            ),
            err="WARNING: Skipping finding 'CJK skipped' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: []\nWARNING: Skipped finding 'CJK skipped' at foo.py:999 not shown: the review body reached the 65536-byte GitHub body limit.\n",
        ),
    ),
    Row(
        "SUMMARY-report-authored-heading-and-fence",
        [],
        Expected(
            summary_has=(
                "Summary prose\n## Not a section\nkept\n## Findings (finding text)\nalso kept",
            ),
            summary_lacks=("\n## Findings\n",),
        ),
        {"arguments": ()},
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
    if row.id in (
        "SUMMARY-report-authored-heading-and-fence",
        "SUMMARY-report-fold-closes-four-backtick-fence",
    ):
        source = (
            "## Summary\n\n````python\n" + "x" * 70000 + "\n\n## Findings\n"
            if row.id == "SUMMARY-report-fold-closes-four-backtick-fence"
            else "## Summary\n\nSummary prose\n## Not a section\nkept\n## Findings (finding text)\nalso kept\n\n## Findings\n\n### High\n"
        )
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
            "  1 of 3 finding(s) skipped inline (lines not in diff) \u2014 appended to review body.\n"
            in run.out
        )
        assert (
            "  2 skipped finding(s) not shown: the review body reached the 65536-byte GitHub body limit.\n"
            in run.out
        )
    if row.id == "SUMMARY-existing-summary-suppresses-guard-and-notices":
        assert "body limit" not in run.out


NOTICE = [
    Row(
        "NOTICE-gh-finding-order-fold-before-sha",
        review(
            findings=[
                fix(end_line=940),
                fix(line=999, end_line=999),
                finding(line=3, body="x" * 1000, omit=("end_line",)),
            ],
            sha=None,
        ),
        Expected(
            anchors=(GH_ANCHOR, {"path": "foo.py", "line": 3, "side": "RIGHT"}),
            body_has=(("Return two instead.",), ("_[folded:",)),
            skipped=(
                "suggested-fix downgraded: foo.py:2 (range_not_in_diff)",
                "Skipping finding 'Range bug' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3]",
                "suggested-fix downgraded: foo.py:999 (range_not_in_diff)",
            ),
            err="WARNING: suggested-fix downgraded: foo.py:2 (range_not_in_diff)\nWARNING: Skipping finding 'Range bug' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3]\nWARNING: suggested-fix downgraded: foo.py:999 (range_not_in_diff)\nWARNING: Inline body folded by 943 bytes at foo.py:3: this inline review comment reached the 200-byte GitHub body limit.\n"
            + UNKNOWN_WARNING,
            counts=(
                "  0 suggested fix(es) passed the apply-check.",
                "  2 suggested fix(es) downgraded to prose.",
            ),
            head_calls=1,
        ),
        {"diff": GH_INDENTED, "head_status": 1},
        (("github", "inline", 200),),
    ),
    Row(
        "NOTICE-gl-partition-before-inline-gate",
        review("gitlab", [fix(end_line=940), fix(line=999, end_line=999)]),
        Expected(
            anchors=(GL_ANCHOR,),
            bodies=(RANGE_PROSE,),
            skipped=(
                "Skipping finding 'Range bug' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3]",
                "suggested-fix downgraded: foo.py:999 (range_not_in_diff)",
                "suggested-fix downgraded: foo.py:2 (range_not_in_diff)",
            ),
            err="WARNING: Skipping finding 'Range bug' at foo.py:999 \u2014 line not found in diff. Valid lines for this file: [1, 2, 3]\nWARNING: suggested-fix downgraded: foo.py:999 (range_not_in_diff)\nWARNING: suggested-fix downgraded: foo.py:2 (range_not_in_diff)\n",
            counts=(
                "  0 suggested fix(es) passed the apply-check.",
                "  2 suggested fix(es) downgraded to prose.",
            ),
        ),
        {"diff": GL_INDENTED},
    ),
    Row(
        "NOTICE-dedup-before-fold-notice",
        review("gitlab", [{**NEW, "body": "x" * 1000001}]),
        Expected(surfaces=()),
        {"diff": CONTRACT_DIFF, "dry_run": False, "entries": prior("6471baa1c45bb13a")},
    ),
    Row(
        "NOTICE-gl-gate-fold-before-submit",
        review("gitlab", [fix(end_line=940, body="x" * 1000)]),
        Expected(
            anchors=(GL_ANCHOR,),
            body_has=(("_[folded:",),),
            counts=DOWN,
            err="WARNING: suggested-fix downgraded: foo.py:2 (range_not_in_diff)\nWARNING: Inline body folded by 800 bytes at foo.py:2: this inline discussion reached the 500-byte GitLab body limit.\n",
            surfaces=("notes", "discussions"),
        ),
        {"diff": GL_INDENTED, "dry_run": False},
        (("gitlab", "discussion", 500),),
    ),
]


@pytest.mark.parametrize("row", params(NOTICE))
def test_delivery_notices(row, posting, monkeypatch, capsys):
    if row.id == "NOTICE-gl-gate-fold-before-submit":
        from tests.support.forge import FakeGitLab

        submit = FakeGitLab.submit
        seen = []

        def after_notices(fake, request):
            if request.endpoint.endswith("/discussions"):
                notices = capsys.readouterr().err
                assert notices == expected.err
                seen.append(notices)
            return submit(fake, request)

        monkeypatch.setattr(FakeGitLab, "submit", after_notices)
        expected = row.expected
        row = Row(
            row.id,
            row.data,
            Expected(
                anchors=expected.anchors,
                body_has=expected.body_has,
                counts=expected.counts,
                surfaces=expected.surfaces,
            ),
            row.options,
            row.limits,
        )
        check(row, posting, monkeypatch)
        assert seen == [expected.err]
        return
    check(row, posting, monkeypatch)


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
            "arguments": (
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
            ),
        },
    )
    wrapped_inputs = []
    post_github = post.post_github

    def observe_wrapper(data, *args, **kwargs):
        wrapped_inputs.append(data)
        return post_github(data, *args, **kwargs)

    monkeypatch.setattr(post, "post_github", observe_wrapper)
    run = check(row, posting, monkeypatch)
    assert wrapped_inputs[0] == {
        "owner": "o",
        "repo": "r",
        "pr_number": 5,
        "sha": SHA,
        "platform": "github",
        "review_body": "",
        "findings": row.data,
    }
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
            methods=("diff", "ensure_available", "ensure_available", "diff_refs"),
            body_has=(("_[folded:",),),
            body_lacks=(("code-gauntlet-finding-key",),),
            err="WARNING: Inline body folded by 185 bytes at src/edited.py:61: this inline discussion reached the 500-byte GitLab body limit.\n",
        ),
        {"diff": CONTRACT_DIFF},
        (("gitlab", "discussion", 500),),
    )
    run = check(row, posting, monkeypatch)
    live = posting(row.data, diff=CONTRACT_DIFF, dry_run=False)
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
    assert list(dry.payload) == ["platform", "summary", "discussions", "skipped"]
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


MARKER = [
    pytest.param("short", id="MARKER-supplied-short-sha-without-head-read"),
    pytest.param("empty-head", id="MARKER-empty-successful-head-unmarkable-warning"),
    pytest.param("head", id="MARKER-absent-invalid-sha-local-head"),
    pytest.param("unknown", id="MARKER-head-failure-unmarkable-warning"),
    pytest.param(
        "embedded-separator", id="MARKER-embedded-unicode-separators-do-not-dedup"
    ),
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
                + '\n\n<!-- code-gauntlet-findings: {"version":"3.0","findings_count":999,"sha":"'
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
        assert find_finding_marker(body) is None
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
        if kind == "short":
            run = posting(review(platform, sha="abc1234"), dry_run=False)
            assert run.code == 0
            assert run.head_calls == ()
            assert run.err == ""
            assert run.requests[0].payload["body"] == (
                "### \u2694\ufe0f Code Gauntlet\n\nSummary\n\n---\n"
                "Generated by code-gauntlet | Reviewed up to: abc1234\n\n"
                '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":0,"sha":"abc1234"} -->'
            )
            padded = posting(review(platform, sha="  " + SHA + "  "))
            assert padded.head_calls == ()
            assert padded.err == ""
            assert (
                padded.payload["payload" if platform == "github" else "summary"]["body"]
                == EMPTY_SUMMARY
            )
        elif kind == "empty-head":
            run = posting(
                review(platform, sha=None), head="", head_status=0, dry_run=False
            )
            assert run.code == 0
            assert run.head_calls == (("git", "rev-parse", "HEAD"),)
            assert run.err == UNKNOWN_WARNING.replace("'unknown'", "''")
            assert run.requests[0].payload["body"] == (
                "### \u2694\ufe0f Code Gauntlet\n\nSummary\n\n---\n"
                "Generated by code-gauntlet | Reviewed up to: \n\n"
                '<!-- code-gauntlet-findings: {"version":"3.0","findings_count":0,"sha":""} -->'
            )
        elif kind == "head":
            for invalid in (None, "not-a-real-sha!!", 12345):
                data = review(platform, sha=invalid)
                if invalid is None:
                    del data["sha"]
                run = posting(data, head=" abc1234\n")
                assert run.head_calls == (("git", "rev-parse", "HEAD"),)
                assert run.err == ""
                body = run.payload["payload" if platform == "github" else "summary"][
                    "body"
                ]
                assert body == EMPTY_SUMMARY.replace(SHA, "abc1234")
        elif kind == "unknown":
            data = review(platform, [CONTEXT] if platform == "gitlab" else [], sha=None)
            run = posting(
                data,
                diff=CONTRACT_DIFF if platform == "gitlab" else "",
                head_status=1,
                dry_run=False,
            )
            assert run.code == 0
            assert run.err == UNKNOWN_WARNING
            assert tuple(call.method for call in run.fake.calls) == (
                (
                    "diff",
                    "ensure_available",
                    "ensure_available",
                    "diff_refs",
                    "submit",
                    "submit",
                )
                if platform == "gitlab"
                else ("diff", "ensure_available", "submit")
            )
            assert find_marker(run.requests[0].payload["body"])["sha"] == "unknown"
            if platform == "gitlab":
                assert run.requests[1].payload["body"] == CONTEXT_BODY
        else:
            if kind == "embedded-separator":
                sources = tuple(
                    (
                        "- \U0001f4a1 [LOW] `foo.py:99`: x" + separator + footer_line,
                        "- \U0001f4a1 [LOW] `foo.py:99`: x" + emitted + footer_line,
                    )
                    for separator, emitted in (
                        ("\u2028", "\u2028"),
                        ("\u0085", ""),
                        ("\x0c", ""),
                        ("\x1e", ""),
                    )
                )
            elif kind == "leading-separator":
                sources = ("\u2028" + footer_line,)
            elif kind == "current-foreign":
                sources = (
                    "Summary\n\n"
                    + footer_line
                    + "\n\n- `foo.py:99`: x Generated by code-gauntlet | Reviewed up to: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                )
            elif kind == "stale-footer":
                sources = (
                    "Some notes.\n\nGenerated by code-gauntlet | Reviewed up to: abc1234",
                )
            if kind != "embedded-separator":
                sources = tuple((source, source) for source in sources)
            for source, rendered_source in sources:
                run = posting(review(platform, review_body=source))
                assert run.code == 0
                assert run.err == ""
                body = run.payload["payload" if platform == "github" else "summary"][
                    "body"
                ]
                if kind == "current-foreign":
                    assert body == HEADER + source + "\n\n" + machine
                    assert sum(line == footer_line for line in body.split("\n")) == 1
                else:
                    assert body == HEADER + rendered_source + FOOTER.replace(
                        "{count}", "0"
                    )
                    assert sum(line == footer_line for line in body.split("\n")) == 1
                signal = find_marker(body)
                assert signal["sha"] == SHA
                assert signal["findings_count"] == 0


@pytest.fixture
def refuse_review_input(tmp_path, monkeypatch, capsys, forge_factory):
    def refuse(data, expected, *, arguments=(), regression=False):
        artifact = tmp_path / "post-review-payload.json"
        modes = ("live", "flag", "env") if regression else ("live", "flag")
        for mode in modes:
            forge_factory.calls.clear()
            if regression:
                artifact.write_bytes(b"sentinel artifact\n")
            platform = data.get("platform") if isinstance(data, dict) else None
            run = invoke_posting(
                tmp_path,
                monkeypatch,
                capsys,
                forge_factory,
                data,
                diff=GL_DIFF if platform == "gitlab" else GH_DIFF,
                dry_run=mode == "flag",
                environment_dry_run=mode == "env",
                configure_forge=regression,
                keep_existing_artifact=regression,
                arguments=arguments,
            )
            assert (run.code, run.out, run.err) == (1, "", expected)
            assert run.forge_factory_calls == ()
            assert run.head_calls == ()
            if regression:
                assert run.raw == b"sentinel artifact\n"
            else:
                assert run.raw is None

    return refuse


INPUT_FINDING = {"file": "foo.py", "line": 2, "title": "A", "body": "Body A"}


@pytest.mark.parametrize(
    "cases",
    [
        pytest.param(
            [
                (review(owner=value), "post_review: owner must be a string\n")
                for value in (7, False, None, list[object](), dict[str, object]())
            ],
            id="W-owner",
        ),
        pytest.param(
            [
                (review(repo=value), "post_review: repo must be a string\n")
                for value in (7, False, None, list[object](), dict[str, object]())
            ],
            id="W-repo",
        ),
        pytest.param(
            [
                (
                    review(pr_number=value),
                    "post_review: pr_number must be an integer or a string\n",
                )
                for value in (
                    True,
                    False,
                    5.0,
                    None,
                    list[object](),
                    dict[str, object](),
                )
            ],
            id="W-pr-number",
        ),
        pytest.param(
            [
                (
                    review(platform=value),
                    "post_review: platform must be a string or null\n",
                )
                for value in (
                    7,
                    False,
                    0,
                    list[object](),
                    dict[str, object](),
                    ["github"],
                )
            ],
            id="W-platform",
        ),
        pytest.param(
            [
                (
                    {**review(), "findings": value},
                    "post_review: findings must be an array\n",
                )
                for value in (dict[str, object](), None, "findings", 0, False)
            ],
            id="W-findings",
        ),
        pytest.param(
            [
                (
                    review(findings=[value]),
                    "post_review: findings[0] must be an object\n",
                )
                for value in (7, None, list[object](), False)
            ],
            id="F-first-element",
        ),
        pytest.param(
            [
                (
                    review(findings=[INPUT_FINDING, None], platform=None),
                    "post_review: findings[1] must be an object\n",
                )
            ],
            id="F-last-element",
        ),
        pytest.param(
            [
                (
                    review(
                        "gitlab",
                        [
                            {
                                **INPUT_FINDING,
                                "consolidation_key": "k",
                                "consolidation_primary": True,
                            },
                            {
                                **INPUT_FINDING,
                                "consolidation_key": "k",
                                "file": 7,
                                "line": 3,
                            },
                        ],
                    ),
                    "post_review: findings[1].file must be a string\n",
                )
            ],
            id="REG-gl-corroborator-file",
        ),
        pytest.param(
            [
                (
                    review(findings=[{**INPUT_FINDING, "line": value}]),
                    "post_review: findings[0].line must be an integer or null\n",
                )
                for value in ([2], dict[str, object](), True, False, 2.0, "2")
            ],
            id="REG-gh-line-list",
        ),
        pytest.param(
            [
                (
                    review(findings=[{**INPUT_FINDING, "file": value, "line": line}]),
                    "post_review: findings[0].file must be a string\n",
                )
                for value in (None, list[object](), dict[str, object](), 0, False)
                for line in (2,)
            ],
            id="F-file",
        ),
        pytest.param(
            [
                (
                    review(findings=[{**INPUT_FINDING, "end_line": value}]),
                    "post_review: findings[0].end_line must be an integer or null\n",
                )
                for value in ([3], dict[str, object](), True, False, 3.0, "3")
            ]
            + [
                (
                    review(
                        findings=[
                            INPUT_FINDING,
                            {
                                **INPUT_FINDING,
                                "line": 999,
                                "end_line": [3],
                                "consolidation_key": "k",
                            },
                        ]
                    ),
                    "post_review: findings[1].end_line must be an integer or null\n",
                )
            ],
            id="F-end-line",
        ),
        pytest.param(
            [
                (
                    review(findings=[{**INPUT_FINDING, "consolidation_key": value}]),
                    "post_review: findings[0].consolidation_key must be a string or null\n",
                )
                for value in (["k"], dict[str, object](), 7, 0, False)
            ],
            id="F-key",
        ),
    ],
)
def test_invalid_review_input(cases, refuse_review_input, request):
    for data, expected in cases:
        refuse_review_input(
            data, expected, regression=request.node.callspec.id.startswith("REG-")
        )


@pytest.mark.parametrize(
    "cases",
    [
        pytest.param(
            [
                (
                    {"repo": "r", "review_body": [], "findings": [{"line": []}]},
                    ["--report", "missing-report.md"],
                    "post_review: Report file not found: missing-report.md\n",
                )
            ],
            id="ORDER-report",
        ),
        pytest.param(
            [
                (
                    {"repo": 7, "findings": [7]},
                    [],
                    "post_review: Missing required field in findings JSON: 'owner'\n",
                ),
                (
                    {"owner": 7, "findings": [7]},
                    [],
                    "post_review: Missing required field in findings JSON: 'repo'\n",
                ),
                (
                    {"owner": 7, "repo": 7, "findings": [7]},
                    [],
                    "post_review: Missing required field in findings JSON: 'pr_number'\n",
                ),
            ],
            id="ORDER-required",
        ),
        pytest.param(
            [
                (
                    review(platform="BitBucket", findings=[{"line": [2]}]),
                    [],
                    "post_review: Unsupported platform: 'bitbucket'. Use 'github' or 'gitlab'.\n",
                )
            ],
            id="ORDER-platform",
        ),
        pytest.param(
            [
                (
                    review(findings=[{"file": 7, "line": True}, {"file": 7}]),
                    [],
                    "post_review: findings[0].file must be a string\n",
                ),
                (
                    review(findings=[{"line": [2]}]),
                    [],
                    "post_review: findings[0].line must be an integer or null\n",
                ),
                (
                    review(
                        findings=[
                            {"line": True, "end_line": True, "consolidation_key": 7}
                        ]
                    ),
                    [],
                    "post_review: findings[0].line must be an integer or null\n",
                ),
                (
                    review(findings=[{"end_line": True, "consolidation_key": 7}]),
                    [],
                    "post_review: findings[0].end_line must be an integer or null\n",
                ),
                (
                    review(owner=7, repo=7, pr_number=True, platform="bitbucket"),
                    [],
                    "post_review: owner must be a string\n",
                ),
                (
                    review(repo=7, pr_number=True, platform="bitbucket"),
                    [],
                    "post_review: repo must be a string\n",
                ),
                (
                    review(pr_number=True, platform="bitbucket"),
                    [],
                    "post_review: pr_number must be an integer or a string\n",
                ),
            ],
            id="ORDER-first-field",
        ),
        pytest.param(
            [
                (
                    value,
                    [],
                    "post_review: Findings JSON must be an object or an array.\n",
                )
                for value in (7, "root", None, True)
            ],
            id="ORDER-scalar-root",
        ),
    ],
)
def test_review_input_error_precedence(
    cases, refuse_review_input, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    for data, arguments, expected in cases:
        refuse_review_input(data, expected, arguments=arguments)


def test_review_input_defaults_and_unknown_fields(posting):
    for platform in ("github", "gitlab"):
        data = review(platform)
        del data["findings"]
        run = posting(data)
        assert run.code == 0
        assert run.err == ""
        assert (
            run.payload["payload" if platform == "github" else "summary"]["body"]
            == EMPTY_SUMMARY
        )

    data = review(
        findings=[
            {
                **INPUT_FINDING,
                "future": {"nested": [1, {"x": True}]},
                "line_start": "canonical",
                "consolidation_key": "k",
                "consolidation_primary": "yes",
            }
        ],
        future_wrapper={"version": 7},
    )
    run = posting(data, diff=GH_DIFF)
    assert run.code == 0
    assert run.err == ""
    assert run.payload["payload"]["comments"] == [
        {
            "path": "foo.py",
            "line": 2,
            "side": "RIGHT",
            "body": "**\U0001f4a1 [LOW] A**\n\nBody A" + TRAILER,
        }
    ]


def test_review_input_overrides_and_report(posting, tmp_path):
    report = tmp_path / "report.md"
    report.write_text(
        "# Review\n\n## Summary\n\nFrom report\n\n## Findings\n", encoding="utf-8"
    )
    arguments = (
        "--owner",
        "o",
        "--repo",
        "r",
        "--pr-number",
        "5",
        "--platform",
        "github",
        "--sha",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--report",
        str(report),
    )
    for data in (
        [],
        review(
            owner=[],
            repo={},
            pr_number=True,
            platform=["github"],
            sha=7,
            review_body=[],
        ),
    ):
        run = posting(data, arguments=arguments)
        assert run.code == 0
        assert run.err == ""
        assert run.head_calls == ()
        assert run.payload["endpoint"] == "repos/o/r/pulls/5/reviews"
        assert run.payload["payload"][
            "body"
        ] == HEADER + "From report" + FOOTER.replace("{count}", "0")
    run = posting(review(), arguments=("--report", "missing-report.md"))
    assert run.code == 0
    assert run.err == ""
    assert run.payload["payload"]["body"] == EMPTY_SUMMARY


def test_review_input_accepted_scalar_values(posting):
    for number, endpoint in (
        ("5", "repos/o/r/pulls/5/reviews"),
        ("legacy-number", "repos/o/r/pulls/legacy-number/reviews"),
    ):
        run = posting(review(pr_number=number))
        assert run.code == 0
        assert run.err == ""
        assert run.payload["endpoint"] == endpoint
    run = posting(review(owner="", repo=""))
    assert run.code == 0
    assert run.err == ""
    assert run.payload["endpoint"] == "repos///pulls/5/reviews"
    for end in (0, -1, None):
        run = posting(
            review(findings=[{**INPUT_FINDING, "end_line": end}]), diff=GH_DIFF
        )
        assert run.code == 0
        assert run.err == ""
        assert run.payload["payload"]["comments"] == [
            {
                "path": "foo.py",
                "line": 2,
                "side": "RIGHT",
                "body": "**\U0001f4a1 [LOW] A**\n\nBody A" + TRAILER,
            }
        ]
    run = posting(
        review(
            findings=[
                {**INPUT_FINDING, "consolidation_key": ""},
                {
                    **INPUT_FINDING,
                    "title": "B",
                    "body": "Body B",
                    "consolidation_key": "",
                },
            ]
        ),
        diff=GH_DIFF,
    )
    assert run.code == 0
    assert run.err == ""
    assert run.payload["payload"]["comments"] == [
        {
            "path": "foo.py",
            "line": 2,
            "side": "RIGHT",
            "body": "**\U0001f4a1 [LOW] A**\n\nBody A" + TRAILER,
        },
        {
            "path": "foo.py",
            "line": 2,
            "side": "RIGHT",
            "body": "**\U0001f4a1 [LOW] B**\n\nBody B" + TRAILER,
        },
    ]
    run = posting(
        review(findings=[{**INPUT_FINDING, "suggested_fix_code": ""}]), diff=GH_DIFF
    )
    assert run.code == 0
    assert run.err == "WARNING: suggested-fix downgraded: foo.py:2 (empty)\n"

    for platform in ("github", "gitlab"):
        run = posting(review(platform, [{"line": 2, "title": "A", "body": "Body A"}]))
        assert run.code == 0
        assert (
            run.err
            == "WARNING: Skipping finding 'A' at ?:2 \u2014 line not found in diff. Valid lines for this file: []\n"
        )
        assert (
            "?:2"
            in run.payload["payload" if platform == "github" else "summary"]["body"]
        )

    unanchored_group = review(
        "gitlab",
        [
            {
                "line": 2,
                "title": "A",
                "body": "Body A",
                "consolidation_key": "missing-file",
                "consolidation_primary": True,
            },
            {
                "line": 3,
                "title": "B",
                "body": "Body B",
                "consolidation_key": "missing-file",
            },
        ],
    )
    run = posting(unanchored_group, diff=GL_DIFF)
    assert run.code == 0
    assert run.err == (
        "WARNING: Skipping finding 'A' at ?:2 — line not found in diff. "
        "Valid lines for this file: [] [group members: A, B]\n"
    )
    assert "#### `?:3`\n\n**" in run.payload["summary"]["body"]

    rejected_group = review(
        "gitlab",
        [
            {
                **INPUT_FINDING,
                "consolidation_key": "missing-file",
                "consolidation_primary": True,
            },
            {
                "line": 3,
                "title": "B",
                "body": "Body B",
                "consolidation_key": "missing-file",
            },
        ],
    )
    run = posting(
        rejected_group,
        diff=GL_DIFF,
        dry_run=False,
        submissions={
            "notes": [PostResult({}, None, None)],
            "discussions": [PostResult(None, "denied", None)],
        },
    )
    assert run.code == 1
    assert run.err == (
        "WARNING: Skipping finding 'A' at foo.py:2 — GitLab rejected the inline "
        "discussion.\ndenied\n"
        "WARNING: Skipping corroborating finding 'B' at ?:3 — line not found in diff.\n"
        "post_review: all 1 finding(s) attempted this run were not delivered — "
        "nothing new was posted inline. The MR summary note is on the MR; rerunning "
        "retries the inline comments without duplicating what is already there.\n"
    )

    for primary, title in (
        (True, "B"),
        (None, "A"),
        ("yes", "B"),
    ):
        run = posting(
            review(
                findings=[
                    {**INPUT_FINDING, "consolidation_key": "k"},
                    {
                        **INPUT_FINDING,
                        "title": "B",
                        "consolidation_key": "k",
                        "consolidation_primary": primary,
                    },
                ]
            ),
            diff=GH_DIFF,
        )
        assert run.code == 0
        assert run.err == ""
        assert run.payload["payload"]["comments"][0]["body"].startswith(
            "**\U0001f4a1 [LOW] " + title + "**\n\nBody A"
        )


def test_review_input_null_and_absent_fields(posting, monkeypatch):
    for platform in ("github", "gitlab"):
        for fields in ({}, {"line": None, "end_line": None, "consolidation_key": None}):
            run = posting(
                review(platform, [{"title": "A", "body": "Body A", **fields}])
            )
            assert run.code == 0
            assert (
                run.err == "WARNING: Finding 'A' has no line number \u2014 skipping.\n"
            )
            assert (
                "#### `?`\n\n**\U0001f4a1 [LOW] A**"
                in run.payload["payload" if platform == "github" else "summary"]["body"]
            )
    monkeypatch.setattr(
        post, "origin_remote", lambda: parse_remote("git@github.com:o/r")
    )
    for fields in ({}, {"platform": None}, {"platform": ""}):
        data = review()
        del data["platform"]
        run = posting({**data, **fields})
        assert run.code == 0
        assert run.err == ""
    for body, rendered in ((["Summary"], "['Summary']"), (7, "7")):
        run = posting(review(review_body=body))
        assert run.code == 0
        assert run.err == ""
        assert run.payload["payload"]["body"] == HEADER + rendered + FOOTER.replace(
            "{count}", "0"
        )
    run = posting(
        review(findings=[{**INPUT_FINDING, "suggested_fix_code": 7}]), diff=GH_DIFF
    )
    assert run.code == 0
    assert run.err == "WARNING: suggested-fix downgraded: foo.py:2 (non_string)\n"
