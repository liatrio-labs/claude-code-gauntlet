#!/usr/bin/env python3
"""The one-command bench harness runner (spec H3).

``python3 bench/run.py --tier smoke|mini|subset|holdout|full [flags]`` drives the skill
over a tier's golden PRs: for each PR it ensures a cached bare mirror, cuts a detached
worktree at the pinned head SHA (with the SHA input-drift guard), writes the REVIEW.md
fixture for designated fixture PRs, invokes the headless review under the pinned
isolated context, collects the dry-run payload + report artifacts into the per-PR dir,
saves the PR diff, removes the worktree, and checkpoints the outcome. PR-granular
checkpointing makes a run resumable; a killed pass loses at most the single PR that was
mid-flight.

This runner does NOT write ledger rows -- scoring (Task 13) does. run.py records per-PR
invoke metadata (status, cost, duration) into the checkpoint detail and a run.json
manifest (run_id, tier, git_sha, start ts, env fingerprint).

``--check RUN_ID`` runs the mechanical functional-smoke checker (Issue #28) against a
completed run directory — payload/schema, no silent degrade, plugin scriptPath identity,
and ≥1 delivered comment. It never invokes the judge.

Stdlib-only (repo CLAUDE.md). The vendored scorer keeps its own deps behind ``uv`` and is
never imported here; ``--score-only`` lazily imports the (Task 13) score module and errors
cleanly if it is not present yet. ``--check`` imports ``bench.runner.check`` only.
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

from bench.runner import checkpoint, invoke, ledger, mirrors  # noqa: E402
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
# `--tier mini` is the 6-PR pre-registered paired-measurement cut (subset of gate).
_TIER_SUBSET_KEY = {
    "smoke": "smoke",
    "subset": "gate",
    "holdout": "holdout",
    "mini": "mini",
}

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
    """Return the ordered list of golden URLs for ``tier``.

    Counts: smoke=3, mini=6, subset=15, holdout=10, full=all shas.json keys.
    """
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


def check_prereqs(env_path=None, workspace_dir=None, min_free_gb=MIN_FREE_GB,
                  child_auth=ledger.API_AUTH_MODE, env=None):
    """Return a list of one-line, actionable failure messages (empty == all prereqs met).

    ``child_auth`` swaps the credential prerequisite and nothing else. ``subscription``
    drops the metered-key requirement outright -- that key is deliberately not in play --
    and demands instead a ``CLAUDE_CODE_OAUTH_TOKEN`` plus an isolated config carrying no
    ``apiKeyHelper``: the helper outranks the token and, living in a settings file rather
    than the env, is the one over-ranking source ``build_env`` cannot strip (see
    ``invoke.api_key_helper_files``). It deliberately imposes no judge-key requirement:
    scoring is a separate step that resolves its own key and fails loud on its own, and
    the mode's primary use is a ``--check`` functional smoke, which never runs the judge.
    ``env`` defaults to ``os.environ`` (the same late binding ``score._judge_api_key``
    uses, so a caller can supply a fixed mapping without a mutable default).
    """
    env_path = Path(env_path if env_path is not None else ENV_PATH)
    workspace_dir = Path(workspace_dir if workspace_dir is not None else WORKSPACE)
    env = os.environ if env is None else env
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

    if child_auth == ledger.SUBSCRIPTION_AUTH_MODE:
        # bench/.env before ambient, the precedence _claude_auth_env applies.
        #
        # Both messages spell the var name out instead of interpolating the invoke
        # constant: a credential-named identifier reaching a print is reported as
        # clear-text logging even when only the NAME travels. Tests hold each literal
        # against the constant its lookup uses, so the two cannot drift.
        if not (_read_env_key(env_path, invoke.OAUTH_TOKEN_VAR)
                or env.get(invoke.OAUTH_TOKEN_VAR)):
            failures.append(
                f"CLAUDE_CODE_OAUTH_TOKEN missing or empty in {env_path} and in the "
                "environment -- run `claude setup-token` and add the token to bench/.env."
            )
        helper_files = invoke.api_key_helper_files(
            invoke.resolve_claude_home(workspace_dir, env)
        )
        if helper_files:
            failures.append(
                "apiKeyHelper set in {files} -- it outranks CLAUDE_CODE_OAUTH_TOKEN, so "
                "the child would authenticate with that key and never reach the "
                "subscription; remove it or run --child-auth api.".format(
                    files=", ".join(helper_files)
                )
            )
    elif not _read_env_key(env_path, "ANTHROPIC_API_KEY"):
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


def _judge_key_note(env_path, child_auth, env=None):
    """A non-fatal note when a subscription run has no key the judge could later use.

    Relaxing the ``ANTHROPIC_API_KEY`` requirement is the point of subscription mode -- the
    review children genuinely do not need it, and the mechanical ``--check`` gate never
    invokes the judge. But scoring still does, hours later and in a separate invocation, so
    saying nothing here lets an operator finish a long leg and only then discover it is
    unscoreable. A note rather than a failure: making it fatal would re-impose exactly the
    requirement the mode exists to drop.

    Mirrors ``score._judge_api_key``'s resolution (ambient first, then ``bench/.env``) as
    an existence question only -- no credential value is read into this process.
    """
    if child_auth != ledger.SUBSCRIPTION_AUTH_MODE:
        return None
    env = os.environ if env is None else env
    for name in ("BENCH_JUDGE_API_KEY", "ANTHROPIC_API_KEY"):
        if env.get(name) or _read_env_key(env_path, name):
            return None
    return (
        "NOTE: no BENCH_JUDGE_API_KEY or ANTHROPIC_API_KEY found. This subscription run "
        "will complete and can be gated with --check, but scoring it (--score-only) needs "
        "an API key for the judge."
    )


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


def _invoke_naive(worktree, pr, run_dir, diff_text, bench_entry, timeout_s,
                  child_auth=ledger.API_AUTH_MODE):
    """Run the bare single-pass anchor review; return an :class:`invoke.InvokeResult`.

    Reuses the invoke layer's public building blocks (``build_env`` for the identical
    isolation envelope, ``parse_costs`` for the receipt) but builds its own command
    (no --plugin-dir, pinned --max-turns, prompt via stdin) and watchdog. No config-echo
    check applies -- a no-plugin run prints no Headless config block -- and no dry-run
    payload is expected; naive candidates are extracted downstream from the raw output.

    The anchor is a real ``claude`` invocation, so it needs credentialing exactly like
    the skill path: ``child_auth`` goes to ``build_env``, which owns the whole decision.
    """
    worktree = Path(worktree).resolve()
    env = invoke.build_env(pr, run_dir, os.environ, child_auth=child_auth)
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
             tool="deep-review-v3", child_model="inherit",
             child_auth=ledger.API_AUTH_MODE):
    """Execute the per-PR flow for ``urls`` (already filtered to the todo set).

    ``tool`` selects the skill pipeline (deep-review-v2|v3) and is forwarded to
    ``invoke.invoke_review`` so v3 can preflight the child CLI version; it is ignored on
    the ``--anchor naive`` path, which does not run the skill. ``child_model`` is the
    resolved child-session model pin, likewise forwarded to the skill path only.
    ``child_auth`` goes to BOTH paths -- the naive anchor shells out to ``claude`` through
    the same ``build_env``, so it spends whichever credential the mode selects.

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
            # Snapshot per-child Workflow records before invoke so we can copy only
            # this PR's new/changed wf_*.json into pr_dir/workflows/ (G3 plugin-identity
            # gate). raw.json is the result envelope and never carries scriptPath.
            is_naive = anchor == "naive"
            claude_home = invoke._claude_home(run_dir, os.environ)
            wf_baseline = (
                {} if is_naive else invoke.snapshot_workflow_records(claude_home)
            )
            if is_naive:
                result = _invoke_naive(
                    worktree, pr, run_dir, diff_text, bench_data.get(url, {}), timeout_s,
                    child_auth=child_auth,
                )
            else:
                result = invoke.invoke_review(
                    worktree, pr, run_dir, timeout_s=timeout_s, tool=tool,
                    child_model=child_model, child_auth=child_auth,
                )
            _collect_artifacts(output_dir, pr_dir)
            if not is_naive:
                invoke.collect_workflow_records(claude_home, pr_dir, wf_baseline)
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


