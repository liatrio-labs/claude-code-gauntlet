"""Profile arithmetic over complete, Boolean per-golden observations."""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from bench.runner.epochs import PROFILE_CATEGORIES, Profile


@dataclass(frozen=True, slots=True)
class ProfileMetrics:
    profile: Profile
    tp: int
    fp: int
    fn: int
    denominator: int
    recall: float | None
    precision_strict: float
    f1_strict: float
    f2_strict: float
    status: Literal["scored", "empty"]

    def to_wire(self):
        return asdict(self)


def profile_metrics(
    observations: Sequence["BugObservation"], fp: int, profile: Profile
) -> ProfileMetrics:
    if profile not in PROFILE_CATEGORIES or type(fp) is not int or fp < 0:
        raise ValueError("invalid profile or false-positive count")
    selected = [
        o
        for o in observations
        if profile == "all" or o.category in PROFILE_CATEGORIES[profile]
    ]
    if any(o.outcome == "unavailable" or o.errors for o in selected):
        raise ValueError("incomplete/error observations cannot produce metrics")
    tp = sum(o.outcome == "matched" for o in selected)
    fn = len(selected) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / len(selected) if selected else None
    return ProfileMetrics(
        profile,
        tp,
        fp,
        fn,
        len(selected),
        recall,
        precision,
        2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0,
        5 * tp / (5 * tp + fp + 4 * fn) if 5 * tp + fp + 4 * fn else 0.0,
        "scored" if selected else "empty",
    )


if TYPE_CHECKING:
    from bench.runner.observations import BugObservation
