#!/usr/bin/env python3
"""Run sequential, checkpointed benchmark reviews and preserve immutable attempts.

Mechanical checks and historical scoring are separate entry modes. Measurement runs
pin their window before mirror work and recheck identity before every review child.
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import traceback
from collections import defaultdict
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

# invoke.py imports as ``bench.runner.costs`` -> the repo root must be importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.runner import checkpoint, invoke, ledger, mirrors, validity  # noqa: E402
from bench.runner.costs import parse_costs  # noqa: E402
from bench.runner.epochs import load_epoch  # noqa: E402

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
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _git_short_sha():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def _run_epoch(args):
    if getattr(args, "_epoch", None) is None:
        args._epoch = load_epoch(root=GOLDEN_DIR)
    return args._epoch


def _target_urls(tier, explicit=None, *, epoch=None):
    if explicit:
        return list(explicit)
    epoch = epoch if epoch is not None else load_epoch(root=GOLDEN_DIR)
    return _resolve_tier(tier, epoch.subsets, epoch.shas)


def _read_run_manifest(run_id):
    try:
        manifest = _load_json(RUNS_ROOT / run_id / "run.json")
    except (ValueError, OSError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _first_target_pr(args):
    run_id = args.resume or args.retry_failed
    if run_id:
        run_dir = RUNS_ROOT / run_id
        if not run_dir.exists():
            return None
        manifest = _read_run_manifest(run_id) or {}
        urls = _target_urls(
            manifest.get("tier") or run_id.split("-", 1)[0],
            manifest.get("pr_urls"),
            epoch=_run_epoch(args),
        )
    elif args.prs or args.tier:
        urls = _target_urls(args.tier, args.prs, epoch=_run_epoch(args))
    else:
        return None
    return {"url": urls[0]} if urls else None


@contextmanager
def _gh_token_scope(env):
    previous = os.environ.get("GH_TOKEN")
    token = env.get("GH_TOKEN")
    if token:
        os.environ["GH_TOKEN"] = token
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = previous


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
        encoding="utf-8",
    )
    return result.stdout


# --------------------------------------------------------------------------- prereqs


def _read_env_key(env_path, key):
    return invoke._load_dotenv_key(env_path, key)


def _free_gb(path):
    """Free GB on the partition holding ``path`` (walks up to the first existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(str(probe)).free / (1024**3)


def check_prereqs(
    env_path=None,
    workspace_dir=None,
    min_free_gb=MIN_FREE_GB,
    child_auth=ledger.API_AUTH_MODE,
    env=None,
    gh_pr=None,
):
    """Return actionable prerequisite failures; put a validated GH_TOKEN into ``env``.

    ``gh_pr`` selects the access probe; omit it for checks without a review target.

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
    ``host`` requires CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST and reads no credential.
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
        result = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, encoding="utf-8"
        )
        blob = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or not re.search(r"\d+\.\d+", blob):
            failures.append(
                "`claude --version` did not return a parseable version -- check the Claude Code install."
            )

    if not shutil.which("gh"):
        failures.append(
            "gh CLI not found on PATH -- install GitHub CLI and run `gh auth login`."
        )

    if child_auth == "host":
        if not env.get("CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"):
            failures.append("host auth requires CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST")
    elif child_auth == ledger.SUBSCRIPTION_AUTH_MODE:
        # bench/.env before ambient, the precedence _claude_auth_env applies.
        #
        # Both messages spell the var name out instead of interpolating the invoke
        # constant: a credential-named identifier reaching a print is reported as
        # clear-text logging even when only the NAME travels. Tests hold each literal
        # against the constant its lookup uses, so the two cannot drift.
        if not (
            _read_env_key(env_path, invoke.OAUTH_TOKEN_VAR)
            or env.get(invoke.OAUTH_TOKEN_VAR)
        ):
            failures.append(
                f"CLAUDE_CODE_OAUTH_TOKEN missing or empty in {env_path} and in the "
                "environment -- run `claude setup-token` and add the token to bench/.env."
            )
    elif not _read_env_key(env_path, "ANTHROPIC_API_KEY"):
        failures.append(
            f"ANTHROPIC_API_KEY missing or empty in {env_path} -- "
            "copy bench/.env.example to bench/.env and add the metered key."
        )

    if child_auth in (ledger.SUBSCRIPTION_AUTH_MODE, "host"):
        helper_files = invoke.api_key_helper_files(
            invoke.resolve_claude_home(workspace_dir, env)
        )
        if helper_files:
            failures.append(
                "apiKeyHelper set in {files} -- it outranks CLAUDE_CODE_OAUTH_TOKEN "
                "and host auth; "
                "remove it or run --child-auth api.".format(
                    files=", ".join(helper_files)
                )
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

    if not failures and gh_pr is not None:
        token = invoke.preflight_gh(gh_pr, workspace_dir, env, child_auth)
        if token is None:
            failures.append(
                "GitHub PR access preflight failed -- set GH_TOKEN or run `gh auth login` "
                "with access to the first target PR."
            )
        else:
            env["GH_TOKEN"] = token

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
    '{"comments": [{"path": "<file path>", "line": <integer line number>, '
    '"body": "<the full review comment>"}]}\n'
    "```\n"
    "Emit one object per distinct issue, in the diff's order; use an empty list "
    '("comments": []) if you found no issues. Each body must be self-contained and '
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
        f"You are reviewing a pull request titled: {title}\n\n"
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
    return line is None or (isinstance(line, int) and not isinstance(line, bool))


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
        tail = _TRAILING_FENCE_RE.sub("", text[match.end() :])
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
    dest.write_text(json.dumps(payload), encoding="utf-8")
    return dest


def _invoke_naive(
    worktree,
    pr,
    run_dir,
    diff_text,
    bench_entry,
    timeout_s,
    child_auth=ledger.API_AUTH_MODE,
):
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

    claude_bin = invoke.pinned_cli(run_dir) or shutil.which(
        "claude", path=env.get("PATH") or os.environ.get("PATH")
    )
    if not claude_bin:
        return invoke.InvokeResult(
            "failed", raw_json_path=str(raw_path), reason="claude_not_found"
        )

    cmd = [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--max-turns",
        str(NAIVE_MAX_TURNS),
    ]
    manifest = (
        _load_json(Path(run_dir) / "run.json")
        if (Path(run_dir) / "run.json").is_file()
        else {}
    )
    requested_model = (manifest.get("requested_models") or {}).get(
        "orchestrator"
    ) or manifest.get("child_model")
    if requested_model not in (None, "inherit"):
        cmd += ["--model", requested_model]
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
        encoding="utf-8",
    )
    try:
        out, _ = proc.communicate(input=prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        try:
            out, _ = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            out = ""
        raw_path.write_text(out or "", encoding="utf-8")
        return invoke.InvokeResult(
            "timeout", raw_json_path=str(raw_path), reason="watchdog_timeout"
        )

    raw_path.write_text(out or "", encoding="utf-8")
    if _ASK_RE.search(out or ""):
        return invoke.InvokeResult(
            "invalid", raw_json_path=str(raw_path), reason="askuserquestion_detected"
        )

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


def _checkpoint_attempt(
    run_dir,
    pr_dir,
    cp,
    url,
    result,
    started_at,
    manifest,
    *,
    payload_path=None,
    checkpoint_status=None,
    extra=None,
):
    result = replace(result, payload_path=payload_path or result.payload_path)
    prior_ref = cp.detail(url).get("attempt_ref")
    reset_path = Path(run_dir) / "reset-time.json"
    attempt = validity.collect_attempt(
        run_dir,
        pr_dir,
        url=url,
        result=result,
        started_at=started_at,
        manifest=manifest,
        prior=validity.load_attempt(validity.confined(run_dir, prior_ref))
        if prior_ref
        else None,
        reset_at=_load_json(reset_path)["confirmed_at"]
        if reset_path.is_file()
        else None,
    )
    attempt_path = validity.selected_attempt_path(run_dir, attempt.attempt_id)
    cp.mark(
        url,
        checkpoint_status or attempt.checkpoint_status,
        detail={
            "status": result.status,
            "cost_usd": result.cost_usd,
            "duration_s": max(
                0,
                (
                    validity.timestamp(attempt.completed_at)
                    - validity.timestamp(started_at)
                ).total_seconds(),
            ),
            "reason": result.reason or ",".join(attempt.validity.reasons) or None,
            "payload_path": result.payload_path,
            "attempt_id": attempt.attempt_id,
            "attempt_ref": str(attempt_path.relative_to(run_dir)),
            "validity": attempt.validity.wire(),
            **(extra or {}),
        },
    )
    return attempt


def _run_prs(
    run_dir,
    urls,
    cp,
    shas,
    fixture_urls,
    timeout_s,
    anchor,
    bench_data,
    tool="deep-review-v3",
    child_model="inherit",
    child_auth=ledger.API_AUTH_MODE,
    epoch=None,
):
    """Execute only scheduled URLs; preserve failed setup and review attempts alike."""
    run_dir = Path(run_dir)
    output_dir = run_dir / "output"
    fixture_text = (
        FIXTURE_PATH.read_text(encoding="utf-8") if FIXTURE_PATH.exists() else ""
    )
    counts = defaultdict(int)
    drifted = []
    manifest_path = run_dir / "run.json"
    manifest = (
        _load_json(manifest_path)
        if manifest_path.is_file()
        else {"run_id": run_dir.name}
    )

    decision_hook("reserve", manifest=manifest, epoch=epoch)

    # A scorable entry has each key present, truthy, and not the fetch_shas "missing"
    # sentinel (owner/repo are needed to build the clone URL; the SHAs/ref to check out).
    required = ("owner", "repo", "head_sha", "base_sha", "base_ref", "pr_number")

    for url in urls:
        if manifest.get("measurement_window"):
            try:
                validity.require_clean_treatment()
                validity.measurement_context(manifest)
                observed = validity.run_provenance(
                    child_model=child_model,
                    child_auth=child_auth,
                    timeout_s=timeout_s,
                    epoch_id=manifest["epoch_id"],
                    window=validity.load_window(Path(manifest["measurement_window"])),
                    arm=manifest["arm"],
                    epoch=epoch,
                )
                if (
                    not validity.same_provenance(observed, manifest["provenance"])
                    or manifest.get("treatment_git_sha") != validity.treatment_sha()
                    or manifest.get("agent_read_sha256") != validity.agent_read_hash()
                ):
                    raise ValueError("input_drift: immutable run context changed")
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print(str(exc), file=sys.stderr)
                raise ValueError(
                    "measurement preflight refused before mirror/child"
                ) from exc
        meta = shas.get(url)
        if not meta or not all(
            meta.get(k) and meta.get(k) != "missing" for k in required
        ):
            _checkpoint_attempt(
                run_dir,
                run_dir / invoke.pr_dir_name({"url": url}),
                cp,
                url,
                invoke.InvokeResult("failed", reason="incomplete_sha_entry"),
                validity.utc_now(),
                manifest,
            )
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
        started_at = validity.utc_now()

        # Mirror clone/fetch is per-PR: a single bad repo must fail only its own PR,
        # never abort the tier. Input drift (make_worktree) is a distinct outcome.
        try:
            mirror = mirrors.ensure_mirror(clone_url, MIRRORS_DIR)
            mirrors.make_worktree(
                mirror,
                meta["head_sha"],
                meta["base_sha"],
                meta["base_ref"],
                worktree,
                pr_number=number,
            )
        except mirrors.DriftError as exc:
            _checkpoint_attempt(
                run_dir,
                pr_dir,
                cp,
                url,
                invoke.InvokeResult("invalid", reason="input_drift:" + str(exc)),
                started_at,
                manifest,
                checkpoint_status="drifted",
            )
            drifted.append((url, str(exc)))
            counts["drifted"] += 1
            continue
        except (subprocess.CalledProcessError, OSError) as exc:
            # OSError covers e.g. shutil.rmtree failing during stale-worktree
            # cleanup -- same containment as a clone/fetch failure.
            _checkpoint_attempt(
                run_dir,
                pr_dir,
                cp,
                url,
                invoke.InvokeResult("failed", reason="mirror_error"),
                started_at,
                manifest,
                extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            counts["failed"] += 1
            continue

        result = None
        try:
            if url in fixture_urls:
                (worktree / "REVIEW.md").write_text(fixture_text, encoding="utf-8")

            diff_text = _compute_diff(worktree, meta["base_sha"], meta["head_sha"])
            (pr_dir / "diff.patch").write_text(diff_text, encoding="utf-8")

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
                    worktree,
                    pr,
                    run_dir,
                    diff_text,
                    bench_data.get(url, {}),
                    timeout_s,
                    child_auth=child_auth,
                )
            else:
                result = invoke.invoke_review(
                    worktree,
                    pr,
                    run_dir,
                    timeout_s=timeout_s,
                    tool=tool,
                    child_model=child_model,
                    child_auth=child_auth,
                )
            _collect_artifacts(output_dir, pr_dir)
            if not is_naive:
                invoke.collect_workflow_records(claude_home, pr_dir, wf_baseline)
                from bench.runner.stages import collect_trace

                collect_trace(pr_dir)
        except Exception as exc:  # noqa: BLE001 - fail only this PR and continue
            # PR-granular progress: an unexpected error (bad JSON, an OSError during
            # collection, a plain bug) must fail only this PR and let the tier continue --
            # distinct from the mirror block's DriftError->drifted and
            # CalledProcessError->mirror_error above. KeyboardInterrupt/SystemExit derive
            # from BaseException (not Exception), so they are never swallowed here and stop
            # the run as intended. The finally below still removes the worktree.
            reason = f"unexpected_error:{type(exc).__name__}: {str(exc)[:200]}"
            print(
                f"!! {url} failed unexpectedly ({reason}) -- continuing to next PR",
                file=sys.stderr,
            )
            _checkpoint_attempt(
                run_dir,
                pr_dir,
                cp,
                url,
                replace(result, status="failed", reason=reason)
                if result is not None
                else invoke.InvokeResult("failed", reason=reason),
                started_at,
                manifest,
                extra={"traceback": traceback.format_exc()},
            )
            counts["failed"] += 1
            continue
        finally:
            # Cleanup failure must never abort the tier (a stale worktree is
            # self-healed by the next make_worktree anyway); warn and move on.
            try:
                mirrors.remove_worktree(mirror, worktree)
            except Exception as cleanup_exc:  # noqa: BLE001 - best-effort cleanup
                print(
                    f"!! worktree cleanup failed for {url} ({cleanup_exc}) -- continuing",
                    file=sys.stderr,
                )

        payload_path = _relocate_payload(result.payload_path, pr_dir)
        _checkpoint_attempt(
            run_dir,
            pr_dir,
            cp,
            url,
            result,
            started_at,
            manifest,
            payload_path=payload_path,
        )
        counts[cp.status(url)] += 1

    return {"counts": dict(counts), "drifted": drifted}


def _resolve_child_model(tool, child_model):
    """Use an explicit child-session model, otherwise inherit the CLI default."""
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
    manifest = _read_run_manifest(run_id)
    return ledger.manifest_auth_mode(manifest) if manifest is not None else None


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
    """Write run provenance, including the shell scrub observed at manifest time.

    A resume in a different shell does not rewrite this manifest.
    """
    epoch = _run_epoch(args)
    env_fingerprint = dict(invoke.BENCH_ENV)  # the registry CODE_GAUNTLET_* values
    env_fingerprint["timeout_s"] = timeout_s
    child_auth = _resolve_child_auth(getattr(args, "child_auth", None))
    env_fingerprint["child_auth"] = child_auth
    _, ambient_scrubbed = invoke.scrub_ambient(os.environ)
    env_fingerprint["config_echo"] = invoke.EXPECTED_ECHO_RECEIPT
    env_fingerprint["ambient_scrubbed"] = ambient_scrubbed
    invocation = (
        f"naive:single-pass max-turns={NAIVE_MAX_TURNS}"
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
            None
            if args.anchor == "naive"
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
    provenance = getattr(args, "_provenance", None) or validity.run_provenance(
        child_model=_resolve_child_model(args.tool, args.child_model),
        child_auth=child_auth,
        timeout_s=timeout_s,
        epoch_id=getattr(args, "golden_epoch", None),
        epoch=epoch,
    )
    manifest.update(
        provenance=provenance,
        unseal_holdout=getattr(args, "unseal_holdout", None),
        rule=getattr(args, "rule", None),
        contamination=getattr(args, "contamination", None),
        contamination_sha256=validity.digest(_load_json(args.contamination))
        if getattr(args, "contamination", None)
        else None,
        epoch_id=provenance.get("controls", provenance)["golden_epoch_id"],
        golden_sha256=provenance.get("controls", provenance)["golden_sha256"],
        measurement_window=getattr(args, "measurement_window", None),
        window_id=args._window.window_id if getattr(args, "_window", None) else None,
        window_sha256=validity.file_hash(Path(args.measurement_window))
        if getattr(args, "measurement_window", None)
        else None,
        run_index=getattr(args, "run_index", None),
        arm=getattr(args, "arm", None),
        instrumentation_version="stage-trace-v1",
        agent_read_sha256=validity.agent_read_hash(),
        pr_identities={u: epoch.shas[u] for u in urls},
    )
    manifest["treatment_git_sha"] = validity.treatment_sha()
    manifest["requested_models"] = {
        "orchestrator": _resolve_child_model(args.tool, args.child_model)
    }
    if getattr(args, "baseline_equivalence_receipt", None):
        manifest["baseline_equivalence_receipt"] = _load_json(
            args.baseline_equivalence_receipt
        )
    if getattr(args, "measurement_window", None):
        # Registration order needs subsecond resolution.
        manifest["started"] = validity.utc_now()
        manifest["child_model"] = _resolve_child_model(args.tool, args.child_model)
    validity.exclusive_json(Path(run_dir) / "run.json", manifest)


def _write_child_auth_stub(manifest_path, run_id, child_auth):
    """Create a minimal ``run.json`` recording only which credential a resume spends.

    For a run dir that never got a manifest, so the auth provenance survives to scoring and
    to any later resume. Deliberately not a full manifest: everything else about the
    original run is unknowable here, and the fields left out already default the same way
    they do for a missing manifest (``score._tool_label``, ``_infer_tier``), so this adds
    provenance without inventing history.
    """
    manifest_path.write_text(
        json.dumps({"run_id": run_id, "child_auth": child_auth}, indent=2),
        encoding="utf-8",
    )


def _print_summary(run_id, run_dir, urls, cp, summary):
    final = defaultdict(int)
    reasons = defaultdict(int)
    for url in urls:
        status = cp.status(url)
        final[status] += 1
        if status != "ok":
            reason = cp.detail(url).get("reason") or "unknown"
            reasons[str(reason)] += 1
    print(f"\nRun {run_id} -> {run_dir}")
    print("  status: " + ", ".join(f"{k}={v}" for k, v in sorted(final.items())))
    if reasons:
        print("  reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    if summary["drifted"]:
        print("  !! DRIFTED (input drift -- never scored):")
        for url, reason in summary["drifted"]:
            print(f"     - {url}: {reason}")
    return final


def _exit_code(final, urls):
    """Return 0 only when every targeted PR ended status ``ok``; 1 otherwise."""
    return 0 if final.get("ok", 0) == len(urls) else 1


# ------------------------------------------------------------------------------- modes


def _new_run(args):
    epoch = _run_epoch(args)
    subsets, shas, bench_data = epoch.subsets, epoch.shas, epoch.scorer_input
    # An explicit --prs list overrides --tier's subset resolution and labels the run
    # "custom"; the URLs were validated against shas.json at parse time.
    if args.prs:
        tier, urls = "custom", _target_urls(args.tier, args.prs, epoch=epoch)
    else:
        tier, urls = args.tier, _target_urls(args.tier, epoch=epoch)
    if getattr(args, "_window", None):
        urls = list(args._window.pr_urls)
    fixture_urls = set(subsets.get("review_md_fixtures", []))
    timeout_s = args.timeout_mins * 60

    run_id, run_dir = _make_run_dir(tier)
    _write_manifest(run_dir, run_id, tier, urls, timeout_s, args)
    cp = checkpoint.Checkpoint(run_dir)
    todo = cp.pending(urls)  # a fresh run -> all pending
    summary = _run_prs(
        run_dir,
        todo,
        cp,
        shas,
        fixture_urls,
        timeout_s,
        args.anchor,
        bench_data,
        tool=args.tool,
        child_model=_resolve_child_model(args.tool, args.child_model),
        child_auth=_resolve_child_auth(args.child_auth),
        epoch=epoch,
    )
    final = _print_summary(run_id, run_dir, urls, cp, summary)
    return _exit_code(final, urls)


def _resume(run_id, args, retry):
    run_dir = RUNS_ROOT / run_id
    verb = "retry" if retry else "resume"
    if not run_dir.exists():
        print(f"No run dir at {run_dir} -- nothing to {verb}.", file=sys.stderr)
        return 2

    manifest_path = run_dir / "run.json"
    manifest = _load_json(manifest_path) if manifest_path.exists() else {}
    tier = manifest.get("tier") or run_id.split("-", 1)[0]
    epoch = _run_epoch(args)
    subsets, shas, bench_data = epoch.subsets, epoch.shas, epoch.scorer_input
    urls = _target_urls(tier, manifest.get("pr_urls"), epoch=epoch)
    fixture_urls = set(subsets.get("review_md_fixtures", []))
    fingerprint = manifest.get("env_fingerprint") or {}
    timeout_s = fingerprint.get("timeout_s") or args.timeout_mins * 60
    anchor = (
        manifest.get("anchor") if manifest.get("anchor") is not None else args.anchor
    )
    # Prefer the manifest's recorded pipeline so a resume re-runs the same tool it began
    # with; fall back to the CLI default for pre-tool run.json files (or a naive run, whose
    # None tool is unused because the anchor path skips the skill).
    tool = manifest.get("tool") or args.tool
    # Same precedence for the child-model pin: the manifest value wins so a resume re-runs
    # the same model it began with; fall back to the per-tool default for pre-child_model
    # run.json files (or a naive run, whose None value is unused by the anchor path).
    child_model = (
        (manifest.get("requested_models") or {}).get("orchestrator")
        or manifest.get("child_model")
        or _resolve_child_model(args.tool, args.child_model)
    )
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
    if retry:
        for url in todo:
            detail = cp.detail(url)
            if detail.get("attempt_ref"):
                original = validity.load_attempt(run_dir / detail["attempt_ref"])
                validity.check_retry(
                    original,
                    now=validity.utc_now(),
                    reset_confirmed_at=args.reset_confirmed_at,
                )
            elif child_auth == "subscription":
                raise ValueError(
                    "reset_not_confirmed: legacy subscription failure has no immutable attempt evidence"
                )
        if args.reset_confirmed_at:
            validity.atomic_json(
                run_dir / "reset-time.json", {"confirmed_at": args.reset_confirmed_at}
            )
    # Archive every todo PR's prior wf records and deliverables before re-invoke so
    # the checker gates the current attempt only, for every deliverable, not just wf
    # records (#85, #165). Meaning is "superseded", not "retry succeeded" — a second
    # failure still leaves only the latest record. Passed PRs are not in todo and keep
    # their records untouched.
    for url in todo:
        meta = shas.get(url) or {}
        pr = {"url": url, **meta}
        pr_dir = run_dir / invoke.pr_dir_name(pr)
        invoke.supersede_workflow_records(pr_dir)
        invoke.supersede_attempt_artifacts(pr_dir)
    summary = _run_prs(
        run_dir,
        todo,
        cp,
        shas,
        fixture_urls,
        timeout_s,
        anchor,
        bench_data,
        tool=tool,
        child_model=child_model,
        child_auth=child_auth,
        epoch=epoch,
    )
    final = _print_summary(run_id, run_dir, urls, cp, summary)
    return _exit_code(final, urls)


def _score_only(run_id, *, epoch_id=None, attribution=False):
    run_dir = RUNS_ROOT / run_id
    try:
        from bench.runner import score
    except ImportError:
        print(
            "--score-only needs bench/runner/score.py, which lands in Task 13 and is not "
            f"present yet -- cannot re-score {run_id}.",
            file=sys.stderr,
        )
        return 2
    if not run_dir.exists():
        print(f"No run dir at {run_dir} to score.", file=sys.stderr)
        return 2
    try:
        options = {
            "epoch_id": epoch_id,
            "attempt_selection": validity.scoring_selection(run_dir),
        }
        if attribution:
            options["attribution"] = True
        result = score.score_run(str(run_dir), **options)
    except (ValueError, RuntimeError) as exc:
        print(f"--score-only failed for {run_id}: {exc}", file=sys.stderr)
        return 2
    print("Scored {}: {}".format(run_id, result["score_ref"]))
    return 0


def _check_only(run_id):
    """Run the mechanical functional-smoke checker against ``RUNS_ROOT/run_id``."""
    run_dir = RUNS_ROOT / run_id
    if not run_dir.is_dir():
        print(f"--check: run directory not found: {run_dir}", file=sys.stderr)
        return 2
    from bench.runner import check as check_mod

    result = check_mod.check_run(run_dir, repo_root=REPO_ROOT)
    stats = result.get("stats") or {}
    print(
        "Check {}: ok={} pr_dirs={} comments={} findings_files={} "
        "deliverable_artifacts={} workflow_records={} script_paths={} unknown_origin={}".format(
            run_id,
            result.get("ok"),
            stats.get("pr_dirs"),
            stats.get("delivered_comments"),
            stats.get("findings_files"),
            stats.get("deliverable_artifacts"),
            stats.get("workflow_records"),
            stats.get("script_paths"),
            stats.get("unknown_origin"),
        )
    )
    citation = stats.get("citation_boilerplate") or {}
    findings = citation.get("findings") or {}
    convention = findings.get("by_dimension", {}).get(
        "convention", {"populated": 0, "absence_preamble": 0}
    )
    populated = findings.get("populated", 0)
    preamble = findings.get("absence_preamble", 0)
    rate = "n/a" if not populated else f"{preamble / populated:.3f}"
    print(
        "  citation_boilerplate: preamble={}/{} (rate {}), convention={}/{}".format(
            preamble,
            populated,
            rate,
            convention["absence_preamble"],
            convention["populated"],
        )
    )
    for failure in result.get("failures") or []:
        print(f"  FAIL: {failure}", file=sys.stderr)
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
    parser.add_argument(
        "--runs", type=int, default=1, help="number of sequential runs (own dir each)"
    )
    parser.add_argument("--fidelity", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument(
        "--resume", metavar="RUN_ID", help="re-run only pending PRs of RUN_ID"
    )
    parser.add_argument(
        "--retry-failed",
        metavar="RUN_ID",
        dest="retry_failed",
        help="re-run the timeout+failed PRs of RUN_ID",
    )
    parser.add_argument(
        "--attribution",
        action="store_true",
        help="score eliminated discovery claims for stage attribution",
    )
    parser.add_argument("--timeout-mins", type=int, default=45, dest="timeout_mins")
    parser.add_argument(
        "--anchor", choices=["naive"], help="bare single-pass anchor review"
    )
    parser.add_argument(
        "--tool",
        choices=["deep-review-v2", "deep-review-v3"],
        default="deep-review-v3",
        help="which deep-review pipeline to invoke/label (default: deep-review-v3)",
    )
    # Default is `inherit` for every tool (see _resolve_child_model); the flag remains
    # for experiments. None is the "not specified" sentinel so an explicit flag can be
    # told apart from the default and the manifest value can win on resume.
    parser.add_argument(
        "--child-model",
        default=None,
        dest="child_model",
        help="model for the child orchestrator session "
        "(default: inherit for every tool)",
    )
    # Which credential the review children spend. None is the "not specified" sentinel
    # (as for --child-model) so the manifest's mode wins on resume. subscription has its
    # own prerequisites -- an OAuth token and no apiKeyHelper, see check_prereqs -- and
    # its runs are not cost-comparable with API-keyed ones; bench/README.md has the detail.
    parser.add_argument(
        "--child-auth",
        choices=list(ledger.AUTH_MODES),
        default=None,
        dest="child_auth",
        help="credential for the review children: the metered bench/.env key (default) "
        "or the operator's Claude subscription via CLAUDE_CODE_OAUTH_TOKEN, "
        "or host authentication inside a cloud session",
    )
    parser.add_argument("--score-only", metavar="RUN_ID", dest="score_only")
    parser.add_argument("--golden-epoch", metavar="EPOCH_ID")
    parser.add_argument("--unseal-holdout", help="one-time final-SHA unseal receipt")
    parser.add_argument(
        "--check",
        metavar="RUN_ID",
        dest="check",
        help="run the mechanical functional-smoke checker against an existing run "
        "(never invokes the judge; exit 0 = pass)",
    )
    parser.add_argument(
        "--prs",
        metavar="URL[,URL...]|mini",
        help="explicit comma-separated golden PR list; overrides --tier's subset "
        "resolution and labels the run 'custom'. Every URL must exist in shas.json. "
        "The alias 'mini' expands to the pre-registered 6-PR mini subset.",
    )
    parser.add_argument("--rule", metavar="PATH")
    parser.add_argument("--contamination", metavar="PATH")
    parser.add_argument("--measurement-window", metavar="PATH")
    parser.add_argument("--run-index", type=int)
    parser.add_argument("--arm")
    parser.add_argument("--reset-confirmed-at")
    parser.add_argument("--baseline-equivalence-receipt")
    args = parser.parse_args(argv)
    # Validate --prs against the golden set at parse time so an unknown URL is a hard
    # argparse error (exit 2), never a mid-run surprise; normalize the string to a list.
    # The documented alias ``mini`` expands to subsets.json["mini"] (still labels custom).
    if args.prs is not None:
        urls = [u.strip() for u in args.prs.split(",") if u.strip()]
        if not urls:
            parser.error("--prs was given but parsed no URLs")
        if urls == ["mini"]:
            subsets = _run_epoch(args).subsets
            urls = list(subsets["mini"])
        if urls in (["dev-slice"], ["dev-pool"]):
            try:
                urls = decision_hook("population", args=args, epoch=_run_epoch(args))
            except (ValueError, OSError) as exc:
                parser.error(str(exc))
        shas = _run_epoch(args).shas
        unknown = [u for u in urls if u not in shas]
        if unknown:
            parser.error(
                "--prs contains URL(s) not in shas.json: {}".format(", ".join(unknown))
            )
        args.prs = urls
    return args


_decision_hook = None


def decision_hook(event, **context):
    global _decision_hook
    if _decision_hook is None:
        import importlib

        importlib.import_module("bench.runner.populations").register_run_hook(globals())
    return _decision_hook(event, **context)


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
        options = {}
        if args.golden_epoch is not None:
            options["epoch_id"] = args.golden_epoch
        if args.attribution:
            options["attribution"] = True
        return _score_only(args.score_only, **options)
    if args.check:
        # --check inspects an existing run; combining it with new-run flags is always a
        # mistake (the flags would be silently ignored). Fail loud.
        #
        # The three per-run knobs -- --tool, --child-model, --child-auth -- are absent from
        # this list on purpose. They describe how a run was produced, so --check and
        # --score-only read them from the run's own manifest and never from the flags;
        # passing one is redundant rather than contradictory, and neither mode invokes
        # claude, so nothing is spent on the wrong credential. Adding them here would also
        # break `--check` for anyone who keeps the flags in a shell alias.
        if args.tier or args.prs or args.anchor or args.runs != 1 or args.attribution:
            print(
                "--check does not accept --tier / --prs / --anchor / --runs "
                "(pass only --check RUN_ID).",
                file=sys.stderr,
            )
            return 2
        return _check_only(args.check)

    if args.attribution:
        print("--attribution requires --score-only", file=sys.stderr)
        return 2

    child_auth = _child_auth_for(args)
    # Only reachable on a resume, where the run's own recorded mode wins: for a new run
    # the resolved mode IS the flag. Refusing beats silently ignoring the flag -- the
    # operator asked to charge a different credential, and one run dir can only honour
    # one (its per-PR envelope costs are summed into a single auth_mode-labelled row).
    if args.child_auth is not None and args.child_auth != child_auth:
        print(
            f"--child-auth {args.child_auth} contradicts the mode run {args.resume or args.retry_failed} recorded ({child_auth}) -- a run dir is "
            "one credential. Resume without the flag, or start a new run.",
            file=sys.stderr,
        )
        return 2

    try:
        prior_manifest = (
            _read_run_manifest(args.resume or args.retry_failed)
            if (args.resume or args.retry_failed)
            else None
        )
        for key in ("unseal_holdout", "contamination", "rule"):
            value = getattr(args, key) or (prior_manifest or {}).get(key)
            setattr(args, key, str(Path(value).resolve()) if value else None)
        urls = (prior_manifest or {}).get("pr_urls") or (
            _target_urls(args.tier, args.prs, epoch=_run_epoch(args))
            if args.tier or args.prs
            else ()
        )
        decision_hook("preflight", args=args, urls=urls, epoch=_run_epoch(args))
        if args.measurement_window:
            validity.refuse_ambient_model_overrides(os.environ, child_auth)
            validity.require_clean_treatment()
            args.measurement_window = str(Path(args.measurement_window).resolve())
        recorded_window = (prior_manifest or {}).get("measurement_window")
        if recorded_window and args.measurement_window != recorded_window:
            raise ValueError(
                "resume/retry requires the original --measurement-window path"
            )
        if args.measurement_window:
            window = validity.load_window(Path(args.measurement_window))
            args._window = window
            prior = prior_manifest or {}
            arm = prior.get("arm") or args.arm
            run_index = prior.get("run_index") if prior_manifest else args.run_index
            run_count = 1 if prior_manifest else max(1, args.runs)
            if (
                arm not in window.arms
                or run_index is None
                or any(
                    index not in window.planned_run_indices
                    for index in range(run_index, run_index + run_count)
                )
            ):
                raise ValueError(
                    "measurement requires a planned --run-index and explicit --arm"
                )
            model = prior.get("child_model") or _resolve_child_model(
                args.tool, args.child_model
            )
            timeout_s = prior.get("env_fingerprint", {}).get(
                "timeout_s", args.timeout_mins * 60
            )
            args._provenance = validity.run_provenance(
                child_model=model,
                child_auth=child_auth,
                timeout_s=timeout_s,
                epoch_id=args.golden_epoch,
                window=window,
                arm=arm,
                epoch=_run_epoch(args),
            )
            urls = prior.get("pr_urls") or _target_urls(
                args.tier, args.prs, epoch=_run_epoch(args)
            )
            if not validity.same_pr_set(urls, window.pr_urls):
                raise ValueError("window requested PR matrix mismatch")
            if prior_manifest and (
                not validity.same_provenance(
                    prior_manifest.get("provenance"), args._provenance
                )
                or prior_manifest.get("window_sha256")
                != validity.file_hash(Path(args.measurement_window))
            ):
                raise ValueError("immutable run/window provenance mismatch")
            decision_hook(
                "window", args=args, window=window, prior=prior, epoch=_run_epoch(args)
            )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    runtime_env = dict(os.environ)
    failures = check_prereqs(
        child_auth=child_auth, env=runtime_env, gh_pr=_first_target_pr(args)
    )
    if failures:
        print(
            "bench/run.py: prerequisite checks failed (fix each and re-run):",
            file=sys.stderr,
        )
        for failure in failures:
            print("  - " + failure, file=sys.stderr)
        return 2

    with _gh_token_scope(runtime_env):
        note = _judge_key_note(ENV_PATH, child_auth)
        if note:
            print(note, file=sys.stderr)

        if args.resume or args.retry_failed:
            try:
                return _resume(
                    args.resume or args.retry_failed,
                    args,
                    retry=bool(args.retry_failed),
                )
            except (ValueError, OSError) as exc:
                print(str(exc), file=sys.stderr)
                return 2

        if not args.tier and not args.prs:
            print(
                "--tier or --prs is required for a new run "
                "(smoke|mini|subset|holdout|full, or an explicit --prs list / --prs mini).",
                file=sys.stderr,
            )
            return 2

        rc = 0
        for _ in range(max(1, args.runs)):
            try:
                rc = _new_run(args) or rc
            except (ValueError, OSError) as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if args.run_index is not None:
                args.run_index += 1
        return rc


if __name__ == "__main__":
    sys.exit(main())
