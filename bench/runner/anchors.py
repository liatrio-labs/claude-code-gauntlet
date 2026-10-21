"""Epoch-scoped anchor rejudging with content-addressed cache inputs."""

import uuid
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from bench.adjudicator.adjudicate import adjudicate as _adjudicate
from bench.adjudicator.adjudicate import slice_hunk
from bench.runner import observations, score, scorer
from bench.runner.contracts import ScoreRef, exclusive_json
from bench.runner.epochs import DEFAULT_ROOT, digest, file_hash, load_epoch, read_json
from bench.runner.observations import (
    ScoreRequest,
)
from bench.runner.profiles import profile_metrics
from bench.runner.score import (
    _judge_api_key,
)

__all__ = ["ANCHOR_TOOLS", "rejudge_anchors"]

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
ANCHORS_CANDIDATES = BENCH_DIR / "golden" / "anchors" / "candidates.json"
WORKSPACE = BENCH_DIR / "workspace"

MARTIAN_BASE_URL = scorer.BASE_URL

ANCHOR_TOOLS = ("claude", "claude-code", "coderabbit")

_MAX_DIFF_CHARS = 30000  # cap on the full-diff adjudicator context per anchor comment.


def _subset_candidates(anchors, urls, tools):
    """Restrict anchor candidates to ``urls`` x ``tools`` (empty entries dropped)."""
    out = {}
    for url in urls:
        tools_map = anchors.get(url, {})
        entry = {t: tools_map[t] for t in tools if t in tools_map}
        if entry:
            out[url] = entry
    return out


def _capped_diff(diff_text, cap=_MAX_DIFF_CHARS):
    """Return ``diff_text`` truncated to ``cap`` chars, marking any truncation."""
    if len(diff_text) <= cap:
        return diff_text
    return diff_text[:cap] + f"\n... [diff truncated at {cap} chars]"


def _anchor_context(diff, candidate):
    full = _capped_diff(diff) if diff else ""
    hunk = full
    if diff and candidate["path"] and candidate["line"]:
        with suppress(ValueError):
            hunk = slice_hunk(diff, candidate["path"], candidate["line"])
    status = (
        "full_diff" if full and hunk == full else "hunk_only" if hunk else "missing"
    )
    return hunk, "", status, status == "full_diff" and len(diff) > _MAX_DIFF_CHARS


