"""Epoch-aware scoring orchestration with immutable execution packets."""

import json
import os
import re
import subprocess
import time
from contextlib import suppress
from dataclasses import asdict, replace
from pathlib import Path

from bench.adapter.adapt import merge_candidates, payload_to_candidates
from bench.adjudicator.adjudicate import adjudicate as _adjudicate
from bench.adjudicator.adjudicate import file_context, slice_hunk
from bench.runner import contracts as io
from bench.runner import observations, provenance, scorer, stages
from bench.runner.contracts import (
    ScoreRef,
    _is_superseded,
    artifact_allowed,
    copy_exclusive,
    exclusive_json,
    selected_attempt_path,
)
from bench.runner.costs import parse_costs
from bench.runner.epochs import (
    DEFAULT_ROOT,
    digest,
    file_hash,
    in_profile,
    load_epoch,
    read_json,
)
from bench.runner.ledger import manifest_auth_mode, replay_packet, review_attempt_costs
from bench.runner.observations import (
    Adjudication,
    AdjudicationContext,
    Evaluator,
    ScoreRequest,
)
from bench.runner.scoring_costs import read_costs, total_cost

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = REPO_ROOT / "bench"
LEDGER_PATH = BENCH_DIR / "experiments.jsonl"

TOOL = "deep-review"
SURFACE_PASS_ERRORS = (
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    OSError,
    subprocess.SubprocessError,
)

_SEVERITY_RE = re.compile(r"\[(CRITICAL|HIGH|MEDIUM|LOW)\]", re.IGNORECASE)
_PULL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _judge_api_key(env=None):
    """Resolve the judge API key: BENCH_JUDGE_API_KEY, else ANTHROPIC_API_KEY.

    Falls back to ``bench/.env`` for whichever is not already in the environment
    (the owner runs single-key: the judge uses the ANTHROPIC_API_KEY fallback).
    Delegates to :func:`io._load_dotenv_key` — the single dotenv parser — so
    prereqs, build_env, and the judge can never disagree on an edge case.
    """
    env = os.environ if env is None else env
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        value = env.get(name)
        if value:
            return value
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        value = io._load_dotenv_key(BENCH_DIR / ".env", name)
        if value:
            return value
    return None


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
                records.append((url, rec.get("status"), _anchor(run_dir, payload_hint)))
    return records


def _anchor(run_dir, recorded):
    """Re-root a recorded absolute path under ``run_dir`` so a run copied to another checkout still scores."""
    if not recorded:
        return recorded
    parts = Path(recorded).parts
    if run_dir.name not in parts:
        return recorded
    return str(run_dir.joinpath(*parts[parts.index(run_dir.name) + 1 :]))


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
    current = run_dir / io.pr_dir_name({"url": url})
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
            "payload": read_json(payload_path),
        }
    if parts:
        merged, _ = merge_candidates(parts)
    else:
        merged = {}
    return merged, per_pr


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
    if not artifact_allowed(Path(path)):
        return []
    if not candidate.resolve().is_relative_to(base.resolve()):
        return []
    if candidate.is_file():
        try:
            return candidate.read_text(errors="replace", encoding="utf-8").splitlines()
        except OSError:
            return []
    return []


