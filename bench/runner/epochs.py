"""Validated, immutable golden inputs and content identities."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
    "strict": frozenset(("api", "bug", "concurrency", "data", "security")),
    "core": frozenset(
        (
            "api",
            "bug",
            "concurrency",
            "data",
            "security",
            "doc_defect",
            "perf",
            "test_gap",
        )
    ),
    "all": frozenset(get_args(Category)),
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
    content_sha256: str


@dataclass(frozen=True, slots=True)
class GoldenEpoch(EpochSpec):
    bugs: tuple[GoldenBug, ...]
    claims_by_pr: dict[
        str, tuple[GoldenCommentWire | CategorizedGoldenCommentWire, ...]
    ]
    scorer_input: dict[str, GoldenPRWire]
    project_by_pr: dict[str, str]
    profile_denominators: dict[Profile, dict[str, int]]
    golden_sha256: str


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path):
    def invalid(value):
        raise ValueError(f"non-finite JSON: {value}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=invalid,
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
            text, severity, category = record.comment, record.severity, record.category
            if not text or text in seen:
                raise ValueError("empty or duplicate golden text within PR")
            seen.add(text)
            content = digest([url, text, severity])
            bugs.append(
                GoldenBug(
                    "g:" + content,
                    url,
                    ordinal,
                    text,
                    severity,
                    category,
                    content,
                )
            )
    return tuple(bugs)


def included_bugs(
    epoch: GoldenEpoch, profile: Profile, pr_urls: frozenset[str]
) -> tuple[GoldenBug, ...]:
    if profile not in epoch.supported_profiles:
        raise ValueError(f"unsupported profile {profile!r} for {epoch.epoch_id}")
    return tuple(
        b
        for b in epoch.bugs
        if b.pr_url in pr_urls
        and (profile == "all" or b.category in PROFILE_CATEGORIES[profile])
    )


def denominators(bugs, profiles, subsets):
    return {
        p: {
            name: sum(
                b.pr_url in urls and (p == "all" or b.category in PROFILE_CATEGORIES[p])
                for b in bugs
            )
            for name, urls in {"full": {b.pr_url for b in bugs}, **subsets}.items()
        }
        for p in profiles
    }


def load_epoch(
    epoch_id: str | None = None, *, root: Path = DEFAULT_ROOT
) -> GoldenEpoch:
    root = Path(root)
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
    bugs = build_bug_index(data)
    if any(b.category is None for b in bugs):
        raise ValueError("active golden requires categories")
    subsets = read_json(root / "subsets.json")
    return GoldenEpoch(
        spec.epoch_id,
        spec.upstream_sha,
        spec.scorer_tree,
        spec.judge_pin,
        spec.supported_profiles,
        spec.primary_profile,
        bugs,
        {u: tuple(p["golden_comments"]) for u, p in data.items()},
        data,
        projects,
        denominators(bugs, spec.supported_profiles, subsets),
        digest(data),
    )
