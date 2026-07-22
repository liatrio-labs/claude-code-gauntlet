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
import traceback
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
_TIER_SUBSET_KEY = {"smoke": "smoke", "subset": "gate", "holdout": "holdout"}

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

    build_env points CODE_GAUNTLET_OUTPUT_DIR at ``{run_dir}/output`` which is SHARED across
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


def _relocate_payload(payload_path, pr_dir):
    """Point a recorded payload path at its post-collection home under ``pr_dir``.

    ``invoke`` returns ``payload_path`` in the shared output dir, but ``_collect_artifacts``
    then moves the payload into ``pr_dir``, leaving the recorded path stale. When that path's
    basename now exists under ``pr_dir``, return the pr-dir location so the checkpoint detail
    records where the payload actually landed; otherwise return the path unchanged. A falsey
    path (no payload produced) passes through as-is; a naive payload already under ``pr_dir``
    resolves to itself.
    """
    if not payload_path:
        return payload_path
    relocated = Path(pr_dir) / Path(payload_path).name
    return str(relocated) if relocated.exists() else payload_path


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
    # Delegates to the canonical dotenv reader so the prereq check and build_env
    # agree on every edge case (quoted-empty values, comments, quoting).
    return invoke._load_dotenv_key(env_path, key)


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

# The output contract appended to the naive prompt: the bare review has no plugin to
# capture comments, so it must self-report them as a machine-readable block that
# _naive_payload_from_result turns into the same dry-run payload the adapter consumes.
_NAIVE_OUTPUT_CONTRACT = (
    "\n\nOutput contract: after your review, END your final message with a single "
    "fenced code block tagged `json` and nothing after it. The block must contain "
    "exactly this shape:\n"
    "```json\n"
    "{\"comments\": [{\"path\": \"<file path>\", \"line\": <integer line number>, "
    "\"body\": \"<the full review comment>\"}]}\n"
    "```\n"
    "Emit one object per distinct issue, in the diff's order; use an empty list "
    "(\"comments\": []) if you found no issues. Each body must be self-contained and "
    "stand on its own without referring to your other comments."
)

# The opening line of a fenced block: ``` plus an optional language tag, at the start
# of a line, through the newline. Anchored with MULTILINE so backticks *inside* a JSON
# string (json.dumps keeps the object on one physical line) are never read as a fence.
_FENCE_OPEN_RE = re.compile(r"^```[^\n]*\n", re.MULTILINE)
# A whole fenced block (opening fence, lazy body, closing fence) -- used only by the
# earlier-blocks fallback in _extract_comments, never for the final contract block.
_JSON_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# A single trailing closing fence (with surrounding whitespace) at end of text.
_TRAILING_FENCE_RE = re.compile(r"\s*```\s*$")


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
        + _NAIVE_OUTPUT_CONTRACT
    )


def _valid_naive_comment(item):
    """True when ``item`` is a well-formed naive review comment.

    The adapter downstream reads ``body`` (required) and optional ``path``/``line``,
    so enforce that shape here: a dict with a non-empty string ``body``, a ``path``
    that is a string or null, and a ``line`` that is an int or null. Anything else
    (a bare string, a missing body, a numeric path, a stringified line) is malformed.
    """
    if not isinstance(item, dict):
        return False
    body = item.get("body")
    if not isinstance(body, str) or not body.strip():
        return False
    path = item.get("path")
    if path is not None and not isinstance(path, str):
        return False
    line = item.get("line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
        return False
    return True


def _parse_comments_block(body):
    """Return the ``comments`` list from a JSON block body, or None if malformed.

    Well-formed = parses as a dict with a top-level ``comments`` list whose every
    element passes ``_valid_naive_comment``. Any other shape yields None.
    """
    try:
        obj = json.loads(body.strip())
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("comments"), list):
        return None
    block = obj["comments"]
    return block if all(_valid_naive_comment(c) for c in block) else None


