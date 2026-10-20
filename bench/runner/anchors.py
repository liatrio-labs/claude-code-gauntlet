"""Epoch-scoped anchor rejudging with content-addressed cache inputs."""

import uuid
from dataclasses import asdict
from pathlib import Path

from bench.adjudicator.adjudicate import adjudicate as _adjudicate
from bench.adjudicator.adjudicate import slice_hunk
from bench.runner import observations, score, scorer
from bench.runner.epochs import DEFAULT_ROOT, digest, file_hash, load_epoch, read_json
from bench.runner.observations import (
    Adjudication,
    AdjudicationContext,
    ScoreRef,
    ScoreRequest,
)
from bench.runner.profiles import profile_metrics
from bench.runner.score import (
    _judge_api_key,
    _load_json,
    _write_json,
    compute_metrics,
)

__all__ = ["ANCHOR_TOOLS", "rejudge_anchors", "spot_check"]

# bench/runner/anchors.py -> parents[2] is the plugin root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
ANCHORS_CANDIDATES = BENCH_DIR / "golden" / "anchors" / "candidates.json"
SUBSETS_PATH = BENCH_DIR / "golden" / "subsets.json"
WORKSPACE = BENCH_DIR / "workspace"

MARTIAN_BASE_URL = score.MARTIAN_BASE_URL

# The judge whose committed evaluations the spot-check reproduces.
UPSTREAM_JUDGE = "claude-opus-4-5-20251101"
UPSTREAM_RESULTS = (
    BENCH_DIR
    / "workspace"
    / "upstream"
    / "offline"
    / "results"
    / "anthropic_claude-opus-4-5-20251101"
)
UPSTREAM_EVAL = UPSTREAM_RESULTS / "evaluations.json"

ANCHOR_TOOLS = ("claude", "claude-code", "coderabbit")

_COUNT_KEYS = ("tp", "fp", "fn")
_TOLERANCE = 1  # ±1 per count per tool: judge nondeterminism at temp 0.
_MAX_DIFF_CHARS = 30000  # cap on the full-diff adjudicator context per anchor comment.


# ------------------------------------------------------------- spot-check


def _counts(ev):
    """Return ``{tp, fp, fn, present}``; a missing key reads 0, a missing
    evaluation reads None (so tolerance never passes on absent data)."""
    if not isinstance(ev, dict):
        return {"tp": None, "fp": None, "fn": None, "present": False}
    return {
        "tp": ev.get("tp", 0),
        "fp": ev.get("fp", 0),
        "fn": ev.get("fn", 0),
        "present": True,
    }


def _matched_texts(ev):
    if not isinstance(ev, dict):
        return []
    return [tp.get("matched_candidate") for tp in ev.get("true_positives", [])]


def _fp_texts(ev):
    if not isinstance(ev, dict):
        return []
    return [fp.get("candidate") for fp in ev.get("false_positives", [])]


def _within_tolerance(ours, theirs):
    for k in _COUNT_KEYS:
        a, b = ours.get(k), theirs.get(k)
        if a is None or b is None:
            return False
        if abs(a - b) > _TOLERANCE:
            return False
    return True


