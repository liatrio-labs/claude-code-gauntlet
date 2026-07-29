#!/usr/bin/env python3
"""
await_workflow.py — Block until a backgrounded Workflow task has a terminal result.

Usage:
    python3 await_workflow.py <task-id-or-output-path>
                              [--attempt N] [--max-attempts M]
                              [--timeout-seconds S] [--poll-interval P]
                              [--artifacts-dir DIR] [--head-sha SHORT]
                              [--artifacts-grace-seconds G]
                              [--since-epoch T]

Phase 3 dispatches the review pipeline with one `Workflow` tool call. That call
returns a **Task ID**, not the pipeline's compact `{ ok, ... }` return: the return
arrives later, in a completion notification, and notifications are delivered only
*between* turns. So the orchestrator cannot both hold its turn (which it must — a
`claude -p` run that ends its turn waits for background tasks only up to
CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS, default 600000 ms, and then terminates them,
losing the review) and receive the notification. This script breaks that
circularity: it watches the task's output file from inside the turn and blocks
until the terminal object is actually on disk.

It replaces a prompt-driven `sleep 60` + Read loop that asked the model to hold its
turn, count to 30, and recognize a terminal object by eye. Every one of those is a
computation, and this is where computations belong.

Target
    The positional is either the output file's path (anything containing a path
    separator or ending in `.output` is used verbatim) or the bare Task ID printed
    by the `Workflow` tool, which is resolved by bounded glob — see resolve_target.

Output — exactly ONE compact JSON line on stdout for every WAIT outcome.
    An empty stdout is indistinguishable from a dead process, so no outcome of the
    wait itself prints nothing. On a terminal result nothing is written to stderr
    either, so a caller that merges the streams (the Bash tool does) still sees
    exactly one line.

    The one exception is exit 2, and it is argparse's, not the wait's: a malformed
    invocation is rejected before the wait starts, with the reason on stderr and
    an EMPTY stdout. That is reachable only from a command the skill itself got
    wrong — never from anything the workflow did — so it is a build-time bug to
    fix, not a run outcome to degrade on.

        exit 0  the terminal compact return itself, e.g.
                {"ok":true,"phaseReached":"report","stats":{...},...}
        exit 3  {"await":"pending",...,"next_command":"python3 ... --attempt 2 ..."}
        exit 4  {"await":"timeout","gap":"workflow-timeout",...}
        exit 4  {"await":"error","gap":"workflow-timeout","message":...}
        exit 5  {"await":"artifacts_only","gap":"workflow-timeout","detail":...}
        exit 2  argparse's own usage error

    `pending`, `timeout` and `artifacts_only` share one field set: await, attempt,
    max_attempts, waited_seconds, target, resolved_path, file_bytes, since_epoch,
    artifacts, saw_ok_without_corroborator, scan_skipped, scan_stop_reason —
    plus `searched` only
    when resolution failed, `next_command` only on `pending`, and `gap`/`detail`
    only on the two that declare one. `error` is deliberately a REDUCED shape
    (await, gap, message, target, attempt, max_attempts): it is emitted from the
    handler around the whole wait, so the failure may well have happened before
    the other fields were ever computed, and reporting a default for something
    never observed would be a fabrication.

    Exit 0's payload is the terminal object with nothing dropped, added, reordered
    by hand, or summarized — re-serialized compactly so it lands on one line.

Exit codes
    0  A terminal result was observed. stdout is it; carry it into Phase 8.
    2  Usage error (argparse).
    3  Not terminal yet and attempts remain. Run stdout's `next_command` verbatim.
    4  Attempts exhausted (or an unexpected failure). Declare a `workflow-timeout`
       gap and deliver per the Phase 8 degradation rules.
    5  The persisted artifacts landed but the compact return was never observed.
       Same `workflow-timeout` gap, but the artifacts on disk are real and
       deliverable — see the marker's `detail`.

No external Python dependencies — stdlib only.
"""

import argparse
import glob
import json
import os
import shlex
import sys
import time


# ---------------------------------------------------------------------------
# Bounds and defaults
# ---------------------------------------------------------------------------

#: Per-invocation wait, in seconds. The script runs inside ONE Bash tool call, and
#: that call's own ceiling (600 s, the maximum the tool accepts) would kill the
#: script mid-wait — leaving the orchestrator with a tool error and no stdout to
#: branch on. 540 s keeps a full minute of headroom under it, and the caller
#: repeats the invocation up to DEFAULT_MAX_ATTEMPTS times for a 36-minute total,
#: which is longer than the 30-minute cap the poll loop this replaces allowed.
DEFAULT_TIMEOUT_SECONDS = 540

