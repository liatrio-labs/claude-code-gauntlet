"""Snapshot boundaries reject races and keep lexical facts close to the change."""

import os
import subprocess
import time
from dataclasses import replace

import pytest
from gauntlet import fact_cards, location
from gauntlet.diff import annotate_diff
from gauntlet.location import SourceAdapter

from tests.support.git_diff import git, init_git_repo


@pytest.mark.parametrize("bad_header", ["missing", "oid", "type", "size"])
def test_batch_blob_headers_confine_bad_entries_to_their_file(
    tmp_path, monkeypatch, bad_header
):
    init_git_repo(tmp_path, monkeypatch)
    for name in ("first", "middle", "last"):
        (tmp_path / name).write_text(name + "\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "snapshot")
    source = SourceAdapter.capture(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    files = sorted(
        inventory.files, key=lambda f: ("first", "middle", "last").index(f.file)
    )
    if bad_header == "missing":
        # Real Git emits only an oid/missing header, with no payload or separator.
        files[1] = replace(files[1], oid="0" * 40)
    else:
        original = location.proc.run_bounded_bytes

        def corrupt_header(*args, **kwargs):
            result = original(*args, **kwargs)
            item = files[1]
            header = f"{item.oid} blob {item.size}\n".encode()
            if bad_header == "oid":
                altered = f"{'0' * 40} blob {item.size}\n".encode()
            elif bad_header == "type":
                altered = f"{item.oid} tree {item.size}\n".encode()
            else:
                # A listing/header size mismatch must not mislabel the payload.
                files[1] = replace(item, size=item.size + 1)
                altered = header
            return replace(result, stdout=result.stdout.replace(header, altered, 1))

        monkeypatch.setattr(location.proc, "run_bounded_bytes", corrupt_header)
    result = source.read_snapshot(files, deadline=time.monotonic() + 5)
    assert result.files == {"first": ("first",), "last": ("last",)}
    assert result.unreadable == 1
    assert not result.deadline_reached


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
    assert callees[0]["file"] == "zone/helper" and callees[0]["text"] == "z_target(…)"
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


@pytest.mark.parametrize("target", ["local", "branch"])
def test_fact_cards_never_export_values_or_read_ignored_and_untracked_sources(
    tmp_path, monkeypatch, target
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "zone").mkdir()
    sentinel = "sk_test_SYNTHETIC_CREDENTIAL_VALUE"
    files = {
        "zone/source": f'public neighbor(value="{sentinel}") {{\n  credential = "{sentinel}"\n}}\nhelper(value)\n',
        "zone/helper": f'public helper(value="{sentinel}") {{\n  credential = "{sentinel}"\n}}\n',
        "zone/ignored": f'public helper(value="{sentinel}") {{\n  ignored_sentinel = "{sentinel}"\n}}\n',
        **{
            f"zone/peer{n}": f'public guard(value="{sentinel}") {{\n  credential = "{sentinel}"\n}}\n'
            for n in range(3)
        },
        ".gitignore": "zone/ignored\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "add", "-f", "zone/ignored")
    git(tmp_path, "commit", "-qm", "snapshot")
    source = SourceAdapter.capture(tmp_path, target, None)
    (tmp_path / "zone/untracked").write_text(files["zone/ignored"], encoding="utf-8")
    if target == "branch":
        # Captured ignore rules must survive a different working-tree version.
        (tmp_path / ".gitignore").write_text("", encoding="utf-8")
    original = SourceAdapter.read_snapshot

    def checked_read(adapter, entries, *, deadline):
        assert not {f.file for f in entries} & {"zone/ignored", "zone/untracked"}
        return original(adapter, entries, deadline=deadline)

    monkeypatch.setattr(SourceAdapter, "read_snapshot", checked_read)
    diff = b"diff --git a/zone/source b/zone/source\n--- a/zone/source\n+++ b/zone/source\n@@ -4 +4 @@\n-old(value)\n+helper(value)\n"
    facts = fact_cards.build_fact_cards(source, annotate_diff(diff), len(diff))
    assert {c["kind"] for c in facts["cards"]} == {
        "neighbor",
        "callee_candidate",
        "sibling_contrast",
    }
    assert {c["text"] for c in facts["cards"]} == {
        "neighbor(…)",
        "helper(…)",
        "guard(…)",
    }
    assert all(c["line_start"] == c["line_end"] for c in facts["cards"])
    assert sentinel not in str(facts)


@pytest.mark.parametrize("change", ["removed", "directory"])
def test_inventory_discloses_tracked_files_that_are_no_longer_regular(
    tmp_path, monkeypatch, change
):
    init_git_repo(tmp_path, monkeypatch)
    path = tmp_path / "source"
    path.write_bytes(b"source\n")
    (tmp_path / "retained").write_bytes(b"retained\n")
    git(tmp_path, "add", ".")
    path.unlink()
    if change == "directory":
        path.mkdir()
    source = SourceAdapter.capture(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert inventory.available and inventory.unreadable == 1
    assert [f.file for f in inventory.files] == ["retained"]


def test_snapshot_deadline_prevents_listing_or_reading_sources(tmp_path, monkeypatch):
    def no_command(*args, **kwargs):
        pytest.fail("expired scan launched Git")

    monkeypatch.setattr(location.proc, "run_bounded_bytes", no_command)
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(lambda _: True, deadline=0, max_listing_bytes=1000)
    assert not inventory.available and inventory.truncated
    read = source.read_snapshot(
        [location.SnapshotFile("source", "0" * 40, 10)], deadline=0
    )
    assert not read.files and read.unreadable == 1 and read.deadline_reached


def test_nonlocal_inventory_requires_a_captured_head(tmp_path):
    source = SourceAdapter(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert not inventory.available and not inventory.truncated
    filtered = source.unignored_files(
        inventory, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert not filtered.available and not filtered.files


def test_failed_priority_listing_falls_back_to_the_complete_index(
    tmp_path, monkeypatch
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_bytes(b"source\n")
    git(tmp_path, "add", ".")
    run = location.proc.run_bounded_bytes

    def fail_priority(argv, **kwargs):
        if "--" in argv:
            return location.proc.BoundedBytes(b"", 128, False)
        return run(argv, **kwargs)

    monkeypatch.setattr(location.proc, "run_bounded_bytes", fail_priority)
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True,
        deadline=time.monotonic() + 5,
        max_listing_bytes=10000,
        priority_directories=["zone"],
    )
    assert inventory.available and inventory.truncated
    assert [f.file for f in inventory.files] == ["source"]


def test_listing_failure_disables_fact_scan_instead_of_using_untracked_files(
    tmp_path, monkeypatch
):
    source = SourceAdapter(tmp_path, "local", None)
    monkeypatch.setattr(
        location.proc,
        "run_bounded_bytes",
        lambda *a, **kw: location.proc.BoundedBytes(b"", 128, False),
    )
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert not inventory.available and not inventory.files


def test_priority_listing_cannot_spend_beyond_the_total_capture_budget(
    tmp_path, monkeypatch
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "zone").mkdir()
    (tmp_path / "zone/source").write_bytes(b"source\n")
    git(tmp_path, "add", ".")
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True,
        deadline=time.monotonic() + 5,
        max_listing_bytes=1,
        priority_directories=["zone"],
    )
    assert inventory.available and inventory.truncated and not inventory.files
    assert (
        source.unignored_files(
            inventory, deadline=time.monotonic() + 5, max_listing_bytes=1000
        )
        == inventory
    )


@pytest.fixture
def truncated_snapshot(tmp_path, monkeypatch):
    init_git_repo(tmp_path, monkeypatch)
    for n in range(200):
        directory = tmp_path / (f"dir{n:03d}-" + "x" * 60)
        directory.mkdir()
        (directory / "source").write_bytes(b"source\n")
    (tmp_path / ".gitignore").write_bytes(b"*/source\n")
    git(tmp_path, "add", "-f", ".")
    git(tmp_path, "commit", "-qm", "snapshot")
    source = SourceAdapter.capture(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=18000
    )
    assert inventory.available and inventory.truncated and len(inventory.files) > 50
    (tmp_path / ".gitignore").write_bytes(b"")
    return source, inventory


def test_truncated_inventory_recovers_captured_ignore_rules_in_bounded_queries(
    truncated_snapshot, monkeypatch
):
    source, inventory = truncated_snapshot
    run = location.proc.run_bounded_bytes
    queries = []

    def record(argv, **kwargs):
        if argv[1] == "ls-tree":
            paths = argv[argv.index("--") + 1 :]
            assert sum(len(path.encode()) + 1 for path in paths) <= 8192
            queries.append(paths)
        return run(argv, **kwargs)

    monkeypatch.setattr(location.proc, "run_bounded_bytes", record)
    filtered = source.unignored_files(
        inventory, deadline=time.monotonic() + 5, max_listing_bytes=18000
    )
    assert len(queries) >= 2
    assert filtered.available and filtered.truncated
    assert [f.file for f in filtered.files] == [".gitignore"]
    assert filtered.excluded == len(inventory.files) - 1


def test_incomplete_ignore_rule_listing_makes_the_snapshot_unavailable(
    truncated_snapshot, monkeypatch
):
    source, inventory = truncated_snapshot
    run = location.proc.run_bounded_bytes

    def fail_rules(argv, **kwargs):
        if argv[1] == "ls-tree":
            return location.proc.BoundedBytes(b"", 128, False)
        return run(argv, **kwargs)

    monkeypatch.setattr(location.proc, "run_bounded_bytes", fail_rules)
    filtered = source.unignored_files(
        inventory, deadline=time.monotonic() + 5, max_listing_bytes=18000
    )
    assert not filtered.available and not filtered.files


@pytest.mark.parametrize("failure", ["command", "oversize", "unreadable"])
def test_unverified_ignore_rules_cannot_admit_sources(tmp_path, monkeypatch, failure):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_bytes(b"source\n")
    (tmp_path / ".gitignore").write_bytes(
        b"#" * (256 * 1024 + 1) if failure == "oversize" else b"source\n"
    )
    git(tmp_path, "add", "-f", ".")
    git(tmp_path, "commit", "-qm", "snapshot")
    source = SourceAdapter.capture(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    if failure == "command":
        monkeypatch.setattr(
            location.proc,
            "run_bounded_bytes",
            lambda *a, **kw: location.proc.BoundedBytes(b"", 128, False),
        )
    elif failure == "unreadable":
        monkeypatch.setattr(
            SourceAdapter,
            "read_snapshot",
            lambda *a, **kw: location.SnapshotRead({}, 1, False),
        )
    filtered = source.unignored_files(
        inventory, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert not filtered.available and not filtered.files


@pytest.mark.parametrize(
    "failure", ["command", "truncated", "header", "unterminated_header", "payload"]
)
def test_incomplete_batch_reads_never_export_partial_blobs(
    tmp_path, monkeypatch, failure
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_bytes(b"source\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "snapshot")
    source = SourceAdapter.capture(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    run = location.proc.run_bounded_bytes

    def incomplete(argv, **kwargs):
        result = run(argv, **kwargs)
        if failure == "command":
            return replace(result, returncode=128)
        if failure == "truncated":
            return replace(result, truncated=True)
        if failure == "header":
            return replace(result, stdout=b"invalid header\n")
        if failure == "unterminated_header":
            return replace(result, stdout=result.stdout.split(b"\n", 1)[0])
        return replace(result, stdout=result.stdout[:-2])

    monkeypatch.setattr(location.proc, "run_bounded_bytes", incomplete)
    read = source.read_snapshot(inventory.files, deadline=time.monotonic() + 5)
    assert not read.files and read.unreadable == 1


def test_unmerged_index_entries_are_disclosed_without_selecting_a_side(
    tmp_path, monkeypatch
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_bytes(b"source\n")
    oid = git(tmp_path, "hash-object", "-w", "source")
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=tmp_path,
        input=f"100644 {oid} 1\tsource\n100644 {oid} 2\tsource\n".encode(),
        capture_output=True,
        check=True,
    )
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert inventory.available and inventory.unreadable == 1 and not inventory.files


def test_captured_inventory_excludes_submodule_objects(tmp_path, monkeypatch):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_bytes(b"source\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "source")
    head = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "update-index", "--add", "--cacheinfo", "160000", head, "nested")
    git(tmp_path, "commit", "-qm", "nested repository")
    source = SourceAdapter.capture(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert inventory.available and inventory.excluded == 1
    assert [f.file for f in inventory.files] == ["source"]


def test_listing_timeout_returns_an_unavailable_inventory(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise location.proc.TimeoutExpired(["git", "ls-files"], 0.1)

    monkeypatch.setattr(location.proc, "run_bounded_bytes", timeout)
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert not inventory.available and not inventory.files


@pytest.mark.parametrize("content", [b"source\0bytes", b"\xff"])
def test_snapshot_reads_disclose_nontext_sources_instead_of_decoding_with_replacement(
    tmp_path, monkeypatch, content
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "source").write_bytes(content)
    git(tmp_path, "add", ".")
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    read = source.read_snapshot(inventory.files, deadline=time.monotonic() + 5)
    assert not read.files and read.unreadable == 1


def test_snapshot_read_discloses_a_file_removed_after_listing(tmp_path, monkeypatch):
    init_git_repo(tmp_path, monkeypatch)
    path = tmp_path / "source"
    path.write_bytes(b"source\n")
    git(tmp_path, "add", ".")
    source = SourceAdapter(tmp_path, "local", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    path.unlink()
    read = source.read_snapshot(inventory.files, deadline=time.monotonic() + 5)
    assert not read.files and read.unreadable == 1


def test_captured_ignore_rules_preserve_a_literal_replacement_character(
    tmp_path, monkeypatch
):
    init_git_repo(tmp_path, monkeypatch)
    (tmp_path / "�").write_bytes(b"source\n")
    (tmp_path / ".gitignore").write_bytes("�\n".encode())
    git(tmp_path, "add", "-f", ".")
    git(tmp_path, "commit", "-qm", "source")
    source = SourceAdapter.capture(tmp_path, "branch", None)
    inventory = source.tracked_files(
        lambda _: True, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    filtered = source.unignored_files(
        inventory, deadline=time.monotonic() + 5, max_listing_bytes=10000
    )
    assert filtered.available and filtered.excluded == 1
    assert [f.file for f in filtered.files] == [".gitignore"]
