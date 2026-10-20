"""Epoch-aware scoring orchestration with immutable execution packets."""

import json
import os
import re
import subprocess
import time
import urllib.request
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from bench.adapter.adapt import merge_candidates, payload_to_candidates
from bench.adjudicator.adjudicate import adjudicate as _adjudicate
from bench.adjudicator.adjudicate import file_context, slice_hunk
from bench.runner import invoke, observations, provenance, scorer, stages
from bench.runner.costs import parse_costs
from bench.runner.epochs import (
    DEFAULT_ROOT,
    PROFILE_POLICY_VERSION,
    digest,
    file_hash,
    load_epoch,
)
from bench.runner.invoke import _is_superseded
from bench.runner.ledger import manifest_auth_mode, replay_packet, review_attempt_costs
from bench.runner.observations import (
    Adjudication,
    AdjudicationContext,
    AdjudicationWire,
    CompatibilityScorePacketWire,
    Evaluator,
    EvaluatorWire,
    LegacyMetricsWire,
    RunMetricsWire,
    ScoreManifestWire,
    ScorePacketWire,
    ScoreRef,
    ScoreRefWire,
    ScoreRequest,
)
from bench.runner.profiles import profile_metrics

__all__ = [
    "compute_metrics",
    "resolve_judge_pin",
    "score_run",
]

# bench/runner/score.py -> parents[2] is the plugin root.
REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
BASELINES_PATH = BENCH_DIR / "baselines.json"
LEDGER_PATH = BENCH_DIR / "experiments.jsonl"

MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

TOOL = "deep-review"

# A dated snapshot id: the family alias plus a 6-digit (or longer) date suffix.
# ``_OPUS_48_DATED`` matches only the 4.8 family; ``_OPUS_DATED`` matches any dated
# Opus (4-8, 4-5, 4-1, ...). The ``[1m]`` context variant has no digit suffix and is
# intentionally excluded by both.
_OPUS_48_DATED = re.compile(r"^claude-opus-4-8-(\d{6,})$")
_OPUS_DATED = re.compile(r"^claude-opus-(?:\d+-)+(\d{6,})$")
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


def _judge_api_key(env=None):
    """Resolve the judge API key: BENCH_JUDGE_API_KEY, else ANTHROPIC_API_KEY.

    Falls back to ``bench/.env`` for whichever is not already in the environment
    (the owner runs single-key: the judge uses the ANTHROPIC_API_KEY fallback).
    Delegates to :func:`invoke._load_dotenv_key` — the single dotenv parser — so
    prereqs, build_env, and the judge can never disagree on an edge case.
    """
    env = os.environ if env is None else env
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        value = env.get(name)
        if value:
            return value
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        value = invoke._load_dotenv_key(BENCH_DIR / ".env", name)
        if value:
            return value
    return None


# --------------------------------------------------------------------- judge pin


def _http_get_json(url, headers):
    """GET ``url`` and return the decoded JSON body (isolated for test injection)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_opus_snapshot(models_json):
    """Return the judge snapshot id: newest dated Opus 4.8, else newest dated Opus.

    Preference order (baselines.json ``judge_pin_resolution``): a dated
    ``claude-opus-4-8-<date>`` id if one ever appears in the models list, otherwise a
    fallback to the newest-dated ``claude-opus-*-<date>`` id across families. As of the
    pin resolution no dated 4.8 exists (models API verified complete), so the fallback
    selects ``claude-opus-4-5-20251101`` -- the settled pin the committed baselines.json
    already ships. Returns None only when the list carries no dated Opus id at all.
    """
    data = models_json.get("data") if isinstance(models_json, dict) else None
    if not isinstance(data, list):
        return None
    dated_48 = []
    dated_any = []
    for entry in data:
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not model_id:
            continue
        m48 = _OPUS_48_DATED.match(model_id)
        if m48:
            dated_48.append((int(m48.group(1)), model_id))
        m_any = _OPUS_DATED.match(model_id)
        if m_any:
            dated_any.append((int(m_any.group(1)), model_id))
    pool = dated_48 or dated_any
    if not pool:
        return None
    pool.sort()
    return pool[-1][1]


def resolve_judge_pin(env=None, force=False, baselines_path=None):
    """Resolve and pin the dated Opus judge snapshot into ``baselines.json``.

    Idempotent: if ``baselines.json`` already carries a ``judge_pin`` it is
    returned without any API call unless ``force=True``. Otherwise the models
    API is queried (``x-api-key`` from the judge key) and ``_pick_opus_snapshot``
    resolves the id -- a dated 4.8 snapshot if one exists, else the newest dated
    Opus of any family. Raises ``RuntimeError`` if no key is available or no dated
    Opus snapshot is found.
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
    pin = _pick_opus_snapshot(models_json)
    if not pin:
        raise RuntimeError(
            "no dated claude-opus snapshot found in the models list: resolve_judge_pin "
            "prefers a dated claude-opus-4-8-* id and otherwise falls back to the newest "
            "dated claude-opus-*-* id, but the list carried neither. Note the committed "
            "baselines.json already ships the settled pin (claude-opus-4-5-20251101), so "
            "scoring needs no live resolution unless you are re-resolving with force=True."
        )

    baselines["judge_pin"] = pin
    _write_json(path, baselines)
    return pin