#: Headroom subtracted from BASH_MAX_TIMEOUT_MS when that variable is exported —
#: the script must return and print before the Bash tool kills it, not at the
#: same instant.
TIMEOUT_HEADROOM_SECONDS = 60

#: Never bound the wait below this, however small BASH_MAX_TIMEOUT_MS is. A
#: sub-30-second await is useless, and silently degrading to one would be worse
#: than the caller seeing several `pending` rounds.
MIN_TIMEOUT_SECONDS = 30

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_POLL_INTERVAL = 2

#: How long to keep waiting for the compact return once every persisted artifact
#: is present and fresh. The Persist stage is the last thing the pipeline does, so
#: the return should follow it almost immediately; 120 s is generous headroom on
#: that expectation rather than a measured figure. Past it the return is not
#: coming, and reporting `artifacts_only` beats burning three more attempts on a
#: run that has already finished.
DEFAULT_ARTIFACTS_GRACE_SECONDS = 120

#: Bounds on the embedded-object scan. `contextReadPlan`'s READ_PLAN_MAX_CHUNKS
#: exists for the same reason: an unbounded loop over attacker- or accident-shaped
#: input is a way to hang the wait itself. SCAN_MAX_CHARS is checked before any
#: allocation; SCAN_MAX_PROBES bounds the ATTEMPTS, not just the successes —
#: bounding successes alone left the cost quadratic in the input, because every
#: failed decode still paid to scan forward. Measured before that bound existed:
#: `find_terminal('{' * 200_000)` took 7.96s, and 400 KB of `{"` did not finish
#: inside 15s. Both are now sub-millisecond.
SCAN_MAX_CANDIDATES = 500
SCAN_MAX_PROBES = 2000
SCAN_MAX_CHARS = 8_000_000
#: How many stack-exhausting candidates to step over before giving up on the file.
#: One is not a reason to stop — a later document start does not re-descend the
#: structure that blew the stack — but a file full of them is not task output.
SCAN_MAX_DEEP_CANDIDATES = 8

#: Fields that only the pipeline's own compact return carries. A bare `ok` is NOT
#: enough to call an object terminal: the assemble receipt
#: ({ok, planVersion, planChecksum, verified, written, errors}) and other agent
#: receipts also carry one, and mistaking a receipt for the return would send
#: Phase 8 off with the wrong object — a far worse outcome than waiting longer.
#: Observed shapes this list covers (not an exhaustive field-by-field enum):
#: the success shape {ok, phaseReached, stats, artifactPaths, resolvedPolicy,
#: checkpoints, gaps}; the early args-rejection envelope (no checkpoints, no
#: resolvedPolicy); and the mid-run catch failure (has checkpoints via
#: buildResumeCheckpoints, omits resolvedPolicy). Any member below corroborates.
#: `error` is deliberately not in this list — it is far too generic to corroborate.
COMPACT_RETURN_KEYS = (
    "phaseReached",
    "stats",
    "artifactPaths",
    "checkpoints",
    "resolvedPolicy",
    "gaps",
    "failingPhase",
)

#: The four terminal artifacts the Persist stage puts on disk, as
#: `{output_dir}/code-gauntlet-{purpose}-{head_sha_short}.{ext}`. Mirrors
#: `workflows/src/stages.js` (`artifactPaths` and the `all` checkpoint name);
#: tests/test_await_workflow.py pins the two in lockstep, because a rename on the
#: JS side would otherwise leave this fallback silently blind forever.
ARTIFACT_BASENAMES = (
    "code-gauntlet-findings-{sha}.json",
    "code-gauntlet-report-{sha}.md",
    "code-gauntlet-post-review-{sha}.json",
    "code-gauntlet-checkpoint-all-{sha}.json",
)

#: Escape hatch for an environment whose task directory this script cannot derive.
#: Mirrors $CODE_GAUNTLET_OUTPUT_DIR: one documented variable, no guessing.
TASKS_DIR_ENV = "CODE_GAUNTLET_TASKS_DIR"


