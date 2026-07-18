"""Anchors: re-judge the stored third-party tool candidates under our judge.

The benchmark ships upstream-committed candidate sets for many code-review tools.
Three of them are our reference anchors — ``claude``, ``claude-code``, and
``coderabbit`` — re-scored under the harness's own judge pin so a deep-review run
can be read against a stable, independently-produced baseline (spec H5).

Two entry points:

* ``spot_check(pr_url, judge_model=...)`` — a *plumbing* check. It re-judges one
  PR's stored anchor candidates under the **upstream** judge
  (``claude-opus-4-5-20251101``) via the harness's own scorer invocation, then
  diffs the resulting TP/FP/FN counts against the repo's committed
  ``evaluations.json`` for that same judge. Agreement within ±1 per tool (judge
  nondeterminism) proves our invocation reproduces upstream behaviour before any
  real scoring runs on the 4.8 pin. This makes real (small) API spend.

* ``rejudge_anchors(judge_pin)`` — the Step-3 machinery: re-judge all three
  anchors on a subset under the pinned judge (dedup regenerated), adjudicate
  their non-golden-matched comments symmetrically via ``bench/adjudicator``, and
  return per-tool recall / noise / precision. Results are cached keyed by
  ``sha256(judge_pin + candidates)``. Adjudication for anchors uses the whole
  (capped) PR diff as context rather than a sliced hunk, because the upstream
  anchor candidates carry no path/line (see ``_adjudicate_anchor_bucket``). It is
  fully injectable so tests drive it with no network.

Both reuse ``score.py``'s scorer-invocation and bucket-join machinery rather than
restating it (single-producer rule). Only ``bench/vendor/`` pulls third-party
deps; this module is stdlib-only and never imports the scorer (it shells out to
``uv run`` exactly as ``score.py`` does).
"""

import hashlib
import json
from pathlib import Path

from bench.runner import score
from bench.runner.score import (
    _judge_api_key,
    _load_json,
    _sanitize_model_name,
    _write_json,
    bucket_join,
    compute_metrics,
)
from bench.adjudicator.adjudicate import adjudicate as _adjudicate
from bench.adjudicator.adjudicate import file_context, slice_hunk

__all__ = ["spot_check", "rejudge_anchors", "ANCHOR_TOOLS"]

# bench/runner/anchors.py -> parents[2] is the plugin root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
ANCHORS_CANDIDATES = BENCH_DIR / "golden" / "anchors" / "candidates.json"
SUBSETS_PATH = BENCH_DIR / "golden" / "subsets.json"
WORKSPACE = BENCH_DIR / "workspace"

# Vendored-scorer paths and golden data are shared with score.py; mirrored as
# module globals so tests can monkeypatch them on this module.
VENDOR_DIR = score.VENDOR_DIR
GOLDEN_DATA = score.GOLDEN_DATA
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


# ------------------------------------------------------------- scorer staging


def _stage_inputs(candidates_by_url, tools, model, results_dir):
    """Stage ``benchmark_data.json`` + ``candidates.json`` for the scorer.

    Mirrors ``score._prepare_scorer_inputs`` but injects one review stub *per
    requested anchor tool* (score.py injects a single deep-review stub). Only the
    URLs present in ``candidates_by_url`` are written, each carrying its golden
    comments (from the min file) and a stub review per tool that has candidates
    for it — ``step3`` forms exactly those work items.
    """
    results_dir = Path(results_dir)
    model_dir = results_dir / _sanitize_model_name(model)
    results_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    _write_json(model_dir / "candidates.json", candidates_by_url)

    golden = _load_json(GOLDEN_DATA)
    missing = [url for url in candidates_by_url if url not in golden]
    if missing:
        raise ValueError(
            "anchor URLs absent from benchmark_data.min.json: {}".format(missing)
        )
    bench = {}
    for url, tools_map in candidates_by_url.items():
        entry = dict(golden[url])
        entry["reviews"] = [
            {
                "tool": tool,
                "review_comments": [],
                "repo_name": entry.get("source_repo"),
                "pr_url": url,
            }
            for tool in tools
            if tool in tools_map
        ]
        bench[url] = entry
    _write_json(results_dir / "benchmark_data.json", bench)
    return model_dir


