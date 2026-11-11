"""Bounded work at the snapshot-to-fact-card boundary."""

from unittest.mock import Mock

import pytest
from gauntlet import fact_cards
from gauntlet.diff import annotate_diff
from gauntlet.location import SourceAdapter

from tests.support.git_diff import git, init_git_repo


@pytest.fixture
def scan_snapshot(tmp_path, monkeypatch):
    def scan(library, changed="target(value)"):
        init_git_repo(tmp_path, monkeypatch)
        (tmp_path / "changed").write_text(changed + "\n", encoding="utf-8")
        (tmp_path / "library").write_text(library, encoding="utf-8")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-qm", "snapshot")
        diff = (
            "diff --git a/changed b/changed\n--- a/changed\n+++ b/changed\n"
            f"@@ -1 +1 @@\n-old\n+{changed}\n"
        ).encode()
        return fact_cards.build_fact_cards(
            SourceAdapter(tmp_path, "local", None), annotate_diff(diff), len(diff)
        )

    return scan


@pytest.mark.parametrize(
    "oversize", ["x" * (3 * 1024 * 1024), "界" * 2048], ids=["minified", "multibyte"]
)
def test_call_scan_skips_oversize_lines_before_regex(
    scan_snapshot, monkeypatch, oversize
):
    monkeypatch.setattr(fact_cards, "MAX_FILE_BYTES", 4 * 1024 * 1024)
    pattern = fact_cards._CALL

    def bounded_matches(line):
        assert len(line.encode("utf-8")) <= fact_cards.MAX_CALL_LINE_BYTES
        return pattern.finditer(line)

    monkeypatch.setattr(
        fact_cards, "_CALL", Mock(wraps=pattern, finditer=bounded_matches)
    )
    facts = scan_snapshot(oversize + "\npublic target(value) {\n  return value\n}\n")
    assert facts["scan"]["selected_files"] == 2
    assert facts["scan"]["oversize_lines"] == 1
    assert facts["scan"]["oversize_files"] == 0
    assert facts["scan_limited"]
    assert any(
        card["kind"] == "callee_candidate" and card["line_start"] == 2
        for card in facts["cards"]
    )


def test_call_scan_does_no_regex_work_without_wanted_names(scan_snapshot, monkeypatch):
    pattern = fact_cards._CALL
    monkeypatch.setattr(
        fact_cards,
        "_CALL",
        Mock(
            wraps=pattern,
            finditer=Mock(side_effect=AssertionError("no call names to look for")),
        ),
    )
    facts = scan_snapshot("public target(value) {\n  return value\n}\n", changed="42")
    assert facts["scan"]["selected_files"] == 2
    assert not facts["scan_limited"]


def test_call_scan_checks_deadline_before_each_line(scan_snapshot, monkeypatch):
    pattern = fact_cards._CALL
    clock = [0.0]
    monkeypatch.setattr(fact_cards.time, "monotonic", lambda: clock[0])

    def expire_after_first_line(line):
        assert not line.startswith("public target"), "scanned after deadline"
        if line == "first(value)":
            clock[0] = fact_cards.MAX_SCAN_SECONDS + 1
        return pattern.finditer(line)

    monkeypatch.setattr(
        fact_cards, "_CALL", Mock(wraps=pattern, finditer=expire_after_first_line)
    )
    facts = scan_snapshot("first(value)\npublic target(value) {\n  return value\n}\n")
    assert facts["scan"]["selected_files"] == 2
    assert clock[0] > fact_cards.MAX_SCAN_SECONDS
    assert facts["scan"]["deadline_reached"]
    assert facts["scan_limited"]
    assert not facts["cards"]


@pytest.mark.parametrize("prefix", ["if", "consider", "for item in"])
def test_repeated_invocation_shapes_do_not_introduce_a_callee(scan_snapshot, prefix):
    # Other callees teach the prefix's usage pattern even when only target is wanted.
    library = (
        f"{prefix} other(value):\n  consume(value)\n" * 5
        + f"{prefix} target(value):\n  consume(value)\n"
        + "def target(value):\n  return value\n"
    )
    facts = scan_snapshot(library)
    callees = [c for c in facts["cards"] if c["kind"] == "callee_candidate"]
    assert len(callees) == 1
    assert callees[0]["text"] == "target(…)"
    assert callees[0]["line_start"] == 13


def test_invocation_shapes_share_variable_slots_without_language_keywords(
    scan_snapshot,
):
    library = "".join(
        f"through {name} from target(value):\n  consume(value)\n"
        for name in ("left", "right", "third")
    )
    facts = scan_snapshot(library + "def target(value):\n  return value\n")
    callees = [c for c in facts["cards"] if c["kind"] == "callee_candidate"]
    assert len(callees) == 1
    assert callees[0]["text"] == "target(…)"
