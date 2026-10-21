"""Leaf score identities and confined artifact IO shared across layers."""

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from bench.runner.epochs import Profile, canonical


@dataclass(frozen=True, slots=True)
class ScoreRef:
    run_id: str
    epoch_id: str
    score_id: str


class LedgerProjection(TypedDict, total=False):
    packet_sha256: str
    golden_recall: float | None
    valid_extra_rate: float
    noise_rate: float
    precision_strict: float
    f1_strict: float
    f2_strict: float
    tp: int
    fp: int
    fn: int
    recall: float | None
    status: Literal["scored", "empty"]
    total_candidates: int
    n_prs: int
    per_bucket: dict[str, int]
    drift: dict[str, Any]
    per_dimension: dict[str, Any]
    dedup_cost_usd: float | None
    judge_cost_usd: float | None
    adjudicator_cost_usd: float | None
    attribution_cost_usd: float | None


class LedgerRow(LedgerProjection):
    schema_version: Literal[2]
    run_id: str
    ts: str
    git_sha: str
    tier: str
    tool: str
    hypothesis: str | None
    change: str | None
    runs: int
    tokens_total: int
    cost_usd: float
    per_model: dict[str, Any]
    auth_mode: Literal["api", "subscription"]
    judge_pin: str
    adjudicator_pin: str
    scorer_sha: str
    envelope: dict[str, Any]
    score_id: str
    epoch_id: str
    golden_sha256: str
    profile: Profile
    denominator: int
    evaluator_id: str
    validity_state: Literal["valid", "operational_failure", "historical_descriptive"]
    population_sha256: str
    packet_path: str
    selected_attempts: dict[str, str]
    review_attempt_costs: dict[str, float | None]
    judge_calls: int
    adjudicator_calls: int


def artifact_allowed(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return not any(
        part in ("pairing", "auth.json", ".codex")
        or part.startswith(".env")
        or "credential" in part
        for part in parts
    ) and not ("backups" in parts and path.suffix.lower() == ".gpg")


def exclusive_json(path: Path, value: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def copy_exclusive(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def selected_attempt_path(run_dir, attempt_id):
    if not isinstance(attempt_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", attempt_id
    ):
        raise ValueError("invalid selected attempt identity")
    root = Path(run_dir)
    paths = list((root / "attempts").glob(f"*/{attempt_id}/attempt.json"))
    if len(paths) != 1 or not paths[0].resolve().is_relative_to(root.resolve()):
        raise ValueError("selected attempt is missing or ambiguous")
    return paths[0]


def _pr_number(pr):
    number = next(
        (pr[k] for k in ("pr_number", "number", "n") if pr.get(k) is not None),
        _parse_url(pr.get("url", ""))[2],
    )
    if number is None:
        raise KeyError("pr dict has no pr_number/number and no parseable url")
    return number


def _parse_url(url):
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url or "")
    return (m[1], m[2], int(m[3])) if m else (None, None, None)


def _owner(pr):
    return pr.get("owner") or _parse_url(pr.get("url", ""))[0]


def _repo(pr):
    return pr.get("repo") or _parse_url(pr.get("url", ""))[1]


def _slug(part):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(part or "")).strip("-")


def pr_dir_name(pr):
    return f"pr-{_slug(_owner(pr))}-{_slug(_repo(pr))}-{_pr_number(pr)}"


def _load_dotenv_key(path, key):
    value = None
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            name, separator, raw = line.strip().partition("=")
            if separator and not name.startswith("#") and name.strip() == key:
                value = raw.strip().strip("'\"") or None
    except OSError:
        return None
    return value


def parse_result_envelope(text):
    text = (text or "").strip()
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, end = decoder.raw_decode(text[match.start() :])
        except ValueError:
            continue
        if obj.get("type") == "result" or (match.start() == 0 and end == len(text)):
            return obj
    return None


def _is_superseded(path):
    return "superseded" in Path(path).parts