def _extract_comments(result_text):
    """Return the comments list from the naive review's contract block, or None.

    The output contract puts the real answer in the FINAL fenced block, with nothing
    after it. Primary path: take the last real opening fence's span to the END of the
    text and strip one trailing fence, then json.loads it -- so a comment body that
    itself contains ``` cannot truncate the block (backticks inside a JSON string are
    harmless once the whole block reaches json.loads). A trailing closing fence
    followed by a newline registers as a fence line with a blank tail, so blank tails
    are walked past to reach the opener.

    If the last block is malformed, fall back to the earlier-blocks last-wins scan (a
    block that fails to parse or validate is skipped) -- this both lets the model echo
    the prompt's example fence and still have its real answer win, and yields None for
    an all-malformed output (the caller marks the run naive_output_unparseable).
    """
    text = result_text or ""

    for match in reversed(list(_FENCE_OPEN_RE.finditer(text))):
        tail = _TRAILING_FENCE_RE.sub("", text[match.end():])
        if not tail.strip():
            continue  # this fence line was the closing fence -- keep walking back
        block = _parse_comments_block(tail)
        if block is not None:
            return block
        break  # the contract block is malformed; defer to the earlier-blocks fallback

    comments = None
    for body in _JSON_BLOCK_RE.findall(text):
        block = _parse_comments_block(body)
        if block is not None:
            comments = block
    return comments


def _naive_payload_from_result(result_text, pr_dir):
    """Turn the naive review's self-reported comments into the adapter's dry-run payload.

    Parses the last fenced ``json`` block (top-level ``comments`` list) out of the
    envelope's result text and writes it to ``{pr_dir}/post-review-payload.json`` in the
    exact GitHub dry-run shape bench/adapter/adapt.py consumes -- it reads only
    ``platform`` + ``payload.comments``, so ``endpoint``/``method`` are null. Returns the
    payload path, or None when no parseable block is present (caller marks the run failed,
    which is retryable via --retry-failed).
    """
    comments = _extract_comments(result_text)
    if comments is None:
        return None
    payload = {
        "platform": "github",
        "endpoint": None,
        "method": None,
        "payload": {"event": "COMMENT", "comments": comments},
        "skipped": [],
    }
    dest = Path(pr_dir) / "post-review-payload.json"
    dest.write_text(json.dumps(payload))
    return dest


def _invoke_naive(worktree, pr, run_dir, diff_text, bench_entry, timeout_s):
    """Run the bare single-pass anchor review; return an :class:`invoke.InvokeResult`.

    Reuses the invoke layer's public building blocks (``build_env`` for the identical
    isolation envelope, ``parse_costs`` for the receipt) but builds its own command
    (no --plugin-dir, pinned --max-turns, prompt via stdin) and watchdog. No config-echo
    check applies -- a no-plugin run prints no Headless config block -- and no dry-run
    payload is expected; naive candidates are extracted downstream from the raw output.
    """
    worktree = Path(worktree).resolve()
    env = invoke.build_env(pr, run_dir, os.environ)
    pr_dir = Path(run_dir) / invoke.pr_dir_name(pr)
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

    envelope = invoke.parse_result_envelope(out)
    costs = parse_costs(envelope or {})
    if envelope is None or proc.returncode != 0 or envelope.get("is_error"):
        # Reuse invoke._fail_reason: it encodes is_error(subtype)/exit_N without the
        # operator-precedence trap of a local ``subtype or ... if ... else ...`` (which
        # stored reason "success" for an is_error envelope whose subtype was "success").
        return invoke.InvokeResult(
            "failed",
            cost_usd=costs["cost_usd"],
            per_model=costs["per_model"],
            raw_json_path=str(raw_path),
            reason=invoke._fail_reason(proc.returncode, envelope),
        )

    payload_path = _naive_payload_from_result(envelope.get("result"), pr_dir)
    if payload_path is None:
        return invoke.InvokeResult(
            "failed",
            cost_usd=costs["cost_usd"],
            per_model=costs["per_model"],
            raw_json_path=str(raw_path),
            reason="naive_output_unparseable",
        )

    return invoke.InvokeResult(
        "ok",
        cost_usd=costs["cost_usd"],
        per_model=costs["per_model"],
        payload_path=str(payload_path),
        raw_json_path=str(raw_path),
    )


# ------------------------------------------------------------------------- per-PR flow


