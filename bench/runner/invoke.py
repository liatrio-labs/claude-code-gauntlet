"""Headless invoker for a single golden PR (spec H3 "Per-PR flow", H8 hang guard).

``build_env`` assembles the pinned, isolated invocation context: the 9 bench
``DEEP_REVIEW_*`` knobs, the per-run output dir, ``GH_REPO``, and an isolated
``HOME``/``CLAUDE_CONFIG_DIR`` so operator config can never leak in. It also pre-seeds
the isolated ``.claude.json`` with the worktree marked trusted -- a headless run cannot
answer a first-run trust dialog, so it must be accepted ahead of time.

``invoke_review`` runs ``claude -p "/deep-review <n>"`` under that context in its own
process session, with a watchdog that kills the whole process group on timeout (no
orphans), scans the output for AskUserQuestion (defense-in-depth -> ``invalid``),
verifies the ``Headless config:`` echo receipt (accepted from raw stdout, the result
envelope's ``.result``, or a collected report ``*.md`` -- see ``_echo_ok``), parses costs,
and locates the dry-run payload. See CLAUDE.md: stdlib-only.
"""

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from bench.runner.costs import parse_costs

__all__ = ["InvokeResult", "build_env", "invoke_review"]

# Repo root == the deep-review plugin dir. bench/runner/invoke.py -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

# The metered-key .env (repo-root-relative). build_env loads ANTHROPIC_API_KEY from
# here into every child env. A module global so tests can repoint it at a tempfile.
ENV_PATH = REPO_ROOT / "bench" / ".env"

# The 9 bench values (spec H2 table, bench overrides): pinned explicitly on every run
# so the harness is drift-immune even though the skill defines its own defaults.
BENCH_ENV = {
    "DEEP_REVIEW_HEADLESS": "1",
    "DEEP_REVIEW_MODEL_TIER": "optimized",
    "DEEP_REVIEW_DELIVERY": "pr_comments,markdown",
    "DEEP_REVIEW_POST_MODE": "dry-run",
    "DEEP_REVIEW_PR_COMMENT_CAP": "25",
    "DEEP_REVIEW_DRAFT_POLICY": "review",
    "DEEP_REVIEW_REVIEWED_POLICY": "full",
    "DEEP_REVIEW_PR_NOT_FOUND_POLICY": "error",
    "DEEP_REVIEW_TRIVIAL_SCOPE": "full",
}

# The resolved-config receipt the runner asserts against (Task 3 echo format). Keys are
# the 8 knob lines under the "Headless config:" header; DEEP_REVIEW_HEADLESS itself is
# the master switch and is not echoed as a knob. Values are the bench expectations.
EXPECTED_ECHO = {
    "model_tier": "optimized",
    "delivery": "pr_comments,markdown",
    "post_mode": "dry-run",
    "pr_comment_cap": "25",
    "draft_policy": "review",
    "reviewed_policy": "full",
    "pr_not_found_policy": "error",
    "trivial_scope": "full",
}

_ASKUSERQUESTION_RE = re.compile(r'"(?:name|tool_name)"\s*:\s*"AskUserQuestion"')


@dataclass
class InvokeResult:
    status: str  # ok | timeout | invalid | failed
    cost_usd: float = 0.0
    per_model: dict = field(default_factory=dict)
    echo_ok: bool = False
    payload_path: str = None
    raw_json_path: str = None
    reason: str = None


# --------------------------------------------------------------------------- env


def _pr_number(pr):
    for key in ("pr_number", "number", "n"):
        value = pr.get(key)
        if value is not None:
            return value
    owner, repo, number = _parse_url(pr.get("url", ""))
    if number is not None:
        return number
    raise KeyError("pr dict has no pr_number/number and no parseable url")


def _parse_url(url):
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url or "")
    if not m:
        return (None, None, None)
    return (m.group(1), m.group(2), int(m.group(3)))


def _owner(pr):
    return pr.get("owner") or _parse_url(pr.get("url", ""))[0]


def _repo(pr):
    return pr.get("repo") or _parse_url(pr.get("url", ""))[1]


def _load_dotenv_key(path, key):
    """Return ``key``'s value from a simple ``KEY=VALUE`` .env file, or None.

    Blank lines and ``#`` comments are ignored and surrounding quotes are stripped.
    A missing/unreadable file, an absent key, or an empty value all yield None. The
    value is never printed or logged (it is a live API credential).
    """
    value = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, raw = line.partition("=")
                if name.strip() == key:
                    value = raw.strip().strip("'\"") or None
    except OSError:
        return None
    return value


