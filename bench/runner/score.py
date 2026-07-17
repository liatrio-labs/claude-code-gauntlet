"""Scoring orchestration: judge pin, vendored scorer, bucket join, adjudicator.

This is the seam between a completed bench run (per-PR dry-run payloads under
``{run_dir}/pr-{n}/``) and a single scored ledger row. The flow (spec H5) is:

1. Assemble candidates from each PR's ``post-review-payload.json`` via the
   adapter, keyed by golden URL (the checkpoint state files under
   ``{run_dir}/state/`` are the source of truth for which URLs ran and their
   golden identity).
2. Stage inputs for the vendored MIT scorer: write ``candidates.json`` and a
   ``benchmark_data.json`` into the scorer's cwd-relative ``results/`` tree.
   The min golden file ships with ``reviews: []``; the scorer forms work items
   only from ``reviews``, so we inject a ``deep-review`` review stub per scored
   URL — without it step3 judges nothing.
3. Run the scorer's dedup (step2_5) then judge (step3) stages under the pinned
   Opus 4.8 snapshot via ``uv run`` (never imported — CLAUDE.md stdlib-only).
4. Parse ``evaluations.json`` and perform the BUCKET JOIN: every candidate whose
   text is a ``matched_candidate`` in ``true_positives`` is golden-matched; every
   other candidate goes to the adjudicator. The join is by exact candidate text
   and asserts a bijection — a matched text absent from our submitted candidates
   raises with a diagnostic rather than guessing.
5. Adjudicate each non-golden-matched comment (valid_extra | noise).
6. Compute per-run metrics, append one ledger row, and write ``scores.json``.

``resolve_judge_pin`` resolves the Opus 4.8 alias to its dated snapshot id once
(models API) and pins it in ``baselines.json``; scoring refuses to run if the
pin is null or if a set ``MARTIAN_MODEL`` disagrees with it.

HTTP and the scorer subprocess live behind module functions / injectable
parameters so the whole pipeline is testable with no network and no keys.
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from bench.adapter.adapt import merge_candidates, payload_to_candidates
from bench.adjudicator.adjudicate import adjudicate as _adjudicate
from bench.adjudicator.adjudicate import file_context, slice_hunk
from bench.runner.costs import parse_costs
from bench.runner.ledger import append_row

__all__ = [
    "resolve_judge_pin",
    "score_run",
    "bucket_join",
    "compute_metrics",
]

# bench/runner/score.py -> parents[2] is the plugin root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
BASELINES_PATH = BENCH_DIR / "baselines.json"
VENDOR_DIR = BENCH_DIR / "vendor" / "code-review-benchmark"
GOLDEN_DATA = BENCH_DIR / "golden" / "benchmark_data.min.json"
LEDGER_PATH = BENCH_DIR / "experiments.jsonl"

MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
MARTIAN_BASE_URL = "https://api.anthropic.com/v1/"

TOOL = "deep-review"

# A dated Opus 4.8 snapshot: the alias plus an 8-digit (or longer) date suffix.
# The ``[1m]`` context variant has no digit suffix and is intentionally excluded.
_OPUS_48_DATED = re.compile(r"^claude-opus-4-8-(\d{6,})$")
_SEVERITY_RE = re.compile(r"\[(CRITICAL|HIGH|MEDIUM|LOW)\]", re.IGNORECASE)
_PULL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


# ------------------------------------------------------------------- baselines/env


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _load_baselines(baselines_path=None):
    path = Path(baselines_path) if baselines_path else BASELINES_PATH
    if not path.exists():
        return {
            "judge_pin": None,
            "adjudicator_pin": None,
            "scorer_sha": None,
            "anchors": {},
            "judge_sd": None,
            "delta_noise": None,
            "notes": {},
        }
    return _load_json(path)


def _parse_dotenv(path):
    """Return a dict from a ``KEY=VALUE`` .env file (quotes/comments stripped)."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return values