def _run_prs(run_dir, urls, cp, shas, fixture_urls, timeout_s, anchor, bench_data,
             tool="deep-review-v3", child_model="inherit"):
    """Execute the per-PR flow for ``urls`` (already filtered to the todo set).

    ``tool`` selects the skill pipeline (deep-review-v2|v3) and is forwarded to
    ``invoke.invoke_review`` so v3 can preflight the child CLI version; it is ignored on
    the ``--anchor naive`` path, which does not run the skill. ``child_model`` is the
    resolved child-session model pin, likewise forwarded to the skill path only.

    Returns ``{"counts": {status: n}, "drifted": [(url, reason), ...]}``. A DriftError
    marks the PR ``drifted`` (never scored) and the run continues to the next PR.
    """
    run_dir = Path(run_dir)
    output_dir = run_dir / "output"
    fixture_text = FIXTURE_PATH.read_text() if FIXTURE_PATH.exists() else ""
    counts = defaultdict(int)
    drifted = []

    # A scorable entry has each key present, truthy, and not the fetch_shas "missing"
    # sentinel (owner/repo are needed to build the clone URL; the SHAs/ref to check out).
    required = ("owner", "repo", "head_sha", "base_sha", "base_ref", "pr_number")

    for url in urls:
        meta = shas.get(url)
        if not meta or not all(meta.get(k) and meta.get(k) != "missing" for k in required):
            cp.mark(url, "failed", detail={"reason": "incomplete_sha_entry"})
            counts["failed"] += 1
            continue

        number = meta["pr_number"]
        clone_url = "https://github.com/{}/{}.git".format(meta["owner"], meta["repo"])
        pr = {"url": url, **meta}
        # pr-{owner}-{repo}-{n}: golden URLs reuse pull numbers across repos, so a bare
        # pr-{n} would collide on --tier full. run.py, invoke.py, and score.py share
        # invoke.pr_dir_name so the worktree placement, raw/payload writes, and scoring
        # resolution all key on the same dir name.
        pr_dir = run_dir / invoke.pr_dir_name(pr)
        pr_dir.mkdir(parents=True, exist_ok=True)
        worktree = pr_dir / "worktree"

        # Mirror clone/fetch is per-PR: a single bad repo must fail only its own PR,
        # never abort the tier. Input drift (make_worktree) is a distinct outcome.
        try:
            mirror = mirrors.ensure_mirror(clone_url, MIRRORS_DIR)
            mirrors.make_worktree(
                mirror, meta["head_sha"], meta["base_sha"], meta["base_ref"], worktree, pr_number=number
            )
        except mirrors.DriftError as exc:
            cp.mark(url, "drifted", detail={"reason": str(exc)})
            drifted.append((url, str(exc)))
            counts["drifted"] += 1
            continue
        except (subprocess.CalledProcessError, OSError) as exc:
            # OSError covers e.g. shutil.rmtree failing during stale-worktree
            # cleanup -- same containment as a clone/fetch failure.
            cp.mark(
                url,
                "failed",
                detail={
                    "reason": "mirror_error",
                    "error": "{}: {}".format(type(exc).__name__, str(exc)[:200]),
                },
            )
            counts["failed"] += 1
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
                result = invoke.invoke_review(
                    worktree, pr, run_dir, timeout_s=timeout_s, tool=tool,
                    child_model=child_model,
                )
            _collect_artifacts(output_dir, pr_dir)
        except Exception as exc:
            # PR-granular progress: an unexpected error (bad JSON, an OSError during
            # collection, a plain bug) must fail only this PR and let the tier continue --
            # distinct from the mirror block's DriftError->drifted and
            # CalledProcessError->mirror_error above. KeyboardInterrupt/SystemExit derive
            # from BaseException (not Exception), so they are never swallowed here and stop
            # the run as intended. The finally below still removes the worktree.
            reason = "unexpected_error:{}: {}".format(type(exc).__name__, str(exc)[:200])
            print(
                "!! {} failed unexpectedly ({}) -- continuing to next PR".format(url, reason),
                file=sys.stderr,
            )
            cp.mark(url, "failed", detail={"reason": reason, "traceback": traceback.format_exc()})
            counts["failed"] += 1
            continue
        finally:
            # Cleanup failure must never abort the tier (a stale worktree is
            # self-healed by the next make_worktree anyway); warn and move on.
            try:
                mirrors.remove_worktree(mirror, worktree)
            except Exception as cleanup_exc:
                print(
                    "!! worktree cleanup failed for {} ({}) -- continuing".format(
                        url, cleanup_exc
                    ),
                    file=sys.stderr,
                )

        payload_path = _relocate_payload(result.payload_path, pr_dir)
        cp.mark(
            url,
            result.status,
            detail={
                "status": result.status,
                "cost_usd": result.cost_usd,
                "duration_s": round(time.monotonic() - start, 3),
                "reason": result.reason,
                "payload_path": payload_path,
            },
        )
        counts[result.status] += 1

    return {"counts": dict(counts), "drifted": drifted}