def _claude_home(run_dir, base_env):
    override = base_env.get("BENCH_CLAUDE_HOME")
    if override:
        return Path(override)
    # Shared isolated home for the workspace: run_dir is <workspace>/runs/<run_id>.
    return Path(run_dir).resolve().parent.parent / "claude-home"


def _worktree_dir(run_dir, pr):
    return Path(run_dir) / "pr-{}".format(_pr_number(pr)) / "worktree"


def _seed_trust(config_dir, worktree):
    """Merge ``worktree`` into ``.claude.json`` projects as trust-accepted.

    Preserves any existing file content (other projects, other top-level keys); a
    corrupt/absent file is treated as empty rather than raising.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".claude.json"

    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}

    key = str(Path(worktree).resolve())
    entry = projects.get(key)
    if not isinstance(entry, dict):
        entry = {}
    entry["hasTrustDialogAccepted"] = True
    projects[key] = entry
    data["projects"] = projects

    path.write_text(json.dumps(data, indent=2))


def build_env(pr, run_dir, base_env):
    """Assemble the pinned isolated env for one PR invocation (side effect: seeds trust).

    Loads ``ANTHROPIC_API_KEY`` from ``bench/.env`` (``ENV_PATH``) into the child env.
    The isolated ``HOME``/``CLAUDE_CONFIG_DIR`` carries no credentials, so without this
    every ``claude`` invocation would be unauthenticated. The ``.env`` value is
    authoritative: it WINS over any ambient ``ANTHROPIC_API_KEY`` so all bench spend
    lands on the single metered key. A missing/empty ``.env`` or key leaves the ambient
    env untouched (the prereq check reports that separately).
    """
    run_dir = Path(run_dir)
    env = dict(base_env)
    env.update(BENCH_ENV)
    api_key = _load_dotenv_key(ENV_PATH, "ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    env["DEEP_REVIEW_OUTPUT_DIR"] = str(run_dir / "output")
    env["GH_REPO"] = "{}/{}".format(_owner(pr), _repo(pr))

    claude_home = _claude_home(run_dir, base_env)
    config_dir = claude_home / "config"
    env["HOME"] = str(claude_home)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)

    _seed_trust(config_dir, _worktree_dir(run_dir, pr))
    return env


# ------------------------------------------------------------------------- invoke


def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _extract_envelope(text):
    """Return the ``type=="result"`` envelope dict from raw stdout, or None.

    Real ``-p`` stdout is pure JSON; the fake (and any future prefix) may print the
    echo block first, so scan for the first parseable object that is the result envelope.
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


def _has_askuserquestion(raw_text, envelope):
    if envelope:
        for denial in envelope.get("permission_denials") or []:
            if isinstance(denial, dict):
                if any(v == "AskUserQuestion" for v in denial.values()):
                    return True
                if "AskUserQuestion" in json.dumps(denial):
                    return True
            elif denial == "AskUserQuestion":
                return True
    return bool(_ASKUSERQUESTION_RE.search(raw_text or ""))


def _echo_in_text(text):
    """True when *text* carries the full receipt: every expected knob line present."""
    text = text or ""
    for key, value in EXPECTED_ECHO.items():
        pattern = r"(?m)^[ \t]*{}={}(?:[ \t]|\(|$)".format(re.escape(key), re.escape(value))
        if not re.search(pattern, text):
            return False
    return True


def _echo_in_reports(report_dirs):
    """True when any ``*.md`` under any of *report_dirs* carries the full receipt."""
    seen = set()
    for base in report_dirs:
        if not base:
            continue
        base = Path(base)
        if not base.exists():
            continue
        for md in sorted(base.rglob("*.md")):
            resolved = md.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                if _echo_in_text(md.read_text(errors="replace")):
                    return True
            except OSError:
                continue
    return False


def _echo_ok(raw_text, envelope=None, report_dirs=()):
    """True when the ``Headless config:`` receipt is present in ANY available source.

    A ``-p --output-format json`` run hides intermediate-turn stdout: only the final
    message survives in the envelope's ``.result``. So the receipt is accepted from
    (a) the raw stdout text, (b) the envelope ``.result`` text, or (c) any collected
    report ``*.md`` under *report_dirs* (the run output dir and the PR dir). Missing
    from all three -> not ok (the run is classified ``invalid``/``config_echo_mismatch``).
    """
    texts = [raw_text or ""]
    if isinstance(envelope, dict):
        result = envelope.get("result")
        if isinstance(result, str):
            texts.append(result)
    if any(_echo_in_text(t) for t in texts):
        return True
    return _echo_in_reports(report_dirs)


