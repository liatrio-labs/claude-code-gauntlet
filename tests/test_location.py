"""Location authority is decided at the source and resolver boundaries."""

import json
import os

import pytest
from gauntlet import proc
from gauntlet.diff import OldSpan, annotate_diff
from gauntlet.location import (
    SourceAdapter,
    canonical_range,
    load_context_map,
    location_allows,
    resolve_location,
)

TEXT = "perform_action(value)"
REQUEST = {"file": "src/item", "line_start": 1, "line_end": 99, "anchor": TEXT}


@pytest.mark.parametrize("line", [TEXT, f"prefix {TEXT} suffix"])
@pytest.mark.parametrize(
    "head,claimed,entry,expected",
    [
        (1, 1, None, (1, 1, "confirmed", "claimed")),
        (2, 1, None, (1, 1, "confirmed", "claimed")),
        (2, 1, ("src/other", "head", 2), (1, 1, "confirmed", "claimed")),
        (2, 1, ("src/item", "head", 2), (2, 2, "relocated", "context_map")),
        (2, 1, ("src/item", "head", 1), (1, 1, "relocated", "context_map")),
        (2, 1, ("src/item", "old", 9), (None, None, "unconfirmed", "ambiguous")),
        (1, 1, ("src/item", "head", 9), (1, 1, "confirmed", "claimed")),
    ],
)
def test_claimed_span_tie_rules(line, head, claimed, entry, expected):
    mapping = (
        {}
        if entry is None
        else {claimed: dict(zip(("file", "side", "line"), entry, strict=True))}
    )
    result = resolve_location(
        {**REQUEST, "line_start": claimed}, [line] * head, mapping, (), ()
    )
    assert (result.start, result.end, result.status, result.reason) == expected


@pytest.mark.parametrize("line", [TEXT, f"prefix {TEXT} suffix"])
@pytest.mark.parametrize(
    "head,entry,hunks,expected",
    [
        ([1, 0, 1], 3, [(1, 1)], (3, "context_map")),
        ([0, 0, 1], None, [], (3, "unique")),
        ([1, 0, 1], None, [(3, 3)], (3, "unique_hunk")),
        ([1, 0, 1], 2, [(3, 3), (2, 3)], (3, "unique_hunk")),
        ([1, 0, 1], None, [(1, 3)], (None, "ambiguous")),
        ([1, 0, 1], None, [], (None, "ambiguous")),
    ],
)
def test_map_then_unique_then_hunk_order(line, head, entry, hunks, expected):
    mapping = (
        {}
        if entry is None
        else {99: {"file": "src/item", "side": "head", "line": entry}}
    )
    result = resolve_location(
        {**REQUEST, "line_start": 99},
        [line if present else "unrelated" for present in head],
        mapping,
        hunks,
        (),
    )
    assert (result.start, result.reason) == expected
    assert result.status == ("relocated" if result.start else "unconfirmed")


@pytest.mark.parametrize(
    "anchor,head,start,end,reason",
    [
        (
            " 17\tN7 +|  first(value)\n18\u2192N8  |\tsecond(value)\n",
            ["first(value)  ", "  second(value)"],
            1,
            2,
            "claimed",
        ),
        (
            "first(value)\r\nsecond(value)\r\n",
            [" first(value)", "second(value) "],
            1,
            2,
            "claimed",
        ),
        (
            "first(  value)\nsecond(value)",
            ["first( value)", "second(value)"],
            None,
            None,
            "not_found",
        ),
        ("\nfirst(value)\n\n", ["", "first(value)", ""], 1, 3, "claimed"),
        ("N7 +|" + TEXT, ["N7 +|" + TEXT], 1, 1, "claimed"),
        ("O77777 -|short", ["O77777 -|short"], None, None, "deleted_side"),
        (" 17\t" + TEXT, [" 17\t" + TEXT], 1, 1, "claimed"),
        ("N7 +|" + TEXT, ["N7 +|" + TEXT, TEXT], None, None, "ambiguous_presentation"),
        ("17\t18\t" + TEXT, [TEXT], None, None, "not_found"),
        ("N7 +|N8 +|" + TEXT, [TEXT], None, None, "not_found"),
        ("+" + TEXT, [TEXT], None, None, "not_found"),
        ("`" + TEXT + "`", [TEXT], None, None, "not_found"),
        ("17: " + TEXT, [TEXT], None, None, "not_found"),
        (
            "first\rvalue\u2028next\u0085final",
            ["first\rvalue\u2028next\u0085final"],
            1,
            1,
            "claimed",
        ),
        ("N1 +|" + "\U0001f600" * 400, ["\U0001f600" * 400], 1, 1, "claimed"),
    ],
)
def test_quote_presentation_and_exact_whitespace(anchor, head, start, end, reason):
    result = resolve_location({**REQUEST, "anchor": anchor}, head, {}, (), ())
    assert (result.start, result.end, result.reason) == (start, end, reason)


