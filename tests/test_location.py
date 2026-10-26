"""Location authority is decided at the source and resolver boundaries."""

import pytest
from gauntlet.diff import OldSpan, annotate_diff
from gauntlet.location import resolve_location

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