def _run_scorer_stages(model, api_key, base_url, dedup_rel, env=None, tool=None):
    """Run dedup (step2_5) then judge (step3) for the anchor paths.

    Delegates to the single implementation ``score.run_scorer_stages`` (``tool=None``
    judges every stubbed review, which is what the anchor paths want) so an anchor
    stage failure surfaces the same stage-named RuntimeError as the main scoring path
    rather than a bare ``CalledProcessError``. ``dedup_rel`` is recomputed from
    ``model`` inside the shared runner (identical value), so the argument passed here
    is unused; it is retained in the signature for the injectable ``run_scorer`` seam.
    """
    score.run_scorer_stages(model, api_key, tool=tool, base_url=base_url, env=env)


def _clear_stale(model_dir):
    """Delete prior dedup/eval outputs so a re-run judges from scratch.

    ``step3``'s ``EvaluationState`` and ``step2_5``'s incremental save both skip
    entries already present; clearing them guarantees the scorer re-judges the
    freshly staged candidates rather than resuming an old run.
    """
    for name in ("evaluations.json", "dedup_groups.json"):
        path = Path(model_dir) / name
        if path.exists():
            path.unlink()


# ------------------------------------------------------------- spot-check


def _counts(ev):
    """Return ``{tp, fp, fn}`` from a step3 evaluation dict (zeros if absent)."""
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
    anchors = _load_json(anchors_path or ANCHORS_CANDIDATES)
    if pr_url not in anchors:
        raise ValueError("pr_url not in anchor candidates: {!r}".format(pr_url))
    tools_map = anchors[pr_url]
    tools = [t for t in ANCHOR_TOOLS if t in tools_map]
    candidates_by_url = {pr_url: {t: tools_map[t] for t in tools}}

    key = api_key or _judge_api_key(env)
    if not key and run_scorer is None:
        raise RuntimeError(
            "no judge API key: set BENCH_JUDGE_API_KEY or ANTHROPIC_API_KEY "
            "(env or bench/.env)"
        )

    results_dir = Path(results_dir) if results_dir else (VENDOR_DIR / "results")
    model_dir = results_dir / _sanitize_model_name(judge_model)
    _clear_stale(model_dir)
    _stage_inputs(candidates_by_url, tools, judge_model, results_dir)

    dedup_rel = "results/{}/dedup_groups.json".format(_sanitize_model_name(judge_model))
    runner = run_scorer if run_scorer is not None else _run_scorer_stages
    runner(judge_model, key, MARTIAN_BASE_URL, dedup_rel, env=env)

    evaluations = _load_json(model_dir / "evaluations.json")
    upstream = _load_json(upstream_eval_path or UPSTREAM_EVAL)

    per_tool = {}
    overall_pass = True
    n_judge_calls = 0
    n_dedup_calls = 0
    n_golden = len((_load_json(GOLDEN_DATA).get(pr_url) or {}).get("golden_comments", []))
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
                k: (None if ours[k] is None or theirs[k] is None else ours[k] - theirs[k])
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