@pytest.mark.parametrize(
    "anchor,head,claimed,hunks,expected",
    [
        ("abcde fghijkl", ["prefix abcde fghijkl suffix"], 99, [], (1, "unique")),
        (
            "abcde fghijk",
            ["prefix abcde fghijk suffix"],
            99,
            [],
            (None, "invalid_anchor"),
        ),
        (TEXT, [TEXT + TEXT], 99, [], (1, "unique")),
        (TEXT, [TEXT, "wrapper(" + TEXT + ")"], 99, [], (None, "ambiguous")),
        (TEXT, [TEXT, "wrapper(" + TEXT + ")"], 99, [(2, 2)], (2, "unique_hunk")),
        (
            "first(value)\nsecond(value)",
            ["prefix first(value)", "second(value) suffix"],
            99,
            [],
            (None, "not_found"),
        ),
        (
            "first(value)\nsecond(value)",
            ["first(value)", "second(value)"] * 2,
            99,
            [(1, 1), (2, 4)],
            (3, "unique_hunk"),
        ),
    ],
)
def test_one_line_fragment_matching(anchor, head, claimed, hunks, expected):
    result = resolve_location(
        {**REQUEST, "anchor": anchor, "line_start": claimed}, head, {}, hunks, ()
    )
    assert (result.start, result.reason) == expected
    if result.start is not None:
        assert result.end == result.start + len(anchor.split("\n")) - 1


@pytest.mark.parametrize("anchor", [TEXT, "first(value)\nsecond(value)"])
@pytest.mark.parametrize("explicit", [False, True])
def test_deleted_quote_has_no_head_location(anchor, explicit):
    lines = [f"  {line}  " for line in anchor.split("\n")]
    if len(lines) == 1:
        lines = [f"prefix {lines[0]} suffix"]
    mapping = {1: {"file": "src/item", "side": "old", "line": 7}}
    quote = (
        "\n".join(f"O{7 + i} -|{line}" for i, line in enumerate(lines))
        if explicit
        else anchor
    )
    result = resolve_location(
        {**REQUEST, "anchor": quote}, lines, mapping, (), (OldSpan(7, tuple(lines)),)
    )
    assert (result.start, result.end, result.status, result.reason) == (
        None,
        None,
        "unconfirmed",
        "deleted_side",
    )


def test_unrelated_deleted_text_does_not_veto_head_quote():
    result = resolve_location(
        REQUEST,
        [TEXT],
        {1: {"file": "src/item", "side": "old", "line": 7}},
        (),
        (OldSpan(7, ("different deleted text",)),),
    )
    assert (result.start, result.reason) == (1, "claimed")


@pytest.mark.parametrize(
    "anchor",
    [
        None,
        "",
        " \t",
        7,
        [],
        "\n" * 4,
        "a" * 401,
        "\U0001f600" * 401,
        "\n".join([TEXT] * 5),
        "no matching source text",
    ],
)
def test_bad_or_missing_anchor_is_unconfirmed(anchor):
    request = {**REQUEST, "anchor": anchor}
    before = dict(request)
    result = resolve_location(request, [TEXT], {}, [(1, 1)], ())
    assert (result.start, result.end, result.status) == (None, None, "unconfirmed")
    assert request == before