def _resolve_child_auth(child_auth):
    """Effective child-auth mode for a NEW run: an explicit flag wins, else the API key.

    ``api`` is the default because every run of record was produced that way and its cost
    figures are only comparable against another API-keyed run, while ``subscription``
    spends the operator's own capacity -- that has to be asked for, never inferred. A None
    ``child_auth`` means the flag was not passed, the same sentinel ``_resolve_child_model``
    uses. Resuming an existing run goes through ``_child_auth_for`` instead.
    """
    return child_auth if child_auth is not None else ledger.DEFAULT_AUTH_MODE


def _recorded_child_auth(run_id):
    """The mode ``run_id``'s manifest committed to, or None when there is none to honour.

    The manifest chain itself is ``ledger.manifest_auth_mode`` -- the same read the scorer
    labels the row with, so the credential a run is resumed on cannot diverge from the
    ``auth_mode`` its costs are recorded under. What this adds is the distinction that
    chain cannot make: whether there was a manifest to consult at all.

    None means "nothing recorded to honour", for two states that both leave the choice to
    the flag: no manifest (``_make_run_dir`` created the dir but the process died before
    ``_write_manifest``, which runs before the first PR, so no credential was spent), and
    an unreadable one (nothing can be claimed about what it recorded; ``_resume`` reports
    that on its own terms). A manifest that merely lacks the field is different: it
    predates the flag and so really did spend the metered key.
    """
    path = RUNS_ROOT / run_id / "run.json"
    if not path.is_file():
        return None
    try:
        manifest = _load_json(path)
    except (ValueError, OSError):
        return None
    if not isinstance(manifest, dict):
        return None
    return ledger.manifest_auth_mode(manifest)