# ------------------------------------------------------- candidate assembly


def _pull_number(url):
    m = _PULL_RE.search(url or "")
    if not m:
        raise ValueError(f"cannot parse a GitHub pull number from url: {url!r}")
    return int(m.group(3))


def _run_pr_records(run_dir):
    """Return ``[(golden_url, status, payload_hint)]`` from the checkpoint state files.

    ``payload_hint`` is the checkpoint detail's recorded ``payload_path`` (the
    post-relocation pr-dir home for runs that recorded it), used by
    ``_resolve_pr_dir`` as the authoritative pr-dir source. It is None when the
    detail is absent or omits the path (e.g. the legacy baseline recorded the now
    empty shared output dir, which resolution then skips).
    """
    state_dir = Path(run_dir) / "state"
    records = []
    if state_dir.is_dir():
        for path in sorted(state_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ValueError("unreadable PR state artifact") from exc
            url = rec.get("url")
            if url:
                payload_hint = (rec.get("detail") or {}).get("payload_path")
                records.append((url, rec.get("status"), payload_hint))
    return records


def _resolve_pr_dir(run_dir, url, payload_hint=None):
    """Resolve a scored PR's artifact dir, tolerating the legacy ``pr-{n}`` layout.

    Resolution order:
      1. The checkpoint detail's ``payload_hint`` parent -- authoritative, but only
         when that recorded payload file still exists (new runs record the
         post-relocation pr-dir home; the legacy baseline recorded the now-empty
         shared output dir, which must fall through).
      2. The current ``pr-{owner}-{repo}-{n}`` name (unique across repos that reuse
         pull numbers).
      3. The legacy ``pr-{n}`` name (pre-rename runs, e.g. the committed baseline
         ``subset-20260718-031746-27875ca``, which must stay re-scorable).
    Returns the first candidate dir that resolves; falls back to the current-name
    dir (which may not exist) so the caller's empty-candidates path still applies.
    """
    run_dir = Path(run_dir)
    if payload_hint:
        hint = Path(payload_hint)
        if hint.is_file():
            return hint.parent
    current = run_dir / invoke.pr_dir_name({"url": url})
    if current.is_dir():
        return current
    legacy = run_dir / f"pr-{_pull_number(url)}"
    if legacy.is_dir():
        return legacy
    return current


def _assemble_candidates(run_dir, pr_records):
    """Build ``{golden_url: {tool: [candidate...]}}`` for every scored ("ok") PR.

    Returns ``(candidates, per_pr)`` where ``per_pr[url]`` carries the PR dir,
    number, and the candidate list (with path/line) used later for adjudication.
    ``pr_records`` entries are ``(url, status[, payload_hint])``; the optional hint
    is the checkpoint detail's recorded payload path (see ``_resolve_pr_dir``).
    """
    run_dir = Path(run_dir)
    parts = []
    per_pr = {}
    for record in pr_records:
        url, status = record[0], record[1]
        payload_hint = record[2] if len(record) > 2 else None
        if status != "ok":
            continue
        number = _pull_number(url)
        pr_dir = _resolve_pr_dir(run_dir, url, payload_hint)
        payload_path = pr_dir / "post-review-payload.json"
        if not payload_path.is_file():
            nested = sorted(
                path
                for path in pr_dir.rglob("post-review-payload.json")
                if not _is_superseded(path)
            )
            payload_path = nested[0] if nested else None
        if payload_path is not None and payload_path.is_file():
            result, _stats = payload_to_candidates(payload_path, url, tool=TOOL)
        else:
            raise ValueError(f"missing payload for successful PR: {url}")
        parts.append(result)
        per_pr[url] = {
            "pr_dir": str(pr_dir),
            "number": number,
            "candidates": result[url][TOOL],
            "payload_path": str(payload_path),
        }
    if parts:
        merged, _ = merge_candidates(parts)
    else:
        merged = {}
    return merged, per_pr


# ----------------------------------------------------------------- adjudication


def _head_file_lines(pr_info, path):
    """Best-effort head-file lines for ``path`` from a surviving worktree.

    Worktrees are removed after each PR, so this is usually unavailable and the
    adjudicator runs with hunk-only context. Returns ``[]`` when absent.
    """
    pr_dir = pr_info.get("pr_dir")
    if not pr_dir or not path:
        return []
    base = Path(pr_dir) / "worktree"
    candidate = base / path
    if not observations.artifact_allowed(Path(path)):
        return []
    if not candidate.resolve().is_relative_to(base.resolve()):
        return []
    if candidate.is_file():
        try:
            return candidate.read_text(errors="replace", encoding="utf-8").splitlines()
        except OSError:
            return []
    return []


# ---------------------------------------------------------------------- metrics


def _severity_of(text):
    m = _SEVERITY_RE.search(text or "")
    return m.group(1).upper() if m else "UNKNOWN"


def compute_metrics(
    evaluations, candidates, buckets, adjudications, tool=TOOL
) -> LegacyMetricsWire:
    """Historical all-category arithmetic; per_dimension means severity here."""
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
        f1_strict = (
            2 * precision_strict * golden_recall / (precision_strict + golden_recall)
        )
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
    severity_dist: dict[str, int] = {}
    per_dimension: dict[str, int] = {}
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
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _tool_label(manifest):
    """Ledger tool label from the run manifest.

    An explicit manifest ``tool`` wins (experiment override); otherwise the run's
    ``anchor`` field decides -- ``"naive"`` -> ``"naive-anchor"`` (the bare same-model
    anchor), anything else -> ``"deep-review-v2"`` (the v2 skill run). The scorer's
    internal candidates key stays ``deep-review`` regardless -- only labeling changes.
    """
    explicit = manifest.get("tool")
    if explicit:
        return explicit
    return "naive-anchor" if manifest.get("anchor") == "naive" else "deep-review-v2"


def _read_run_manifest(run_dir):
    path = Path(run_dir) / "run.json"
    if path.is_file():
        return _load_json(path)
    return {}


def _infer_tier(run_id):
    if run_id and "-" in run_id:
        head = run_id.split("-", 1)[0]
        if head in ("smoke", "subset", "full"):
            return head
    return run_id or "unknown"


def _read_pr_costs(pr_dir):
    envelope = None
    # raw.json is the real review's envelope; raw-naive.json the naive anchor's.
    # A PR dir carries one or the other -- probe both, real review taking precedence.
    for raw_name in ("raw.json", "raw-naive.json"):
        raw = pr_dir / raw_name
        if raw.is_file():
            envelope = invoke.parse_result_envelope(
                raw.read_text(errors="replace", encoding="utf-8")
            )
            if envelope is not None:
                break
    if envelope is None:
        for name in ("result.json", "costs.json"):
            path = pr_dir / name
            if path.is_file():
                try:
                    envelope = json.loads(
                        path.read_text(errors="replace", encoding="utf-8")
                    )
                except (ValueError, OSError):
                    envelope = None
                break
    return (
        parse_costs(envelope)
        if isinstance(envelope, dict)
        else {"cost_usd": 0.0, "tokens_total": 0, "per_model": {}}
    )


def _read_run_costs(run_dir, *, pr_dirs=None):
    total_cost = 0.0
    total_tokens = 0
    per_model = {}
    for pr_dir in (
        pr_dirs if pr_dirs is not None else sorted(Path(run_dir).glob("pr-*"))
    ):
        costs = _read_pr_costs(pr_dir)
        total_cost += costs["cost_usd"]
        total_tokens += costs["tokens_total"]
        for model, usage in costs["per_model"].items():
            agg = per_model.setdefault(model, {"tokens": 0, "cost_usd": 0.0})
            agg["tokens"] += usage["tokens"]
            agg["cost_usd"] += usage["cost_usd"]
    return {
        "cost_usd": total_cost,
        "tokens_total": total_tokens,
        "per_model": per_model,
    }


def _build_ledger_row(
    run_dir, metrics, costs, manifest, pin, adjudicator_pin, scorer_sha
):
    run_id = manifest.get("run_id") or Path(run_dir).name
    tier = manifest.get("tier") or _infer_tier(run_id)
    envelope = {
        "cap": manifest.get("cap", 25),
        "fixtures": manifest.get("fixtures", []),
        "invocation": manifest.get("invocation")
        or (
            "naive:single-pass"
            if manifest.get("anchor") == "naive"
            else "headless:/code-gauntlet"
        ),
    }
    return {
        "run_id": run_id,
        "ts": _now(),
        "git_sha": manifest.get("git_sha") or _git_head(),
        "tier": tier,
        "tool": _tool_label(manifest),
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
        # The label only; cost_usd/tokens_total above are the envelope figures
        # unchanged. ledger.cost_is_billable is what consumers read it through.
        "auth_mode": manifest_auth_mode(manifest),
        "judge_pin": pin,
        "adjudicator_pin": adjudicator_pin,
        "scorer_sha": scorer_sha,
        "envelope": envelope,
    }


# --------------------------------------------------------------------- score_run


def evaluator_for(epoch, pin, adjudicator_pin):
    runner = Path(__file__).parent
    harness = [
        (name, file_hash(runner / (name + ".py")))
        for name in (
            "score",
            "scorer",
            "observations",
            "profiles",
            "epochs",
            "stages",
            "provenance",
        )
        if (runner / (name + ".py")).is_file()
    ]
    fields = {
        "judge_pin": pin,
        "adjudicator_pin": adjudicator_pin,
        "scorer_sha": epoch.scorer_sha,
        "scorer_sha256": epoch.scorer_sha256,
        "harness_scoring_sha256": digest(harness),
        "adapter_sha256": digest(
            [
                (p.name, file_hash(p))
                for p in sorted((BENCH_DIR / "adapter").glob("*.py"))
            ]
        ),
        "adjudicator_code_sha256": file_hash(BENCH_DIR / "adjudicator/adjudicate.py"),
        "adjudicator_prompt_sha256": file_hash(BENCH_DIR / "adjudicator/prompt.txt"),
        "profile_policy_version": PROFILE_POLICY_VERSION + ":" + epoch.bucket_policy,
        "stage_matcher_version": stages.STAGE_MATCHER_VERSION,
        "temperature": 0.0,
    }
    return Evaluator(evaluator_id=digest(fields), **fields)


def _candidate_context(info, candidate):
    diff_path = Path(info["pr_dir"]) / "diff.patch"
    diff = (
        diff_path.read_text(encoding="utf-8", errors="replace")
        if diff_path.is_file()
        else ""
    )
    path = candidate.get("path")
    line = candidate.get("line")
    if type(line) is not int:
        try:
            line = (
                int(line) if not isinstance(line, bool) and line is not None else None
            )
        except (ValueError, TypeError):
            line = None
    hunk = ctx = ""
    if diff and path and line:
        with suppress(ValueError, TypeError):
            hunk = slice_hunk(diff, path, line)
        lines = _head_file_lines(info, path)
        ctx = file_context(lines, line) if lines else ""
    status = "head_and_hunk" if hunk and ctx else "hunk_only" if hunk else "missing"
    return hunk, ctx, status


def _capture_adjudications(request, buckets, per_pr, api_key, adjudicator):
    contexts, verdicts = [], []
    for c in request.candidates:
        if c.text not in buckets[c.pr_url]["adjudicator"]:
            continue
        hunk, ctx, status = _candidate_context(per_pr[c.pr_url], asdict(c))
        context_hash = digest([hunk, ctx, status, False])
        contexts.append(
            AdjudicationContext(c.candidate_id, hunk, ctx, status, False, context_hash)
        )
        verdict = adjudicator(
            c.text, hunk, ctx, request.evaluator.adjudicator_pin, api_key
        )
        if verdict.get("bucket") not in ("valid_extra", "noise") or verdict.get(
            "error"
        ):
            raise ValueError("invalid adjudicator result")
        verdicts.append(
            Adjudication(
                c.candidate_id,
                c.pr_url,
                verdict["bucket"],
                verdict.get("reason"),
                request.evaluator.evaluator_id,
                context_hash,
                status,
                None,
            )
        )
    return tuple(verdicts), tuple(contexts)


def score_run(
    run_dir,
    env=None,
    baselines_path=None,
    ledger_path=None,
    run_scorer=None,
    adjudicator=None,
    *,
    epoch_id=None,
    score_id=None,
    attempt_selection=None,
    capture_stages=False,
    stage_cost_cap_usd=None,
    stage_matcher=None,
    stage_price_estimator=None,
) -> ScorePacketWire:
    """Execute a fresh score; failures retain a pending diagnostic packet."""
    run_dir = Path(run_dir).resolve()
    env = os.environ if env is None else env
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    epoch_baselines = _load_baselines(epoch.root / "baselines.json")
    baselines = _load_baselines(baselines_path) if baselines_path else epoch_baselines
    pin = baselines.get("judge_pin")
    if not pin:
        raise RuntimeError(
            "baselines.json judge_pin is null; run resolve_judge_pin() before scoring"
        )
    if env.get("MARTIAN_MODEL") and env["MARTIAN_MODEL"] != pin:
        raise RuntimeError(
            "MARTIAN_MODEL disagrees with baselines judge_pin; refusing to score"
        )
    if capture_stages:
        stages.validate_cap(stage_cost_cap_usd)
    adjudicator_pin = baselines.get("adjudicator_pin") or pin
    if (pin, adjudicator_pin) != (
        epoch_baselines.get("judge_pin"),
        epoch_baselines.get("adjudicator_pin") or epoch_baselines.get("judge_pin"),
    ):
        raise ValueError(f"baseline pin override disagrees with epoch {epoch.epoch_id}")
    records = _run_pr_records(run_dir)
    if not records:
        raise RuntimeError(
            "no scorable PRs: no PR completed with status 'ok'; re-run or --retry-failed"
        )
    urls = tuple(sorted(r[0] for r in records))
    if len(urls) != len(set(urls)) or set(urls) - {b.pr_url for b in epoch.bugs}:
        raise ValueError("duplicate or unknown requested PR identity")
    manifest = _read_run_manifest(run_dir)
    requested = manifest.get("urls", manifest.get("pr_urls", urls))
    if set(requested) != set(urls):
        raise ValueError("requested PR matrix is incomplete")
    from bench.runner import validity

    selected = {}
    selected_records = {}
    if attempt_selection is None:
        checkpoint_attempts = {
            state["url"]: state.get("detail", {}).get("attempt_id")
            for path in (run_dir / "state").glob("*.json")
            if (state := _load_json(path)).get("detail", {}).get("attempt_id")
        }
        if checkpoint_attempts:
            attempt_selection = checkpoint_attempts
    failures = set()
    if attempt_selection is not None:
        if set(attempt_selection) != set(urls):
            raise ValueError("attempt selection must cover the requested PR matrix")
        records = []
        for url in urls:
            aid = attempt_selection[url]
            if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", aid):
                raise ValueError("invalid selected attempt identity")
            paths = list((run_dir / "attempts").glob(f"*/{aid}/attempt.json"))
            if len(paths) != 1 or not paths[0].resolve().is_relative_to(run_dir):
                raise ValueError("selected attempt is missing or ambiguous")
            path = paths[0]
            record = validity.selected_attempt(path)
            if record.pr_url != url or record.run_id != (
                manifest.get("run_id") or run_dir.name
            ):
                raise ValueError("selected attempt identity mismatch")
            value = record.validity
            if value.state == "operational_failure":
                if (
                    not validity.failure_confirmation_ref(record)
                    or not value.failure_fingerprint
                ):
                    raise ValueError("unconfirmed operational failure")
                failures.add(url)
            elif record.checkpoint_status != "ok" or value.state == "invalid":
                raise ValueError("selected attempt is invalid; retry before scoring")
            artifacts = path.parent / "artifacts"
            payloads = list(artifacts.rglob("post-review-payload.json"))
            if url not in failures and len(payloads) != 1:
                raise ValueError("missing or ambiguous payload in selected attempt")
            selected[url] = path
            selected_records[url] = record
            records.append(
                (
                    url,
                    "failed" if url in failures else "ok",
                    str(payloads[0]) if payloads else None,
                )
            )
    else:
        for _url, status, _hint in records:
            if status != "ok":
                raise RuntimeError(
                    "no scorable PRs: unresolved/invalid PR; re-run or --retry-failed"
                )
    candidates, per_pr = _assemble_candidates(run_dir, records)
    for url in failures:
        candidates[url] = {TOOL: []}
        per_pr[url] = {
            "candidates": [],
            "pr_dir": str(selected[url].parent / "artifacts")
            if url in selected
            else str(_resolve_pr_dir(run_dir, url)),
        }
    traces = {}
    trace_errors = {}
    for url, info in per_pr.items():
        if url in failures:
            trace_errors[url] = "operational_failure: funnel evidence unavailable"
            continue
        pr_dir = Path(info["pr_dir"])
        try:
            if (pr_dir / "measurement-unavailable.json").is_file():
                trace_errors[url] = _load_json(
                    pr_dir / "measurement-unavailable.json"
                ).get("reason", "trace collection unavailable")
            paths = [
                p
                for p in pr_dir.rglob("code-gauntlet-measurement-*.json")
                if not _is_superseded(p)
            ]
            if len(paths) == 1:
                path = paths[0]
                if path.is_symlink() or not path.resolve().is_relative_to(run_dir):
                    raise ValueError("measurement artifact escapes selected run")
                receipt = pr_dir / "measurement-artifact.json"
                binding = _load_json(receipt) if receipt.is_file() else None
                expected = binding["sha256"] if binding else file_hash(path)
                if binding and binding.get("path") != path.name:
                    raise ValueError("measurement artifact binding mismatch")
                traces[url] = (path, stages.load_trace(path, expected_sha256=expected))
            elif paths:
                raise ValueError("ambiguous measurement artifacts")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            trace_errors[url] = str(exc)
    source_hashes = {}
    sources = [run_dir / "run.json", *sorted((run_dir / "state").glob("*.json"))]
    sources.extend(selected.values())
    confirmation_paths = {
        url: run_dir / "failure-confirmations" / (record.attempt_id + ".json")
        for url, record in selected_records.items()
        if (run_dir / "failure-confirmations" / (record.attempt_id + ".json")).is_file()
    }
    sources.extend(confirmation_paths.values())
    for info in per_pr.values():
        pr_dir = Path(info["pr_dir"])
        sources += [
            pr_dir / name
            for name in (
                "diff.patch",
                "raw.json",
                "raw-naive.json",
                "result.json",
                "costs.json",
                "measurement-artifact.json",
                "measurement-unavailable.json",
            )
        ]
        if info.get("payload_path"):
            sources.append(Path(info["payload_path"]))
        for pattern in (
            "code-gauntlet-measurement-*.json",
            "code-gauntlet-post-review-*.json",
        ):
            sources.extend(p for p in pr_dir.rglob(pattern) if not _is_superseded(p))
        for c in info["candidates"]:
            if c.get("path"):
                head = pr_dir / "worktree" / c["path"]
                if observations.artifact_allowed(
                    Path(c["path"])
                ) and head.resolve().is_relative_to((pr_dir / "worktree").resolve()):
                    sources.append(head)
    for source in sources:
        if not source.resolve().is_relative_to(run_dir):
            raise ValueError("selected artifact escapes run directory")
        if source.is_file():
            source_hashes[str(source.relative_to(run_dir))] = file_hash(source)
    state_names = {
        read_json_path["url"]: str(p.relative_to(run_dir))
        for p in (run_dir / "state").glob("*.json")
        if (read_json_path := _load_json(p)).get("url")
    }
    attempts = {
        url: "legacy-"
        + digest(
            [
                url,
                {
                    name: value
                    for name, value in source_hashes.items()
                    if name == state_names[url]
                    or name.startswith(
                        str(Path(per_pr[url]["pr_dir"]).relative_to(run_dir)) + "/"
                    )
                },
            ]
        )
        for url in urls
    }
    if attempt_selection is not None:
        attempts = dict(attempt_selection)
    legacy_score = run_dir / "scores.json"
    if (
        legacy_score.is_file()
        and _load_json(legacy_score).get("schema_version", 1) == 1
        and not (run_dir / "scores").exists()
    ):
        observations.import_legacy(run_dir, load_epoch("dfc6cb4-v1", root=DEFAULT_ROOT))
    sid, pending = observations.begin_packet(run_dir, epoch, score_id)
    ref = ScoreRef(manifest.get("run_id") or run_dir.name, epoch.epoch_id, sid)
    evaluator = evaluator_for(epoch, pin, adjudicator_pin)
    candidate_map = observations.candidate_records(candidates, attempts)
    delivery_bindings = {}
    for url, (path, trace) in traces.items():
        info = per_pr[url]
        pr_dir = Path(info["pr_dir"])
        diff_path = pr_dir / "diff.patch"
        selected_paths = [
            p
            for p in pr_dir.rglob("code-gauntlet-post-review-*.json")
            if not _is_superseded(p)
        ]
        if not selected_paths or not diff_path.is_file():
            trace_errors[url] = "selected findings or captured diff unavailable"
            continue
        if len(selected_paths) != 1:
            trace_errors[url] = "ambiguous selected findings"
            continue
        try:
            selected_data = _load_json(selected_paths[0])
            selected_findings = (
                selected_data.get("findings", [])
                if isinstance(selected_data, dict)
                else selected_data
            )
            selected_snapshot = next(
                (s for s in trace.snapshots if s.stage == "select"), None
            )
            versions = {v.version_id: v for v in trace.versions}
            trace_findings = (
                [
                    versions[v].projection()
                    for v in selected_snapshot.finding_version_ids
                ]
                if selected_snapshot
                else []
            )
            if [f.get("trace_id") for f in selected_findings] != [
                f.get("trace_id") for f in trace_findings
            ]:
                raise ValueError("selected findings disagree with trace")
            # Persisted aliases are accepted, while every producer field must agree.
            if any(
                any(f.get(k) != v for k, v in original.items())
                for f, original in zip(selected_findings, trace_findings, strict=True)
            ):
                raise ValueError("selected finding content disagrees with trace")
            mapped = provenance.map_delivery(
                _load_json(info["payload_path"]),
                selected_findings,
                diff_path.read_text(encoding="utf-8"),
            )
            delivery_bindings[url] = {
                "selected_path": str(
                    Path("sources") / selected_paths[0].relative_to(run_dir)
                ),
                "payload_path": str(
                    Path("sources") / Path(info["payload_path"]).relative_to(run_dir)
                ),
                "diff_path": str(Path("sources") / diff_path.relative_to(run_dir)),
            }
            omissions = provenance.delivery_omissions(
                selected_findings,
                diff_path.read_text(encoding="utf-8"),
                platform=_load_json(info["payload_path"])["platform"],
            )
            traces[url] = (
                path,
                stages.bind_delivery_trace(trace, mapped, omissions=omissions),
            )
            bound = provenance.bind_candidates(
                tuple(c for c in candidate_map if c.pr_url == url), mapped
            )
            candidate_map = tuple(c for c in candidate_map if c.pr_url != url) + bound
        except (ValueError, KeyError, TypeError) as exc:
            trace_errors[url] = str(exc)
    candidate_map = tuple(
        sorted(
            (
                replace(c, provenance_status="ambiguous")
                if c.pr_url in trace_errors
                else c
                for c in candidate_map
            ),
            key=lambda c: (c.pr_url, c.index),
        )
    )
    request = ScoreRequest(
        ref,
        epoch,
        pending,
        urls,
        tuple(sorted(attempts.items())),
        candidate_map,
        TOOL,
        evaluator,
    )
    # Copy selected inputs before dispatch so every score can be audited independently.
    for name in source_hashes:
        dest = pending / "sources" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        observations.copy_exclusive(run_dir / name, dest)
        if file_hash(dest) != source_hashes[name]:
            raise ValueError("input changed during score snapshot")
    for info in per_pr.values():
        info["pr_dir"] = str(
            pending / "sources" / Path(info["pr_dir"]).relative_to(run_dir)
        )
    api_key = (
        _judge_api_key(env)
        if run_scorer is None
        or env.get("BENCH_JUDGE_API_KEY")
        or env.get("ANTHROPIC_API_KEY")
        else None
    )
    if run_scorer is None:
        outputs = scorer.run_stages(request, api_key, env=env)
        model_dir = outputs.candidates_path.parent
    else:
        model_dir = scorer.stage_inputs(
            epoch, candidates, pin, pending / "work/delivered"
        )
        run_scorer(pin, api_key, model_dir)
    for name in ("candidates.json", "dedup_groups.json", "evaluations.json"):
        observations.copy_exclusive(model_dir / name, pending / name)
    evaluations = _load_json(pending / "evaluations.json")
    groups = observations.dedup_groups(
        _load_json(pending / "dedup_groups.json"), candidate_map
    )
    group_ids = {cid: group.group_id for group in groups for cid in group.candidate_ids}
    candidate_map = tuple(
        replace(c, dedup_group_id=group_ids[c.candidate_id]) for c in candidate_map
    )
    request = replace(request, candidates=candidate_map)
    bugs = observations.build_observations(
        request, evaluations, groups, failures=frozenset(failures)
    )
    matched = observations.bucket_candidate_ids(epoch, bugs)
    buckets = {
        url: {
            "golden_matched": [
                c.text
                for c in candidate_map
                if c.pr_url == url and c.candidate_id in matched
            ],
            "adjudicator": [
                c.text
                for c in candidate_map
                if c.pr_url == url and c.candidate_id not in matched
            ],
        }
        for url in urls
    }
    adjudications, contexts = _capture_adjudications(
        request, buckets, per_pr, api_key, adjudicator or _adjudicate
    )
    stage_rows, stage_matches, stage_capture, stage_calls = stages.capture_score(
        traces,
        bugs,
        epoch,
        enabled=capture_stages,
        cap=stage_cost_cap_usd,
        matcher=stage_matcher,
        price_estimator=stage_price_estimator,
        errors=trace_errors,
        evaluator_id=evaluator.evaluator_id,
        pin=pin,
        api_key=api_key,
        prices=baselines.get("stage_prices"),
    )
    terminal_losses = {
        (row.pr_url, row.bug_id): row.loss
        for row in stage_rows
        if row.stage == "deliver" and row.loss is not None
    }
    bugs = tuple(
        replace(
            bug,
            loss=observations.freeze(asdict(terminal_losses[bug.pr_url, bug.bug_id])),
        )
        if (bug.pr_url, bug.bug_id) in terminal_losses
        else bug
        for bug in bugs
    )
    fp = sum(
        ev.get(TOOL, {}).get("fp", 0)
        for u, ev in evaluations.items()
        if u in urls and u not in failures
    )
    profiles = {
        p: profile_metrics(bugs, fp, p).to_wire() for p in epoch.supported_profiles
    }
    legacy = compute_metrics(
        evaluations, candidates, buckets, [asdict(a) for a in adjudications]
    )
    metrics: RunMetricsWire = {
        **legacy,
        **profiles[epoch.primary_profile],
        "golden_recall": profiles[epoch.primary_profile]["recall"],
        "per_dimension": stages.dimension_metrics(
            candidate_map, bugs, stage_rows, epoch.primary_profile, traces
        ),
        "per_bucket": {
            "golden_matched": legacy["per_bucket"]["golden_matched"],
            "valid_extra": legacy["per_bucket"]["valid_extra"],
            "noise": legacy["per_bucket"]["noise"],
        },
    }
    costs = _read_run_costs(
        run_dir, pr_dirs=[Path(info["pr_dir"]) for info in per_pr.values()]
    )
    row = _build_ledger_row(
        run_dir, metrics, costs, manifest, pin, adjudicator_pin, epoch.scorer_sha
    )
    packet_dir = pending.parent / sid
    row.update(
        schema_version=2,
        score_id=sid,
        epoch_id=epoch.epoch_id,
        golden_sha256=epoch.golden_sha256,
        profile=epoch.primary_profile,
        denominator=metrics["denominator"],
        evaluator_id=evaluator.evaluator_id,
        validity_state=("operational_failure" if failures else "valid")
        if selected_records
        and manifest.get("provenance", {}).get("status") != "non_record"
        and manifest.get("provenance", {}).get("golden_epoch_id") == epoch.epoch_id
        and all(
            r.validity.state in ("valid", "operational_failure")
            for r in selected_records.values()
        )
        else "historical_descriptive",
        population_sha256=digest(urls),
        packet_path=str(packet_dir),
        packet_sha256=None,
        selected_attempts=attempts,
        review_attempt_costs=review_attempt_costs(run_dir)
        or {
            attempts[url]: _read_pr_costs(Path(info["pr_dir"]))["cost_usd"]
            for url, info in per_pr.items()
        },
        judge_cost_usd=None,
        adjudicator_cost_usd=None,
        judge_calls=sum(
            len([b for b in epoch.bugs if b.pr_url == u]) * len(candidates[u][TOOL])
            for u in urls
        ),
        adjudicator_calls=len(adjudications),
        stage_judge_calls=stage_capture["actual_call_count"],
        stage_judge_cost_usd=stage_capture["actual_cost_usd"],
    )
    score_manifest: ScoreManifestWire = {
        "schema_version": 2,
        "score_ref": cast(ScoreRefWire, asdict(ref)),
        "created_at": _now(),
        "epoch_hashes": observations.epoch_hashes(epoch),
        "evaluator": cast(EvaluatorWire, asdict(evaluator)),
        "requested_pr_urls": list(urls),
        "selected_attempts": attempts,
        "source_artifact_hashes": source_hashes,
        "run_validity_ref": "run-provenance.json"
        if manifest.get("provenance")
        else None,
        "provenance_status": "ambiguous"
        if trace_errors
        else "complete"
        if all(c.provenance_status == "complete" for c in candidate_map)
        and not trace_errors
        and len(traces) == len(urls)
        else "legacy_unknown",
        "profiles": list(epoch.supported_profiles),
        "completion_status": "complete",
    }
    packet: CompatibilityScorePacketWire = {
        "schema_version": 2,
        "manifest": score_manifest,
        "run_id": ref.run_id,
        "score_ref": cast(ScoreRefWire, asdict(ref)),
        "profiles": profiles,
        "metrics": metrics,
        "observations_path": "bugs.jsonl",
        "stages_path": "stages.jsonl" if stage_rows else None,
        "stage_capture": stage_capture,
        "adjudications_path": "adjudications.jsonl",
        "ledger_row": row,
        "complete_path": "complete.json",
        "judge_pin": pin,
        "adjudicator_pin": adjudicator_pin,
        "scorer_sha": epoch.scorer_sha,
        "buckets": {
            u: {k: len(v) for k, v in split.items()} for u, split in buckets.items()
        },
        "adjudications": [cast(AdjudicationWire, asdict(a)) for a in adjudications],
    }
    observations.exclusive_json(
        pending / "stage_trace_bindings.json",
        {
            url: {
                "attempt_id": attempts[url],
                "trace_path": str(Path("sources") / path.relative_to(run_dir)),
                "trace_sha256": source_hashes[str(path.relative_to(run_dir))],
                "delivery": delivery_bindings.get(url),
            }
            for url, (path, trace) in traces.items()
        },
    )
    if manifest.get("provenance"):
        observations.exclusive_json(pending / "run-provenance.json", manifest)
    for record in selected_records.values():
        observations.exclusive_json(
            pending / "attempts" / (record.attempt_id + ".json"), record.wire()
        )
    for path in confirmation_paths.values():
        observations.copy_exclusive(path, pending / "failure-confirmations" / path.name)
    observations.exclusive_json(pending / "provenance_errors.json", trace_errors)
    observations.exclusive_json(pending / "stage_calls.json", list(stage_calls))
    observations.write_packet(
        request,
        packet=packet,
        observations=bugs,
        candidates=candidate_map,
        groups=groups,
        adjudications=adjudications,
        contexts=contexts,
        stage_observations=stage_rows,
        stage_matches=stage_matches,
    )
    if not (run_dir / "scores.json").exists():
        observations.exclusive_json(
            run_dir / "scores.json", {**packet, "snapshot_status": "first_score_only"}
        )
    pointer = run_dir / (".latest-score-" + sid + ".json")
    observations.exclusive_json(pointer, asdict(ref))
    pointer.replace(run_dir / "latest-score.json")
    replay_packet(ledger_path or LEDGER_PATH, packet_dir)
    # The replayable projection stores its completion hash outside the hashed packet.
    packet["ledger_row"] = {
        **row,
        "packet_sha256": file_hash(packet_dir / "complete.json"),
    }
    return packet