def rejudge_anchors(
    judge_pin,
    tools=ANCHOR_TOOLS,
    subset="gate",
    anchors_path=None,
    cache_dir=None,
    api_key=None,
    env=None,
    base_url=None,
    run_scorer=None,
    adjudicator=None,
    diffs=None,
    results_dir=None,
    *,
    epoch_id: str,
):
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    tools = list(tools)
    base_url = base_url or MARTIAN_BASE_URL
    anchors_file = Path(anchors_path or ANCHORS_CANDIDATES)
    anchors = read_json(anchors_file)
    subs = epoch.subsets
    if subset not in subs:
        raise ValueError(f"unknown subset {subset!r}")
    candidates = _subset_candidates(anchors, subs[subset], tools)
    if set(candidates) != set(subs[subset]) or any(
        set(t) != set(tools) for t in candidates.values()
    ):
        raise ValueError("incomplete anchor candidate matrix")
    adj_pin = epoch.adjudicator_pin
    evaluator = score.evaluator_for(epoch, judge_pin, adj_pin)
    key = digest(
        {
            "epoch": observations.epoch_hashes(epoch),
            "evaluator": asdict(evaluator),
            "candidates": candidates,
            "candidate_bytes": file_hash(anchors_file),
            "subset_urls": digest(sorted(subs[subset])),
            "diffs": diffs or {},
            "context_policy": [_MAX_DIFF_CHARS, file_hash(Path(__file__))],
            "base_url": base_url,
        }
    )
    cache_dir = Path(cache_dir) if cache_dir else WORKSPACE / "anchor-cache"
    cache_file = cache_dir / (key + ".json")
    if cache_file.is_file():
        cached = read_json(cache_file)
        work_dir = Path(cached["work_dir"])
        if cached != read_json(work_dir / "result.json") or cached["cache_key"] != key:
            raise ValueError("anchor cache projection mismatch")
        for name, expected in read_json(work_dir / "complete.json")["files"].items():
            if file_hash(observations.confined(work_dir, name)) != expected:
                raise ValueError("anchor packet hash mismatch")
        cached["cache_hit"] = True
        return cached
    key_api = api_key or (_judge_api_key(env) if run_scorer is None else None)
    work_dir = (
        Path(results_dir) if results_dir else cache_dir / "executions"
    ) / uuid.uuid4().hex
    model_dir = scorer.stage_inputs(epoch, candidates, judge_pin, work_dir)
    exclusive_json(
        work_dir / "inputs.json", {"diffs": diffs or {}, "evaluator": asdict(evaluator)}
    )
    if run_scorer is None:
        scorer.execute(
            epoch, judge_pin, key_api, model_dir, tool=None, env=env, base_url=base_url
        )
    else:
        run_scorer(
            judge_pin, key_api, base_url, str(model_dir / "dedup_groups.json"), env=env
        )
    evaluations = read_json(model_dir / "evaluations.json")
    raw_groups = read_json(model_dir / "dedup_groups.json")
    per_tool = {}
    for tool in tools:
        tool_candidates = {u: {tool: t[tool]} for u, t in candidates.items()}
        attempts = {
            u: "frozen-anchor-" + digest(tool_candidates[u]) for u in tool_candidates
        }
        candidate_map = observations.candidate_records(tool_candidates, attempts, tool)
        request = ScoreRequest(
            ScoreRef("anchors-" + tool, epoch.epoch_id, work_dir.name),
            epoch,
            work_dir,
            tuple(sorted(tool_candidates)),
            tuple(sorted(attempts.items())),
            candidate_map,
            tool,
            evaluator,
        )
        groups = observations.dedup_groups(raw_groups, candidate_map, tool)
        bugs = observations.build_observations(request, evaluations, groups)
        matched = observations.bucket_candidate_ids(bugs)
        verdict_records, contexts, _ = score._capture_adjudications(
            request,
            matched,
            {u: (diffs or {}).get(u, "") for u in tool_candidates},
            key_api,
            adjudicator or _adjudicate,
            _anchor_context,
        )
        for ctx in contexts:
            exclusive_json(
                work_dir / "contexts" / tool / (ctx.candidate_id + ".json"), asdict(ctx)
            )
        fp = sum(evaluations[u][tool]["fp"] for u in tool_candidates)
        profiles = {
            p: asdict(profile_metrics(bugs, fp, p)) for p in epoch.supported_profiles
        }
        primary = profiles[epoch.primary_profile]
        per_tool[tool] = {
            **observations.bucket_metrics(candidate_map, bugs, verdict_records),
            **primary,
            "recall": primary["recall"],
            "golden_recall": primary["recall"],
            "profiles": profiles,
            "epoch_id": epoch.epoch_id,
            "profile": epoch.primary_profile,
            "per_dimension": {},
        }
        exclusive_json(
            work_dir / (tool + "-adjudications.json"),
            [asdict(a) for a in verdict_records],
        )
    result = {
        "epoch_id": epoch.epoch_id,
        "golden_sha256": epoch.golden_sha256,
        "profile": epoch.primary_profile,
        "judge_pin": judge_pin,
        "adjudicator_pin": adj_pin,
        "subset": subset,
        "tools": tools,
        "cache_key": key,
        "per_tool": per_tool,
        "cache_hit": False,
        "work_dir": str(work_dir.resolve()),
    }
    exclusive_json(work_dir / "result.json", result)
    exclusive_json(
        work_dir / "complete.json",
        {
            "files": {
                p.relative_to(work_dir).as_posix(): file_hash(p)
                for p in sorted(work_dir.rglob("*"))
                if p.is_file()
            }
        },
    )
    exclusive_json(cache_file, result)
    return result