def _resolve_child_model(tool, child_model):
    """Effective child-session model: an explicit flag wins, else the per-tool default.

    v3's child orchestrator session is mechanical -- it drives the Workflow pipeline while
    the review agents' models are set by the pipeline's own policy -- yet the orchestrator
    burns ~40-45% of a run's tokens on opus[1m]. Measured 2026-07-21: BOTH sonnet (2x per-PR tokens) and
    sonnet[1m] (+51%) are WORSE than the inherited opus[1m] child (its 1M context avoids
    compaction churn), so every tool defaults to ``inherit`` now; the flag remains for experiments. A None
    ``child_model`` means the flag was not passed (take the per-tool default).
    """
    if child_model is not None:
        return child_model
    return "inherit"


def _write_manifest(run_dir, run_id, tier, urls, timeout_s, args):
    env_fingerprint = dict(invoke.BENCH_ENV)  # the 9 CODE_GAUNTLET_* values
    env_fingerprint["timeout_s"] = timeout_s
    invocation = (
        "naive:single-pass max-turns={}".format(NAIVE_MAX_TURNS)
        if args.anchor == "naive"
        else "headless:/code-gauntlet"
    )
    manifest = {
        "run_id": run_id,
        "tier": tier,
        "git_sha": _git_short_sha(),
        "started": _utc_iso(),
        "anchor": args.anchor,
        # The naive anchor is its own baseline (not a deep-review pipeline), so it carries
        # no tool label -- score._tool_label gives an explicit tool precedence over anchor,
        # so writing one here would mask the "naive-anchor" ledger label. A skill run records
        # the selected pipeline (deep-review-v2|v3) verbatim for the ledger row.
        "tool": None if args.anchor == "naive" else args.tool,
        # The child-session model pin, resolved per-tool. Naive anchor runs record None
        # (they run _invoke_naive, not the skill) -- mirrors the tool field above.
        "child_model": (
            None if args.anchor == "naive"
            else _resolve_child_model(args.tool, args.child_model)
        ),
        "fidelity": args.fidelity,
        "invocation": invocation,
        "env_fingerprint": env_fingerprint,
        "pr_urls": list(urls),
        # The explicit --prs override list (None for a tier-resolved run). When set, tier
        # is "custom" and pr_urls carries the same URLs, so resume works from the manifest.
        "prs": list(args.prs) if getattr(args, "prs", None) else None,
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
    return final


def _exit_code(final, urls):
    """Return 0 only when every targeted PR ended status ``ok``; 1 otherwise."""
    return 0 if final.get("ok", 0) == len(urls) else 1


# ------------------------------------------------------------------------------- modes


def _new_run(args):
    subsets = _load_json(GOLDEN_DIR / "subsets.json")
    shas = _load_json(GOLDEN_DIR / "shas.json")
    bench_data = _load_json(GOLDEN_DIR / "benchmark_data.min.json")
    # An explicit --prs list overrides --tier's subset resolution and labels the run
    # "custom"; the URLs were validated against shas.json at parse time.
    if args.prs:
        tier, urls = "custom", list(args.prs)
    else:
        tier, urls = args.tier, _resolve_tier(args.tier, subsets, shas)
    fixture_urls = set(subsets.get("review_md_fixtures", []))
    timeout_s = args.timeout_mins * 60

    run_id, run_dir = _make_run_dir(tier)
    _write_manifest(run_dir, run_id, tier, urls, timeout_s, args)
    cp = checkpoint.Checkpoint(run_dir)
    todo = cp.pending(urls)  # a fresh run -> all pending
    summary = _run_prs(
        run_dir, todo, cp, shas, fixture_urls, timeout_s, args.anchor, bench_data,
        tool=args.tool, child_model=_resolve_child_model(args.tool, args.child_model),
    )
    final = _print_summary(run_id, run_dir, urls, cp, summary)
    return _exit_code(final, urls)


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
    # Prefer the manifest's recorded pipeline so a resume re-runs the same tool it began
    # with; fall back to the CLI default for pre-tool run.json files (or a naive run, whose
    # None tool is unused because the anchor path skips the skill).
    tool = manifest.get("tool") or args.tool
    # Same precedence for the child-model pin: the manifest value wins so a resume re-runs
    # the same model it began with; fall back to the per-tool default for pre-child_model
    # run.json files (or a naive run, whose None value is unused by the anchor path).
    child_model = manifest.get("child_model") or _resolve_child_model(args.tool, args.child_model)

    cp = checkpoint.Checkpoint(run_dir)
    todo = cp.failed(urls) if retry else cp.pending(urls)
    summary = _run_prs(
        run_dir, todo, cp, shas, fixture_urls, timeout_s, anchor, bench_data,
        tool=tool, child_model=child_model,
    )
    final = _print_summary(run_id, run_dir, urls, cp, summary)
    return _exit_code(final, urls)


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
    try:
        score.score_run(str(run_dir))
    except (ValueError, RuntimeError) as exc:
        print("--score-only failed for {}: {}".format(run_id, exc), file=sys.stderr)
        return 2
    print("Scored {}: wrote {}".format(run_id, run_dir / "scores.json"))
    return 0


# -------------------------------------------------------------------------------- CLI


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench/run.py",
        description="Drive the code-gauntlet skill over a tier's golden PRs and checkpoint outcomes. Tool labels and scorer keys keep their pre-rename deep-review-* names for measurement continuity.",
    )
    parser.add_argument("--tier", choices=["smoke", "subset", "holdout", "full"], help="which PR set to run")
    parser.add_argument("--runs", type=int, default=1, help="number of sequential runs (own dir each)")
    parser.add_argument("--fidelity", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--resume", metavar="RUN_ID", help="re-run only pending PRs of RUN_ID")
    parser.add_argument(
        "--retry-failed", metavar="RUN_ID", dest="retry_failed",
        help="re-run the timeout+failed PRs of RUN_ID",
    )
    # Default calibrated from the Task 16 smoke shakedown: full-skill reviews ran
    # 971-1345s, peaking at 75% of the original 30-min budget (plan threshold: >50%).
    parser.add_argument("--timeout-mins", type=int, default=45, dest="timeout_mins")
    parser.add_argument("--anchor", choices=["naive"], help="bare single-pass anchor review")
    parser.add_argument(
        "--tool", choices=["deep-review-v2", "deep-review-v3"], default="deep-review-v3",
        help="which deep-review pipeline to invoke/label (default: deep-review-v3)",
    )
    # Default is per-tool (resolved by _resolve_child_model): sonnet for v3, inherit for v2.
    # None is the "not specified" sentinel so an explicit flag can be told apart from the
    # default and the manifest value can win on resume.
    parser.add_argument(
        "--child-model", choices=["sonnet", "sonnet[1m]", "opus", "opus[1m]", "inherit"], default=None,
        dest="child_model",
        help="model for the child orchestrator session "
        "(default: sonnet for deep-review-v3, inherit for deep-review-v2)",
    )
    parser.add_argument("--score-only", metavar="RUN_ID", dest="score_only")
    parser.add_argument(
        "--prs", metavar="URL[,URL...]",
        help="explicit comma-separated golden PR list; overrides --tier's subset "
        "resolution and labels the run 'custom'. Every URL must exist in shas.json.",
    )
    args = parser.parse_args(argv)
    # Validate --prs against the golden set at parse time so an unknown URL is a hard
    # argparse error (exit 2), never a mid-run surprise; normalize the string to a list.
    if args.prs is not None:
        urls = [u.strip() for u in args.prs.split(",") if u.strip()]
        if not urls:
            parser.error("--prs was given but parsed no URLs")
        shas = _load_json(GOLDEN_DIR / "shas.json")
        unknown = [u for u in urls if u not in shas]
        if unknown:
            parser.error(
                "--prs contains URL(s) not in shas.json: {}".format(", ".join(unknown))
            )
        args.prs = urls
    return args


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

    if not args.tier and not args.prs:
        print(
            "--tier or --prs is required for a new run "
            "(smoke|subset|holdout|full, or an explicit --prs list).",
            file=sys.stderr,
        )
        return 2

    rc = 0
    for _ in range(max(1, args.runs)):
        rc = _new_run(args) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
