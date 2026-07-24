"""Headless invoker for a single golden PR (spec H3 "Per-PR flow", H8 hang guard).

``build_env`` assembles the pinned, isolated invocation context: the 9 bench
``CODE_GAUNTLET_*`` knobs, the per-run output dir, ``GH_REPO``, and an isolated
``HOME``/``CLAUDE_CONFIG_DIR`` so operator config can never leak in. It also pre-seeds
the isolated ``.claude.json`` with the worktree marked trusted -- a headless run cannot
answer a first-run trust dialog, so it must be accepted ahead of time.

``invoke_review`` runs ``claude -p "/code-gauntlet:code-gauntlet <n>"`` under that context in its own
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
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bench.runner.costs import parse_costs

__all__ = [
    "InvokeResult",
    "build_env",
    "invoke_review",
    "parse_result_envelope",
    "pr_dir_name",
    "resolve_claude_home",
    "api_key_helper_sources",
    "snapshot_workflow_records",
    "collect_workflow_records",
    "_claude_home",
]

# Repo root == the code-gauntlet plugin dir. bench/runner/invoke.py -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

# The metered-key .env (repo-root-relative). build_env loads ANTHROPIC_API_KEY from
# here into every child env. A module global so tests can repoint it at a tempfile.
ENV_PATH = REPO_ROOT / "bench" / ".env"

# The 9 bench values (spec H2 table, bench overrides): pinned explicitly on every run
# so the harness is drift-immune even though the skill defines its own defaults.
BENCH_ENV = {
    "CODE_GAUNTLET_HEADLESS": "1",
    "CODE_GAUNTLET_MODEL_TIER": "optimized",
    "CODE_GAUNTLET_DELIVERY": "pr_comments,markdown",
    "CODE_GAUNTLET_POST_MODE": "dry-run",
    "CODE_GAUNTLET_PR_COMMENT_CAP": "25",
    "CODE_GAUNTLET_DRAFT_POLICY": "review",
    "CODE_GAUNTLET_REVIEWED_POLICY": "full",
    "CODE_GAUNTLET_PR_NOT_FOUND_POLICY": "error",
    "CODE_GAUNTLET_TRIVIAL_SCOPE": "full",
}

# The resolved-config receipt the runner asserts against (Task 3 echo format). Keys are
# the 8 knob lines under the "Headless config:" header; CODE_GAUNTLET_HEADLESS itself is
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

# The CLI prints this to stdout (ahead of the result envelope) when the main agent turn
# ends while a tool it launched is still running as a background task, and that task
# outlives the CLI's background-wait ceiling: e.g. "Background tasks still running after
# 600s; terminating." For a headless review this means the Phase 3 Workflow ran detached
# and was killed before Phase 8 delivered — so the config-echo receipt and payload are
# absent for that reason, NOT a config mismatch. Matched ceiling-agnostically (the number
# of seconds varies with CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS).
_BG_TASKS_KILLED_RE = re.compile(r"Background tasks still running after\b")

# v3 drives its pipeline through the Workflow tool, first exposed by the Claude Code CLI
# in 2.1.154. A ``--tool deep-review-v3`` run pre-flights the child CLI's version and is
# marked ``invalid`` (never scored) on an older/unreadable CLI, rather than silently
# running a degraded review or hanging. v2 needs no such gate. See ``_v3_preflight``.
V3_MIN_CLAUDE_VERSION = (2, 1, 154)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# The slash command that triggers the review skill. It MUST be namespace-qualified as
# ``<plugin>:<skill>`` (both are "code-gauntlet"): in the harness's pinned isolated context
# (isolated HOME/CLAUDE_CONFIG_DIR + --plugin-dir, no --bare), Claude Code registers a
# plugin skill's slash command only under its namespaced name. The bare/flat ``/code-gauntlet``
# alias is not reliably registered there and resolves to "Unknown command: /code-gauntlet"
# (num_turns 0, $0), which the runner then classes ``invalid``/``config_echo_mismatch`` --
# the intermittent registration is why some children in a run failed and others did not.
# Namespace-qualifying makes command resolution deterministic. See references/artifact 33
# (P2b): skills are namespace-qualified in this isolation mode, unlike the flat --bare mode.
SKILL_COMMAND = "code-gauntlet:code-gauntlet"

# How the review child authenticates: "api" bills the metered key in bench/.env,
# "subscription" runs on the operator's own Claude subscription capacity via the
# long-lived OAuth token from ``claude setup-token``. The judge/scoring path is
# always API-keyed and is unaffected. See ``_claude_auth_env``.
CHILD_AUTH_MODES = ("api", "subscription")

# Env credential sources the documented Claude Code precedence chain places ABOVE
# CLAUDE_CODE_OAUTH_TOKEN (cloud providers -> ANTHROPIC_AUTH_TOKEN -> ANTHROPIC_API_KEY
# -> apiKeyHelper -> CLAUDE_CODE_OAUTH_TOKEN -> /login). Any one of them surviving into
# the child env silently defeats subscription mode, so subscription strips them all.
_OUTRANKING_CREDENTIAL_VARS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


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


def resolve_claude_home(workspace_dir, base_env):
    """Return the isolated claude HOME for *workspace_dir* (``BENCH_CLAUDE_HOME`` wins).

    The single derivation rule, shared by ``build_env`` (which hands the dir to the
    child) and by ``run.py``'s preflight (which must inspect the very same dir -- an
    ``apiKeyHelper`` in a settings file the preflight never looked at would outrank the
    OAuth token at runtime). Callers that only have a run dir go through
    :func:`_claude_home`; nothing else may re-derive the path.
    """
    override = base_env.get("BENCH_CLAUDE_HOME")
    if override:
        return Path(override)
    return Path(workspace_dir) / "claude-home"


def _claude_home(run_dir, base_env):
    # Shared isolated home for the workspace: run_dir is <workspace>/runs/<run_id>.
    return resolve_claude_home(Path(run_dir).resolve().parent.parent, base_env)


# The settings filenames Claude Code layers, and the dirs under the isolated home they
# can appear in: ``config/`` is the CLAUDE_CONFIG_DIR build_env sets, and ``.claude/`` is
# where a HOME-relative user-settings read would land. Both are scanned because the
# inspection is fail-closed -- its only outcome is a refusal -- and BENCH_CLAUDE_HOME can
# point at a home the harness did not create.
_SETTINGS_FILES = ("settings.json", "settings.local.json")
_SETTINGS_DIRS = ("config", ".claude")


def api_key_helper_sources(claude_home):
    """Return the settings files under *claude_home* that define an ``apiKeyHelper``.

    An ``apiKeyHelper`` outranks ``CLAUDE_CODE_OAUTH_TOKEN`` in the documented
    precedence chain, and unlike an env var it cannot be stripped from the child env --
    so subscription mode has to refuse the run instead, naming the offending file. This
    is a preflight-only inspection: it runs on every prereq check, including on machines
    with no claude home at all, so a missing dir, an unreadable file, corrupt JSON, or a
    non-object document all read as "no helper" rather than raising. Paths are returned
    sorted as ``str`` so the failure message is deterministic.

    Scoped to the isolated home. Settings scopes outside it -- a golden worktree's own
    ``.claude/settings.json``, enterprise managed settings -- are documented limitations
    of the mode rather than checks here (bench/README.md, "Child auth modes").
    """
    home = Path(claude_home)
    found = []
    for subdir in _SETTINGS_DIRS:
        for name in _SETTINGS_FILES:
            path = home / subdir / name
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            helper = data.get("apiKeyHelper")
            if isinstance(helper, str) and not helper.strip():
                continue
            if not helper:
                continue
            found.append(str(path))
    return sorted(found)


def _worktree_dir(run_dir, pr):
    return Path(run_dir) / pr_dir_name(pr) / "worktree"


def snapshot_workflow_records(claude_home):
    """Return ``{resolved_path: (mtime_ns, size)}`` for every ``wf_*.json`` under config.

    Per-child Workflow tool records live under
    ``{CLAUDE_CONFIG_DIR}/projects/<slug>/<session>/workflows/wf_*.json`` and carry the
    ``scriptPath`` the plugin-identity smoke gate needs. The result envelope in
    ``raw.json`` (``claude -p --output-format json``) does **not** include tool inputs,
    so these files are the only durable source. Snapshot before an invoke so
    ``collect_workflow_records`` can copy only records this PR created or updated.
    """
    root = Path(claude_home) / "config"
    out = {}
    if not root.is_dir():
        return out
    for path in root.rglob("wf_*.json"):
        try:
            st = path.stat()
        except OSError:
            continue
        out[str(path.resolve())] = (st.st_mtime_ns, st.st_size)
    return out


def collect_workflow_records(claude_home, pr_dir, baseline=None):
    """Copy new/changed ``wf_*.json`` records into ``{pr_dir}/workflows/``.

    ``baseline`` is the dict from :func:`snapshot_workflow_records` taken before the
    child ran. Returns the list of basenames copied. Name collisions (same ``wf_`` id
    from a different project slug) get a numeric suffix so nothing is overwritten.
    """
    baseline = baseline or {}
    root = Path(claude_home) / "config"
    dest_dir = Path(pr_dir) / "workflows"
    copied = []
    if not root.is_dir():
        return copied
    for path in sorted(root.rglob("wf_*.json")):
        try:
            resolved = str(path.resolve())
            st = path.stat()
        except OSError:
            continue
        prev = baseline.get(resolved)
        if prev is not None and prev == (st.st_mtime_ns, st.st_size):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / path.name
        if target.exists():
            stem, suffix = path.stem, path.suffix
            n = 2
            while target.exists():
                target = dest_dir / "{}-{}{}".format(stem, n, suffix)
                n += 1
        try:
            shutil.copy2(path, target)
        except OSError:
            continue
        copied.append(target.name)
    return copied


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


def _claude_auth_env(base_env, child_auth):
    """Return ``(updates, removals)``: how one ``child_auth`` mode credentials the child.

    The auth analogue of ``_gh_auth_env`` -- the one place a credential decision is
    made, so the mode cannot be re-derived (or silently defaulted) anywhere downstream.
    Claude Code resolves credentials in a fixed order: cloud providers
    (``CLAUDE_CODE_USE_BEDROCK``/``CLAUDE_CODE_USE_VERTEX``) -> ``ANTHROPIC_AUTH_TOKEN``
    -> ``ANTHROPIC_API_KEY`` -> ``apiKeyHelper`` -> ``CLAUDE_CODE_OAUTH_TOKEN`` ->
    ``/login``. The child inherits the operator's full env, so the mode is only real if
    the losing sources are removed rather than merely left unset.

    - ``"api"`` (the default, and behaviourally what every recorded run used): inject
      ``ANTHROPIC_API_KEY`` from ``bench/.env`` when it holds one, remove nothing. The
      isolated ``HOME``/``CLAUDE_CONFIG_DIR`` carries no credentials, so without this
      every ``claude`` invocation would be unauthenticated; the ``.env`` value WINS over
      any ambient key so all bench spend lands on the single metered key. A
      missing/empty ``.env`` or key leaves the ambient env untouched (the prereq check
      reports that separately).
    - ``"subscription"``: strip ``_OUTRANKING_CREDENTIAL_VARS`` and set
      ``CLAUDE_CODE_OAUTH_TOKEN`` from ``bench/.env``, else from *base_env* -- one
      precedence rule for that file, mirroring ``ANTHROPIC_API_KEY``. The remaining
      over-ranking source, an ``apiKeyHelper`` in the isolated settings, lives in a file
      and cannot be stripped here; ``check_prereqs`` refuses the run instead (see
      :func:`api_key_helper_sources`). No token in either source is unrecoverable at
      invocation time, so it raises rather than falling back to the metered key.

    Raises ``RuntimeError`` for a subscription run with no token and ``ValueError`` for
    an unknown mode. Neither message -- nor anything else here -- may carry a credential
    value: these strings reach stderr, the run log, and the artifacts.
    """
    if child_auth == "api":
        api_key = _load_dotenv_key(ENV_PATH, "ANTHROPIC_API_KEY")
        return ({"ANTHROPIC_API_KEY": api_key} if api_key else {}, ())
    if child_auth == "subscription":
        token = _load_dotenv_key(ENV_PATH, OAUTH_TOKEN_VAR) or base_env.get(OAUTH_TOKEN_VAR)
        if not token:
            raise RuntimeError(
                "child_auth=subscription needs {var}: run `claude setup-token` and put "
                "the token in {path} as {var}=..., or export it. Found none in either "
                "source.".format(var=OAUTH_TOKEN_VAR, path=ENV_PATH)
            )
        return ({OAUTH_TOKEN_VAR: token}, _OUTRANKING_CREDENTIAL_VARS)
    raise ValueError(
        "unknown child_auth {!r}; expected one of {}".format(
            child_auth, ", ".join(CHILD_AUTH_MODES)
        )
    )


def build_env(pr, run_dir, base_env, child_auth="api"):
    """Assemble the pinned isolated env for one PR invocation (side effect: seeds trust).

    ``child_auth`` selects how the child authenticates (``CHILD_AUTH_MODES``); see
    ``_claude_auth_env`` for the credential contract and for why subscription mode has
    to REMOVE the higher-precedence credential vars rather than just not set them.
    Subscription mode additionally requires that the isolated config carry no
    ``apiKeyHelper`` -- a file cannot be stripped from an env, so ``check_prereqs``
    guards that ahead of the run via :func:`api_key_helper_sources`.

    The ``HOME``/``CLAUDE_CONFIG_DIR`` override isolates the *claude* binary's settings
    and plugins (the S7 boundary); it must not strand ``gh``, which the skill's children
    call. ``_gh_auth_env`` re-establishes ``gh``'s auth (``GH_CONFIG_DIR`` pointing at the
    real home, plus any ``GH_TOKEN``/``GITHUB_TOKEN``) across that override -- ``gh`` auth
    is an ambient prerequisite, not part of the claude-config isolation.
    """
    run_dir = Path(run_dir)
    env = dict(base_env)
    env.update(BENCH_ENV)
    auth_updates, auth_removals = _claude_auth_env(base_env, child_auth)
    for name in auth_removals:
        env.pop(name, None)
    env.update(auth_updates)
    env["CODE_GAUNTLET_OUTPUT_DIR"] = str(run_dir / "output")
    env["GH_REPO"] = "{}/{}".format(_owner(pr), _repo(pr))
    # Uncap the CLI's "background tasks at exit" wait. A ``-p`` run blocks on any background
    # task still running when the main turn ends, but only up to CLAUDE_CODE_PRINT_BG_WAIT_
    # CEILING_MS (default 600000 = 600s since v2.1.182; docs: code.claude.com/docs/en/headless
    # "Background tasks at exit"). The Phase 3 review Workflow can run detached and legitimately
    # exceed 600s, so the default ceiling terminates it before Phase 8 delivers -- the whole run
    # in smoke-20260719-190902-a14b4cc failed this way (all 3 PRs "workflow_backgrounded"). "0"
    # waits without limit; the per-PR watchdog (invoke_review's timeout_s -> _kill_group) remains
    # the sole time bound, so an unbounded wait cannot hang the run past that budget.
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"

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


# ---------------------------------------------------------------- plugin integrity

# Paths in the plugin repo the CONTROLLER/operator legitimately rewrites during a run (not
# child contamination), excluded from the mutation guard so they never false-flag a clean
# run:
#   - bench/experiments.jsonl — the experiment ledger, appended by the controller every PR.
#   - bench/report.html — the live dashboard, regenerated from the ledger by bench/report.py
#     (a tracked file stamped with today's date + the plugin sha, so ANY regeneration dirties
#     it). Operators run report.py mid-run to watch progress; without this exemption that
#     regeneration lands in REPO_ROOT's `git status` while a review child is in-flight and is
#     mis-attributed to it — invalidating a good (often long-running, expensive) PR as
#     'plugin_mutated_by_child' and resetting the dashboard. It is NOT written by any review
#     child (the pipeline writes every artifact to the absolute, gitignored {output_dir}; only
#     report.py touches report.html), so it belongs with the ledger as controller/operator-owned.
# Everything else changing means a child wrote into the plugin (self-healing mid-run), which
# contaminates the measurement.
_CONTROLLER_OWNED_PATHS = frozenset({"bench/experiments.jsonl", "bench/report.html"})


def _git(args, repo_root):
    """Run a git command in ``repo_root``; return ``(stdout, returncode)`` (rc=-1 on
    launch failure). Never raises — the guard degrades to "cannot judge" instead."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True, text=True
        )
    except OSError:
        return ("", -1)
    return (proc.stdout, proc.returncode)