def _child_auth_for(args, run_id=None):
    """The credential mode this invocation will actually spend.

    The one place the resume precedence lives, because three callers must agree on it or
    the run breaks: ``_resume`` (which spends the credential), ``main``'s preflight (which
    validates it), and ``main``'s conflict guard. Preflighting the flag's default while
    resuming a subscription run would demand a metered key the run does not use and skip
    the token/apiKeyHelper checks it does; preflighting subscription for a run that then
    spends the metered key is the same bug mirrored. So the resolution is read from disk
    once here rather than from whatever the caller happens to hold -- ``_resume`` passes
    only the id, never its own already-loaded manifest, so the two cannot drift apart.

    A resumed run's own recorded mode always wins. ``main`` refuses an explicit flag that
    contradicts it rather than ignoring it, because unlike the ``tool``/``child_model``
    labels this decides which credential is charged: honouring the flag would let
    ``--retry-failed X --child-auth subscription`` bill the remaining PRs the other way
    while the single ledger row can only carry one ``auth_mode``.
    """
    run_id = run_id or args.resume or args.retry_failed
    recorded = _recorded_child_auth(run_id) if run_id else None
    return recorded or _resolve_child_auth(args.child_auth)


def _write_manifest(run_dir, run_id, tier, urls, timeout_s, args):
    env_fingerprint = dict(invoke.BENCH_ENV)  # the 9 CODE_GAUNTLET_* values
    env_fingerprint["timeout_s"] = timeout_s
    child_auth = _resolve_child_auth(getattr(args, "child_auth", None))
    env_fingerprint["child_auth"] = child_auth
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
        # Recorded for EVERY run, deliberately breaking the two fields above: a naive
        # anchor still authenticates (_invoke_naive credentials its child through the same
        # build_env), so a null here would leave the run's auth provenance -- and with it
        # whether its cost is billable API spend -- unrecoverable at scoring time.
        "child_auth": child_auth,
        "fidelity": args.fidelity,
        "invocation": invocation,
        "env_fingerprint": env_fingerprint,
        "pr_urls": list(urls),
        # The explicit --prs override list (None for a tier-resolved run). When set, tier
        # is "custom" and pr_urls carries the same URLs, so resume works from the manifest.
        "prs": list(args.prs) if getattr(args, "prs", None) else None,
    }
    (Path(run_dir) / "run.json").write_text(json.dumps(manifest, indent=2))