def spot_check(
    pr_url,
    judge_model=UPSTREAM_JUDGE,
    api_key=None,
    env=None,
    run_scorer=None,
    results_dir=None,
    anchors_path=None,
    upstream_eval_path=None,
    diff_out=None,
    *,
    epoch_id: str,
):
    """Re-judge one PR's stored anchor candidates and diff against upstream.

    Stages the three anchors' committed candidates for ``pr_url``, runs the
    vendored dedup+judge under ``judge_model`` via the Anthropic OpenAI-compat
    endpoint, and compares the resulting per-tool TP/FP/FN against the repo's
    committed ``evaluations.json`` for that same judge. Returns a report dict
    with ``pass`` True iff every tool's counts are within ±1 of upstream. Always
    writes the full per-comment comparison to ``diff_out`` (default
    ``bench/workspace/spot-check-diff.json``) for verbatim inspection when the
    drift is larger. ``run_scorer`` is injectable so tests run with no network.
    """
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    anchors = _load_json(anchors_path or ANCHORS_CANDIDATES)
    if pr_url not in anchors:
        raise ValueError(f"pr_url not in anchor candidates: {pr_url!r}")
    tools_map = anchors[pr_url]
    tools = [t for t in ANCHOR_TOOLS if t in tools_map]
    candidates_by_url = {pr_url: {t: tools_map[t] for t in tools}}

    key = api_key or _judge_api_key(env)
    if not key and run_scorer is None:
        raise RuntimeError(
            "no judge API key: set BENCH_JUDGE_API_KEY or ANTHROPIC_API_KEY "
            "(env or bench/.env)"
        )

    work_dir = (
        Path(results_dir) if results_dir else WORKSPACE / "spot-checks"
    ) / uuid.uuid4().hex
    model_dir = scorer.stage_inputs(epoch, candidates_by_url, judge_model, work_dir)
    if run_scorer is None:
        scorer.execute(epoch, judge_model, key, model_dir, tool=None, env=env)
    else:
        run_scorer(
            judge_model,
            key,
            MARTIAN_BASE_URL,
            str(model_dir / "dedup_groups.json"),
            env=env,
        )

    evaluations = _load_json(model_dir / "evaluations.json")
    upstream = _load_json(upstream_eval_path or UPSTREAM_EVAL)

    per_tool = {}
    overall_pass = True
    n_judge_calls = 0
    n_dedup_calls = 0
    n_golden = len(
        (_load_json(epoch.root / "benchmark_data.min.json").get(pr_url) or {}).get(
            "golden_comments", []
        )
    )
    for t in tools:
        ours_ev = (evaluations.get(pr_url) or {}).get(t)
        theirs_ev = (upstream.get(pr_url) or {}).get(t)
        ours = _counts(ours_ev)
        theirs = _counts(theirs_ev)
        within = _within_tolerance(ours, theirs)
        overall_pass = overall_pass and within
        n_cands = len(tools_map[t])
        n_judge_calls += n_golden * n_cands
        if n_cands >= 2:
            n_dedup_calls += 1
        per_tool[t] = {
            "ours": {k: ours[k] for k in _COUNT_KEYS},
            "upstream": {k: theirs[k] for k in _COUNT_KEYS},
            "deltas": {
                k: (
                    None
                    if ours[k] is None or theirs[k] is None
                    else ours[k] - theirs[k]
                )
                for k in _COUNT_KEYS
            },
            "within_tolerance": within,
            "n_candidates": n_cands,
            "matched_ours": _matched_texts(ours_ev),
            "matched_upstream": _matched_texts(theirs_ev),
            "fp_ours": _fp_texts(ours_ev),
            "fp_upstream": _fp_texts(theirs_ev),
        }

    report = {
        "epoch_id": epoch.epoch_id,
        "pr_url": pr_url,
        "judge_model": judge_model,
        "base_url": MARTIAN_BASE_URL,
        "n_golden": n_golden,
        "tools": tools,
        "per_tool": per_tool,
        "pass": overall_pass,
        "tolerance": _TOLERANCE,
        "spend_estimate": {
            "judge_calls": n_judge_calls,
            "dedup_calls": n_dedup_calls,
        },
    }

    out_path = Path(diff_out) if diff_out else (WORKSPACE / "spot-check-diff.json")
    _write_json(out_path, report)
    report["diff_path"] = str(out_path)
    return report


# ------------------------------------------------------------- rejudge (Step 3)


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


def _adjudicate_anchor_bucket(
    buckets,
    candidates,
    tool,
    pin,
    api_key,
    adjudicator,
    diffs,
    *,
    context_records=None,
    candidate_ids=None,
):
    """Unlocated anchor comments need the capped full diff for grounding."""
    verdicts = []
    for url, split in buckets.items():
        adj_texts = set(split["adjudicator"])
        diff_text = (diffs or {}).get(url, "")
        full = _capped_diff(diff_text) if diff_text else ""
        for index, cand in enumerate(candidates.get(url, {}).get(tool, [])):
            text = cand.get("text")
            if text not in adj_texts:
                continue
            path = cand.get("path")
            line = cand.get("line")
            hunk = full  # default: the whole (capped) PR diff for anchor comments
            ctx = ""
            if diff_text and path and line:
                try:
                    hunk = slice_hunk(diff_text, path, line)
                except ValueError:
                    hunk = full
            status = (
                "full_diff"
                if full and hunk == full
                else "hunk_only"
                if hunk
                else "missing"
            )
            truncated = status == "full_diff" and len(diff_text) > _MAX_DIFF_CHARS
            context_hash = digest([hunk, ctx, status, truncated])
            verdict = dict(adjudicator(text, hunk, ctx, pin, api_key))
            if context_records is not None:
                cid = candidate_ids[url, index]
                context_records.append(
                    AdjudicationContext(cid, hunk, ctx, status, truncated, context_hash)
                )
                verdict.update(
                    candidate_id=cid, context_sha256=context_hash, context_status=status
                )
            verdict["url"] = url
            verdict["tool"] = tool
            verdict["candidate"] = text
            verdicts.append(verdict)
    return verdicts