def _parse_porcelain(text):
    """Parse ``git status --porcelain`` into ``[(status_xy, path)]``.

    Resolves rename/copy targets (``orig -> new`` -> ``new``) and strips the quotes git
    adds around paths with special characters. Blank lines are ignored.
    """
    entries = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:]
        if " -> " in path:  # rename/copy entry: 'orig -> new'
            path = path.split(" -> ", 1)[1]
        entries.append((status, path.strip().strip('"')))
    return entries


def _plugin_dirty_paths(repo_root):
    """Set of paths currently dirty in the plugin repo — the pre-run BASELINE for the
    mutation guard. Empty set on a clean repo; ``None`` when ``git status`` can't be read.

    The guard compares against this so a legitimate pre-existing local edit (a dev's WIP,
    or the controller's experiments.jsonl already modified) is never mistaken for — or
    reset as — child contamination. Only NEW dirtiness a child introduces is flagged.
    """
    out, rc = _git(["status", "--porcelain"], repo_root)
    if rc != 0:
        return None
    return {p for _s, p in _parse_porcelain(out)}


def _plugin_mutations(repo_root, baseline_paths=frozenset()):
    """Return child-caused changes ``[(status, path)]`` in the plugin repo: paths dirty
    now that were NOT dirty in ``baseline_paths`` and are not controller-owned. ``None``
    when ``git status`` can't be read (cannot judge — the caller then does not flag/reset)."""
    out, rc = _git(["status", "--porcelain"], repo_root)
    if rc != 0:
        return None
    baseline = baseline_paths or frozenset()
    return [
        (s, p) for (s, p) in _parse_porcelain(out)
        if p not in _CONTROLLER_OWNED_PATHS and p not in baseline
    ]


