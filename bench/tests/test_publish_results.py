"""The READMEs show the current release's numbers exactly as RELEASES records them."""

import copy
import dataclasses
import shutil

import pytest

from bench import publish_results, report

RUN_IDS = {row.get("run_id") for row in report.load_ledger(report.DEFAULT_LEDGER)}


def _release(**changes):
    release = copy.deepcopy(report.RELEASES[0])
    release["pooled"].update(changes)
    return release


def _metrics():
    return report.RELEASES[0]["pooled"]["metrics"]


def test_committed_readmes_match_a_fresh_render():
    assert publish_results.main(["--check"]) == 0


def test_a_new_release_entry_republishes_every_span(tmp_path):
    for name in publish_results.FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(publish_results.REPO_ROOT / name, tmp_path / name)
    release = _release(
        claude_code="9.9.9",
        minutes={"median": 30.5, "low": 1.2, "high": 40.0},
        metrics=tuple(
            dataclasses.replace(m, pooled="91.6%") if m.key == "full" else m
            for m in _metrics()
        ),
    )
    release["label"] = "v9.9"
    releases = (release, *report.RELEASES)

    assert publish_results.main(["--check"], root=tmp_path, releases=releases) == 1
    assert publish_results.main([], root=tmp_path, releases=releases) == 0
    assert publish_results.main(["--check"], root=tmp_path, releases=releases) == 0
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "it caught 92% of them" in readme
    assert "| Measure | v9.9 |" in readme
    assert "a median of 31 minutes, from 1 to 40" in readme
    history = (tmp_path / "bench/README.md").read_text(encoding="utf-8")
    assert "Claude Code 9.9.9" in history


@pytest.mark.parametrize(
    "release, message",
    [
        ({"label": "v3.1", "legs": ()}, "only a pooled release"),
        (_release(lanes=(("no-such-run",),)), "missing from the ledger: no-such-run"),
        (_release(auth_modes={"subscription": 1, "api": 1}), "auth modes"),
        (
            _release(metrics=_metrics()[:1] * 2),
            "repeated metric keys",
        ),
    ],
)
def test_inconsistent_releases_are_refused(release, message):
    with pytest.raises(ValueError, match=message):
        publish_results.render(release, RUN_IDS)


def test_an_unmarked_file_is_refused_before_anything_is_written(tmp_path):
    for name in publish_results.FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(publish_results.REPO_ROOT / name, tmp_path / name)
    (tmp_path / "bench/README.md").write_text("no markers here\n", encoding="utf-8")
    before = (tmp_path / "README.md").read_text(encoding="utf-8")
    release = _release(claude_code="9.9.9")
    with pytest.raises(ValueError, match="bench:release"):
        publish_results.main([], root=tmp_path, releases=(release,))
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == before