def _judge_api_key(env=None):
    """Resolve the judge API key: BENCH_JUDGE_API_KEY, else ANTHROPIC_API_KEY.

    Falls back to ``bench/.env`` for whichever is not already in the environment
    (the owner runs single-key: the judge uses the ANTHROPIC_API_KEY fallback).
    """
    env = os.environ if env is None else env
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        value = env.get(name)
        if value:
            return value
    dotenv = _parse_dotenv(BENCH_DIR / ".env")
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        value = dotenv.get(name)
        if value:
            return value
    return None


# --------------------------------------------------------------------- judge pin


def _http_get_json(url, headers):
    """GET ``url`` and return the decoded JSON body (isolated for test injection)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_opus_48_snapshot(models_json):
    """Return the newest dated ``claude-opus-4-8-<date>`` id, or None."""
    data = models_json.get("data") if isinstance(models_json, dict) else None
    if not isinstance(data, list):
        return None
    dated = []
    for entry in data:
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not model_id:
            continue
        m = _OPUS_48_DATED.match(model_id)
        if m:
            dated.append((int(m.group(1)), model_id))
    if not dated:
        return None
    dated.sort()
    return dated[-1][1]


def resolve_judge_pin(env=None, force=False, baselines_path=None):
    """Resolve and pin the Opus 4.8 dated judge snapshot into ``baselines.json``.

    Idempotent: if ``baselines.json`` already carries a ``judge_pin`` it is
    returned without any API call unless ``force=True``. Otherwise the models
    API is queried (``x-api-key`` from the judge key) and the newest dated
    snapshot is written back. Raises ``RuntimeError`` if no key is available or
    no dated snapshot is found.
    """
    path = Path(baselines_path) if baselines_path else BASELINES_PATH
    baselines = _load_baselines(path)
    existing = baselines.get("judge_pin")
    if existing and not force:
        return existing

    api_key = _judge_api_key(env)
    if not api_key:
        raise RuntimeError(
            "no judge API key: set BENCH_JUDGE_API_KEY or ANTHROPIC_API_KEY "
            "(env or bench/.env)"
        )

    models_json = _http_get_json(
        MODELS_URL,
        {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
    )
    pin = _pick_opus_48_snapshot(models_json)
    if not pin:
        raise RuntimeError(
            "no dated claude-opus-4-8 snapshot found in the models list"
        )

    baselines["judge_pin"] = pin
    _write_json(path, baselines)
    return pin


# ------------------------------------------------------- candidate assembly


def _pull_number(url):
    m = _PULL_RE.search(url or "")
    if not m:
        raise ValueError("cannot parse a GitHub pull number from url: {!r}".format(url))
    return int(m.group(3))


def _run_pr_records(run_dir):
    """Return ``[(golden_url, status)]`` from the run's checkpoint state files."""
    state_dir = Path(run_dir) / "state"
    records = []
    if state_dir.is_dir():
        for path in sorted(state_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            url = rec.get("url")
            if url:
                records.append((url, rec.get("status")))
    return records


def _assemble_candidates(run_dir, pr_records):
    """Build ``{golden_url: {tool: [candidate...]}}`` for every scored ("ok") PR.

    Returns ``(candidates, per_pr)`` where ``per_pr[url]`` carries the PR dir,
    number, and the candidate list (with path/line) used later for adjudication.
    """
    run_dir = Path(run_dir)
    parts = []
    per_pr = {}
    for url, status in pr_records:
        if status != "ok":
            continue
        number = _pull_number(url)
        pr_dir = run_dir / "pr-{}".format(number)
        payload_path = pr_dir / "post-review-payload.json"
        if payload_path.is_file():
            result, _stats = payload_to_candidates(payload_path, url, tool=TOOL)
        else:
            result = {url: {TOOL: []}}
        parts.append(result)
        per_pr[url] = {
            "pr_dir": str(pr_dir),
            "number": number,
            "candidates": result[url][TOOL],
        }
    if parts:
        merged, _ = merge_candidates(parts)
    else:
        merged = {}
    return merged, per_pr


def _prepare_scorer_inputs(candidates, results_dir, model_dir):
    """Stage ``candidates.json`` and an injected ``benchmark_data.json``.

    The vendored ``step3`` reads goldens from ``results/benchmark_data.json``
    (a cwd-relative module constant) and forms one work item per entry in each
    PR's ``reviews`` list. The shipped min file has ``reviews: []``, so a
    ``deep-review`` review stub is injected for every scored URL; PRs not in the
    run keep an empty ``reviews`` and are skipped by the scorer.
    """
    results_dir = Path(results_dir)
    model_dir = Path(model_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    _write_json(model_dir / "candidates.json", candidates)

    golden = _load_json(GOLDEN_DATA)
    missing = [url for url in candidates if url not in golden]
    if missing:
        raise ValueError(
            "scored URLs absent from benchmark_data.min.json: {}".format(missing)
        )
    for url, entry in golden.items():
        if url in candidates:
            entry = dict(entry)
            entry["reviews"] = [
                {
                    "tool": TOOL,
                    "review_comments": [],
                    "repo_name": entry.get("source_repo"),
                    "pr_url": url,
                }
            ]
            golden[url] = entry
    _write_json(results_dir / "benchmark_data.json", golden)


def _sanitize_model_name(model):
    """Mirror the vendored scorer's results-dir naming (``/`` -> ``_``)."""
    return model.strip().replace("/", "_")


def _run_scorer_stages(pin, api_key, model_dir):
    """Run dedup (step2_5) then judge (step3) via ``uv run`` in the vendor dir."""
    if not api_key:
        raise RuntimeError("no judge API key available to run the scorer")
    env = dict(os.environ)
    env["MARTIAN_BASE_URL"] = MARTIAN_BASE_URL
    env["MARTIAN_API_KEY"] = api_key
    env["MARTIAN_MODEL"] = pin

    sanitized = _sanitize_model_name(pin)
    dedup_rel = "results/{}/dedup_groups.json".format(sanitized)

    subprocess.run(
        ["uv", "run", "python", "-m",
         "code_review_benchmark.step2_5_dedup_candidates", "--tool", TOOL],
        cwd=str(VENDOR_DIR), env=env, check=True,
    )
    subprocess.run(
        ["uv", "run", "python", "-m",
         "code_review_benchmark.step3_judge_comments",
         "--tool", TOOL, "--dedup-groups", dedup_rel],
        cwd=str(VENDOR_DIR), env=env, check=True,
    )


# --------------------------------------------------------------------- bucket join


def bucket_join(candidates, evaluations, tool=TOOL):
    """Split each PR's candidates into golden-matched vs adjudicator buckets.

    Golden-matched = candidates whose exact text is a ``matched_candidate`` in
    the judge's ``true_positives``; everything else goes to the adjudicator. The
    join is by exact text and asserts a bijection: a matched text that is not one
    of our submitted candidates raises ``ValueError`` (a plumbing/identity bug),
    and an ambiguous candidate text (submitted more than once for the same PR)
    raises too, because the split would be ill-defined. Returns
    ``{golden_url: {"golden_matched": [text...], "adjudicator": [text...]}}``.
    """
    result = {}
    for url, tools in candidates.items():
        cand_texts = [c["text"] for c in tools.get(tool, [])]
        counts = {}
        for text in cand_texts:
            counts[text] = counts.get(text, 0) + 1

        ev = (evaluations.get(url) or {}).get(tool)
        if ev is None:
            if cand_texts:
                raise ValueError(
                    "bucket join: {} submitted {} candidate(s) but has no "
                    "evaluation for tool {!r}".format(url, len(cand_texts), tool)
                )
            result[url] = {"golden_matched": [], "adjudicator": []}
            continue

        matched_texts = [tp.get("matched_candidate") for tp in ev.get("true_positives", [])]
        cand_set = set(cand_texts)
        for text in matched_texts:
            if text not in cand_set:
                raise ValueError(
                    "bucket join bijection violation for {}: matched_candidate "
                    "is not one of the submitted candidates: {!r}".format(url, text)
                )
            if counts.get(text, 0) != 1:
                raise ValueError(
                    "bucket join for {}: candidate text is ambiguous (submitted "
                    "{} times): {!r}".format(url, counts.get(text, 0), text)
                )

        matched_set = set(matched_texts)
        golden_matched = [t for t in cand_texts if t in matched_set]
        adjudicator = [t for t in cand_texts if t not in matched_set]
        result[url] = {"golden_matched": golden_matched, "adjudicator": adjudicator}
    return result


# ----------------------------------------------------------------- adjudication


def _head_file_lines(pr_info, path):
    """Best-effort head-file lines for ``path`` from a surviving worktree.

    Worktrees are removed after each PR, so this is usually unavailable and the
    adjudicator runs with hunk-only context. Returns ``[]`` when absent.
    """
    pr_dir = pr_info.get("pr_dir")
    if not pr_dir or not path:
        return []
    candidate = Path(pr_dir) / "worktree" / path
    if candidate.is_file():
        try:
            return candidate.read_text(errors="replace").splitlines()
        except OSError:
            return []
    return []


def _adjudicate_bucket(buckets, per_pr, pin, api_key, adjudicator):
    """Adjudicate every non-golden-matched comment; return a flat verdict list."""
    verdicts = []
    for url, split in buckets.items():
        info = per_pr.get(url, {})
        cand_by_text = {c["text"]: c for c in info.get("candidates", [])}
        diff_path = Path(info.get("pr_dir", "")) / "diff.patch"
        diff_text = diff_path.read_text(errors="replace") if diff_path.is_file() else ""

        for text in split["adjudicator"]:
            cand = cand_by_text.get(text, {})
            path = cand.get("path")
            line = cand.get("line")
            hunk = ""
            ctx = ""
            if diff_text and path and line:
                try:
                    hunk = slice_hunk(diff_text, path, line)
                except ValueError:
                    hunk = ""
                head_lines = _head_file_lines(info, path)
                ctx = file_context(head_lines, line) if head_lines else ""
            verdict = dict(adjudicator(text, hunk, ctx, pin, api_key))
            verdict["url"] = url
            verdict["candidate"] = text
            verdicts.append(verdict)
    return verdicts


# ---------------------------------------------------------------------- metrics


def _severity_of(text):
    m = _SEVERITY_RE.search(text or "")
    return m.group(1).upper() if m else "UNKNOWN"


def compute_metrics(evaluations, candidates, buckets, adjudications, tool=TOOL):
    """Compute the per-run metrics dict from evaluations + buckets + verdicts.

    ``golden_recall`` and ``precision_strict = tp/(tp+fp)`` come from the judge's
    aggregate golden/candidate counts (spec H5 standardized precision). The three
    buckets partition the tool's total candidates: golden_matched (from the join)
    + valid_extra + noise (from the adjudicator). Rates are over total candidates.
    ``per_dimension`` is keyed by severity — the rendered PR comment carries a
    severity tag but not the discovery dimension, so severity is the finest
    available breakdown.
    """
    tp = fp = fn = 0
    for _url, ev_tools in evaluations.items():
        ev = ev_tools.get(tool)
        if not ev or ev.get("skipped"):
            continue
        tp += ev.get("tp", 0)
        fp += ev.get("fp", 0)
        fn += ev.get("fn", 0)

    golden_recall = tp / (tp + fn) if (tp + fn) else 0.0
    precision_strict = tp / (tp + fp) if (tp + fp) else 0.0
    if precision_strict + golden_recall:
        f1_strict = 2 * precision_strict * golden_recall / (precision_strict + golden_recall)
    else:
        f1_strict = 0.0

    total_candidates = sum(len(tools.get(tool, [])) for tools in candidates.values())
    n_golden_matched = sum(len(split["golden_matched"]) for split in buckets.values())
    n_valid_extra = sum(1 for v in adjudications if v.get("bucket") == "valid_extra")
    n_noise = sum(1 for v in adjudications if v.get("bucket") == "noise")

    valid_extra_rate = n_valid_extra / total_candidates if total_candidates else 0.0
    noise_rate = n_noise / total_candidates if total_candidates else 0.0

    # drift telltales + severity-keyed dimension breakdown over all candidates.
    texts = [c["text"] for tools in candidates.values() for c in tools.get(tool, [])]
    desc_len_mean = sum(len(t or "") for t in texts) / len(texts) if texts else 0.0
    severity_dist = {}
    per_dimension = {}
    for tools in candidates.values():
        for cand in tools.get(tool, []):
            sev = _severity_of(cand.get("text"))
            severity_dist[sev] = severity_dist.get(sev, 0) + 1
            per_dimension.setdefault(sev, 0)
            per_dimension[sev] += 1

    return {
        "golden_recall": golden_recall,
        "valid_extra_rate": valid_extra_rate,
        "noise_rate": noise_rate,
        "precision_strict": precision_strict,
        "f1_strict": f1_strict,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "total_candidates": total_candidates,
        "n_prs": len(candidates),
        "per_bucket": {
            "golden_matched": n_golden_matched,
            "valid_extra": n_valid_extra,
            "noise": n_noise,
        },
        "per_dimension": per_dimension,
        "drift": {"desc_len_mean": desc_len_mean, "severity_dist": severity_dist},
    }


# ------------------------------------------------------------------- ledger row


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_head():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _read_run_manifest(run_dir):
    path = Path(run_dir) / "run.json"
    if path.is_file():
        try:
            return _load_json(path)
        except (ValueError, OSError):
            return {}
    return {}


def _infer_tier(run_id):
    if run_id and "-" in run_id:
        head = run_id.split("-", 1)[0]
        if head in ("smoke", "subset", "full"):
            return head
    return run_id or "unknown"


def _read_run_costs(run_dir):
    """Sum cost/tokens across the run's per-PR result envelopes; 0s if absent."""
    total_cost = 0.0
    total_tokens = 0
    per_model = {}
    for pr_dir in sorted(Path(run_dir).glob("pr-*")):
        envelope = None
        raw = pr_dir / "raw.json"
        if raw.is_file():
            envelope = _extract_result_envelope(raw.read_text(errors="replace"))
        if envelope is None:
            for name in ("result.json", "costs.json"):
                path = pr_dir / name
                if path.is_file():
                    try:
                        envelope = json.loads(path.read_text(errors="replace"))
                    except (ValueError, OSError):
                        envelope = None
                    break
        if not isinstance(envelope, dict):
            continue
        costs = parse_costs(envelope)
        total_cost += costs["cost_usd"]
        total_tokens += costs["tokens_total"]
        for model, usage in costs["per_model"].items():
            agg = per_model.setdefault(model, {"tokens": 0, "cost_usd": 0.0})
            agg["tokens"] += usage["tokens"]
            agg["cost_usd"] += usage["cost_usd"]
    return {"cost_usd": total_cost, "tokens_total": total_tokens, "per_model": per_model}


def _extract_result_envelope(text):
    """Return the ``type=="result"`` envelope from raw stdout, or None.

    Real ``-p`` stdout is pure JSON; a fake may print the echo block first.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
        idx = text.find("{", idx + 1)
    return None


def _build_ledger_row(run_dir, metrics, costs, manifest, pin, adjudicator_pin, scorer_sha):
    run_id = manifest.get("run_id") or Path(run_dir).name
    tier = manifest.get("tier") or _infer_tier(run_id)
    envelope = {
        "cap": manifest.get("cap", 25),
        "fixtures": manifest.get("fixtures", []),
        "invocation": manifest.get("invocation", "headless:/deep-review"),
    }
    return {
        "run_id": run_id,
        "ts": _now(),
        "git_sha": manifest.get("git_sha") or _git_head(),
        "tier": tier,
        "tool": manifest.get("tool", "deep-review-v2"),
        "hypothesis": manifest.get("hypothesis"),
        "change": manifest.get("change"),
        "n_prs": metrics["n_prs"],
        "runs": manifest.get("runs", 1),
        "golden_recall": metrics["golden_recall"],
        "valid_extra_rate": metrics["valid_extra_rate"],
        "noise_rate": metrics["noise_rate"],
        "precision_strict": metrics["precision_strict"],
        "f1_strict": metrics["f1_strict"],
        "per_bucket": metrics["per_bucket"],
        "per_dimension": metrics["per_dimension"],
        "drift": metrics["drift"],
        "tokens_total": costs["tokens_total"],
        "cost_usd": costs["cost_usd"],
        "per_model": costs["per_model"],
        "judge_pin": pin,
        "adjudicator_pin": adjudicator_pin,
        "scorer_sha": scorer_sha,
        "envelope": envelope,
    }


# --------------------------------------------------------------------- score_run


def score_run(
    run_dir,
    env=None,
    baselines_path=None,
    ledger_path=None,
    run_scorer=None,
    adjudicator=None,
):
    """Score one completed bench run: candidates -> judge -> buckets -> ledger row.

    Returns the scores dict (also written to ``{run_dir}/scores.json``) and
    appends one row to the ledger. Refuses (raises) if the judge pin is null in
    ``baselines.json`` or if a set ``MARTIAN_MODEL`` disagrees with it.

    ``run_scorer`` and ``adjudicator`` are injectable seams (defaulting to the
    real ``uv run`` scorer and the HTTP adjudicator) so the pipeline runs in
    tests with no network and no keys.
    """
    run_dir = Path(run_dir)
    env = os.environ if env is None else env

    baselines = _load_baselines(baselines_path)
    pin = baselines.get("judge_pin")
    if not pin:
        raise RuntimeError(
            "baselines.json judge_pin is null; run resolve_judge_pin() before scoring"
        )
    martian = env.get("MARTIAN_MODEL")
    if martian and martian != pin:
        raise RuntimeError(
            "MARTIAN_MODEL={!r} disagrees with baselines judge_pin={!r}; "
            "refusing to score".format(martian, pin)
        )
    adjudicator_pin = baselines.get("adjudicator_pin") or pin
    scorer_sha = baselines.get("scorer_sha")

    # 1) candidates from each PR payload, keyed by golden URL.
    pr_records = _run_pr_records(run_dir)
    candidates, per_pr = _assemble_candidates(run_dir, pr_records)

    # 2) stage scorer inputs (candidates.json + injected benchmark_data.json).
    sanitized = _sanitize_model_name(pin)
    results_dir = VENDOR_DIR / "results"
    model_dir = results_dir / sanitized
    _prepare_scorer_inputs(candidates, results_dir, model_dir)

    # 3) dedup + judge under the pin (subprocess unless injected).
    api_key = _judge_api_key(env)
    stage_runner = run_scorer if run_scorer is not None else _run_scorer_stages
    stage_runner(pin, api_key, model_dir)

    # 4) parse evaluations.
    evaluations = _load_json(model_dir / "evaluations.json")

    # 5) bucket join (raises on any bijection violation).
    buckets = bucket_join(candidates, evaluations, tool=TOOL)

    # 6) adjudicate non-golden-matched comments.
    adjudicator_fn = adjudicator if adjudicator is not None else _adjudicate
    adjudications = _adjudicate_bucket(buckets, per_pr, adjudicator_pin, api_key, adjudicator_fn)

    # 7) metrics.
    metrics = compute_metrics(evaluations, candidates, buckets, adjudications, tool=TOOL)

    # 8) ledger row + scores.json.
    costs = _read_run_costs(run_dir)
    manifest = _read_run_manifest(run_dir)
    row = _build_ledger_row(
        run_dir, metrics, costs, manifest, pin, adjudicator_pin, scorer_sha
    )
    append_row(str(ledger_path) if ledger_path else str(LEDGER_PATH), row)

    scores = {
        "run_id": row["run_id"],
        "metrics": metrics,
        "buckets": {
            url: {
                "golden_matched": len(split["golden_matched"]),
                "adjudicator": len(split["adjudicator"]),
            }
            for url, split in buckets.items()
        },
        "adjudications": adjudications,
        "judge_pin": pin,
        "adjudicator_pin": adjudicator_pin,
        "scorer_sha": scorer_sha,
        "ledger_row": row,
    }
    _write_json(run_dir / "scores.json", scores)
    return scores
