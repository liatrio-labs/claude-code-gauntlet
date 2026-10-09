"""Ordered target effects and the generator outcome boundary."""

import pytest
from gauntlet import generate
from gauntlet.cli import CliError
from gauntlet.generate import finish, sync_targets


def test_changed_text(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("old", encoding="utf-8")
    assert sync_targets(tmp_path, {"target.md": "new"}, False) == ["target.md"]
    assert target.read_bytes() == b"new"


def test_unchanged_text(tmp_path, monkeypatch):
    target = tmp_path / "target.md"
    target.write_text("same", encoding="utf-8")
    before = target.stat().st_mtime_ns
    writes = []
    monkeypatch.setattr(generate, "write_atomic", lambda *args: writes.append(args))
    assert sync_targets(tmp_path, {"target.md": "same"}, False) == []
    assert writes == []
    assert target.stat().st_mtime_ns == before


def test_callable_current_text(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("prefix", encoding="utf-8")

    def render(current):
        assert current == "prefix"
        return current + " suffix"

    assert sync_targets(tmp_path, {"target.md": render}, False) == ["target.md"]
    assert target.read_bytes() == b"prefix suffix"


def test_check_writes_nothing(tmp_path, monkeypatch):
    target = tmp_path / "target.md"
    target.write_text("old", encoding="utf-8")
    before = target.stat().st_mtime_ns
    writes = []
    monkeypatch.setattr(generate, "write_atomic", lambda *args: writes.append(args))
    assert sync_targets(tmp_path, {"target.md": "new"}, True) == ["target.md"]
    assert writes == []
    assert target.read_bytes() == b"old"
    assert target.stat().st_mtime_ns == before


def test_mapping_order(tmp_path):
    assert sync_targets(tmp_path, {"z.md": "z", "a.md": "a"}, True) == [
        "z.md",
        "a.md",
    ]


def test_missing_whole_file(tmp_path):
    assert sync_targets(tmp_path, {"new.md": "created"}, False) == ["new.md"]
    assert (tmp_path / "new.md").read_bytes() == b"created"


def test_later_failure_keeps_earlier_write(tmp_path):
    (tmp_path / "late.md").write_text("late", encoding="utf-8")

    def fail(current):
        assert (tmp_path / "early.md").read_bytes() == b"early"
        raise CliError("late rendering failed")

    with pytest.raises(CliError, match="late rendering failed"):
        sync_targets(tmp_path, {"early.md": "early", "late.md": fail}, False)
    assert (tmp_path / "early.md").read_bytes() == b"early"
    assert (tmp_path / "late.md").read_bytes() == b"late"


def test_missing_callable_target(tmp_path):
    with pytest.raises(FileNotFoundError):
        sync_targets(tmp_path, {"absent.md": lambda current: "new"}, False)
    assert not (tmp_path / "absent.md").exists()


def test_unreadable_target(tmp_path):
    (tmp_path / "directory.md").mkdir()
    with pytest.raises(OSError):
        sync_targets(tmp_path, {"directory.md": "new"}, True)


@pytest.mark.parametrize(
    ("stale", "check", "expected"),
    [
        pytest.param([], True, "current\n", id="current"),
        pytest.param(
            ["z.md", "a.md"], False, "regenerated: z.md, a.md\n", id="written"
        ),
    ],
)
def test_success_outcome(capsys, stale, check, expected):
    assert (
        finish(
            stale,
            check,
            current_message="current",
            stale_description="stale targets",
            command="python3 scripts/generator.py",
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == expected
    assert captured.err == ""


def test_stale_outcome(capsys):
    with pytest.raises(CliError) as error:
        finish(
            ["z.md", "a.md"],
            True,
            current_message="current",
            stale_description="stale targets",
            command="python3 scripts/generator.py",
        )
    assert str(error.value) == (
        "stale targets: z.md, a.md; run: python3 scripts/generator.py"
    )
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