def _reset_plugin_repo(repo_root, mutations):
    """Undo child mutations to the plugin repo: delete untracked files/dirs, revert
    tracked edits with ``git checkout --``. Controller-owned paths were already filtered
    out (``_plugin_mutations``), so experiments.jsonl is never reverted."""
    repo_root = Path(repo_root)
    for status, path in mutations:
        target = repo_root / path
        if status.strip() == "??":  # untracked — checkout won't remove it
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except OSError:
                    pass
        else:
            _git(["checkout", "--", path], repo_root)


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


def _workflow_backgrounded(raw_text):
    """True when the CLI killed a still-running background task at its wait ceiling.

    Signature: the ``Background tasks still running after <n>s; terminating`` notice the
    CLI prints ahead of the result envelope. For a headless review it means the Phase 3
    Workflow ran detached and was terminated before Phase 8 — a distinct outcome from a
    genuine config-echo mismatch, so the runner labels it ``workflow_backgrounded``.
    """
    return bool(_BG_TASKS_KILLED_RE.search(raw_text or ""))


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


def invoke_review(worktree, pr, run_dir, timeout_s=1800, tool="deep-review-v3",
                  child_model="inherit", child_auth="api"):
    """Run the headless review for one PR and classify the outcome.

    ``tool`` selects the pipeline label and gates the v3 preflight: a ``deep-review-v3``
    run first asserts the child CLI is new enough to expose the Workflow tool (see
    ``_v3_preflight``) and returns ``invalid`` if not; ``deep-review-v2`` skips that check.
    The invocation itself is unchanged either way -- the ``--plugin-dir`` repo is whichever
    pipeline version is checked out; ``tool`` records/gates, it does not fork the command.

    ``child_model`` pins the child orchestrator session's model: any value other than
    ``inherit`` appends ``--model <child_model>`` to the ``claude`` command; ``inherit``
    leaves the child to its own default. The review agents' models are unaffected -- they
    are set by the pipeline's own policy, not this flag.

    ``child_auth`` is passed straight to ``build_env``, which owns the entire credential
    decision -- it is never re-derived or defaulted here.

    Returns an :class:`InvokeResult`. The isolated ``HOME``/``CLAUDE_CONFIG_DIR`` has no
    allowlist and no user, so ``--dangerously-skip-permissions`` plus the pinned context
    is the containment boundary; any prompt would deadlock, which the watchdog + scan catch.
    """
    worktree = Path(worktree).resolve()
    run_dir = Path(run_dir)
    number = _pr_number(pr)

    env = build_env(pr, run_dir, os.environ, child_auth=child_auth)
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

    # Snapshot the plugin repo's pre-run dirty state so the post-run integrity guard flags
    # only NEW mutations the child introduced — never a pre-existing local edit.
    baseline_dirty = _plugin_dirty_paths(REPO_ROOT) or frozenset()

    cmd = [
        claude_bin,
        "-p",
        "/{} {}".format(SKILL_COMMAND, number),
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--plugin-dir",
        str(REPO_ROOT),
    ]
    # Pin the child orchestrator session's model unless inheriting its default. The review
    # agents' models are set by the pipeline policy and are unaffected by this flag.
    if child_model != "inherit":
        cmd += ["--model", child_model]

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
            # A killed child may have left the plugin repo dirty; reset so the NEXT PR
            # starts from a clean plugin (this PR is already invalid via 'timeout').
            leftover = _plugin_mutations(REPO_ROOT, baseline_dirty)
            if leftover:
                _reset_plugin_repo(REPO_ROOT, leftover)
                print(
                    "PLUGIN MUTATED BY CHILD (timed-out PR {}) — reset {}: {}".format(
                        number, REPO_ROOT, ", ".join(p for _s, p in leftover)
                    ),
                    file=sys.stderr,
                )
            return InvokeResult(
                "timeout", raw_json_path=str(raw_path), reason="watchdog_timeout"
            )

    raw_text = raw_path.read_text(errors="replace")
    envelope = parse_result_envelope(raw_text)
    # The receipt may live in stdout, the envelope .result, or a report .md — collected
    # into pr_dir by run.py, or still in the shared output dir at echo-check time.
    report_dirs = (env.get("CODE_GAUNTLET_OUTPUT_DIR"), str(pr_dir))

    # 0) Plugin-repo integrity (measurement guard): a child that self-healed the plugin
    #    mid-run leaves REPO_ROOT dirty and contaminates this and every later PR. Reset
    #    the repo and invalidate BEFORE any outcome gate — the run is no longer trustworthy.
    mutations = _plugin_mutations(REPO_ROOT, baseline_dirty)
    if mutations:
        _reset_plugin_repo(REPO_ROOT, mutations)
        print(
            "PLUGIN MUTATED BY CHILD during PR {} — invalidated + reset {}: {}".format(
                number, REPO_ROOT, ", ".join(p for _s, p in mutations)
            ),
            file=sys.stderr,
        )
        costs = parse_costs(envelope or {})
        return InvokeResult(
            "invalid",
            cost_usd=costs["cost_usd"],
            per_model=costs["per_model"],
            echo_ok=_echo_ok(raw_text, envelope, report_dirs),
            raw_json_path=str(raw_path),
            reason="plugin_mutated_by_child",
        )

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

    # 2b) Background-task kill: the Phase 3 Workflow ran detached and the CLI terminated it
    #     at the background-wait ceiling before Phase 8 could deliver. The echo receipt and
    #     payload are then absent for THIS reason, not a config mismatch -- label it distinctly
    #     so the failure isn't conflated with a genuine echo/config problem. Invalid (unscored).
    if _workflow_backgrounded(raw_text):
        return InvokeResult(
            "invalid",
            cost_usd=cost_usd,
            per_model=per_model,
            echo_ok=_echo_ok(raw_text, envelope, report_dirs),
            raw_json_path=str(raw_path),
            reason="workflow_backgrounded",
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
    payload_path = _find_payload(env["CODE_GAUNTLET_OUTPUT_DIR"])
    delivery = env.get("CODE_GAUNTLET_DELIVERY", "")
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
