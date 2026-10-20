"""Validated, immutable golden inputs and content identities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict, cast, get_args

JsonValue: TypeAlias = (
    "bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None"
)
Profile = Literal["core", "all", "strict"]
BucketPolicy = Literal["direct", "dedup_expanded"]
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
PROFILE_POLICY_VERSION = "1"
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
KEPT_FILES = (
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    "code_review_benchmark/__init__.py",
    "code_review_benchmark/step2_5_dedup_candidates.py",
    "code_review_benchmark/step3_judge_comments.py",
    "tests/conftest.py",
    "tests/test_step3.py",
)


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


class BugWire(TypedDict):
    bug_id: str
    pr_url: str
    ordinal: int
    comment: str
    severity: str
    category: Category | None
    content_sha256: str


class EpochWire(TypedDict):
    schema_version: Literal[1]
    epoch_id: str
    upstream_sha: str
    scorer_sha: str
    scorer_tree: str
    bucket_policy: BucketPolicy
    scorer_file_hashes: dict[str, str]
    golden_sha256: str
    scorer_sha256: str
    bugs_sha256: str
    labels_sha256: str
    primary_profile: Profile
    supported_profiles: list[Profile]
    pr_count: int
    profile_denominators: dict[Profile, dict[str, int]]


class EpochIndexWire(TypedDict):
    schema_version: Literal[1]
    active_epoch_id: str
    epochs: dict[str, str]


class EpochIndexIntegrityWire(EpochIndexWire):
    manifest_sha256: dict[str, str]


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
class GoldenEpoch:
    schema_version: Literal[1]
    epoch_id: str
    upstream_sha: str
    scorer_sha: str
    scorer_tree: str
    bucket_policy: BucketPolicy
    scorer_file_hashes: tuple[tuple[str, str], ...]
    golden_sha256: str
    scorer_sha256: str
    bugs_sha256: str
    labels_sha256: str
    primary_profile: Profile
    supported_profiles: tuple[Profile, ...]
    pr_count: int
    profile_denominators: tuple[tuple[Profile, tuple[tuple[str, int], ...]], ...]
    bugs: tuple[GoldenBug, ...]
    root: Path


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


def scorer_hash(root: Path) -> str:
    return digest(
        [
            (p.relative_to(root).as_posix(), file_hash(p))
            for p in sorted((root / "code_review_benchmark").glob("*.py"))
            if confined(root, p.relative_to(root).as_posix())
        ]
    )


def validate_source(tree, hashes):
    if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise ValueError("scorer requires a full git tree id")
    if set(hashes) != set(KEPT_FILES) or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in hashes.values()
    ):
        raise ValueError("invalid scorer file hash inventory")


def build_bug_index(data: dict[str, GoldenPRWire]) -> tuple[GoldenBug, ...]:
    bugs = []
    for url, pr in sorted(data.items()):
        if not isinstance(url, str) or pr.get("reviews") != []:
            raise ValueError("frozen golden input requires URL keys and empty reviews")
        seen = set()
        for ordinal, claim in enumerate(pr["golden_comments"]):
            text, severity = claim["comment"], claim["severity"]
            category = claim.get("category")
            if not isinstance(text, str) or not text or not isinstance(severity, str):
                raise ValueError("invalid golden claim")
            if text in seen:
                raise ValueError("duplicate golden text within PR")
            seen.add(text)
            if category is not None and category not in get_args(Category):
                raise ValueError("unknown golden category")
            content = digest([url, text, severity])
            bugs.append(
                GoldenBug(
                    "g:" + content,
                    url,
                    ordinal,
                    text,
                    severity,
                    cast(Category | None, category),
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


def load_epoch(epoch_id: str | None = None, *, root: Path) -> GoldenEpoch:
    root = Path(root).resolve()
    index = read_json(root / "epochs/index.json")
    if index["schema_version"] != 1:
        raise ValueError("unsupported epoch index schema")
    identity = epoch_id or index["active_epoch_id"]
    manifest_path = confined(root, index["epochs"][identity])
    if file_hash(manifest_path) != index["manifest_sha256"][identity]:
        raise ValueError("epoch manifest hash mismatch")
    manifest = read_json(manifest_path)
    archive = manifest_path.parent
    if manifest["schema_version"] != 1 or manifest["epoch_id"] != identity:
        raise ValueError("epoch manifest identity/schema mismatch")
    validate_source(manifest["scorer_tree"], manifest["scorer_file_hashes"])
    if manifest["bucket_policy"] not in get_args(BucketPolicy):
        raise ValueError("invalid epoch bucket policy")
    profiles = manifest["supported_profiles"]
    if (
        not profiles
        or len(set(profiles)) != len(profiles)
        or any(p not in get_args(Profile) for p in profiles)
        or manifest["primary_profile"] not in profiles
    ):
        raise ValueError("invalid epoch profile policy")
    for name in ("benchmark_data.min.json", "bugs.json", "pr_labels.json"):
        confined(archive, name)
    data = read_json(archive / "benchmark_data.min.json")
    bugs = build_bug_index(data)
    if any(b.category is None for b in bugs) and profiles != ["all"]:
        raise ValueError("uncategorized epoch supports only all")
    checks = {
        "golden_sha256": digest(data),
        "scorer_sha256": digest(
            sorted(
                (name, expected)
                for name, expected in manifest["scorer_file_hashes"].items()
                if name.startswith("code_review_benchmark/") and name.endswith(".py")
            )
        ),
        "bugs_sha256": digest(read_json(archive / "bugs.json")),
        "labels_sha256": digest(read_json(archive / "pr_labels.json")),
    }
    if any(manifest[key] != value for key, value in checks.items()):
        raise ValueError("epoch content hash mismatch")
    if epoch_id is None:
        for filename, key in (
            ("benchmark_data.min.json", "golden_sha256"),
            ("pr_labels.json", "labels_sha256"),
        ):
            if digest(read_json(root / filename)) != manifest[key]:
                raise ValueError("active compatibility input differs from epoch")
    if read_json(archive / "bugs.json") != [asdict(b) for b in bugs]:
        raise ValueError("bug index disagrees with authoritative input")
    subsets = read_json(root / "subsets.json")
    if manifest["pr_count"] != len(data) or manifest[
        "profile_denominators"
    ] != denominators(bugs, profiles, subsets):
        raise ValueError("epoch population/denominator mismatch")
    for name, expected in manifest.get("file_hashes", {}).items():
        if file_hash(confined(archive, name)) != expected:
            raise ValueError("epoch archived file hash mismatch")
    return GoldenEpoch(
        **{
            k: manifest[k]
            for k in EpochWire.__annotations__
            if k
            not in ("supported_profiles", "profile_denominators", "scorer_file_hashes")
        },
        scorer_file_hashes=tuple(sorted(manifest["scorer_file_hashes"].items())),
        supported_profiles=tuple(profiles),
        profile_denominators=tuple(
            (p, tuple(sorted(v.items())))
            for p, v in sorted(manifest["profile_denominators"].items())
        ),
        bugs=bugs,
        root=archive,
    )
