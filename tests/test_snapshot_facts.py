"""Snapshot boundaries reject races and keep lexical facts close to the change."""

import os
import time

import pytest
from gauntlet import fact_cards, location
from gauntlet.diff import annotate_diff
from gauntlet.location import SourceAdapter

from tests.support.git_diff import git, init_git_repo


@pytest.mark.parametrize("change", ["size_before", "size_during", "mtime_during"])
def test_snapshot_read_discards_a_file_changed_since_listing(
    tmp_path, monkeypatch, change
):
    init_git_repo(tmp_path, monkeypatch)
    path = tmp_path / "source"
    path.write_bytes(b"original\n")
    git(tmp_path, "add", ".")
    source = SourceAdapter.capture(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    if change == "size_before":
        path.write_bytes(b"larger content\n")
    else:
        original_stat = location.os.fstat
        calls = 0

        def change_after_read(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                if change == "size_during":
                    path.write_bytes(b"larger content\n")
                else:
                    stamp = path.stat()
                    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
            return original_stat(fd)

        monkeypatch.setattr(location.os, "fstat", change_after_read)
    result = source.read_snapshot(inventory.files, deadline=time.monotonic() + 5)
    assert result.files == {} and result.unreadable == 1


@pytest.mark.parametrize("call_limit", [1, 32])
def test_callee_cards_suppress_widespread_tokens_and_prefer_nearby_definitions(
    tmp_path, monkeypatch, call_limit
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "zone").mkdir()
    (tmp_path / "far").mkdir()
    calls = [f"branch{n}(value)" for n in range(40)] + [
        "alpha_target(value)",
        "z_target(value)",
    ]
    (tmp_path / "zone/source").write_text("\n".join(calls) + "\n", encoding="utf-8")
    (tmp_path / "zone/helper").write_text(
        "public z_target(value) {\n  NEAR_MARKER(value)\n}\n", encoding="utf-8"
    )
    (tmp_path / "far/alpha").write_text(
        "public alpha_target(value) {\n  FAR_MARKER(value)\n}\n", encoding="utf-8"
    )
    (tmp_path / "far/noise").write_text(
        "\n".join(
            f"public branch{n}(value) {{\n  work(value)\n}}\n"
            + (f"branch{n}(value)\n" * 65)
            for n in range(40)
        ),
        encoding="utf-8",
    )
    git(tmp_path, "add", ".")
    monkeypatch.setattr(fact_cards, "MAX_CALLEE_CALLS", call_limit)
    diff = (
        "diff --git a/zone/source b/zone/source\n--- a/zone/source\n+++ b/zone/source\n@@ -0,0 +1,42 @@\n"
        + "".join("+" + c + "\n" for c in calls)
    ).encode()
    result = fact_cards.build_fact_cards(
        SourceAdapter.capture(tmp_path, "local", None), annotate_diff(diff), len(diff)
    )
    callees = [c for c in result["cards"] if c["kind"] == "callee_candidate"]
    assert callees[0]["file"] == "zone/helper" and "NEAR_MARKER" in callees[0]["text"]
    assert all("branch" not in c["detail"] for c in callees)


def test_argument_object_is_not_a_callee_definition_body(tmp_path, monkeypatch):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_text("payload(value)\n", encoding="utf-8")
    (tmp_path / "candidate").write_text(
        "return payload({\n  key: value\n});\n", encoding="utf-8"
    )
    git(tmp_path, "add", ".")
    diff = b"diff --git a/source b/source\n--- a/source\n+++ b/source\n@@ -1 +1 @@\n-old(value)\n+payload(value)\n"
    facts = fact_cards.build_fact_cards(
        SourceAdapter.capture(tmp_path, "local", None), annotate_diff(diff), len(diff)
    )
    assert not [c for c in facts["cards"] if c["kind"] == "callee_candidate"]
