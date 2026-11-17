"""The READMEs show the current release's numbers exactly as RELEASES records them."""

import copy
import shutil

import pytest

from bench import publish_results, report


def test_committed_readmes_match_a_fresh_render():
    assert publish_results.main(["--check"]) == 0


def test_a_new_release_entry_republishes_every_span(tmp_path):
    for name in publish_results.FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(publish_results.REPO_ROOT / name, tmp_path / name)
    release = copy.deepcopy(report.RELEASES[0])
    release["label"] = "v9.9"
    pooled = release["pooled"]
    pooled["claude_code"] = "9.9.9"
    pooled["metrics"] = tuple(
        (key, label, lanes, "91.6%" if key == "full" else value, count)
        for key, label, lanes, value, count in pooled["metrics"]
    )
    pooled["minutes"] = {"median": 30.5, "low": 1.2, "high": 40.0}
    releases = (release, *report.RELEASES)

    assert publish_results.main(["--check"], root=tmp_path, releases=releases) == 1
    assert publish_results.main([], root=tmp_path, releases=releases) == 0
    assert publish_results.main(["--check"], root=tmp_path, releases=releases) == 0
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "it caught 92% of them" in readme
    assert "| Measure | v9.9 |" in readme
    assert "a median of 31 minutes, from 1 to 40" in readme
    assert "Claude Code 9.9.9" in (tmp_path / "bench/README.md").read_text(
        encoding="utf-8"
    )


def test_unmarked_or_unpooled_sources_are_refused():
    spans = publish_results.render(report.RELEASES[0])
    with pytest.raises(ValueError, match="bench:results"):
        publish_results.apply("no markers here\n", ("results",), spans)
    with pytest.raises(ValueError, match="only a pooled release"):
        publish_results.render({"label": "v3.1", "legs": ()})