@pytest.mark.parametrize("claimed", [True, 0, -1, "1", 1.0, 2**53])
def test_invalid_hint_does_not_prevent_unique_resolution(claimed):
    result = resolve_location({**REQUEST, "line_start": claimed}, [TEXT], {}, (), ())
    assert (result.start, result.status, result.reason) == (1, "relocated", "unique")


@pytest.mark.parametrize("target_kind", ["pr", "mr", "branch", "local"])
def test_source_adapter_captures_revision_and_caches_source(
    tmp_path, monkeypatch, target_kind
):
    head = "abcd" + "0" * 36
    calls = []
    (tmp_path / "src").mkdir()
    (tmp_path / "src/item").write_bytes(
        b"dirty worktree\r\nsecond\rcontent\xe2\x80\xa8tail\n"
    )

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["cwd"] == str(tmp_path)
        if argv[1] == "rev-parse":
            return proc.CompletedProcess(argv, 0, head.encode() + b"\n", b"")
        if argv[1] == "ls-tree":
            assert argv[3:] == [head, "--", ":(literal)src/item"]
            return proc.CompletedProcess(
                argv, 0, b"100644 blob " + b"1" * 40 + b"\tsrc/item\0", b""
            )
        assert argv == ["git", "cat-file", "blob", "1" * 40]
        return proc.CompletedProcess(
            argv, 0, b"committed source\r\nsecond\rcontent\xe2\x80\xa8tail\n", b""
        )

    monkeypatch.setattr(proc, "run_bytes", run)
    adapter = SourceAdapter.capture(tmp_path, target_kind, "abcd")
    first = "dirty worktree" if target_kind == "local" else "committed source"
    assert adapter.read("src/item") == (first, "second\rcontent\u2028tail")
    count = len(calls)
    (tmp_path / "src/item").write_bytes(b"changed again")
    assert adapter.read("src/item")[0] == first
    assert len(calls) == count
    assert adapter.captured_head == head
    assert sum(argv[1] == "rev-parse" for argv in calls) == 1


@pytest.mark.parametrize(
    "mode,content",
    [
        (b"120000", b"symlink target"),
        (b"160000", b"submodule"),
        (b"040000", b"tree"),
        (b"100644", b"bad\xff"),
        (b"100755", b"binary\0text"),
        (None, b""),
    ],
)
def test_committed_source_never_falls_back_to_worktree(
    tmp_path, monkeypatch, mode, content
):
    (tmp_path / "item").write_text(TEXT, encoding="utf-8")

    def run(argv, **kwargs):
        output = b"" if mode is None else mode + b" blob " + b"1" * 40 + b"\titem\0"
        return proc.CompletedProcess(
            argv, 0, output if argv[1] == "ls-tree" else content, b""
        )

    monkeypatch.setattr(proc, "run_bytes", run)
    adapter = SourceAdapter(tmp_path, "pr", "a" * 40)
    assert adapter.read("item") is None
    result = resolve_location(REQUEST, adapter.read("item"), {}, (), ())
    assert result.reason == "unreadable_source"


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "directory",
        "binary",
        "invalid",
        "file_link",
        "parent_link",
        "traversal",
        "absolute",
        "unreadable",
    ],
)
def test_local_source_refuses_nonregular_or_unreadable_paths(
    tmp_path, monkeypatch, request, kind, portable
):
    if portable:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    if kind in ("file_link", "parent_link"):
        request.getfixturevalue("symlink_or_skip")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir/file").write_bytes(b"valid source text")
    file = "dir/file"
    if kind == "missing":
        file = "absent"
    elif kind == "directory":
        file = "dir"
    elif kind in ("binary", "invalid"):
        (tmp_path / file).write_bytes(b"a\0b" if kind == "binary" else b"a\xff")
    elif kind == "file_link":
        (tmp_path / "link").symlink_to(tmp_path / file)
        file = "link"
    elif kind == "parent_link":
        (tmp_path / "link").symlink_to(tmp_path / "dir", target_is_directory=True)
        file = "link/file"
    elif kind == "traversal":
        file = "dir/../dir/file"
    elif kind == "absolute":
        file = str(tmp_path / file)
    elif kind == "unreadable":
        monkeypatch.setattr(
            os,
            "open",
            lambda *a, **kw: (_ for _ in ()).throw(PermissionError("denied")),
        )
    assert SourceAdapter(tmp_path, "local", "a" * 40).read(file) is None