def _find_payload(output_dir):
    base = Path(output_dir)
    if not base.exists():
        return None
    direct = base / "post-review-payload.json"
    if direct.is_file():
        return direct
    matches = sorted(base.rglob("post-review-payload.json"))
    return matches[0] if matches else None


def _fail_reason(returncode, envelope):
    if envelope is None:
        return "no_envelope"
    if envelope.get("is_error"):
        # subtype alone can read "success" on a mid-response API error; keep both.
        return "is_error({})".format(envelope.get("subtype") or "unknown")
    if returncode != 0:
        return "exit_{}".format(returncode)
    return "failed"


def invoke_review(worktree, pr, run_dir, timeout_s=1800):
    """Run the headless review for one PR and classify the outcome.

    Returns an :class:`InvokeResult`. The isolated ``HOME``/``CLAUDE_CONFIG_DIR`` has no
    allowlist and no user, so ``--dangerously-skip-permissions`` plus the pinned context
    is the containment boundary; any prompt would deadlock, which the watchdog + scan catch.
    """
    worktree = Path(worktree).resolve()
    run_dir = Path(run_dir)
    number = _pr_number(pr)

    env = build_env(pr, run_dir, os.environ)
    # Belt-and-suspenders: also trust the exact worktree we will cd into, in case run.py
    # placed it somewhere other than the build_env-derived convention path.
    _seed_trust(Path(env["CLAUDE_CONFIG_DIR"]), worktree)

    pr_dir = run_dir / "pr-{}".format(number)
    pr_dir.mkdir(parents=True, exist_ok=True)
    raw_path = pr_dir / "raw.json"

    claude_bin = shutil.which("claude", path=env.get("PATH") or os.environ.get("PATH"))
    if not claude_bin:
        return InvokeResult("failed", raw_json_path=str(raw_path), reason="claude_not_found")

    cmd = [
        claude_bin,
        "-p",
        "/deep-review {}".format(number),
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--plugin-dir",
        str(REPO_ROOT),
    ]

    with open(raw_path, "w") as fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(worktree),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return InvokeResult(
                "timeout", raw_json_path=str(raw_path), reason="watchdog_timeout"
            )

    raw_text = raw_path.read_text(errors="replace")
    envelope = _extract_envelope(raw_text)
    # The receipt may live in stdout, the envelope .result, or a report .md — collected
    # into pr_dir by run.py, or still in the shared output dir at echo-check time.
    report_dirs = (env.get("DEEP_REVIEW_OUTPUT_DIR"), str(pr_dir))

    # 1) AskUserQuestion (safety) wins even over a clean-looking envelope.
    if _has_askuserquestion(raw_text, envelope):
        return InvokeResult(
            "invalid",
            echo_ok=_echo_ok(raw_text, envelope, report_dirs),
            raw_json_path=str(raw_path),
            reason="askuserquestion_detected",
        )

    costs = parse_costs(envelope or {})
    cost_usd = costs["cost_usd"]
    per_model = costs["per_model"]

    # 2) Process/tool failure -> failed (trust nothing else on a failed run).
    if envelope is None or proc.returncode != 0 or envelope.get("is_error"):
        return InvokeResult(
            "failed",
            cost_usd=cost_usd,
            per_model=per_model,
            echo_ok=_echo_ok(raw_text, envelope, report_dirs),
            raw_json_path=str(raw_path),
            reason=_fail_reason(proc.returncode, envelope),
        )

    # 3) Config receipt (accepted from stdout, the .result envelope, or a report .md).
    echo_ok = _echo_ok(raw_text, envelope, report_dirs)
    if not echo_ok:
        return InvokeResult(
            "invalid",
            cost_usd=cost_usd,
            per_model=per_model,
            echo_ok=False,
            raw_json_path=str(raw_path),
            reason="config_echo_mismatch",
        )

    # 4) Dry-run payload (the scored candidate set).
    payload_path = _find_payload(env["DEEP_REVIEW_OUTPUT_DIR"])
    delivery = env.get("DEEP_REVIEW_DELIVERY", "")
    if payload_path is None and "pr_comments" in [d.strip() for d in delivery.split(",")]:
        return InvokeResult(
            "failed",
            cost_usd=cost_usd,
            per_model=per_model,
            echo_ok=echo_ok,
            raw_json_path=str(raw_path),
            reason="no_payload",
        )

    return InvokeResult(
        "ok",
        cost_usd=cost_usd,
        per_model=per_model,
        echo_ok=echo_ok,
        payload_path=str(payload_path) if payload_path else None,
        raw_json_path=str(raw_path),
    )
