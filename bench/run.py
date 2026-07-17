#!/usr/bin/env python3
"""The one-command bench harness runner (spec H3).

``python3 bench/run.py --tier smoke|subset|full [flags]`` drives the v2 skill over a
tier's golden PRs: for each PR it ensures a cached bare mirror, cuts a detached worktree
at the pinned head SHA (with the SHA input-drift guard), writes the REVIEW.md fixture for
designated fixture PRs, invokes the headless review under the pinned isolated context,
collects the dry-run payload + report artifacts into the per-PR dir, saves the PR diff,
removes the worktree, and checkpoints the outcome. PR-granular checkpointing makes a run
resumable; a killed pass loses at most the single PR that was mid-flight.

This runner does NOT write ledger rows -- scoring (Task 13) does. run.py records per-PR
invoke metadata (status, cost, duration) into the checkpoint detail and a run.json
manifest (run_id, tier, git_sha, start ts, env fingerprint).

Stdlib-only (repo CLAUDE.md). The vendored scorer keeps its own deps behind ``uv`` and is
never imported here; ``--score-only`` lazily imports the (Task 13) score module and errors
cleanly if it is not present yet.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# invoke.py imports as ``bench.runner.costs`` -> the repo root must be importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.runner import checkpoint, invoke, mirrors  # noqa: E402
from bench.runner.costs import parse_costs  # noqa: E402

# Workspace layout (all under bench/workspace/, gitignored). Referenced as module
# globals so tests can repoint them at a tempdir.
WORKSPACE = REPO_ROOT / "bench" / "workspace"
RUNS_ROOT = WORKSPACE / "runs"
MIRRORS_DIR = WORKSPACE / "mirrors"
GOLDEN_DIR = REPO_ROOT / "bench" / "golden"
ENV_PATH = REPO_ROOT / "bench" / ".env"
FIXTURE_PATH = GOLDEN_DIR / "review_md_fixture.md"

MIN_FREE_GB = 10

# `--tier subset` maps to the 15-PR gate subset; `full` is every shas.json key.
_TIER_SUBSET_KEY = {"smoke": "smoke", "subset": "gate"}

# Naive anchor (spec H3): a bare single-pass review -- no --plugin-dir, prompt from the
# PR title + full diff, a pinned turn budget so it cannot loop. Same isolation envelope
# and output-capture path as the real review; scored through the same adapter downstream.
NAIVE_MAX_TURNS = 40
_ASK_RE = re.compile(r'"(?:name|tool_name)"\s*:\s*"AskUserQuestion"')


# --------------------------------------------------------------------------- helpers


def _load_json(path):
    with open(path) as fh:
        return json.load(fh)


def _git_short_sha():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "nogit"
    return result.stdout.strip() or "nogit"


def _utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_run_dir(tier):
    """Create and return ``(run_id, run_dir)``, guaranteeing a distinct directory.

    run_id is ``{tier}-{UTC yyyymmdd-hhmmss}-{git short sha}``; a numeric suffix is
    appended on collision so ``--runs N`` fired within the same second still yields N
    distinct run dirs.
    """
    base = f"{tier}-{_utc_stamp()}-{_git_short_sha()}"
    run_id = base
    suffix = 2
    while (RUNS_ROOT / run_id).exists():
        run_id = f"{base}-{suffix}"
        suffix += 1
    run_dir = RUNS_ROOT / run_id
    run_dir.mkdir(parents=True)
    return run_id, run_dir


def _resolve_tier(tier, subsets, shas):
    """Return the ordered list of golden URLs for ``tier`` (smoke=3, subset=15, full=50)."""
    if tier == "full":
        return list(shas.keys())
    return list(subsets[_TIER_SUBSET_KEY[tier]])


def _clear_dir(directory):
    """Empty ``directory`` (creating it if absent) -- the shared output dir per PR."""
    directory = Path(directory)
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        return
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _collect_artifacts(output_dir, pr_dir):
    """Move every produced artifact from the shared output dir into ``pr_dir``.

    build_env points DEEP_REVIEW_OUTPUT_DIR at ``{run_dir}/output`` which is SHARED across
    PRs; moving the artifacts out (payload, findings, report) leaves it empty for the next
    PR. Returns the moved names.
    """
    output_dir = Path(output_dir)
    pr_dir = Path(pr_dir)
    moved = []
    if not output_dir.exists():
        return moved
    for child in sorted(output_dir.iterdir()):
        dest = pr_dir / child.name
        if dest.exists():
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        shutil.move(str(child), str(dest))
        moved.append(child.name)
    return moved


def _compute_diff(worktree, base_sha, head_sha):
    """Return ``git diff base...head`` run inside the worktree (empty string on failure)."""
    result = subprocess.run(
        ["git", "diff", f"{base_sha}...{head_sha}"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    return result.stdout


# --------------------------------------------------------------------------- prereqs


def _read_env_key(env_path, key):
    env_path = Path(env_path)
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line[len(key) + 1 :].strip()
    return None


def _free_gb(path):
    """Free GB on the partition holding ``path`` (walks up to the first existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(str(probe)).free / (1024 ** 3)


def check_prereqs(env_path=None, workspace_dir=None, min_free_gb=MIN_FREE_GB):
    """Return a list of one-line, actionable failure messages (empty == all prereqs met)."""
    env_path = Path(env_path if env_path is not None else ENV_PATH)
    workspace_dir = Path(workspace_dir if workspace_dir is not None else WORKSPACE)
    failures = []

    claude_bin = shutil.which("claude")
    if not claude_bin:
        failures.append(
            "claude CLI not found on PATH -- install Claude Code so `claude` is runnable."
        )
    else:
        result = subprocess.run([claude_bin, "--version"], capture_output=True, text=True)
        blob = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or not re.search(r"\d+\.\d+", blob):
            failures.append(
                "`claude --version` did not return a parseable version -- check the Claude Code install."
            )

    if not shutil.which("gh"):
        failures.append(
            "gh CLI not found on PATH -- install GitHub CLI and run `gh auth login`."
        )
    else:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if result.returncode != 0:
            failures.append("`gh auth status` failed -- run `gh auth login` to authenticate.")

    if not _read_env_key(env_path, "ANTHROPIC_API_KEY"):
        failures.append(
            f"ANTHROPIC_API_KEY missing or empty in {env_path} -- "
            "copy bench/.env.example to bench/.env and add the metered key."
        )

    if not shutil.which("uv"):
        failures.append(
            "uv not found on PATH -- install uv (https://docs.astral.sh/uv/) for the vendored scorer."
        )

    try:
        free = _free_gb(workspace_dir)
        if free < min_free_gb:
            failures.append(
                f"Only {free:.1f} GB free on the workspace partition -- "
                f"need >= {min_free_gb} GB for repo mirrors."
            )
    except OSError as exc:
        failures.append(f"Could not check free disk space for {workspace_dir}: {exc}")

    return failures


# ----------------------------------------------------------------------- naive anchor


def _parse_envelope(text):
    """Tolerantly extract the ``type=="result"`` envelope dict from raw stdout, or None."""
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


def _naive_prompt(pr, diff_text, bench_entry):
    # Vendored golden data carries pr_title but no PR body; the naive prompt uses the
    # title plus the full diff (the changes are what the anchor is asked to review).
    title = (bench_entry or {}).get("pr_title") or "PR #{}".format(pr["pr_number"])
    return (
        "You are reviewing a pull request titled: {}\n\n".format(title)
        + "Below is the full diff of the change. Review it and report any bugs, "
        "correctness problems, security issues, or notable quality concerns as concise "
        "review comments, each citing the file and line it refers to. Only report real, "
        "grounded issues.\n\n"
        "----- BEGIN DIFF -----\n"
        + (diff_text or "")
        + "\n----- END DIFF -----\n"
    )


def _invoke_naive(worktree, pr, run_dir, diff_text, bench_entry, timeout_s):
    """Run the bare single-pass anchor review; return an :class:`invoke.InvokeResult`.

    Reuses the invoke layer's public building blocks (``build_env`` for the identical
    isolation envelope, ``parse_costs`` for the receipt) but builds its own command
    (no --plugin-dir, pinned --max-turns, prompt via stdin) and watchdog. No config-echo
    check applies -- a no-plugin run prints no Headless config block -- and no dry-run
    payload is expected; naive candidates are extracted downstream from the raw output.
    """
    worktree = Path(worktree).resolve()
    number = pr["pr_number"]
    env = invoke.build_env(pr, run_dir, os.environ)
    pr_dir = Path(run_dir) / "pr-{}".format(number)
    pr_dir.mkdir(parents=True, exist_ok=True)
    raw_path = pr_dir / "raw-naive.json"

    claude_bin = shutil.which("claude", path=env.get("PATH") or os.environ.get("PATH"))
    if not claude_bin:
        return invoke.InvokeResult("failed", raw_json_path=str(raw_path), reason="claude_not_found")

    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(NAIVE_MAX_TURNS),
    ]
    prompt = _naive_prompt(pr, diff_text, bench_entry)

    proc = subprocess.Popen(
        cmd,
        cwd=str(worktree),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    try:
        out, _ = proc.communicate(input=prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            out, _ = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out = ""
        raw_path.write_text(out or "")
        return invoke.InvokeResult("timeout", raw_json_path=str(raw_path), reason="watchdog_timeout")

    raw_path.write_text(out or "")
    if _ASK_RE.search(out or ""):
        return invoke.InvokeResult("invalid", raw_json_path=str(raw_path), reason="askuserquestion_detected")

    envelope = _parse_envelope(out)
    costs = parse_costs(envelope or {})
    if envelope is None or proc.returncode != 0 or envelope.get("is_error"):
        reason = "no_envelope" if envelope is None else (
            envelope.get("subtype") or "is_error" if envelope.get("is_error") else "exit_{}".format(proc.returncode)
        )
        return invoke.InvokeResult(
            "failed",
            cost_usd=costs["cost_usd"],
            per_model=costs["per_model"],
            raw_json_path=str(raw_path),
            reason=reason,
        )

    return invoke.InvokeResult(
        "ok",
        cost_usd=costs["cost_usd"],
        per_model=costs["per_model"],
        raw_json_path=str(raw_path),
    )


# ------------------------------------------------------------------------- per-PR flow


def _run_prs(run_dir, urls, cp, shas, fixture_urls, timeout_s, anchor, bench_data):
    """Execute the per-PR flow for ``urls`` (already filtered to the todo set).

    Returns ``{"counts": {status: n}, "drifted": [(url, reason), ...]}``. A DriftError
    marks the PR ``drifted`` (never scored) and the run continues to the next PR.
    """
    run_dir = Path(run_dir)
    output_dir = run_dir / "output"
    fixture_text = FIXTURE_PATH.read_text() if FIXTURE_PATH.exists() else ""
    counts = defaultdict(int)
    drifted = []

    for url in urls:
        meta = shas.get(url)
        if not meta or not all(meta.get(k) for k in ("head_sha", "base_sha", "base_ref", "pr_number")):
            cp.mark(url, "failed", detail={"reason": "incomplete_sha_entry"})
            counts["failed"] += 1
            continue

        number = meta["pr_number"]
        clone_url = "https://github.com/{}/{}.git".format(meta["owner"], meta["repo"])
        pr = {"url": url, **meta}
        pr_dir = run_dir / "pr-{}".format(number)
        pr_dir.mkdir(parents=True, exist_ok=True)
        worktree = pr_dir / "worktree"

        mirror = mirrors.ensure_mirror(clone_url, MIRRORS_DIR)
        try:
            mirrors.make_worktree(
                mirror, meta["head_sha"], meta["base_sha"], meta["base_ref"], worktree, pr_number=number
            )
        except mirrors.DriftError as exc:
            cp.mark(url, "drifted", detail={"reason": str(exc)})
            drifted.append((url, str(exc)))
            counts["drifted"] += 1
            continue

        start = time.monotonic()
        try:
            if url in fixture_urls:
                (worktree / "REVIEW.md").write_text(fixture_text)

            diff_text = _compute_diff(worktree, meta["base_sha"], meta["head_sha"])
            (pr_dir / "diff.patch").write_text(diff_text)

            _clear_dir(output_dir)
            if anchor == "naive":
                result = _invoke_naive(
                    worktree, pr, run_dir, diff_text, bench_data.get(url, {}), timeout_s
                )
            else:
                result = invoke.invoke_review(worktree, pr, run_dir, timeout_s=timeout_s)
            _collect_artifacts(output_dir, pr_dir)
        finally:
            mirrors.remove_worktree(mirror, worktree)

        cp.mark(
            url,
            result.status,
            detail={
                "status": result.status,
                "cost_usd": result.cost_usd,
                "duration_s": round(time.monotonic() - start, 3),
                "reason": result.reason,
                "payload_path": result.payload_path,
            },
        )
        counts[result.status] += 1

    return {"counts": dict(counts), "drifted": drifted}


def _write_manifest(run_dir, run_id, tier, urls, timeout_s, args):
    env_fingerprint = dict(invoke.BENCH_ENV)  # the 9 DEEP_REVIEW_* values
    env_fingerprint["timeout_s"] = timeout_s
    manifest = {
        "run_id": run_id,
        "tier": tier,
        "git_sha": _git_short_sha(),
        "started": _utc_iso(),
        "anchor": args.anchor,
        "fidelity": args.fidelity,
        "env_fingerprint": env_fingerprint,
        "pr_urls": list(urls),
    }
    (Path(run_dir) / "run.json").write_text(json.dumps(manifest, indent=2))


def _print_summary(run_id, run_dir, urls, cp, summary):
    final = defaultdict(int)
    for url in urls:
        final[cp.status(url)] += 1
    print("\nRun {} -> {}".format(run_id, run_dir))
    print("  status: " + ", ".join("{}={}".format(k, v) for k, v in sorted(final.items())))
    if summary["drifted"]:
        print("  !! DRIFTED (input drift -- never scored):")
        for url, reason in summary["drifted"]:
            print("     - {}: {}".format(url, reason))


# ------------------------------------------------------------------------------- modes


def _new_run(args):
    tier = args.tier
    subsets = _load_json(GOLDEN_DIR / "subsets.json")
    shas = _load_json(GOLDEN_DIR / "shas.json")
    bench_data = _load_json(GOLDEN_DIR / "benchmark_data.min.json")
    urls = _resolve_tier(tier, subsets, shas)
    fixture_urls = set(subsets.get("review_md_fixtures", []))
    timeout_s = args.timeout_mins * 60

    run_id, run_dir = _make_run_dir(tier)
    _write_manifest(run_dir, run_id, tier, urls, timeout_s, args)
    cp = checkpoint.Checkpoint(run_dir)
    todo = cp.pending(urls)  # a fresh run -> all pending
    summary = _run_prs(run_dir, todo, cp, shas, fixture_urls, timeout_s, args.anchor, bench_data)
    _print_summary(run_id, run_dir, urls, cp, summary)
    return 0


def _resume(run_id, args, retry):
    run_dir = RUNS_ROOT / run_id
    verb = "retry" if retry else "resume"
    if not run_dir.exists():
        print("No run dir at {} -- nothing to {}.".format(run_dir, verb), file=sys.stderr)
        return 2

    manifest_path = run_dir / "run.json"
    manifest = _load_json(manifest_path) if manifest_path.exists() else {}
    tier = manifest.get("tier") or run_id.split("-", 1)[0]
    subsets = _load_json(GOLDEN_DIR / "subsets.json")
    shas = _load_json(GOLDEN_DIR / "shas.json")
    bench_data = _load_json(GOLDEN_DIR / "benchmark_data.min.json")
    urls = manifest.get("pr_urls") or _resolve_tier(tier, subsets, shas)
    fixture_urls = set(subsets.get("review_md_fixtures", []))
    fingerprint = manifest.get("env_fingerprint") or {}
    timeout_s = fingerprint.get("timeout_s") or args.timeout_mins * 60
    anchor = manifest.get("anchor") if manifest.get("anchor") is not None else args.anchor

    cp = checkpoint.Checkpoint(run_dir)
    todo = cp.failed(urls) if retry else cp.pending(urls)
    summary = _run_prs(run_dir, todo, cp, shas, fixture_urls, timeout_s, anchor, bench_data)
    _print_summary(run_id, run_dir, urls, cp, summary)
    return 0


def _score_only(run_id):
    run_dir = RUNS_ROOT / run_id
    try:
        from bench.runner import score  # noqa: F401  (Task 13)
    except ImportError:
        print(
            "--score-only needs bench/runner/score.py, which lands in Task 13 and is not "
            "present yet -- cannot re-score {}.".format(run_id),
            file=sys.stderr,
        )
        return 2
    if not run_dir.exists():
        print("No run dir at {} to score.".format(run_dir), file=sys.stderr)
        return 2
    return int(score.score_run(str(run_dir)) or 0)


# -------------------------------------------------------------------------------- CLI


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench/run.py",
        description="Drive the deep-review skill over a tier's golden PRs and checkpoint outcomes.",
    )
    parser.add_argument("--tier", choices=["smoke", "subset", "full"], help="which PR set to run")
    parser.add_argument("--runs", type=int, default=1, help="number of sequential runs (own dir each)")
    parser.add_argument("--fidelity", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--resume", metavar="RUN_ID", help="re-run only pending PRs of RUN_ID")
    parser.add_argument(
        "--retry-failed", metavar="RUN_ID", dest="retry_failed",
        help="re-run the timeout+failed PRs of RUN_ID",
    )
    parser.add_argument("--timeout-mins", type=int, default=30, dest="timeout_mins")
    parser.add_argument("--anchor", choices=["naive"], help="bare single-pass anchor review")
    parser.add_argument("--score-only", metavar="RUN_ID", dest="score_only")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    modes = [bool(args.resume), bool(args.retry_failed), bool(args.score_only)]
    if sum(modes) > 1:
        print(
            "Choose at most one of --resume / --retry-failed / --score-only.", file=sys.stderr
        )
        return 2

    # --score-only has different prerequisites (no claude/gh invocation) and its module may
    # not exist yet, so it is handled before the run prereq checks.
    if args.score_only:
        return _score_only(args.score_only)

    failures = check_prereqs()
    if failures:
        print("bench/run.py: prerequisite checks failed (fix each and re-run):", file=sys.stderr)
        for failure in failures:
            print("  - " + failure, file=sys.stderr)
        return 2

    if args.resume:
        return _resume(args.resume, args, retry=False)
    if args.retry_failed:
        return _resume(args.retry_failed, args, retry=True)

    if not args.tier:
        print("--tier is required for a new run (smoke|subset|full).", file=sys.stderr)
        return 2

    rc = 0
    for _ in range(max(1, args.runs)):
        rc = _new_run(args) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