def _severity_of(text):
    m = _SEVERITY_RE.search(text or "")
    return m.group(1).upper() if m else "UNKNOWN"


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
        return read_json(path)
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
            envelope = io.parse_result_envelope(
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


def _build_ledger_row(run_dir, costs, manifest, pin, adjudicator_pin, scorer_sha):
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
        "runs": manifest.get("runs", 1),
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


def evaluator_for(epoch, pin, adjudicator_pin):
    paths = [
        Path(__file__).parent / (n + ".py")
        for n in (
            "score",
            "scorer",
            "scoring_costs",
            "observations",
            "profiles",
            "epochs",
            "decode",
            "stages",
            "provenance",
        )
    ]
    paths += sorted((BENCH_DIR / "adapter").glob("*.py"))
    paths += [
        BENCH_DIR / "adjudicator/adjudicate.py",
        BENCH_DIR / "adjudicator/prompt.txt",
    ]
    fields = {
        "judge_pin": pin,
        "adjudicator_pin": adjudicator_pin,
        "scorer_tree": epoch.scorer_tree,
        "harness_scoring_sha256": digest(
            [(str(p.relative_to(BENCH_DIR)), file_hash(p)) for p in paths]
        ),
        "temperature": 0.0,
    }
    return Evaluator(**fields)


def _candidate_context(info, candidate):
    if not candidate.get("line"):
        return "", str(candidate.get("path") or ""), "file_only_unconfirmed", False
    diff_path = Path(info["pr_dir"]) / "diff.patch"
    diff = (
        diff_path.read_text(encoding="utf-8", errors="replace")
        if diff_path.is_file()
        else ""
    )
    path = candidate.get("path")
    line = candidate.get("line")
    hunk = ctx = ""
    if diff and path and line:
        with suppress(ValueError, TypeError):
            hunk = slice_hunk(diff, path, line)
        lines = _head_file_lines(info, path)
        ctx = file_context(lines, line) if lines else ""
    status = "head_and_hunk" if hunk and ctx else "hunk_only" if hunk else "missing"
    return hunk, ctx, status, False


def _capture_adjudications(
    request, matched, per_pr, api_key, adjudicator, context_builder=_candidate_context
):
    contexts, verdicts, usage = [], [], []
    pin = request.evaluator.adjudicator_pin
    for c in request.candidates:
        if c.candidate_id in matched:
            continue
        hunk, ctx, status, truncated = context_builder(per_pr[c.pr_url], asdict(c))
        context = AdjudicationContext(c.candidate_id, hunk, ctx, status, truncated)
        contexts.append(context)
        verdict = adjudicator(c.text, hunk, ctx, pin, api_key)
        if verdict.get("bucket") not in ("valid_extra", "noise") or verdict.get(
            "error"
        ):
            raise ValueError("invalid adjudicator result")
        usage.extend(verdict.get("usage", [{"model": pin, "usage": None}]))
        verdicts.append(
            Adjudication(
                c.candidate_id,
                c.pr_url,
                verdict["bucket"],
                verdict.get("reason"),
                request.evaluator.evaluator_id,
                context.context_sha256,
            )
        )
    exclusive_json(request.work_dir / (request.tool + "-adjudicator-usage.json"), usage)
    return tuple(verdicts), tuple(contexts), total_cost(usage)


def _collect_surface_candidates(
    urls,
    failures,
    delivery_bindings,
    trace_errors,
    original_traces,
    per_pr,
    run_dir,
    replay_root,
    attempts,
):
    candidates = []
    for url in urls:
        if url in failures:
            continue
        binding = delivery_bindings.get(url)
        if not binding or url in trace_errors:
            return candidates, "original body or trace evidence unavailable"
        try:
            info = per_pr[url]
            _, mapped = provenance.trace_delivery(
                original_traces[url][1],
                read_json(
                    run_dir / Path(binding["selected_path"]).relative_to("sources")
                ),
                info["payload"],
                (Path(info["pr_dir"]) / "diff.patch").read_text(encoding="utf-8"),
                treatment_root=replay_root,
                include_body=True,
            )
            raw = {url: {TOOL: [{**asdict(m), "source": "extracted"} for m in mapped]}}
            candidates.extend(observations.candidate_records(raw, attempts))
        except SURFACE_PASS_ERRORS as exc:
            return candidates, str(exc) or "original body or trace evidence unavailable"
    return candidates, None


def score_surface_view(
    request,
    mapped,
    inline_bugs,
    per_pr,
    failures,
    api_key,
    env,
    run_scorer,
    adjudicator,
    source_run,
):
    """Score a separate population; canonical inputs and outputs stay inline."""
    raw = {u: {TOOL: []} for u in request.requested_pr_urls}
    for c in observations.surface_candidates(mapped):
        raw[c.pr_url][TOOL].append(
            {
                **{
                    key: getattr(c, key)
                    for key in observations.SurfaceItemWire.__annotations__
                },
                "source": "extracted",
                "provenance_status": c.provenance_status,
            }
        )
    candidates = observations.candidate_records(raw, dict(request.selected_attempts))
    root = request.work_dir / "surface-all"
    source_hashes = {}
    surface_prs = {}
    for url, info in per_pr.items():
        relative = Path(info["pr_dir"]).relative_to(request.work_dir / "sources")
        surface_prs[url] = {**info, "pr_dir": str(root / "sources" / relative)}
        paths = [relative / "diff.patch"]
        for c in candidates:
            if c.pr_url != url or not c.path:
                continue
            candidate_path = Path(c.path)
            if candidate_path.is_absolute() or ".." in candidate_path.parts:
                raise ValueError(
                    "surface candidate path must be relative without traversal"
                )
            if artifact_allowed(candidate_path):
                paths.append(relative / "worktree" / candidate_path)
        for name in dict.fromkeys(paths):
            snapshot = request.work_dir / "sources" / name
            origin = request.work_dir / "sources" if snapshot.is_file() else source_run
            source = origin / name
            if not source.resolve().is_relative_to(source_run):
                raise ValueError("surface source escapes run directory")
            if name != relative / "diff.patch" and not source.resolve().is_relative_to(
                (origin / relative / "worktree").resolve()
            ):
                raise ValueError("surface head source escapes worktree")
            if source.is_file():
                expected = file_hash(source)
                dest = root / "sources" / name
                if not dest.resolve().is_relative_to(request.work_dir.resolve()):
                    raise ValueError("surface destination escapes packet root")
                dest.parent.mkdir(parents=True, exist_ok=True)
                copy_exclusive(source, dest)
                if file_hash(dest) != expected:
                    raise ValueError("surface input changed during score snapshot")
                source_hashes[name.as_posix()] = expected
    model = scorer.stage_inputs(
        request.epoch, raw, request.evaluator.judge_pin, root / "work"
    )
    if run_scorer is None:
        scorer.execute(
            request.epoch, request.evaluator.judge_pin, api_key, model, env=env
        )
    else:
        run_scorer(request.evaluator.judge_pin, api_key, model)
    for name in ("candidates.json", "dedup_groups.json", "evaluations.json"):
        copy_exclusive(model / name, root / name)
    groups = observations.dedup_groups(
        read_json(root / "dedup_groups.json"), candidates
    )
    gids = {cid: g.group_id for g in groups for cid in g.candidate_ids}
    candidates = tuple(
        replace(c, dedup_group_id=gids[c.candidate_id]) for c in candidates
    )
    surface_request = replace(request, work_dir=root, candidates=candidates)
    bugs = observations.build_observations(
        surface_request,
        read_json(root / "evaluations.json"),
        groups,
        failures=frozenset(failures),
    )
    bugs = observations.union_surface_observations(
        bugs, inline_bugs, candidates, request.candidates
    )
    verdicts, contexts, cost = _capture_adjudications(
        surface_request,
        observations.bucket_candidate_ids(bugs),
        surface_prs,
        api_key,
        adjudicator,
    )
    view = observations.SurfaceScoreView(
        request.evaluator.evaluator_id,
        candidates,
        bugs,
        verdicts,
        contexts,
        {**read_costs(model), "adjudicator": cost},
        source_hashes,
    )
    observations.validate_surface_view(
        request.work_dir, inline_bugs, request, view, frozenset(failures)
    )
    return view


def score_run(
    run_dir,
    env=None,
    ledger_path=None,
    run_scorer=None,
    adjudicator=None,
    *,
    epoch_id=None,
    score_id=None,
    attempt_selection=None,
    attribution=False,
    surface_views=False,
):
    """Execute a fresh score; failures retain a pending diagnostic packet."""
    run_dir = Path(run_dir).resolve()
    env = os.environ if env is None else env
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    pin, adjudicator_pin = epoch.judge_pin, epoch.adjudicator_pin
    if env.get("MARTIAN_MODEL") and env["MARTIAN_MODEL"] != pin:
        raise RuntimeError("MARTIAN_MODEL disagrees with epoch judge_pin")
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
    if len(requested) != len(urls) or set(requested) != set(urls):
        raise ValueError("requested PR matrix is incomplete")

    selected = {}
    selected_records = {}
    checkpoint_attempts = {
        state["url"]: state["detail"]["attempt_id"]
        for path in (run_dir / "state").glob("*.json")
        if (state := read_json(path)).get("detail", {}).get("attempt_id")
    }
    failures = set()
    if checkpoint_attempts:
        if (
            attempt_selection is None
            or set(attempt_selection) != set(urls)
            or set(checkpoint_attempts) != set(urls)
        ):
            raise ValueError("attempt selection must cover the requested PR matrix")
        records = []
        for url in urls:
            aid = checkpoint_attempts[url]
            path = selected_attempt_path(run_dir, aid)
            record = attempt_selection[url]
            if (
                record.attempt_id != aid
                or record.pr_url != url
                or record.run_id != (manifest.get("run_id") or run_dir.name)
            ):
                raise ValueError("selected attempt identity mismatch")
            if record.validity.state == "operational_failure":
                if not record.validity.retry_of:
                    raise ValueError("unconfirmed operational failure")
                failures.add(url)
            elif record.checkpoint_status != "ok" or record.validity.state != "valid":
                raise ValueError("selected attempt is invalid; retry before scoring")
            payloads = list(
                (path.parent / "artifacts").rglob("post-review-payload.json")
            )
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
    attempt_costs = review_attempt_costs(run_dir)
    candidates, per_pr = _assemble_candidates(run_dir, records)
    for url in failures:
        candidates[url] = {TOOL: []}
        per_pr[url] = {
            "candidates": [],
            "pr_dir": str(selected[url].parent / "artifacts"),
        }
    traces, trace_errors, collection = {}, {}, {}
    for url, info in per_pr.items():
        collection[url] = stages.TraceCollection(
            "unavailable", "collection not recorded"
        )
        if url in failures:
            collection[url] = stages.TraceCollection(
                "unavailable", "operational failure"
            )
            trace_errors[url] = "operational failure"
            continue
        try:
            pr_dir = Path(info["pr_dir"])
            for name in ("measurement-collection.json", "measurement-unavailable.json"):
                if (pr_dir / name).is_file():
                    collection[url] = stages.decode(
                        stages.TraceCollection, read_json(pr_dir / name)
                    )
                    if collection[url].status == "unavailable":
                        trace_errors[url] = (
                            collection[url].reason or "collection failed"
                        )
                    break
            paths = [
                p
                for p in pr_dir.rglob("code-gauntlet-measurement-*.json")
                if not _is_superseded(p)
            ]
            if len(paths) > 1:
                raise ValueError("ambiguous measurement artifacts")
            if not paths and collection[url].status == "available":
                raise ValueError("collected measurement artifact missing")
            if paths and url not in trace_errors:
                path = paths[0]
                if path.is_symlink() or not path.resolve().is_relative_to(run_dir):
                    raise ValueError("measurement artifact escapes selected run")
                receipt = pr_dir / "measurement-artifact.json"
                binding = (
                    read_json(receipt)
                    if receipt.is_file()
                    else {"path": path.name, "sha256": file_hash(path)}
                )
                if binding["path"] != path.name:
                    raise ValueError("measurement artifact binding mismatch")
                collection[url] = stages.TraceCollection("available", None)
                traces[url] = (
                    path,
                    stages.load_trace(path, expected_sha256=binding["sha256"]),
                )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            collection[url] = stages.TraceCollection("unavailable", str(exc))
            trace_errors[url] = str(exc)
    source_hashes = {}
    sources = [run_dir / "run.json", *sorted((run_dir / "state").glob("*.json"))]
    sources.extend(selected.values())
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
                "measurement-collection.json",
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
                if artifact_allowed(Path(c["path"])) and head.resolve().is_relative_to(
                    (pr_dir / "worktree").resolve()
                ):
                    sources.append(head)
    for source in sources:
        if not source.resolve().is_relative_to(run_dir):
            raise ValueError("selected artifact escapes run directory")
        if source.is_file():
            source_hashes[str(source.relative_to(run_dir))] = file_hash(source)
    state_names = {
        read_json_path["url"]: str(p.relative_to(run_dir))
        for p in (run_dir / "state").glob("*.json")
        if (read_json_path := read_json(p)).get("url")
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
    if checkpoint_attempts:
        attempts = checkpoint_attempts
    sid, pending = observations.begin_packet(run_dir, epoch, score_id)
    ref = ScoreRef(manifest.get("run_id") or run_dir.name, epoch.epoch_id, sid)
    evaluator = evaluator_for(epoch, pin, adjudicator_pin)
    candidate_map = observations.candidate_records(candidates, attempts)
    delivery_bindings = {}
    # Outside the per-PR guard: a missing treatment tree must fail, not mark PRs ambiguous.
    replay_root = (
        provenance.resolve_treatment_root(run_dir, manifest.get("treatment_git_sha"))
        if traces
        else None
    )
    original_traces = traces.copy()
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
            inputs = dict(
                selected_path=selected_paths[0],
                payload_path=Path(info["payload_path"]),
                diff_path=diff_path,
            )
            bound_trace, mapped = provenance.trace_delivery(
                trace,
                read_json(inputs["selected_path"]),
                read_json(inputs["payload_path"]),
                inputs["diff_path"].read_text(encoding="utf-8"),
                treatment_root=replay_root,
            )
            delivery_bindings[url] = {
                key: str(Path("sources") / p.relative_to(run_dir))
                for key, p in inputs.items()
            }
            traces[url] = path, bound_trace
            bound = provenance.bind_candidates(
                tuple(c for c in candidate_map if c.pr_url == url), mapped
            )
            candidate_map = tuple(c for c in candidate_map if c.pr_url != url) + bound
        except (ValueError, KeyError, TypeError) as exc:
            trace_errors[url] = str(exc)
    candidate_map = tuple(
        sorted(
            (
                replace(
                    c,
                    provenance_status="ambiguous",
                    source_trace_ids=(),
                    discovery_dimensions=("unknown",),
                )
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
    all_candidates = []
    surface_unavailable = "surface views not requested"
    if surface_views:
        all_candidates, surface_unavailable = _collect_surface_candidates(
            urls,
            failures,
            delivery_bindings,
            trace_errors,
            original_traces,
            per_pr,
            run_dir,
            replay_root,
            attempts,
        )
    # Copy selected inputs before dispatch so every score can be audited independently.
    for name in source_hashes:
        dest = pending / "sources" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_exclusive(run_dir / name, dest)
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
    discovery = stages.discovery_inputs(traces, trace_errors) if attribution else {}
    model_dir = scorer.stage_inputs(epoch, candidates, pin, pending / "work/delivered")
    if run_scorer is None:
        scorer.execute(epoch, pin, api_key, model_dir, env=env)
    else:
        run_scorer(pin, api_key, model_dir)
    for name in ("candidates.json", "dedup_groups.json", "evaluations.json"):
        copy_exclusive(model_dir / name, pending / name)
    evaluations = read_json(pending / "evaluations.json")
    groups = observations.dedup_groups(
        read_json(pending / "dedup_groups.json"), candidate_map
    )
    group_ids = {cid: group.group_id for group in groups for cid in group.candidate_ids}
    candidate_map = tuple(
        replace(c, dedup_group_id=group_ids[c.candidate_id]) for c in candidate_map
    )
    request = replace(request, candidates=candidate_map)
    bugs = observations.build_observations(
        request, evaluations, groups, failures=frozenset(failures)
    )
    matched = observations.bucket_candidate_ids(bugs)
    adjudications, contexts, adjudicator_cost = _capture_adjudications(
        request, matched, per_pr, api_key, adjudicator or _adjudicate
    )
    surface_view = None
    if surface_unavailable is None:
        try:
            surface_view = score_surface_view(
                request,
                all_candidates,
                bugs,
                per_pr,
                failures,
                api_key,
                env,
                run_scorer,
                adjudicator or _adjudicate,
                run_dir,
            )
        except SURFACE_PASS_ERRORS as exc:
            surface_unavailable = "all-surface scoring failed: " + str(exc)
    attribution_cost = None
    stage_evaluations = stage_groups = {}
    if any(discovery.values()):
        attribution_dir = scorer.stage_inputs(
            epoch, discovery, pin, pending / "work/attribution"
        )
        try:
            if run_scorer is None:
                scorer.execute(epoch, pin, api_key, attribution_dir, env=env)
            else:
                run_scorer(pin, api_key, attribution_dir)
            stage_evaluations = read_json(attribution_dir / "evaluations.json")
            stage_groups = read_json(attribution_dir / "dedup_groups.json")
        except (RuntimeError, ValueError, OSError) as exc:
            trace_errors.update(
                {u: "attribution scoring failed: " + str(exc) for u in discovery}
            )
        charges = read_costs(attribution_dir)
        attribution_cost = (
            sum(charges.values())
            if all(v is not None for v in charges.values())
            else None
        )
    elif attribution:
        attribution_cost = 0.0
    stage_rows = (
        stages.capture_score(
            traces,
            bugs,
            request,
            stage_evaluations,
            stage_groups,
            discovery,
            trace_errors,
        )
        if attribution
        else ()
    )
    terminal_losses = {
        (row.pr_url, row.bug_id): row.loss
        for row in stage_rows
        if row.stage == "deliver" and row.loss is not None
    }
    bugs = tuple(
        replace(b, loss=terminal_losses[b.pr_url, b.bug_id])
        if (b.pr_url, b.bug_id) in terminal_losses
        else b
        for b in bugs
    )
    costs = _read_run_costs(
        run_dir, pr_dirs=[Path(info["pr_dir"]) for info in per_pr.values()]
    )
    row = _build_ledger_row(
        run_dir, costs, manifest, pin, adjudicator_pin, epoch.upstream_sha
    )
    packet_dir = pending.parent / sid
    if not selected_records or not manifest.get("measurement_window"):
        validity_state = "historical_descriptive"
    elif failures:
        validity_state = "operational_failure"
    else:
        validity_state = "valid"
    row.update(
        schema_version=2,
        score_id=sid,
        epoch_id=epoch.epoch_id,
        golden_sha256=epoch.golden_sha256,
        profile=epoch.primary_profile,
        evaluator_id=evaluator.evaluator_id,
        denominator=sum(
            b.pr_url in urls and in_profile(b.category, epoch.primary_profile)
            for b in epoch.bugs
        ),
        validity_state=validity_state,
        population_sha256=digest(urls),
        packet_path=str(packet_dir),
        selected_attempts=attempts,
        review_attempt_costs=attempt_costs
        or {
            attempts[url]: _read_pr_costs(Path(info["pr_dir"]))["cost_usd"]
            for url, info in per_pr.items()
        },
        judge_calls=sum(
            len([b for b in epoch.bugs if b.pr_url == u]) * len(candidates[u][TOOL])
            for u in urls
        ),
        adjudicator_calls=len(adjudications),
    )
    score_manifest = observations.ScoreManifest(
        attribution=attribution,
        trace_collection=collection,
        score_ref=ref,
        epoch_hashes=observations.epoch_hashes(epoch),
        evaluator=evaluator,
        requested_pr_urls=urls,
        selected_attempts=attempts,
        source_artifact_hashes=source_hashes,
    )
    exclusive_json(
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
    exclusive_json(pending / "run-provenance.json", manifest)
    for record in selected_records.values():
        exclusive_json(
            pending / "attempts" / (record.attempt_id + ".json"), record.wire()
        )
    exclusive_json(pending / "provenance_errors.json", trace_errors)
    observations.write_packet(
        request,
        manifest=score_manifest,
        metadata=row,
        observations=bugs,
        adjudications=adjudications,
        contexts=contexts,
        stage_observations=stage_rows,
        costs={
            **read_costs(model_dir),
            "adjudicator": adjudicator_cost,
            "attribution": attribution_cost,
        },
        surface_view=surface_view,
        surface_unavailable_reason=surface_unavailable,
    )
    pointer = run_dir / (".latest-score-" + sid + ".json")
    exclusive_json(pointer, asdict(ref))
    pointer.replace(run_dir / "latest-score.json")
    replay_packet(ledger_path or LEDGER_PATH, packet_dir)
    return observations.load_packet(packet_dir).projection()
