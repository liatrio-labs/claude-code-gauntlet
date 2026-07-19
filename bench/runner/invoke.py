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

__all__ = [
    "InvokeResult",
    "build_env",
    "invoke_review",
    "parse_result_envelope",
    "pr_dir_name",
]

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

# v3 drives its pipeline through the Workflow tool, first exposed by the Claude Code CLI
# in 2.1.154. A ``--tool deep-review-v3`` run pre-flights the child CLI's version and is
# marked ``invalid`` (never scored) on an older/unreadable CLI, rather than silently
# running a degraded review or hanging. v2 needs no such gate. See ``_v3_preflight``.
V3_MIN_CLAUDE_VERSION = (2, 1, 154)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


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


# Anything outside this set is collapsed to a hyphen so the dir name is safe on any
# filesystem; GitHub owner/repo already stay within it (alnum, dot, hyphen, underscore).
_PR_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(part):
    return _PR_KEY_UNSAFE.sub("-", str(part or "")).strip("-")


def pr_dir_name(pr):
    """Per-PR artifact dir name: ``pr-{owner}-{repo}-{n}`` (filesystem-safe).

    Golden URLs reuse pull numbers across repos (e.g. ``/pull/1`` exists in three
    forks), so a bare ``pr-{n}`` collides -- ``--tier full`` would overwrite one
    PR's artifacts with another's and mis-join candidates at scoring time. Keying on
    owner/repo/number keeps every PR's dir distinct. Owner/repo come from the ``pr``
    dict when present, else are parsed from its ``url`` -- the identical resolution
    ``build_env`` uses -- so run.py, invoke.py, and score.py all derive the same key.
    """
    return "pr-{}-{}-{}".format(_slug(_owner(pr)), _slug(_repo(pr)), _pr_number(pr))


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
    return Path(run_dir) / pr_dir_name(pr) / "worktree"


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


def _gh_auth_env(base_env):
    """Return the ``gh`` auth env vars to carry across the ``HOME`` override.

    ``build_env`` overrides ``HOME``/``CLAUDE_CONFIG_DIR`` to isolate the *claude*
    binary's settings/plugins (the S7 isolation boundary). But the skill's child
    processes also shell out to ``gh`` (``gh pr view/diff``), and on machines where
    ``gh`` keeps its token in a config file (the common Linux case) that token lives
    under the REAL home -- ``$GH_CONFIG_DIR`` if set, else ``$XDG_CONFIG_HOME/gh``,
    else ``$HOME/.config/gh``. The moved ``HOME`` would strand it, so re-point ``gh``
    back at the real dir. This does NOT weaken the claude-config isolation: ``gh``
    auth is an ambient prerequisite (references/headless-mode.md, validated by
    ``check_prereqs``'s ``gh auth status``), not a claude setting.

    - ``GH_CONFIG_DIR``: an explicit value in ``base_env`` wins untouched; otherwise
      derive it from the ORIGINAL base ``HOME`` (XDG-aware) and set it only when that
      dir actually exists -- keychain / env-token setups keep no such dir and need no
      pointer, so setting nothing there is harmless.
    - ``GH_TOKEN`` / ``GITHUB_TOKEN``: passed through when present; in CI they may be
      the only auth (no config dir, no keychain).
    """
    result = {}
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = base_env.get(name)
        if value:
            result[name] = value

    explicit = base_env.get("GH_CONFIG_DIR")
    if explicit:
        result["GH_CONFIG_DIR"] = explicit
        return result

    xdg = base_env.get("XDG_CONFIG_HOME")
    home = base_env.get("HOME")
    if xdg:
        gh_dir = Path(xdg) / "gh"
    elif home:
        gh_dir = Path(home) / ".config" / "gh"
    else:
        return result
    if gh_dir.is_dir():
        result["GH_CONFIG_DIR"] = str(gh_dir)
    return result


