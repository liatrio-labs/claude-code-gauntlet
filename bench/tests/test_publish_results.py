"""The READMEs show the current release's numbers exactly as RELEASES records them."""

import dataclasses
import shutil

import pytest

from bench import publish_results, report

ROWS = report.load_ledger(report.DEFAULT_LEDGER)


def _current():
    return report.RELEASES[0]


def _release(**changes):
    current = _current()
    return {**current, "pooled": dataclasses.replace(current["pooled"], **changes)}


def _copy_readmes(root):
    for name in publish_results.FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(publish_results.REPO_ROOT / name, root / name)


def test_committed_readmes_match_a_fresh_render():
    assert publish_results.main(["--check"]) == 0


def test_a_new_release_entry_republishes_every_span(tmp_path):
    _copy_readmes(tmp_path)
    release = _release(
        claude_code="9.9.9",
        minutes={"median": 30.5, "low": 1.2, "high": 40.0},
        metrics=tuple(
            dataclasses.replace(m, lanes=("90%",) * 3, pooled="91.4%", count="181/198")
            if m.key == "full"
            else m
            for m in _current()["pooled"].metrics
        ),
    )
    releases = ({**release, "label": "v9.9"}, *report.RELEASES)

    assert publish_results.main(["--check"], root=tmp_path, releases=releases) == 1
    assert publish_results.main([], root=tmp_path, releases=releases) == 0
    assert publish_results.main(["--check"], root=tmp_path, releases=releases) == 0
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "it caught 91% of them" in readme
    assert "| Measure | v9.9 |" in readme
    assert "a median of 31 minutes, from 1 to 40" in readme
    history = (tmp_path / "bench/README.md").read_text(encoding="utf-8")
    assert "Claude Code 9.9.9" in history


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"auth_modes": {"subscription": 1, "api": 1}}, "auth modes do not cover"),
        ({"minutes": {"median": 1.0}}, "lack a field"),
        (
            {
                "metrics": (
                    dataclasses.replace(
                        _current()["pooled"].metrics[0], pooled="70.0%"
                    ),
                )
            },
            "disagrees with its count",
        ),
        (
            {"metrics": _current()["pooled"].metrics[:1] * 2},
            "repeated metric keys",
        ),
        (
            {
                "metrics": (
                    dataclasses.replace(_current()["pooled"].metrics[0], lanes=("1%",)),
                )
            },
            "lacks a value per lane",
        ),
    ],
)
def test_an_inconsistent_entry_fails_when_it_is_built(changes, message):
    with pytest.raises(ValueError, match=message):
        _release(**changes)


def test_only_a_pooled_release_whose_runs_are_in_its_epoch_publishes():
    current = _current()
    run_ids = publish_results.epoch_run_ids(ROWS, current["epoch_id"])
    with pytest.raises(ValueError, match="only a pooled release"):
        publish_results.render({"label": "v3.1", "legs": ()}, run_ids)
    other_epoch = publish_results.epoch_run_ids(ROWS, "no-such-epoch")
    with pytest.raises(ValueError, match="missing from epoch e616e84-v1"):
        publish_results.render(current, other_epoch)


def test_a_single_lane_release_reads_as_one_run():
    current = _current()
    lane = current["pooled"].lanes[0]
    single = _release(
        lanes=(lane,),
        auth_modes={"subscription": 15, "api": 0},
        metrics=tuple(
            dataclasses.replace(m, lanes=m.lanes[:1], count=None)
            for m in current["pooled"].metrics
        ),
    )
    spans = publish_results.render(single, set(lane))
    assert "over one run," in spans["pitch"]
    assert "the same 15 PRs once on" in spans["results"]


@pytest.mark.parametrize(
    "text",
    [
        "no markers here\n",
        "<!-- bench:release:begin -->\n<!-- bench:release:begin -->\nx\n<!-- bench:release:end -->\n",
    ],
)
def test_a_bad_marker_exits_2_before_anything_is_written(tmp_path, capsys, text):
    _copy_readmes(tmp_path)
    (tmp_path / "bench/README.md").write_text(text, encoding="utf-8")
    before = (tmp_path / "README.md").read_text(encoding="utf-8")
    release = _release(claude_code="9.9.9")
    assert publish_results.main([], root=tmp_path, releases=(release,)) == 2
    assert "expected one bench:release span" in capsys.readouterr().err
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == before
