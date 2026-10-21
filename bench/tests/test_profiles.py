"""Profile filtering preserves evaluator false positives and unknown observations."""

from dataclasses import replace

import pytest

from bench.runner.observations import BugObservation, ScoreRef
from bench.runner.profiles import profile_metrics


def test_profiles_use_category_denominators_and_the_same_false_positives():
    ref = ScoreRef("run", "epoch", "score")
    bugs = [
        BugObservation(
            1, ref, "url", "attempt", str(i), category, outcome, (), (), None
        )
        for i, (category, outcome) in enumerate(
            (
                ("bug", "matched"),
                ("security", "missed"),
                ("perf", "matched"),
                ("style", "matched"),
            )
        )
    ]
    for profile, tp, denominator in (("all", 3, 4), ("core", 2, 3), ("strict", 1, 2)):
        result = profile_metrics(bugs, 1, profile)
        assert result.tp == tp and result.denominator == denominator and result.fp == 1
        assert result.recall == tp / denominator
        assert result.precision_strict == tp / (tp + 1)
        assert result.f1_strict == 2 * tp / (2 * tp + 1 + denominator - tp)
        assert result.f2_strict == 5 * tp / (5 * tp + 1 + 4 * (denominator - tp))
    with pytest.raises(ValueError):
        profile_metrics([replace(bugs[0], outcome="unavailable")], 0, "core")
    assert profile_metrics([], 0, "core").recall is None