def _write_child_auth_stub(manifest_path, run_id, child_auth):
    """Create a minimal ``run.json`` recording only which credential a resume spends.

    For a run dir that never got a manifest, so the auth provenance survives to scoring and
    to any later resume. Deliberately not a full manifest: everything else about the
    original run is unknowable here, and the fields left out already default the same way
    they do for a missing manifest (``score._tool_label``, ``_infer_tier``), so this adds
    provenance without inventing history.
    """
    manifest_path.write_text(
        json.dumps({"run_id": run_id, "child_auth": child_auth}, indent=2)
    )


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
        child_auth=_resolve_child_auth(args.child_auth),
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
    # And again for the credential mode, where the stakes are higher than a label: letting
    # a flag override the recorded mode would bill half a run's PRs one way and half the
    # other, under a single ledger row that can only carry one auth_mode. Resolved from
    # the run id, not the manifest loaded above, so this answer and the one main()
    # preflighted come from the same read (main refuses a contradicting flag outright).
    child_auth = _child_auth_for(args, run_id)
    # A run dir with no run.json at all (killed between _make_run_dir and _write_manifest)
    # has no mode to honour, so the flag above chose one -- and nothing would remember it:
    # score would read no manifest, default auth_mode to api, and let subscription spend
    # into billable figures, while a second resume could pick the other credential for the
    # remaining PRs. Record what this resume is about to spend. Only ever creates the file:
    # a manifest that merely lacks child_auth is not missing its provenance (predating the
    # flag IS its provenance), and writing into it would mutate a historical record.
    if not manifest_path.exists():
        _write_child_auth_stub(manifest_path, run_id, child_auth)

    cp = checkpoint.Checkpoint(run_dir)
    todo = cp.failed(urls) if retry else cp.pending(urls)
    summary = _run_prs(
        run_dir, todo, cp, shas, fixture_urls, timeout_s, anchor, bench_data,
        tool=tool, child_model=child_model, child_auth=child_auth,
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


def _check_only(run_id):
    """Run the mechanical functional-smoke checker against ``RUNS_ROOT/run_id``."""
    run_dir = RUNS_ROOT / run_id
    if not run_dir.is_dir():
        print("--check: run directory not found: {}".format(run_dir), file=sys.stderr)
        return 2
    from bench.runner import check as check_mod

    result = check_mod.check_run(run_dir, repo_root=REPO_ROOT)
    stats = result.get("stats") or {}
    print(
        "Check {}: ok={} pr_dirs={} comments={} findings_files={} "
        "workflow_records={} script_paths={} unknown_origin={}".format(
            run_id,
            result.get("ok"),
            stats.get("pr_dirs"),
            stats.get("delivered_comments"),
            stats.get("findings_files"),
            stats.get("workflow_records"),
            stats.get("script_paths"),
            stats.get("unknown_origin"),
        )
    )
    for failure in result.get("failures") or []:
        print("  FAIL: {}".format(failure), file=sys.stderr)
    # Naive-anchor refusal is a usage error (exit 2), not a smoke-gate failure.
    if result.get("refused"):
        return 2
    return 0 if result.get("ok") else 1


# -------------------------------------------------------------------------------- CLI


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench/run.py",
        description="Drive the code-gauntlet skill over a tier's golden PRs and checkpoint outcomes. Tool labels and scorer keys keep their pre-rename deep-review-* names for measurement continuity.",
    )
    parser.add_argument(
        "--tier",
        choices=["smoke", "mini", "subset", "holdout", "full"],
        help="which PR set to run (smoke=3, mini=6 paired cut, subset=15 gate, "
        "holdout=10, full=50)",
    )
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
    # Which credential the review children spend. None is the "not specified" sentinel
    # (as for --child-model) so the manifest's mode wins on resume. subscription has its
    # own prerequisites -- an OAuth token and no apiKeyHelper, see check_prereqs -- and
    # its runs are not cost-comparable with API-keyed ones; bench/README.md has the detail.
    parser.add_argument(
        "--child-auth", choices=list(ledger.AUTH_MODES), default=None,
        dest="child_auth",
        help="credential for the review children: the metered bench/.env key (default) "
        "or the operator's Claude subscription via CLAUDE_CODE_OAUTH_TOKEN",
    )
    parser.add_argument("--score-only", metavar="RUN_ID", dest="score_only")
    parser.add_argument(
        "--check", metavar="RUN_ID", dest="check",
        help="run the mechanical functional-smoke checker against an existing run "
        "(never invokes the judge; exit 0 = pass)",
    )
    parser.add_argument(
        "--prs", metavar="URL[,URL...]|mini",
        help="explicit comma-separated golden PR list; overrides --tier's subset "
        "resolution and labels the run 'custom'. Every URL must exist in shas.json. "
        "The alias 'mini' expands to the pre-registered 6-PR mini subset.",
    )
    args = parser.parse_args(argv)
    # Validate --prs against the golden set at parse time so an unknown URL is a hard
    # argparse error (exit 2), never a mid-run surprise; normalize the string to a list.
    # The documented alias ``mini`` expands to subsets.json["mini"] (still labels custom).
    if args.prs is not None:
        urls = [u.strip() for u in args.prs.split(",") if u.strip()]
        if not urls:
            parser.error("--prs was given but parsed no URLs")
        if urls == ["mini"]:
            subsets = _load_json(GOLDEN_DIR / "subsets.json")
            urls = list(subsets["mini"])
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

    modes = [
        bool(args.resume),
        bool(args.retry_failed),
        bool(args.score_only),
        bool(args.check),
    ]
    if sum(modes) > 1:
        print(
            "Choose at most one of --resume / --retry-failed / --score-only / --check.",
            file=sys.stderr,
        )
        return 2

    # --score-only / --check have different prerequisites (no claude/gh invocation) and
    # are handled before the run prereq checks.
    if args.score_only:
        return _score_only(args.score_only)
    if args.check:
        # --check inspects an existing run; combining it with new-run flags is always a
        # mistake (the flags would be silently ignored). Fail loud.
        if args.tier or args.prs or args.anchor or args.runs != 1:
            print(
                "--check does not accept --tier / --prs / --anchor / --runs "
                "(pass only --check RUN_ID).",
                file=sys.stderr,
            )
            return 2
        return _check_only(args.check)

    child_auth = _child_auth_for(args)
    # Only reachable on a resume, where the run's own recorded mode wins: for a new run
    # the resolved mode IS the flag. Refusing beats silently ignoring the flag -- the
    # operator asked to charge a different credential, and one run dir can only honour
    # one (its per-PR envelope costs are summed into a single auth_mode-labelled row).
    if args.child_auth is not None and args.child_auth != child_auth:
        print(
            "--child-auth {} contradicts the mode run {} recorded ({}) -- a run dir is "
            "one credential. Resume without the flag, or start a new run.".format(
                args.child_auth, args.resume or args.retry_failed, child_auth
            ),
            file=sys.stderr,
        )
        return 2

    failures = check_prereqs(child_auth=child_auth)
    if failures:
        print("bench/run.py: prerequisite checks failed (fix each and re-run):", file=sys.stderr)
        for failure in failures:
            print("  - " + failure, file=sys.stderr)
        return 2

    note = _judge_key_note(ENV_PATH, child_auth)
    if note:
        print(note, file=sys.stderr)

    if args.resume:
        return _resume(args.resume, args, retry=False)
    if args.retry_failed:
        return _resume(args.retry_failed, args, retry=True)

    if not args.tier and not args.prs:
        print(
            "--tier or --prs is required for a new run "
            "(smoke|mini|subset|holdout|full, or an explicit --prs list / --prs mini).",
            file=sys.stderr,
        )
        return 2

    rc = 0
    for _ in range(max(1, args.runs)):
        rc = _new_run(args) or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