def _cache_key(judge_pin, candidates):
    """``sha256(judge_pin + canonical(candidates))`` — the anchor-cache key."""
    blob = judge_pin + json.dumps(candidates, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _subset_candidates(anchors, urls, tools):
    """Restrict the anchor candidates to ``urls`` × ``tools`` (empty entries dropped)."""
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
    return diff_text[:cap] + "\n... [diff truncated at {} chars]".format(cap)


def _adjudicate_anchor_bucket(buckets, candidates, tool, pin, api_key, adjudicator, diffs):
    """Adjudicate every non-golden-matched comment for one anchor tool.

    Anchor candidates carry no path/line (upstream extracted them without a
    location), so a sliced hunk cannot be built. When a PR diff is available it
    is passed **in full** (capped at ``_MAX_DIFF_CHARS``, truncation marked) as
    the adjudicator's diff context, so check 1 ("the referenced location exists
    in the shown code") stays groundable for anchors; head-file ±5-line context
    is still unavailable (anchor PRs are not checked out). If a candidate does
    carry path/line and the diff contains that path, the sliced hunk is used
    instead (forward-compatible with located candidates).

    Mirrors ``score._adjudicate_bucket``: ``bucket_join`` permits duplicate
    UNMATCHED candidate texts, so this iterates the tool's candidate records and
    selects those whose text is in the adjudicator bucket, adjudicating EACH with
    its own path/line-derived context — rather than through a text-keyed dict that
    would collapse same-body comments at different locations to a single context.
    (Anchor candidates carry null path/line today, so every such comment still gets
    the whole capped diff and behaviour is unchanged in practice; this is
    forward-parity for located candidates.)
    """
    verdicts = []
    for url, split in buckets.items():
        adj_texts = set(split["adjudicator"])
        diff_text = (diffs or {}).get(url, "")
        full = _capped_diff(diff_text) if diff_text else ""
        for cand in candidates.get(url, {}).get(tool, []):
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
            verdict = dict(adjudicator(text, hunk, ctx, pin, api_key))
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
):
    """Re-judge the anchors on ``subset`` under ``judge_pin``; per-tool metrics.

    Regenerates dedup under the pin, judges the stored anchor candidates, splits
    each tool's comments golden-matched vs adjudicator (``bucket_join``),
    classifies the latter symmetrically via ``bench/adjudicator``, and computes
    recall / noise_rate / precision_strict per tool (``compute_metrics``).

    Results are cached under ``cache_dir`` keyed ``sha256(judge_pin +
    candidates)`` — a settled pin re-reads instead of re-spending. ``run_scorer``
    and ``adjudicator`` are injectable so the whole path runs in tests with no
    network; this function is not invoked live until the 4.8 pin is settled.
    """
    tools = list(tools)
    base_url = base_url or MARTIAN_BASE_URL
    anchors = _load_json(anchors_path or ANCHORS_CANDIDATES)
    subs = _load_json(subsets_path or SUBSETS_PATH)
    if subset not in subs:
        raise ValueError("unknown subset {!r} (have: {})".format(subset, sorted(subs)))
    candidates = _subset_candidates(anchors, subs[subset], tools)

    cache_dir = Path(cache_dir) if cache_dir else (WORKSPACE / "anchor-cache")
    key = _cache_key(judge_pin, candidates)
    cache_file = cache_dir / (key + ".json")
    if cache_file.is_file():
        cached = _load_json(cache_file)
        cached["cache_hit"] = True
        return cached

    key_api = api_key or _judge_api_key(env)
    results_dir = Path(results_dir) if results_dir else (VENDOR_DIR / "results")
    model_dir = results_dir / _sanitize_model_name(judge_pin)
    _clear_stale(model_dir)
    _stage_inputs(candidates, tools, judge_pin, results_dir)

    dedup_rel = "results/{}/dedup_groups.json".format(_sanitize_model_name(judge_pin))
    runner = run_scorer if run_scorer is not None else _run_scorer_stages
    runner(judge_pin, key_api, base_url, dedup_rel, env=env)

    evaluations = _load_json(model_dir / "evaluations.json")
    adjudicator_fn = adjudicator if adjudicator is not None else _adjudicate

    per_tool = {}
    for t in tools:
        tool_cands = {
            url: {t: tools_map[t]}
            for url, tools_map in candidates.items()
            if t in tools_map
        }
        buckets = bucket_join(tool_cands, evaluations, tool=t)
        adjudications = _adjudicate_anchor_bucket(
            buckets, tool_cands, t, judge_pin, key_api, adjudicator_fn, diffs
        )
        metrics = compute_metrics(evaluations, tool_cands, buckets, adjudications, tool=t)
        per_tool[t] = {
            "recall": metrics["golden_recall"],
            "noise_rate": metrics["noise_rate"],
            "precision_strict": metrics["precision_strict"],
            "valid_extra_rate": metrics["valid_extra_rate"],
            "per_bucket": metrics["per_bucket"],
            "tp": metrics["tp"],
            "fp": metrics["fp"],
            "fn": metrics["fn"],
            "total_candidates": metrics["total_candidates"],
            "n_prs": metrics["n_prs"],
        }

    result = {
        "judge_pin": judge_pin,
        "subset": subset,
        "tools": tools,
        "cache_key": key,
        "per_tool": per_tool,
        "cache_hit": False,
    }
    _write_json(cache_file, result)
    return result
