"""Validated, immutable golden inputs and content identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict, get_args

from bench.runner.decode import decode

JsonValue: TypeAlias = (
    "bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None"
)
Profile = Literal["core", "all", "strict"]
Category = Literal[
    "api",
    "bug",
    "concurrency",
    "data",
    "doc_defect",
    "perf",
    "security",
    "speculative",
    "style",
    "test_gap",
]
PROFILE_CATEGORIES = {
    "strict": frozenset(["api", "bug", "concurrency", "data", "security"]),
    "all": frozenset(get_args(Category)),
}
PROFILE_CATEGORIES["core"] = PROFILE_CATEGORIES["strict"] | {
    "doc_defect",
    "perf",
    "test_gap",
}
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "golden"


class GoldenCommentWire(TypedDict):
    comment: str
    severity: str


class CategorizedGoldenCommentWire(GoldenCommentWire):
    category: Category


class GoldenPRWire(TypedDict):
    pr_title: str
    original_url: str | None
    source_repo: str
    golden_comments: list[GoldenCommentWire | CategorizedGoldenCommentWire]
    reviews: list[dict[str, JsonValue]]


@dataclass(frozen=True, slots=True)
class EpochSpec:
    epoch_id: str
    upstream_sha: str
    scorer_tree: str
    judge_pin: str
    adjudicator_pin: str
    golden_sha256: str
    supported_profiles: tuple[Profile, ...]
    primary_profile: Profile


@dataclass(frozen=True, slots=True)
class GoldenClaim:
    comment: str
    severity: str
    category: Category | None = None


@dataclass(frozen=True, slots=True)
class GoldenBug:
    bug_id: str
    pr_url: str
    ordinal: int
    comment: str
    severity: str
    category: Category | None


@dataclass(frozen=True, slots=True)
class GoldenEpoch(EpochSpec):
    bugs: tuple[GoldenBug, ...]
    scorer_input: dict[str, GoldenPRWire]
    project_by_pr: dict[str, str]
    subsets: dict[str, tuple[str, ...]]
    shas: dict[str, dict[str, JsonValue]]


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=int,  # Reject non-finite spellings before decoding.
        object_pairs_hook=unique,
    )


def confined(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("path escapes input root")
    return path


def build_bug_index(data: dict[str, GoldenPRWire]) -> tuple[GoldenBug, ...]:
    bugs = []
    for url, pr in sorted(data.items()):
        if not isinstance(url, str) or pr.get("reviews") != []:
            raise ValueError("frozen golden input requires URL keys and empty reviews")
        seen = set()
        for ordinal, claim in enumerate(pr["golden_comments"]):
            record = decode(GoldenClaim, claim)
            text, severity = record.comment, record.severity
            if not text or text in seen:
                raise ValueError("empty or duplicate golden text within PR")
            seen.add(text)
            content = digest([url, text, severity])
            bugs.append(
                GoldenBug(
                    "g:" + content,
                    url,
                    ordinal,
                    **asdict(record),
                )
            )
    return tuple(bugs)


def included_bugs(
    epoch: GoldenEpoch, profile: Profile, pr_urls: frozenset[str]
) -> tuple[GoldenBug, ...]:
    if profile not in epoch.supported_profiles:
        raise ValueError(f"unsupported profile {profile!r} for {epoch.epoch_id}")
    return tuple(
        b for b in epoch.bugs if b.pr_url in pr_urls and in_profile(b.category, profile)
    )


def in_profile(category, profile):
    return (category is None and profile == "all") or category in PROFILE_CATEGORIES[
        profile
    ]


def epoch_context(epoch):
    return {
        "spec": {f.name: getattr(epoch, f.name) for f in fields(EpochSpec)},
        **{
            k: getattr(epoch, k)
            for k in ("scorer_input", "project_by_pr", "subsets", "shas")
        },
    }


def from_context(context) -> GoldenEpoch:
    spec = decode(EpochSpec, context["spec"])
    data, projects = context["scorer_input"], context["project_by_pr"]
    if digest([data, projects]) != spec.golden_sha256 or set(data) != set(projects):
        raise ValueError("golden digest mismatch")
    bugs = build_bug_index(data)
    if any(b.category is None for b in bugs):
        raise ValueError("active golden requires categories")
    return GoldenEpoch(
        **asdict(spec),
        bugs=bugs,
        scorer_input=data,
        project_by_pr=projects,
        subsets={k: tuple(v) for k, v in context["subsets"].items()},
        shas=context["shas"],
    )


def load_epoch(
    epoch_id: str | None = None, *, root: Path = DEFAULT_ROOT
) -> GoldenEpoch:
    spec = decode(EpochSpec, read_json(root / "epoch.json"))
    if epoch_id is not None and epoch_id != spec.epoch_id:
        raise ValueError("only the active golden epoch is runnable")
    if (
        not spec.supported_profiles
        or len(set(spec.supported_profiles)) != len(spec.supported_profiles)
        or spec.primary_profile not in spec.supported_profiles
    ):
        raise ValueError("invalid epoch profile policy")
    data: dict[str, GoldenPRWire] = {}
    projects: dict[str, str] = {}
    for path in sorted((root / "golden_comments").glob("*.json")):
        for pr in read_json(path):
            url = pr["url"]
            if url in data:
                raise ValueError("duplicate golden PR")
            projects[url] = path.stem
            data[url] = {
                "pr_title": pr["pr_title"],
                "original_url": pr.get("original_url"),
                "source_repo": url.split("/")[4],
                "golden_comments": pr["comments"],
                "reviews": [],
            }
    if not data:
        raise ValueError("empty golden population")
    return from_context(
        {
            "spec": asdict(spec),
            "scorer_input": data,
            "project_by_pr": projects,
            "subsets": read_json(root / "subsets.json"),
            "shas": read_json(root / "shas.json"),
        }
    )