def build_env(pr, run_dir, base_env):
    """Assemble the pinned isolated env for one PR invocation (side effect: seeds trust).

    Loads ``ANTHROPIC_API_KEY`` from ``bench/.env`` (``ENV_PATH``) into the child env.
    The isolated ``HOME``/``CLAUDE_CONFIG_DIR`` carries no credentials, so without this
    every ``claude`` invocation would be unauthenticated. The ``.env`` value is
    authoritative: it WINS over any ambient ``ANTHROPIC_API_KEY`` so all bench spend
    lands on the single metered key. A missing/empty ``.env`` or key leaves the ambient
    env untouched (the prereq check reports that separately).

    The ``HOME``/``CLAUDE_CONFIG_DIR`` override isolates the *claude* binary's settings
    and plugins (the S7 boundary); it must not strand ``gh``, which the skill's children
    call. ``_gh_auth_env`` re-establishes ``gh``'s auth (``GH_CONFIG_DIR`` pointing at the
    real home, plus any ``GH_TOKEN``/``GITHUB_TOKEN``) across that override -- ``gh`` auth
    is an ambient prerequisite, not part of the claude-config isolation.
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
    # Re-point gh at its real auth AFTER the HOME override (derived from the original
    # base HOME), so child `gh pr view/diff` stays authenticated despite the isolation.
    env.update(_gh_auth_env(base_env))

    _seed_trust(config_dir, _worktree_dir(run_dir, pr))
    return env


# ------------------------------------------------------------------------- invoke


def _kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def parse_result_envelope(text):
    """Return the ``type=="result"`` envelope dict from raw stdout, or None.

    The canonical tolerant parser shared by the whole runner (``run.py`` and
    ``score.py`` delegate here rather than re-implementing it). Real ``-p`` stdout is
    pure JSON; a fake (or any future preamble) may print the echo block or other
    non-JSON lines first, and there may be trailing output after the envelope -- so if
    the whole text is not a single JSON object, scan for the first parseable object
    that is the ``type=="result"`` envelope. Returns None on empty/unparseable input.
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


def _claude_version(claude_bin):
    """Return the child ``claude`` CLI version as ``(major, minor, patch)``, or None.

    Runs ``claude --version`` and parses the first ``N.N.N`` it prints (real output is
    ``2.1.154 (Claude Code)``). Any launch/parse failure yields None -- the caller treats
    an unreadable version as "cannot confirm v3 support" and fails the run rather than
    guessing which pipeline the CLI can honor.
    """
    try:
        out = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search((out.stdout or "") + (out.stderr or ""))
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def _fmt_version(version):
    return ".".join(str(part) for part in version) if version else "unknown"


def _v3_preflight(claude_bin):
    """Return a failure reason when the child CLI cannot drive v3, else None.

    v3 dispatches its pipeline through the Workflow tool, first exposed in Claude Code
    ``2.1.154`` (``V3_MIN_CLAUDE_VERSION``). An older -- or unreadable -- CLI cannot honor
    the v3 SKILL.md, so the caller marks the PR ``invalid`` (never scored) with this reason
    instead of running a degraded review or deadlocking.
    """
    required = _fmt_version(V3_MIN_CLAUDE_VERSION)
    version = _claude_version(claude_bin)
    if version is None:
        return "v3_workflow_unsupported: claude --version unreadable, need >= {}".format(required)
    if version < V3_MIN_CLAUDE_VERSION:
        return "v3_workflow_unsupported: claude {} < required {}".format(
            _fmt_version(version), required
        )
    return None


def invoke_review(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3"):
    """Run the headless review for one PR and classify the outcome.

    ``tool`` selects the pipeline label and gates the v3 preflight: a ``deep-review-v3``
    run first asserts the child CLI is new enough to expose the Workflow tool (see
    ``_v3_preflight``) and returns ``invalid`` if not; ``deep-review-v2`` skips that check.
    The invocation itself is unchanged either way -- the ``--plugin-dir`` repo is whichever
    pipeline version is checked out; ``tool`` records/gates, it does not fork the command.

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

    pr_dir = run_dir / pr_dir_name(pr)
    pr_dir.mkdir(parents=True, exist_ok=True)
    raw_path = pr_dir / "raw.json"

    claude_bin = shutil.which("claude", path=env.get("PATH") or os.environ.get("PATH"))
    if not claude_bin:
        return InvokeResult("failed", raw_json_path=str(raw_path), reason="claude_not_found")

    # v3 preflight: the pipeline runs through the Workflow tool (Claude Code >= 2.1.154).
    # An older/unreadable CLI cannot honor the v3 skill, so fail the PR invalid (never
    # scored) with a clear reason rather than silently degrading. v2 needs no such gate.
    if tool == "deep-review-v3":
        reason = _v3_preflight(claude_bin)
        if reason:
            return InvokeResult("invalid", raw_json_path=str(raw_path), reason=reason)

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
    envelope = parse_result_envelope(raw_text)
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