def rejudge_anchors(
    judge_pin,
    tools=ANCHOR_TOOLS,
    subset="gate",
    anchors_path=None,
    subsets_path=None,
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
    anchors = _load_json(anchors_file)
    subs = _load_json(subsets_path or SUBSETS_PATH)
    if subset not in subs:
        raise ValueError(f"unknown subset {subset!r}")
    candidates = _subset_candidates(anchors, subs[subset], tools)
    if set(candidates) != set(subs[subset]) or any(
        set(t) != set(tools) for t in candidates.values()
    ):
        raise ValueError("incomplete anchor candidate matrix")
    baselines = read_json(epoch.root / "baselines.json")
    adj_pin = baselines.get("adjudicator_pin") or judge_pin
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
        cached = _load_json(cache_file)
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
    observations.exclusive_json(
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
    evaluations = _load_json(model_dir / "evaluations.json")
    raw_groups = _load_json(model_dir / "dedup_groups.json")
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
        matched = {m.candidate_id for b in bugs for m in b.matches}
        buckets = {
            u: {
                "golden_matched": [
                    c.text
                    for c in candidate_map
                    if c.pr_url == u and c.candidate_id in matched
                ],
                "adjudicator": [
                    c.text
                    for c in candidate_map
                    if c.pr_url == u and c.candidate_id not in matched
                ],
            }
            for u in tool_candidates
        }
        contexts: list[AdjudicationContext] = []
        adjudications = _adjudicate_anchor_bucket(
            buckets,
            tool_candidates,
            tool,
            adj_pin,
            key_api,
            adjudicator or _adjudicate,
            diffs,
            context_records=contexts,
            candidate_ids={(c.pr_url, c.index): c.candidate_id for c in candidate_map},
        )
        if any(a.get("bucket") not in ("noise", "valid_extra") for a in adjudications):
            raise ValueError("invalid anchor adjudication")
        for ctx in contexts:
            observations.exclusive_json(
                work_dir / "contexts" / tool / (ctx.candidate_id + ".json"), asdict(ctx)
            )
        verdict_records = [
            Adjudication(
                a["candidate_id"],
                a["url"],
                a["bucket"],
                a.get("reason"),
                evaluator.evaluator_id,
                a["context_sha256"],
                a["context_status"],
                None,
            )
            for a in adjudications
        ]
        metrics = compute_metrics(
            evaluations, tool_candidates, buckets, adjudications, tool=tool
        )
        fp = sum(evaluations[u][tool]["fp"] for u in tool_candidates)
        profiles = {
            p: profile_metrics(bugs, fp, p).to_wire() for p in epoch.supported_profiles
        }
        primary = profiles[epoch.primary_profile]
        per_tool[tool] = {
            **metrics,
            **primary,
            "recall": primary["recall"],
            "golden_recall": primary["recall"],
            "profiles": profiles,
            "epoch_id": epoch.epoch_id,
            "profile": epoch.primary_profile,
            "per_dimension": {},
        }
        observations.exclusive_json(
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
    observations.exclusive_json(work_dir / "result.json", result)
    observations.exclusive_json(
        work_dir / "complete.json",
        {
            "files": {
                p.relative_to(work_dir).as_posix(): file_hash(p)
                for p in sorted(work_dir.rglob("*"))
                if p.is_file()
            }
        },
    )
    observations.exclusive_json(cache_file, result)
    return result
