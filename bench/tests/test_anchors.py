"""Anchor rejudging shares the active scorer and reuses only intact evidence."""

import json
from pathlib import Path

import pytest

from bench.runner import anchors
from bench.runner.epochs import load_epoch


def test_anchors_use_active_profiles_context_and_cache(score_case, tmp_path):
    epoch = load_epoch()
    source, subsets = tmp_path / "anchors.json", tmp_path / "subsets.json"
    source.write_text(
        json.dumps(
            {
                score_case.url: {
                    "claude": [
                        {"text": t, "path": None, "line": None}
                        for t in ("match", "sibling", "extra")
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    subsets.write_text(json.dumps({"gate": [score_case.url]}), encoding="utf-8")
    calls = []

    def scorer(pin, key, base_url, dedup_path, env=None):
        calls.append(dedup_path)
        score_case.scorer(pin, key, Path(dedup_path).parent)

    def run(diffs=None):
        return anchors.rejudge_anchors(
            epoch.judge_pin,
            tools=["claude"],
            epoch_id=epoch.epoch_id,
            anchors_path=source,
            subsets_path=subsets,
            cache_dir=tmp_path / "cache",
            run_scorer=scorer,
            diffs=diffs,
            adjudicator=lambda *a: {"bucket": "noise"},
        )

    first = run()
    assert not first["cache_hit"] and run()["cache_hit"] and len(calls) == 1
    assert first["per_tool"]["claude"]["profiles"]["all"]["tp"] == 2
    assert first["per_tool"]["claude"]["profile"] == "core"
    assert not run({score_case.url: "changed context"})["cache_hit"] and len(calls) == 2
    work = Path(first["work_dir"])
    receipt = work / "result.json"
    receipt.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        run()
