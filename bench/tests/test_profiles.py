"""Profile arithmetic counts golden detections, preserving all-golden membership."""

from dataclasses import replace

import pytest

from bench.runner.epochs import DEFAULT_ROOT, build_bug_index, load_epoch
from bench.runner.observations import (
    ScoreRef,
    ScoreRequest,
    build_observations,
    candidate_records,
    dedup_groups,
)
from bench.runner.profiles import profile_metrics
from bench.runner.score import evaluator_for


def test_profiles_keep_fp_and_all_golden_join_with_multi_match_and_sibling(tmp_path):
    epoch = load_epoch(root=DEFAULT_ROOT)
    bugs = build_bug_index(
        {
            "u": {
                "reviews": [],
                "golden_comments": [
                    {"comment": "a", "severity": "High", "category": "bug"},
                    {"comment": "b", "severity": "High", "category": "security"},
                    {"comment": "c", "severity": "Low", "category": "style"},
                ],
            }
        }
    )
    epoch = replace(epoch, bugs=bugs)
    candidates = {
        "u": {
            "tool": [
                {"text": t, "path": None, "line": None}
                for t in ("one", "sibling", "style only", "unmatched")
            ]
        }
    }
    records = candidate_records(candidates, {"u": "attempt"}, "tool")
    groups = dedup_groups({"u": {"tool": [[0, 1]]}}, records, "tool")
    request = ScoreRequest(
        ScoreRef("run", epoch.epoch_id, "a" * 32),
        epoch,
        tmp_path,
        ("u",),
        (("u", "attempt"),),
        records,
        "tool",
        evaluator_for(epoch, "j", "a"),
    )
    evaluations = {
        "u": {
            "tool": {
                "true_positives": [
                    {"golden_comment": t, "matched_candidate": c, "confidence": 0.8}
                    for t, c in (("a", "one"), ("b", "one"), ("c", "style only"))
                ],
                "false_negatives": [],
                "errors": [],
                "tp": 3,
                "fn": 0,
                "fp": 1,
                "precision": 0.001,
            }
        }
    }
    observations = build_observations(request, evaluations, groups)
    all_matched = {m.candidate_id for o in observations for m in o.matches}
    assert all_matched == {r.candidate_id for r in records[:3]}
    for profile, tp in (("all", 3), ("core", 2), ("strict", 2)):
        result = profile_metrics(observations, 1, profile)
        assert result.tp == result.denominator == tp
        assert result.fn == 0 and result.fp == 1
        assert result.precision_strict == tp / (tp + 1)
        assert result.f1_strict == 2 * tp / (2 * tp + 1)
        assert result.f2_strict == 5 * tp / (5 * tp + 1)
        assert {m.candidate_id for o in observations for m in o.matches} == all_matched


def test_empty_profile_and_unavailable_observations_are_explicit():
    result = profile_metrics([], 0, "core")
    assert result.recall is None and result.status == "empty"
    with pytest.raises(ValueError):
        profile_metrics([], -1, "core")