def default_timeout_seconds(environ=None):
    """Return the per-invocation wait, bounded by BASH_MAX_TIMEOUT_MS if exported.

    The ceiling that would kill this process is a platform setting, not something
    the caller should have to remember to mirror in a flag. When it is visible in
    the environment, compute the bound; when it is not (it may live in
    settings.json instead), fall back to the constant. An unparseable value
    changes nothing — a malformed setting must not shorten the wait to something
    absurd.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get("BASH_MAX_TIMEOUT_MS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        ceiling = int(float(raw)) // 1000
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if ceiling <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(DEFAULT_TIMEOUT_SECONDS,
                                        ceiling - TIMEOUT_HEADROOM_SECONDS))


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def looks_like_path(target):
    """True when *target* should be used verbatim rather than resolved as an id."""
    if not target:
        return False
    return os.sep in target or target.endswith(".output")


def task_roots(environ=None):
    """Return every directory that MAY hold this session's tasks/ tree.

    Background-task output lives at
    ``<tmp-root>/<project-slug>/<session-uuid>/tasks/<task-id>.output``. The
    tmp-root is `claude-<uid>` under the system temp directory, but which spelling
    of that directory is real varies (on macOS ``/tmp`` is a symlink to
    ``/private/tmp``), so every candidate is listed and de-duplicated by realpath
    rather than assumed.

    Candidates that do not exist are RETAINED, not filtered out. They cost nothing
    to skip at glob time, and dropping them made the failure undiagnosable: on a
    machine with no such directory at all, `searched` came back empty, so a run
    that could not resolve its target reported no reason and pointed at no fix.
    A caller reading the marker needs to see what was looked for.
    """
    environ = os.environ if environ is None else environ
    bases = ["/tmp", "/private/tmp"]
    tmpdir = environ.get("TMPDIR")
    if tmpdir:
        bases.append(tmpdir.rstrip(os.sep) or os.sep)
    roots, seen = [], set()
    for base in bases:
        candidate = os.path.join(base, "claude-%d" % os.getuid())
        real = os.path.realpath(candidate)
        if real in seen:
            continue
        seen.add(real)
        roots.append(candidate)
    return roots


def _newest(paths):
    """Return the most recently modified of *paths*, or None. Never raises.

    Task ids are short and random, so a glob across every session directory can in
    principle match more than one. Lexicographic order would then be arbitrary —
    and arbitrarily wrong, because the loser might be a COMPLETED run from another
    session, whose terminal result would be handed to Phase 8 as if it were this
    review's. Newest-first is the only ordering that correlates with "the run we
    just dispatched".

    An mtime FLOOR would be the stronger guard and is deliberately not used here:
    a fast failure (args validation returns in well under a second) can land
    before the orchestrator gets around to issuing the Bash call, so a floor would
    reject the very result it was meant to protect. The artifacts signal can
    afford that floor because artifacts are written mid-run; this file is not.
    """
    best, best_mtime = None, None
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if best_mtime is None or mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def resolve_target(target, environ=None):
    """Return ``(path_or_None, searched)`` for *target*. Never raises.

    *searched* is the list of patterns actually tried, and it is echoed in the
    timeout marker: an environment this cannot resolve must say what it looked for,
    so $CODE_GAUNTLET_TASKS_DIR is an actionable fix rather than a guess.

    Resolution is retried on every poll tick, not once at startup — the harness
    may not have created the file at the instant the Workflow call returned.
    """
    environ = os.environ if environ is None else environ
    if looks_like_path(target):
        return target, []

    searched = []
    override = environ.get(TASKS_DIR_ENV)
    if override:
        direct = os.path.join(override, target + ".output")
        searched.append(direct)
        if os.path.exists(direct):
            return direct, searched

    for root in task_roots(environ):
        pattern = os.path.join(root, "*", "*", "tasks", target + ".output")
        # Recorded whether or not the root exists — see task_roots on why.
        searched.append(pattern)
        if not os.path.isdir(root):
            continue
        try:
            hits = glob.glob(pattern)
        except OSError:
            hits = []
        if hits:
            return _newest(hits) or sorted(hits)[0], searched
    return None, searched


def read_text(path):
    """Return the file's text, or ``""`` for anything unreadable. Never raises.

    Unreadable is not an error here: before the workflow finishes, the file is
    routinely absent or zero bytes, and both are simply "not yet terminal".
    Decoding uses errors="replace" so one undecodable byte cannot turn a live wait
    into a crash with no JSON on stdout.

    The isfile() guard is not decoration. A plain open() on a FIFO blocks until a
    writer appears — forever, in practice — and a blocked open prints nothing at
    all, which is the single outcome the one-line-stdout contract exists to rule
    out. It also disposes of directories and device nodes on the way past.
    """
    if not path or not os.path.isfile(path):
        return ""
    try:
        # utf-8-sig, not utf-8: a leading BOM is not whitespace and not '{', so it
        # would defeat both the whole-file parse and the document-start scan, and
        # the file would read as "never terminal" for the entire wait.
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        return ""


def file_size(path):
    """Return the file's size in bytes, or None when it cannot be stat'ed."""
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Terminal detection — pure
# ---------------------------------------------------------------------------

def is_terminal_return(obj):
    """True when *obj* is the pipeline's compact return.

    Two conditions, both required: a boolean ``ok`` and at least one field only
    the return carries. See COMPACT_RETURN_KEYS for why the second exists.
    """
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("ok"), bool):
        return False
    return any(key in obj for key in COMPACT_RETURN_KEYS)


def _has_bare_ok(obj):
    """True when *obj* carries a boolean ``ok`` but corroborates nothing.

    Recorded in the marker as `saw_ok_without_corroborator`. If the return's shape
    ever changes such that none of COMPACT_RETURN_KEYS survives, this flag is the
    difference between a diagnosable failure and a wait that silently times out on
    every run forever.
    """
    return (isinstance(obj, dict)
            and isinstance(obj.get("ok"), bool)
            and not any(key in obj for key in COMPACT_RETURN_KEYS))


def terminal_from(value):
    """Return the compact return carried by *value*, or ``(None, saw_bare_ok)``.

    Returns a ``(terminal_or_None, saw_bare_ok)`` pair. Three shapes are checked,
    in this order:

    1. ``value["result"]`` as an object — the shape the Workflow tool actually
       writes: an envelope of {summary, agentCount, logs, result, workflowProgress,
       totalTokens, totalToolCalls} with the script's return value nested at
       ``result``.
    2. ``value["result"]`` as a JSON *string* — the same envelope from a producer
       that stringified its return.
    3. *value* itself — a bare return, for a harness that writes it unwrapped.

    Nothing deeper is inspected. A recursive hunt for any ``ok``-bearing object
    would eventually find one inside a progress entry or an agent receipt, and a
    false terminal is worse than a slow one.
    """
    saw_bare_ok = False
    if isinstance(value, dict):
        result = value.get("result")
        if is_terminal_return(result):
            return result, saw_bare_ok
        saw_bare_ok = saw_bare_ok or _has_bare_ok(result)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except (ValueError, RecursionError):
                parsed = None
            if is_terminal_return(parsed):
                return parsed, saw_bare_ok
            saw_bare_ok = saw_bare_ok or _has_bare_ok(parsed)
    if is_terminal_return(value):
        return value, saw_bare_ok
    saw_bare_ok = saw_bare_ok or _has_bare_ok(value)
    return None, saw_bare_ok


def _note(bounds, reason):
    """Record the FIRST bound that stopped a scan early, if anyone is listening."""
    if bounds is not None and not bounds.get("stopped"):
        bounds["stopped"] = reason


def _document_starts(text):
    """Yield the offsets at which a JSON *document* may plausibly begin.

    Only a ``{`` that is the first non-whitespace character of its line counts.
    That single restriction does two jobs, and it is the reason the scan is both
    fast and safe:

    * It bounds the cost. Probing at every ``{`` made the scan quadratic — each
      failed decode still scanned forward — so a file of `{{{{...` or `{"{"{"...`
      could hang the wait for hours while staying under SCAN_MAX_CHARS. Runs of
      braces are no longer probe sites at all.
    * It prevents a false terminal. The target is written whole, but a torn read
      is still possible, and a *fragment* of a half-written file can end exactly
      after some nested object closes. One such nested object — an agent's
      recorded receipt inside `workflowProgress` — carries a boolean ``ok`` and a
      field name that overlaps COMPACT_RETURN_KEYS, so scanning mid-line offsets
      accepted it as the pipeline's return. Nested values never start a line at
      column zero, so they are no longer reachable.

    Documents that are genuinely appended after other output — the case issue #26
    R2 names — do start their own line, so they are still found.
    """
    for line_start in _line_starts(text):
        cursor = line_start
        # \r is in the skip set for CRLF input: lines are split on \n, so a stray
        # carriage return can sit between the split point and the brace.
        while cursor < len(text) and text[cursor] in " \t\r":
            cursor += 1
        if cursor < len(text) and text[cursor] == "{":
            yield cursor


def _line_starts(text):
    """Yield the offset of each line's first character."""
    yield 0
    idx = text.find("\n")
    while idx != -1:
        yield idx + 1
        idx = text.find("\n", idx + 1)


def _iter_json_objects(text, bounds=None, max_candidates=SCAN_MAX_CANDIDATES,
                       max_probes=SCAN_MAX_PROBES,
                       max_deep=SCAN_MAX_DEEP_CANDIDATES):
    """Yield each JSON object decodable at a document start in *text*, in order.

    Escaped braces inside a JSON string cannot start a valid object (``{\\"``
    fails immediately), so quoted content is rejected by the decoder rather than
    by a hand-rolled string scanner. Both the successes and the ATTEMPTS are
    bounded; bounding only the successes is what left the old scan quadratic.
    """
    decoder = json.JSONDecoder()
    yielded = 0
    probes = 0
    deep = 0
    consumed_to = -1
    for idx in _document_starts(text):
        # Every bound that stops the scan early records WHY. A bound that
        # truncates the search silently is the same defect as the ones this scan
        # has already shipped twice: a real terminal object further down the file
        # is dropped, and nothing anywhere says the file was not fully searched.
        if yielded >= max_candidates:
            _note(bounds, "max_candidates")
            return
        if probes >= max_probes:
            _note(bounds, "max_probes")
            return
        if idx < consumed_to:
            continue  # already inside an object this scan decoded
        probes += 1
        try:
            obj, end = decoder.raw_decode(text, idx)
        except RecursionError:
            # Skip THIS candidate, never the rest of the scan. Aborting outright
            # was wrong: a later document start is at a different offset and does
            # not re-descend the structure that blew the stack, so a wholly
            # well-formed terminal object further down the file was being dropped
            # — and dropped silently, since nothing recorded that it happened.
            deep += 1
            if deep >= max_deep:
                _note(bounds, "max_deep_candidates")
                return
            continue
        except ValueError:
            # Deliberately NOT skipping forward to the decoder's error position.
            # A failed decode's `pos` is where it gave up, which can be far past a
            # later — and perfectly valid — document start; using it as a floor
            # made the scan refuse to probe that offset at all, losing a real
            # terminal object. The cost this was meant to control is already
            # handled by only probing document starts and by SCAN_MAX_PROBES.
            continue
        # A SUCCESSFUL decode is different: it really did consume that span, and
        # any document start inside it is a nested value, not a sibling document.
        consumed_to = end
        if isinstance(obj, dict):
            yielded += 1
            yield obj


def find_terminal(text):
    """Return ``(terminal_or_None, saw_bare_ok, stop_reason)`` for a file's text.

    *stop_reason* is None when the whole file was searched, else the name of the
    bound that cut the search short (``max_chars``, ``max_candidates``,
    ``max_probes``, ``max_deep_candidates``). It reaches the caller's marker so a
    truncated search is disclosed rather than reported as "nothing here".

    ``None`` ALWAYS means "not yet terminal, keep waiting" — never an error. A
    half-written file is the normal state of the target for most of the wait, so
    a parse failure that raised or exited would turn the ordinary case into a lost
    review.

    Whole-file parse first, then an embedded scan. The scan is what covers a
    terminal object embedded in, or appended after, other task output rather than
    being the whole file; the LAST qualifying candidate wins, because terminal
    means final and a retried producer can leave two.
    """
    if not text or not text.strip():
        return None, False, None

    saw_bare_ok = False
    try:
        whole = json.loads(text)
    except (ValueError, RecursionError):
        pass
    else:
        found, bare = terminal_from(whole)
        saw_bare_ok = saw_bare_ok or bare
        if found is not None:
            return found, saw_bare_ok, None

    # Checked BEFORE the scan allocates anything: the point of a bound is to be
    # cheaper than the work it is bounding.
    if len(text) > SCAN_MAX_CHARS:
        return None, saw_bare_ok, "max_chars"

    bounds = {}
    found = None
    for candidate in _iter_json_objects(text, bounds):
        hit, bare = terminal_from(candidate)
        saw_bare_ok = saw_bare_ok or bare
        if hit is not None:
            found = hit
    # A bound that tripped AFTER the terminal object was already found did not
    # cost anything, so it is not reported as a truncated search.
    return found, saw_bare_ok, (None if found is not None else bounds.get("stopped"))


# ---------------------------------------------------------------------------
# Secondary signal — the persisted artifacts
# ---------------------------------------------------------------------------

def artifacts_state(artifacts_dir, head_sha, since_epoch):
    """Return the freshness state of the four terminal artifacts. Never raises.

    "Complete" requires every one of them to exist, be non-empty, and have an
    mtime at or after *since_epoch*. The mtime floor is what makes a STALE artifact
    set — a previous run at the same head SHA, left behind because Phase 2's
    stale_truncate was skipped or failed — unusable. Without it this fallback
    could deliver a previous review as if it were this one, which is worse than
    losing the current one.

    Why a second signal exists at all: the primary target is the harness's file at
    a path this script has to derive, while the artifacts are ours, at a path the
    skill constructs. Two independent observables mean a harness layout change
    alone cannot lose a review, and a writer failure alone cannot either.
    """
    state = {"checked": False, "complete": False, "present": [], "missing": []}
    if not artifacts_dir or not head_sha:
        return state
    state["checked"] = True
    for template in ARTIFACT_BASENAMES:
        path = os.path.join(artifacts_dir, template.format(sha=head_sha))
        try:
            stat = os.stat(path)
            # S_ISREG, not just a successful stat: a DIRECTORY named like an
            # artifact stats fine and reports a non-zero size, so a size check
            # alone would count it as present and fire the fallback on a run that
            # persisted nothing.
            fresh = (os.path.isfile(path)
                     and stat.st_size > 0
                     and stat.st_mtime >= since_epoch)
        except OSError:
            fresh = False
        name = os.path.basename(path)
        if fresh:
            state["present"].append(name)
        else:
            state["missing"].append(name)
    state["complete"] = not state["missing"]
    return state


# ---------------------------------------------------------------------------
# Marker assembly and emission
# ---------------------------------------------------------------------------

def build_next_command(args, resolved_path, since_epoch):
    """Return the literal command string for the next attempt.

    The caller runs this verbatim: no arithmetic, no template filling, no
    remembering which flags to carry forward. That is the whole point — the
    attempt count and the artifact freshness floor are both state this process
    holds and the next one needs, so this process writes them down.

    Every token is shlex-quoted, and the result contains no variable reference,
    command substitution, or heredoc — those are the forms the tree-sitter-bash
    parser rejects, and a recovery command that gets silently denied is no
    recovery at all.

    The RESOLVED path is preferred over the original id once known, so later
    attempts skip the glob entirely.

    The target goes LAST, behind a literal ``--``. A path or id beginning with a
    dash is otherwise swallowed by argparse as an option-like token, and since the
    fallback target is echoed verbatim on every retry, one such name would
    reproduce the same usage error on every attempt until the budget ran out.
    """
    parts = [
        "python3",
        shlex.quote(os.path.abspath(__file__)),
        "--attempt", str(args.attempt + 1),
        "--max-attempts", str(args.max_attempts),
        "--timeout-seconds", str(args.timeout_seconds),
        "--poll-interval", str(args.poll_interval),
        "--since-epoch", repr(float(since_epoch)),
    ]
    if args.artifacts_dir:
        parts += ["--artifacts-dir", shlex.quote(args.artifacts_dir)]
    if args.head_sha:
        parts += ["--head-sha", shlex.quote(args.head_sha)]
    if args.artifacts_dir or args.head_sha:
        parts += ["--artifacts-grace-seconds", str(args.artifacts_grace_seconds)]
    parts += ["--", shlex.quote(resolved_path or args.target)]
    return " ".join(parts)


def _wait_error_payload(args, message):
    """Build the degrade-and-disclose marker for an unexpected wait failure."""
    return {
        "await": "error",
        "gap": "workflow-timeout",
        "message": message,
        "target": args.target,
        "attempt": args.attempt,
        "max_attempts": args.max_attempts,
    }


def emit(payload):
    """Print *payload* as exactly one compact JSON line.

    Compact, never indent=2: this is `assemble_artifacts.py`'s rule and it holds
    for the same reason — a pretty-printer's embedded newlines would split the one
    line the caller is told to read. If the payload will not serialize, a
    hand-built minimal line goes out instead, because printing nothing is the one
    outcome that leaves the caller unable to tell a failure from a dead process.

    Re-raises ``OSError`` after a broken-pipe redirect so the caller can degrade
    to a documented exit code; every other failure still produces a line.
    """
    try:
        line = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        line = json.dumps({
            "await": "error",
            "gap": "workflow-timeout",
            "message": "result would not serialize: %s" % exc,
        }, separators=(",", ":"))
    try:
        print(line)
        sys.stdout.flush()
    except OSError:
        # A reader that closed the pipe early (`| head -1`) raises BrokenPipeError
        # here, and letting it escape would exit 1 — a code the caller's branch
        # table does not cover — after a half-written line. Retarget stdout at
        # /dev/null so the interpreter's shutdown flush cannot raise again, and
        # let the caller fall through to a documented degrade.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                # dup2 DUPLICATES the descriptor, so this one is still ours to
                # close whether or not the duplication succeeded.
                os.close(devnull)
        except OSError:
            # Best-effort only. If even /dev/null cannot be opened or duplicated
            # there is nothing further to salvage here, and the original write
            # failure re-raised below is the one the caller needs to see.
            pass
        raise


def build_marker(kind, args, observed, since_epoch, started_at):
    """Assemble the non-terminal marker. Pure."""
    marker = {
        "await": kind,
        "attempt": args.attempt,
        "max_attempts": args.max_attempts,
        "waited_seconds": round(time.time() - started_at, 1),
        "target": args.target,
        "resolved_path": observed["resolved_path"],
        "file_bytes": observed["file_bytes"],
        "since_epoch": since_epoch,
        "artifacts": observed["artifacts"],
        "saw_ok_without_corroborator": observed["saw_bare_ok"],
        # Two fields, because they answer two questions: did the search cover the
        # whole file, and if not, which bound stopped it. A truncated search that
        # reported plain "nothing here" is how a real terminal object gets dropped
        # with nothing anywhere saying so.
        "scan_skipped": observed["scan_stopped"] is not None,
        "scan_stop_reason": observed["scan_stopped"],
    }
    if observed["resolved_path"] is None:
        # Only useful when resolution actually failed; on the happy path it is
        # noise in front of the field the caller needs.
        marker["searched"] = observed["searched"]
    return marker


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------

def await_terminal(args, environ=None):
    """Watch the target until terminal, or until this invocation's deadline.

    Returns ``(payload, exit_code)``. Every outcome returns a payload; there is no
    path that returns None.
    """
    environ = os.environ if environ is None else environ
    started_at = time.time()
    since_epoch = args.since_epoch if args.since_epoch is not None else started_at
    deadline = started_at + max(0, args.timeout_seconds)
    artifacts_complete_at = None
    observed = {
        "resolved_path": None,
        "searched": [],
        "file_bytes": None,
        "artifacts": artifacts_state(None, None, since_epoch),
        "saw_bare_ok": False,
        "scan_stopped": None,
    }

    while True:
        path, searched = resolve_target(args.target, environ)
        observed["resolved_path"] = path
        observed["searched"] = searched
        observed["file_bytes"] = file_size(path)

        terminal, saw_bare_ok, scan_stopped = find_terminal(read_text(path))
        observed["saw_bare_ok"] = observed["saw_bare_ok"] or saw_bare_ok
        observed["scan_stopped"] = scan_stopped
        if terminal is not None:
            # Deliberately silent on stderr here. The documented caller is a Bash
            # tool call, which MERGES the two streams — so a diagnostic line would
            # arrive as a second line in front of the payload, and "stdout is the
            # terminal return" would stop being true for the one caller that
            # matters. Diagnostics are reserved for the non-terminal outcomes,
            # whose marker already carries the same facts as fields.
            return terminal, 0

        observed["artifacts"] = artifacts_state(
            args.artifacts_dir, args.head_sha, since_epoch
        )
        now = time.time()
        if observed["artifacts"]["complete"]:
            if artifacts_complete_at is None:
                artifacts_complete_at = now
            grace_elapsed = now - artifacts_complete_at >= args.artifacts_grace_seconds
            # Two different triggers, because they answer two different questions.
            #
            # Unresolved target: the fallback is all we are ever going to get, so
            # stop as soon as the grace window closes rather than spend the whole
            # attempt budget re-globbing directories that do not exist.
            #
            # Resolved target: we are watching the right file and the return is
            # still coming, so bailing at the grace window would DISCARD it. Here
            # the fallback only earns its keep at the very end, where it converts
            # what would have been a bare timeout into a deliverable result.
            if ((grace_elapsed and observed["resolved_path"] is None)
                    or (args.attempt >= args.max_attempts and now >= deadline)):
                marker = build_marker("artifacts_only", args, observed,
                                      since_epoch, started_at)
                marker["gap"] = "workflow-timeout"
                marker["detail"] = (
                    "every persisted artifact is present and fresh, but the "
                    "workflow's compact return was never observed; deliver from "
                    "the artifacts on disk and disclose the gap"
                )
                return marker, 5
        else:
            artifacts_complete_at = None

        if now >= deadline:
            break
        time.sleep(max(0, min(args.poll_interval, deadline - now)))

    if args.attempt < args.max_attempts:
        marker = build_marker("pending", args, observed, since_epoch, started_at)
        marker["next_command"] = build_next_command(
            args, observed["resolved_path"], since_epoch
        )
        return marker, 3

    marker = build_marker("timeout", args, observed, since_epoch, started_at)
    marker["gap"] = "workflow-timeout"
    marker["detail"] = (
        "no terminal workflow result after %d attempts; declare the gap and "
        "deliver whatever partial artifacts exist" % args.max_attempts
    )
    return marker, 4


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser(environ=None):
    parser = argparse.ArgumentParser(
        description="Block until a backgrounded Workflow task has a terminal result."
    )
    parser.add_argument(
        "target",
        metavar="TASK_ID_OR_PATH",
        help="The Task ID printed by the Workflow tool, or the task output file's "
             "path. Anything containing a path separator or ending in .output is "
             "used verbatim; anything else is resolved as a task id.",
    )
    parser.add_argument(
        "--attempt", type=int, default=1, metavar="N",
        help="Which attempt this is (default 1). Carried forward by next_command.",
    )
    parser.add_argument(
        "--max-attempts", dest="max_attempts", type=int,
        default=DEFAULT_MAX_ATTEMPTS, metavar="M",
        help="Total attempts allowed before declaring the workflow-timeout gap "
             "(default %d)." % DEFAULT_MAX_ATTEMPTS,
    )
    parser.add_argument(
        "--timeout-seconds", dest="timeout_seconds", type=float,
        default=default_timeout_seconds(environ), metavar="S",
        help="Per-invocation wait. Defaults to %d, lowered automatically when "
             "BASH_MAX_TIMEOUT_MS is exported." % DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--poll-interval", dest="poll_interval", type=float,
        default=DEFAULT_POLL_INTERVAL, metavar="P",
        help="Seconds between checks (default %d)." % DEFAULT_POLL_INTERVAL,
    )
    parser.add_argument(
        "--artifacts-dir", dest="artifacts_dir", metavar="DIR",
        help="The review's output directory. With --head-sha, enables the "
             "secondary signal: the four persisted artifacts.",
    )
    parser.add_argument(
        "--head-sha", dest="head_sha", metavar="SHORT",
        help="head_sha_short, the artifact filename discriminator.",
    )
    parser.add_argument(
        "--artifacts-grace-seconds", dest="artifacts_grace_seconds", type=float,
        default=DEFAULT_ARTIFACTS_GRACE_SECONDS, metavar="G",
        help="How long to keep waiting for the compact return once every artifact "
             "is present (default %d)." % DEFAULT_ARTIFACTS_GRACE_SECONDS,
    )
    parser.add_argument(
        "--since-epoch", dest="since_epoch", type=float, default=None, metavar="T",
        help="Artifact freshness floor, as a unix timestamp. Defaults to now; "
             "next_command carries the ORIGINAL value forward so an artifact "
             "written during attempt 1 still counts as fresh during attempt 4.",
    )
    return parser


def main(argv=None, environ=None):
    """Run the wait and print exactly one JSON line. Returns the exit code."""
    parser = build_parser(environ)
    args = parser.parse_args(argv)
    # Reject the half-configured secondary signal rather than quietly running
    # without it. artifacts_state() needs both halves to name a file, so one flag
    # alone disables the fallback — and a safety net that turns itself off because
    # of a typo, without saying so, is worse than no safety net.
    if bool(args.artifacts_dir) != bool(args.head_sha):
        parser.error("--artifacts-dir and --head-sha must be given together")
    try:
        payload, code = await_terminal(args, environ)
    except KeyboardInterrupt:
        # Even an interrupt gets a line: the caller branches on stdout, and a
        # silent exit is the one shape it cannot interpret.
        payload, code = _wait_error_payload(args, "interrupted"), 4
    except Exception as exc:  # noqa: BLE001 - the never-print-nothing contract
        payload, code = (
            _wait_error_payload(args, "%s: %s" % (type(exc).__name__, exc)),
            4,
        )
    # One emit site for every outcome — including the error markers above — so a
    # broken pipe during an interrupt/exception report degrades the same way as
    # a broken pipe on the happy path, instead of escaping as exit 1.
    try:
        emit(payload)
    except OSError:
        # The payload could not be delivered at all, so there is nothing useful to
        # print — but the exit code must still be one the caller's branch table
        # covers. 4 is the degrade-and-disclose family; exit 1 was not documented
        # anywhere and would read as an interpreter crash.
        return 4
    return code


if __name__ == "__main__":
    sys.exit(main())