@pytest.mark.parametrize("revision", [b"ffff" + b"0" * 36, b"abcd", b"\xff", b""])
def test_revision_mismatch_disables_source_reads(tmp_path, monkeypatch, revision):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return proc.CompletedProcess(argv, 0, revision, b"")

    monkeypatch.setattr(proc, "run_bytes", run)
    adapter = SourceAdapter.capture(tmp_path, "local", "abcd")
    assert adapter.captured_head is None
    assert adapter.read("file") is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,start,end,allowed",
    [
        ("confirmed", 1, 4, True),
        ("relocated", 2, 2, True),
        ("unconfirmed", 1, 1, False),
        (None, 1, 1, False),
        ("forged", 1, 1, False),
        ("confirmed", 1, 5, False),
        ("confirmed", True, 1, False),
        ("confirmed", 2, 1, False),
    ],
)
def test_location_rights_require_canonical_range(status, start, end, allowed):
    finding = {"location_status": status, "line_start": start, "line_end": end}
    assert location_allows(finding, "inline") == allowed
    assert location_allows(finding, "body")
    assert (canonical_range(finding) is not None) == allowed


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "version",
        "sha",
        "context_sha256",
        "diff_sha256",
        "status",
        "entry_side",
        "entry_line",
        "entry_file",
        "entry_key",
        "entry_extra",
        "missing",
        "json",
    ],
)
def test_context_map_rejects_invalid_identity_or_entries(tmp_path, invoke, damage):
    from tests.support.phase2 import SHA, write_context_inputs

    diff = b"--- a/item\n+++ b/item\n@@ -1 +1 @@\n-old source text\n+new source text\n"
    output_dir = tmp_path / "out"
    write_context_inputs(output_dir, b"rules\n", diff)
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(output_dir), "--head-sha", SHA],
        tmp_path,
        b"triage",
    )
    assert result.returncode == 0
    context_path = output_dir / f"code-gauntlet-context-{SHA}.md"
    map_path = output_dir / f"code-gauntlet-context-map-{SHA}.json"
    raw = json.loads(map_path.read_bytes())
    if damage in ("version", "sha", "context_sha256", "diff_sha256", "status"):
        raw[damage] = "wrong"
    elif damage == "entry_key":
        raw["entries"]["01"] = next(iter(raw["entries"].values()))
    elif damage and damage.startswith("entry_"):
        field = damage.removeprefix("entry_")
        next(iter(raw["entries"].values()))[field] = {
            "side": "wrong",
            "line": True,
            "file": "",
            "extra": 1,
        }[field]
    map_path.write_text(json.dumps(raw) if damage != "json" else "{", encoding="utf-8")
    if damage == "missing":
        map_path = output_dir / "missing.json"
    loaded = load_context_map(map_path, context_path, diff, SHA)
    if damage is None:
        assert dict(loaded.entries) == {
            int(key): value for key, value in raw["entries"].items()
        }
        assert loaded.diagnostic is None
    else:
        assert not loaded.entries
        assert loaded.diagnostic


def test_diff_walker_exposes_file_specific_hunks_and_old_spans():
    patch = b"--- a/item\n+++ b/item\n@@ -7,3 +7,3 @@\n context value\n-old deleted value\n+new value here\n tail value\n"
    facts = annotate_diff(patch, policy="git-prefixed")
    assert facts.hunks == {"item": ((7, 9),)}
    assert facts.old_spans == {
        "item": (OldSpan(7, ("context value", "old deleted value", "tail value")),)
    }
    result = resolve_location(
        {**REQUEST, "file": "item", "line_start": 5, "anchor": "old deleted value"},
        ["old deleted value"],
        facts.entries,
        facts.hunks["item"],
        facts.old_spans["item"],
    )
    assert result.reason == "deleted_side"
