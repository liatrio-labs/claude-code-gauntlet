"""Mechanical functional-smoke checker for completed bench runs (Issue #28).

Inspects a finished run directory and returns a pass/fail verdict for the
functional smoke gates. Never invokes the judge, adjudicator, or ``score_run``.

Gates (aligned with ``bench/MEASUREMENT.md``):

  G1  Completeness — every ``run.json`` ``pr_urls`` entry has terminal status ``ok``
  G2  Payload parse + adapter-required fields + union-schema findings check
      (requires ≥1 findings artifact per PR)
  G3  Zero ``origin=unknown`` findings; no writer no-write-proof / partial-artifacts
      degrade. ALSO fails when a PR delivers any unclassified finding (origin
      not 'new'/'surfaced', including a finding with no ``origin`` key at
      all — a strictly wider test than the ``origin=unknown`` check, see
      ``_is_classified``) whose persisted artifacts carry no health-degradation
      banner sentinel on EITHER delivery surface — ``code-gauntlet-report-*.md``
      or ``code-gauntlet-post-review-*.json``'s ``review_body`` (see
      ``_report_has_health_banner`` / ``_post_review_has_health_banner``, and
      the EITHER-not-BOTH note at the call site): a degraded review that never
      discloses itself is the exact defect issue #25 req 7 exists to prevent,
      and until this check existed it was undetectable.
  G4  Plugin identity — Headless config echo receipts (``pipeline_version``,
      ``plugin_root``) are primary; a complete valid receipt is sufficient when
      no ``workflows/wf_*.json`` records were collected. When records exist,
      top-level Workflow ``scriptPath`` is also checked (defense-in-depth).
      Without a complete echo receipt, collected workflow records are required
      (scriptPath-only fallback).
  G5  ≥1 delivered inline comment across the run set

Reported stats (not gates):

  input_proof  Slice-input content-proof measurement (issue #25 PR3), read
      structurally from each ``workflows/wf_*.json`` record's
      ``result.stats.inputProof``. Deliberately NOT a gate — a slice whose
      input never got proven degrades to ``origin=unknown``, which G3 already
      fails; a second verdict on the same root cause would double-count it.
      Absent on any run recorded before PR3 landed, and reported as such
      (``None``), never as zeros — a checker that printed 0/0 for a
      never-measured run would claim a clean measurement it never made.

  health  The delivered review's own health (issue #25 reqs 7-9), read
      structurally from each ``workflows/wf_*.json`` record's
      ``result.stats.health`` and aggregated across the run's PRs. This is a
      DIFFERENT signal from the G3 banner-pairing failure condition above:
      that check is derived directly from the persisted findings artifact and
      report, so it still fires correctly even on a run that collected no
      ``wf_*.json`` records at all — the case where this stat reads ``None``
      ("not measured"). Same absent-means-unmeasured contract as input_proof.

Stdlib-only (CLAUDE.md).
"""

import json
import re
from pathlib import Path

from bench.runner.invoke import (
    extract_identity_receipt,
    parse_result_envelope,
    read_pipeline_version,
    script_path_matches_repo,
    scriptpath_from_record,
)

# Union-schema surface the persist boundary writes (canonical + v2 aliases).
# A findings file may use either naming; we accept either for each pair.
_CANONICAL_OR_ALIAS = (
    ("description", "body"),
    ("file",),
    ("origin",),
)
# line identity: at least one of these must be present
_LINE_FIELDS = ("line_start", "line")

# ``stats.inputProof`` counters on the compact Workflow return (issue #25 PR3).
# Fixed contract, mirrored from workflows/src/stages.js: every key is an integer,
# and the whole object is ABSENT (not zeroed) on a run recorded before PR3 landed.
_INPUT_PROOF_FIELDS = (
    "slices", "proven", "unproven", "recovered", "rewritten", "degraded",
)

# ``stats.health`` on the compact Workflow return (issue #25 reqs 7-9), mirrored
# from reviewHealth() in workflows/src/stages.js. Same "structural read, never
# regex" and "absent means not measured, never zero" contract as
# _INPUT_PROOF_FIELDS above — kept as the JS contract's own (camelCase) key
# spelling rather than translated to snake_case, so a reader cross-referencing
# stages.js finds the same names; synthesized aggregate keys this module adds
# on top (``measured_prs``, ``degraded_prs``, ...) follow this module's own
# snake_case convention instead, matching input_proof's ``measured_prs``. Only
# the plain-integer counters are listed here; `dimensionsLost` (array) and
# `degraded`/`evidenceIsFresh` (booleans) are handled separately below.
_HEALTH_INT_FIELDS = (
    "delivered", "notChallenged", "unclassified",
    "verifySlicesDegraded", "inputUnproven", "inputRecovered",
)

# The health-degradation banner's begin sentinel (issue #25 req 7) — a literal
# copy of HEALTH_BEGIN in workflows/src/stages.js. applyHealthBanner() prepends
# this exact string to the persisted report whenever reviewHealth().degraded is
# true, and strips any stale copy (with its END pair) before recomputing on a
# resume, so a substring scan for it is a sound presence/absence signal without
# parsing markdown.
_HEALTH_BANNER_SENTINEL = "<!-- code-gauntlet:health:begin -->"

# Origins verify's classify_blame actually decided are 'new' or 'surfaced';
# anything else — INCLUDING A MISSING origin KEY — is unclassified. Mirrors
# CLASSIFIED_ORIGINS / isClassified in workflows/src/stages.js exactly, and is
# deliberately broader than the literal `origin == "unknown"` scan G3 already
# does below: that scan is what G3's existing failure fires on, but the
# banner's real production trigger (reviewHealth -> isClassified) fires on
# this wider set, so pairing the banner against only the narrower set would
# under-detect exactly the silent-degradation class issue #25 req 7 exists to
# catch.
_CLASSIFIED_ORIGINS = ("new", "surfaced")


def _is_classified(finding):
    return isinstance(finding, dict) and finding.get("origin") in _CLASSIFIED_ORIGINS


_SCRIPT_PATH_RE = re.compile(r'"scriptPath"\s*:\s*"([^"]+)"')
_DEGRADE_RE = re.compile(
    r"(no write proof|partial-artifacts|partial.artifacts)",
    re.IGNORECASE,
)

# Value of a JSON ``"script"`` field — a workflow record's echo of the whole
# pipeline bundle, whose own source carries the degrade sentinels as ordinary
# substrings (issue #52). The escape-aware body is required: a naive
# ``[^"]*`` stops at the first ``\"`` and leaves most of the bundle behind.
_SCRIPT_FIELD_RE = re.compile(r'"script"\s*:\s*"(?:\\.|[^"\\])*"', re.DOTALL)
# The same field truncated mid-write, with no closing quote.
_SCRIPT_FIELD_OPEN_RE = re.compile(r'"script"\s*:\s*"(?:\\.|[^"\\])*\Z', re.DOTALL)

# Expected pipeline entry relative to the plugin/repo root.
PIPELINE_REL = Path("workflows") / "pipeline.js"

# Where G3 looks for writer no-write-proof / partial-artifacts signals, and how
# each carrier is read (issue #52). writeArtifacts puts that gap on the compact
# Workflow return (gaps[]), which lands in collected workflows/wf_*.json and
# often in raw.json's .result text — NOT in the persisted report/checkpoint
# (those are written before the echo proof runs). Report/checkpoint remain
# scanned as secondary carriers.
#
# STRUCTURED carriers own a real ``gaps`` array, so that parsed array is the
# authoritative — and only — signal consulted; their raw bytes are never
# regex-scanned:
#   workflows/wf_*.json  carries the compact return at ``result.gaps``. It also
#       echoes the whole ~230 KB workflows/pipeline.js bundle into its
#       ``script`` field, and that bundle's source contains the sentinels as
#       ordinary substrings ("no write proof" x4 string/template literals;
#       "partial-artifacts" x6 — 1 string literal + 5 comments) — so a raw
#       scan matches on EVERY collected record, degraded or not.
#   code-gauntlet-checkpoint-all-*.json  the persisted checkpoint's ``gaps``.
#
# TEXT carriers have no ``gaps`` structure to parse, so a raw-text scan is the
# only mechanism that can ever see their signal:
#   raw.json  the child CLI's result envelope, whose ``result`` is free prose
#       (the model's final turn), never a nested ``gaps`` array. Measured over
#       the retained corpus: 0/131 carry a structured ``gaps``, and 0/131 embed
#       the bundle (max 9.5 KB — invoke.py never forwards Workflow tool-call
#       inputs here), so scanning its bytes is both necessary and safe.
#   code-gauntlet-report-*.md  markdown; there is no parse step to consult.
#
# Do not include bench-only fixture names such as deep-review-report.md.
_DEGRADE_STRUCTURED = "structured"
_DEGRADE_TEXT = "text"

_DEGRADE_CARRIER_POLICY = {
    "workflows/wf_*.json": _DEGRADE_STRUCTURED,
    "raw.json": _DEGRADE_TEXT,
    "code-gauntlet-report-*.md": _DEGRADE_TEXT,
    "code-gauntlet-checkpoint-all-*.json": _DEGRADE_STRUCTURED,
}

# Single source of truth: the scanned patterns ARE the policy's keys, so the
# two cannot desync. Scan order follows insertion order above.
_DEGRADE_SCAN_PATTERNS = tuple(_DEGRADE_CARRIER_POLICY)


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _pr_dirs(run_dir):
    """Return sorted per-PR artifact directories under ``run_dir``."""
    run_dir = Path(run_dir)
    return sorted(
        p for p in run_dir.iterdir()
        if p.is_dir() and p.name.startswith("pr-")
    )


def _iter_findings_files(pr_dir):
    return sorted(pr_dir.glob("code-gauntlet-findings-*.json"))


def _iter_workflow_records(pr_dir):
    """Return collected per-child Workflow records under ``pr_dir/workflows/``."""
    wf_dir = Path(pr_dir) / "workflows"
    if not wf_dir.is_dir():
        return []
    return sorted(wf_dir.glob("wf_*.json"))


def _findings_list(data):
    """Normalize a findings artifact to a list of finding dicts.

    Real persist output is a bare JSON list; some wrappers use ``{findings: [...]}``.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        findings = data.get("findings")
        if isinstance(findings, list):
            return findings
    return None


def _check_union_schema(finding, label):
    """Return failure strings for one finding's union-schema surface."""
    failures = []
    if not isinstance(finding, dict):
        return ["{}: finding is not an object".format(label)]
    for group in _CANONICAL_OR_ALIAS:
        if not any(finding.get(k) not in (None, "") for k in group):
            failures.append(
                "{}: missing required field group {}".format(label, "/".join(group))
            )
    if not any(finding.get(k) not in (None, "") for k in _LINE_FIELDS):
        failures.append(
            "{}: missing line identity (line_start or line)".format(label)
        )
    return failures


def _count_delivered_comments(payload):
    """Count adapter-visible delivered comments in a post-review payload."""
    platform = payload.get("platform")
    if platform == "github":
        comments = (payload.get("payload") or {}).get("comments") or []
        return len(comments)
    if platform == "gitlab":
        return len(payload.get("discussions") or [])
    return 0


def _validate_payload_fields(payload, label):
    """G1 adapter-required field checks for one payload."""
    failures = []
    platform = payload.get("platform")
    if platform not in ("github", "gitlab"):
        failures.append(
            "{}: unrecognized payload platform {!r} (expected 'github' or 'gitlab')".format(
                label, platform
            )
        )
        return failures
    if platform == "github":
        comments = (payload.get("payload") or {}).get("comments")
        if comments is None:
            failures.append("{}: github payload missing payload.comments".format(label))
            return failures
        if not isinstance(comments, list):
            failures.append("{}: github payload.comments must be a list".format(label))
            return failures
        for i, c in enumerate(comments):
            if not isinstance(c, dict):
                failures.append("{}: comment[{}] is not an object".format(label, i))
                continue
            for key in ("body", "path", "line"):
                if key not in c:
                    failures.append(
                        "{}: comment[{}] missing required field {!r}".format(label, i, key)
                    )
    else:  # gitlab
        discussions = payload.get("discussions")
        if discussions is None:
            failures.append("{}: gitlab payload missing discussions".format(label))
            return failures
        if not isinstance(discussions, list):
            failures.append("{}: gitlab discussions must be a list".format(label))
            return failures
        for i, d in enumerate(discussions):
            if not isinstance(d, dict):
                failures.append("{}: discussion[{}] is not an object".format(label, i))
                continue
            if "body" not in d:
                failures.append("{}: discussion[{}] missing body".format(label, i))
            position = d.get("position") or {}
            if "new_path" not in position or "new_line" not in position:
                failures.append(
                    "{}: discussion[{}] position missing new_path/new_line".format(label, i)
                )
    return failures


def _extract_script_paths(path):
    """Return the Workflow-tool ``scriptPath`` from a ``wf_*.json`` record.

    G4 checks plugin identity via the child Workflow invocation path
    (``workflows/pipeline.js``). Nested paths such as ``args.verify.scriptPath``
    (``scripts/verify_findings.py``) are intentionally ignored — see
    :func:`bench.runner.invoke.scriptpath_from_record`.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Partial/corrupt JSON: first top-level-ish match only (not every hit).
        m = _SCRIPT_PATH_RE.search(text)
        return [m.group(1)] if m else []
    sp = scriptpath_from_record(data)
    return [sp] if sp else []


def _extract_input_proof(path):
    """Return a ``wf_*.json`` record's ``result.stats.inputProof`` dict, or None.

    Structural only — ``json.loads`` then dict-walk, never a regex over prose
    (issue #52's lesson applies here too: this same file embeds the whole
    pipeline bundle in its ``script`` field). No fallback for a record that
    will not parse or whose shape does not match: an unreadable record is
    reported as "not measured" for this stat, the same disclose-don't-fabricate
    stance the rest of this module takes for absent/degraded signals — it must
    never be confused with a present-and-zero measurement.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    stats = result.get("stats")
    if not isinstance(stats, dict):
        return None
    proof = stats.get("inputProof")
    return proof if isinstance(proof, dict) else None


def _extract_review_health(path):
    """Return a ``wf_*.json`` record's ``result.stats.health`` dict, or None.

    Structural only, mirroring :func:`_extract_input_proof` exactly — same
    file, same embedded pipeline bundle in ``script``, same reason this is
    never a regex scan. An unreadable or misshapen record reads as "not
    measured" for this stat, never as a present-and-healthy one.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    stats = result.get("stats")
    if not isinstance(stats, dict):
        return None
    health = stats.get("health")
    return health if isinstance(health, dict) else None


def _script_path_ok(script_path, expected_pipeline, repo_root=None):
    """True when scriptPath is the repo's ``workflows/pipeline.js``.

    Delegates to the shared :func:`bench.runner.invoke.script_path_matches_repo`
    so invoke-time and smoke-check identity answers cannot drift.
    """
    if repo_root is None:
        repo_root = Path(expected_pipeline).resolve().parent.parent
    return script_path_matches_repo(
        script_path, repo_root, expected_pipeline=expected_pipeline
    )


def _check_echo_identity(pr_dir, repo_root, label):
    """Return ``(failures, identity_ok)`` for the echo identity receipt.

    ``identity_ok`` is True only when a complete receipt is present and matches
    the expected ``PIPELINE_VERSION`` + plugin root. Uses the tolerant result
    envelope parser — ``raw.json`` may carry stderr/preamble ahead of the JSON
    (``invoke_review`` merges stderr into the same file).
    """
    raw_text = ""
    envelope = None
    raw_path = Path(pr_dir) / "raw.json"
    if raw_path.is_file():
        try:
            raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_text = ""
        else:
            envelope = parse_result_envelope(raw_text)
    receipt = extract_identity_receipt(raw_text, envelope, (pr_dir,))
    if not receipt:
        return [], False
    failures = []
    expected_ver = read_pipeline_version(repo_root)
    if not expected_ver:
        failures.append(
            "{}: cannot read expected PIPELINE_VERSION from workflows/pipeline.js".format(
                label
            )
        )
        return failures, False
    if receipt.get("pipeline_version") != expected_ver:
        failures.append(
            "{}: identity pipeline_version {!r} != expected {!r}".format(
                label, receipt.get("pipeline_version"), expected_ver
            )
        )
    plugin_root = receipt.get("plugin_root")
    if not plugin_root:
        failures.append("{}: identity receipt missing plugin_root".format(label))
        return failures, False
    try:
        got_root = Path(plugin_root).resolve()
        exp_root = Path(repo_root).resolve()
    except OSError as exc:
        failures.append("{}: identity plugin_root resolve failed: {}".format(label, exc))
        return failures, False
    if got_root != exp_root:
        failures.append(
            "{}: identity plugin_root {!r} != expected {!r}".format(
                label, str(got_root), str(exp_root)
            )
        )
    return failures, (not failures)


def _iter_degrade_scan_paths(pr_dir):
    """Yield ``(path, pattern)`` for artifacts G3 scans for writer degrades.

    The pattern travels with the path so the caller can look up that carrier's
    ``_DEGRADE_CARRIER_POLICY`` entry.
    """
    pr_dir = Path(pr_dir)
    for pat in _DEGRADE_SCAN_PATTERNS:
        if "*" in pat:
            for path in sorted(pr_dir.glob(pat)):
                if path.is_file():
                    yield path, pat
        else:
            path = pr_dir / pat
            if path.is_file():
                yield path, pat


def _gaps_lists(node):
    """Yield every ``gaps`` array nested anywhere under ``node``."""
    if isinstance(node, dict):
        gaps = node.get("gaps")
        if isinstance(gaps, list):
            yield gaps
        for value in node.values():
            yield from _gaps_lists(value)
    elif isinstance(node, list):
        for value in node:
            yield from _gaps_lists(value)


def _strip_script_field(text):
    """Blank out any JSON ``"script"`` field value ahead of a raw-text scan.

    ``workflows/wf_*.json`` echoes the entire pipeline bundle into that field,
    and the bundle's own source carries the degrade sentinels as ordinary
    substrings (issue #52). Handles both a well-formed value and one
    truncated mid-write.

    Only STRUCTURED carriers get this treatment. The unterminated-field pattern
    necessarily runs to end-of-text, so applying it to a TEXT carrier could
    swallow genuine trailing prose — and TEXT carriers never embed the bundle,
    so there is nothing there to neutralize.
    """
    text = _SCRIPT_FIELD_RE.sub('"script":""', text)
    return _SCRIPT_FIELD_OPEN_RE.sub('"script":""', text)


def _scan_degrade_text(pr_dir):
    """Return basenames of artifacts carrying writer-degrade signals.

    A STRUCTURED carrier is judged by its parsed ``gaps`` array and nothing
    else: the presence of any ``gaps`` list — *including an empty one* —
    settles the question, so the healthy ``result.gaps == []`` shape reads as
    clean instead of falling through to a byte scan that the embedded pipeline
    bundle would always match (issue #52).

    A structured carrier that will not parse, or that has no ``gaps`` anywhere,
    is still scanned — reporting "clean" for an artifact we could not read
    would be a new way to lose a run — but with the bundle-bearing ``script``
    field blanked first. TEXT carriers have no structure to consult and are
    scanned as-is. See ``_DEGRADE_CARRIER_POLICY`` for the classification.
    """
    hits = []
    seen = set()
    for path, pattern in _iter_degrade_scan_paths(pr_dir):
        label = str(path.relative_to(pr_dir)) if path.is_relative_to(pr_dir) else path.name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _DEGRADE_CARRIER_POLICY[pattern] == _DEGRADE_STRUCTURED:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                data = None
            gaps_lists = list(_gaps_lists(data)) if data is not None else []
            if gaps_lists:
                matched = any(
                    _DEGRADE_RE.search(item)
                    for gaps in gaps_lists
                    for item in gaps
                    if isinstance(item, str)
                )
            else:
                matched = bool(_DEGRADE_RE.search(_strip_script_field(text)))
        else:
            matched = bool(_DEGRADE_RE.search(text))
        if matched and label not in seen:
            seen.add(label)
            hits.append(label)
    return hits


def _report_has_health_banner(pr_dir):
    """True if any persisted ``code-gauntlet-report-*.md`` under ``pr_dir``
    carries the health-degradation banner's begin sentinel.

    A markdown report has no parse step to consult (same TEXT-carrier
    reasoning as ``_DEGRADE_CARRIER_POLICY``'s report entry), so this is a
    literal-substring scan rather than a regex — the sentinel is a fixed
    string with no variable parts to match.
    """
    for path in sorted(Path(pr_dir).glob("code-gauntlet-report-*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _HEALTH_BANNER_SENTINEL in text:
            return True
    return False


def _post_review_has_health_banner(pr_dir):
    """True if any persisted ``code-gauntlet-post-review-*.json`` under
    ``pr_dir`` carries the health-degradation banner sentinel in its
    ``review_body`` field — the SECOND delivery surface (issue #25 req 7):
    ``reviewBodyOf``/``writerPayload``/``persistPlan`` in
    ``workflows/src/stages.js`` put the same banner text here so a
    ``pr_comments``-only delivery, which never shows ``report.md`` to anyone,
    still discloses degradation on the surface it actually delivers on.

    Structural (JSON-parsed, then a plain field read), matching how every
    other JSON-shaped artifact in this module is read. The bare-array shape
    (no PR identity — live-run L3 not wired) carries no ``review_body`` key at
    all; that reads as absent here, same as a file that fails to parse, never
    as an error.
    """
    for path in sorted(Path(pr_dir).glob("code-gauntlet-post-review-*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        body = data.get("review_body")
        if isinstance(body, str) and _HEALTH_BANNER_SENTINEL in body:
            return True
    return False


def _checkpoint_statuses(run_dir):
    """Map golden URL -> status from state/*.json if present."""
    state_dir = Path(run_dir) / "state"
    out = {}
    if not state_dir.is_dir():
        return out
    for path in state_dir.glob("*.json"):
        try:
            rec = _load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        url = rec.get("url")
        if url:
            out[url] = rec.get("status")
    return out


def _load_manifest(run_dir):
    """Return run.json dict or None when absent/unparseable."""
    path = Path(run_dir) / "run.json"
    if not path.is_file():
        return None
    try:
        return _load_json(path)
    except (json.JSONDecodeError, OSError):
        return None


def check_run(run_dir, *, repo_root=None, plugin_pipeline=None):
    """Run functional smoke gates against ``run_dir``.

    Returns ``{"ok": bool, "failures": [str, ...], "stats": {...}}``.
    Does not raise on gate failures — callers use ``ok`` / exit codes.

    Naive-anchor runs are rejected (they never produce Workflow ``scriptPath``
    records); callers should treat that as a usage error (exit 2).
    """
    run_dir = Path(run_dir)
    failures = []
    stats = {
        "pr_dirs": 0,
        "delivered_comments": 0,
        "findings_files": 0,
        "script_paths": 0,
        "unknown_origin": 0,
        # Broader than unknown_origin — see _CLASSIFIED_ORIGINS: every finding
        # whose origin is not 'new'/'surfaced', including one with no origin
        # key at all. Drives the G3 banner-pairing failure condition below.
        "unclassified_findings": 0,
        "workflow_records": 0,
        # Not measured until proven otherwise (see module docstring): None means
        # no wf_*.json record in this run carried result.stats.inputProof, which
        # is the honest reading for both "no records" and "pre-PR3 records".
        "input_proof": None,
        # Same "None means not measured" reading as input_proof, for
        # result.stats.health (issue #25 reqs 7-9).
        "health": None,
    }
    input_proof_totals = {k: 0 for k in _INPUT_PROOF_FIELDS}
    input_proof_measured_prs = 0
    input_proof_unmeasured_prs = 0
    health_totals = {k: 0 for k in _HEALTH_INT_FIELDS}
    health_measured_prs = 0
    health_unmeasured_prs = 0
    health_degraded_prs = 0
    health_dimensions_lost = set()

    if not run_dir.is_dir():
        return {
            "ok": False,
            "failures": ["run directory does not exist: {}".format(run_dir)],
            "stats": stats,
        }

    if repo_root is None:
        # bench/runner/check.py -> repo root is parents[2]
        repo_root = Path(__file__).resolve().parents[2]
    else:
        repo_root = Path(repo_root)
    expected_pipeline = (
        Path(plugin_pipeline) if plugin_pipeline is not None
        else repo_root / PIPELINE_REL
    )

    manifest = _load_manifest(run_dir)
    if manifest is None:
        failures.append("missing or unparseable run.json")
    elif manifest.get("anchor") == "naive":
        return {
            "ok": False,
            "failures": [
                "refused: --check applies to skill runs only "
                "(this run has anchor=naive; no Workflow scriptPath records)"
            ],
            "stats": stats,
            "refused": True,
        }

    # G1: every declared pr_urls entry must have a terminal ok checkpoint.
    # Absence of a state file means pending (crash/kill mid-run) — must fail.
    statuses = _checkpoint_statuses(run_dir)
    declared = list((manifest or {}).get("pr_urls") or [])
    if manifest is not None and not declared:
        failures.append("run.json pr_urls is empty or missing")
    for url in declared:
        status = statuses.get(url)
        if status is None:
            failures.append(
                "precondition: PR {} has no checkpoint (pending / mid-run kill)".format(url)
            )
        elif status != "ok":
            failures.append(
                "precondition: PR {} status is {!r} (want 'ok')".format(url, status)
            )
    # Also flag leftover non-ok state rows not in pr_urls (defensive).
    for url, status in sorted(statuses.items()):
        if url not in declared and status != "ok":
            failures.append(
                "precondition: PR {} status is {!r} (want 'ok')".format(url, status)
            )

    pr_dirs = _pr_dirs(run_dir)
    stats["pr_dirs"] = len(pr_dirs)
    if not pr_dirs:
        failures.append("no pr-* artifact directories found under {}".format(run_dir))
        return {"ok": False, "failures": failures, "stats": stats}

    if declared and len(pr_dirs) < len(declared):
        failures.append(
            "precondition: run.json declares {} PR(s) but only {} pr-* dir(s) exist".format(
                len(declared), len(pr_dirs)
            )
        )

    total_comments = 0

    for pr_dir in pr_dirs:
        label = pr_dir.name
        pr_unclassified = 0  # drives the G3 banner-pairing check below

        # --- G2: payload ---
        payload_path = pr_dir / "post-review-payload.json"
        if not payload_path.is_file():
            failures.append("{}: missing post-review-payload.json".format(label))
            payload = None
        else:
            try:
                payload = _load_json(payload_path)
            except (json.JSONDecodeError, OSError) as exc:
                failures.append(
                    "{}: post-review-payload.json not parseable: {}".format(label, exc)
                )
                payload = None
            else:
                failures.extend(_validate_payload_fields(payload, label))
                total_comments += _count_delivered_comments(payload)

        # --- G2: findings required + union schema ---
        findings_files = _iter_findings_files(pr_dir)
        if not findings_files:
            failures.append(
                "{}: missing code-gauntlet-findings-*.json "
                "(union-schema / origin gates require a persisted findings artifact)".format(
                    label
                )
            )
        for findings_path in findings_files:
            stats["findings_files"] += 1
            try:
                data = _load_json(findings_path)
            except (json.JSONDecodeError, OSError) as exc:
                failures.append(
                    "{}: {} not parseable: {}".format(label, findings_path.name, exc)
                )
                continue
            findings = _findings_list(data)
            if findings is None:
                failures.append(
                    "{}: {} must be a list or {{findings: [...]}}".format(
                        label, findings_path.name
                    )
                )
                continue
            for i, finding in enumerate(findings):
                flabel = "{}:{}[{}]".format(label, findings_path.name, i)
                failures.extend(_check_union_schema(finding, flabel))
                # --- G3: origin=unknown ---
                if isinstance(finding, dict) and finding.get("origin") == "unknown":
                    stats["unknown_origin"] += 1
                    failures.append(
                        "{}: origin=unknown (verify/slice degrade)".format(flabel)
                    )
                # --- G3: unclassified (broader than the literal "unknown"
                # check above — see _CLASSIFIED_ORIGINS) ---
                if not _is_classified(finding):
                    pr_unclassified += 1
                    stats["unclassified_findings"] += 1

        # --- G3: writer no-write-proof / partial-artifacts ---
        for hit in _scan_degrade_text(pr_dir):
            failures.append(
                "{}: writer degrade signal in {} (no-write-proof / partial-artifacts)".format(
                    label, hit
                )
            )

        # --- G3: unclassified findings must carry the disclosure banner ---
        # (issue #25 req 7). Reported as an additional G3 failure condition,
        # not a new gate number: the fault is identical to the origin=unknown
        # scan above — an unclassified finding shipped in the review — and
        # input_proof was kept a stat rather than a sixth gate for the
        # matching reason (a second verdict on one root cause double-counts
        # it). What is new here is the DISCLOSURE half of that same fault: G3
        # already refuses a run that ships an unclassified finding, but until
        # now nothing checked whether such a run also told anyone via the
        # report. A degraded run whose report stays silent is precisely the
        # defect req 7 exists to prevent, and it was undetectable before this.
        # This check is necessarily per-PR (findings vs. that PR's own report)
        # rather than per-finding like the loop above, so it lives here.
        #
        # EITHER delivery surface satisfies this, not BOTH. Two independent
        # reasons, not just one:
        #   1. `pr_comments` is a legal standalone delivery mode
        #      (references/headless-mode.md) and is the realistic mode for an
        #      automated bot — report.md is never shown to anyone in that
        #      mode, so requiring a banner there would check a surface nobody
        #      reads while this checker has no way to know which mode ran.
        #   2. This is not merely a hedge against the unknown: on the
        #      pipeline's own empty-report path (`bannered = !emptyReport` in
        #      stages.js, ``runWith``) the report artifact is deliberately
        #      NOT persisted and its path is nulled, so ONLY review_body
        #      carries the banner — the pipeline's own gap message says so
        #      explicitly ("banner rides on the PR review summary only").
        #      Requiring BOTH would fail that documented, correct behavior.
        if pr_unclassified > 0 and not (
            _report_has_health_banner(pr_dir) or _post_review_has_health_banner(pr_dir)
        ):
            failures.append(
                "{}: {} unclassified finding(s) (origin not 'new'/'surfaced', "
                "including a missing origin key) but neither the persisted "
                "code-gauntlet-report-*.md nor code-gauntlet-post-review-*.json "
                "review_body carries the health-degradation banner ({!r}) — a "
                "degraded review must disclose it on the surface it delivers "
                "on (issue #25 req 7)".format(label, pr_unclassified, _HEALTH_BANNER_SENTINEL)
            )

        # --- G4: plugin identity (echo receipt primary; scriptPath defense-in-depth) ---
        id_failures, identity_ok = _check_echo_identity(pr_dir, repo_root, label)
        failures.extend(id_failures)

        wf_records = _iter_workflow_records(pr_dir)
        stats["workflow_records"] += len(wf_records)
        if not wf_records:
            # A complete valid echo receipt is sufficient (matches invoke.py).
            # Without one, collected workflow records remain required.
            if not identity_ok:
                failures.append(
                    "{}: no workflows/wf_*.json records collected "
                    "(cannot verify plugin scriptPath; stale-plugin contamination "
                    "cannot be ruled out)".format(label)
                )
        else:
            script_paths = []
            for wf_path in wf_records:
                script_paths.extend(_extract_script_paths(wf_path))
            stats["script_paths"] += len(script_paths)
            if not script_paths:
                failures.append(
                    "{}: workflows/wf_*.json present but no scriptPath field found".format(
                        label
                    )
                )
            for sp in script_paths:
                if not _script_path_ok(sp, expected_pipeline, repo_root=repo_root):
                    failures.append(
                        "{}: scriptPath {!r} is not under {!r}".format(
                            label, sp, str(expected_pipeline)
                        )
                    )

        # --- input_proof: reported stat, not a gate (see module docstring) ---
        # Only PRs that produced a wf record are counted as measured/unmeasured;
        # a PR with zero wf records already failed G4 above and would otherwise
        # be double-counted here under a different name.
        if wf_records:
            pr_proof = None
            for wf_path in wf_records:
                proof = _extract_input_proof(wf_path)
                if proof is None:
                    continue
                if pr_proof is None:
                    pr_proof = {k: 0 for k in _INPUT_PROOF_FIELDS}
                for k in _INPUT_PROOF_FIELDS:
                    v = proof.get(k)
                    # Contract says every key is an integer; a producer bug that
                    # violates it must not corrupt the aggregate with a float or
                    # a bool (bool is an int subclass in Python).
                    if isinstance(v, int) and not isinstance(v, bool):
                        pr_proof[k] += v
            if pr_proof is None:
                input_proof_unmeasured_prs += 1
            else:
                input_proof_measured_prs += 1
                for k in _INPUT_PROOF_FIELDS:
                    input_proof_totals[k] += pr_proof[k]

        # --- health: reported stat, not a gate (see module docstring) ---
        # The gate-facing half of this signal is the banner-pairing check
        # above, which is derived from the findings/report directly, not from
        # this. A health object is a SNAPSHOT of the whole delivered review,
        # not a per-dispatch delta like input_proof's counters — so when a PR has
        # multiple wf_*.json records (e.g. a resumed run produced more than
        # one), this takes the LAST one that carries `stats.health` (sorted
        # glob order, same as wf_records above) rather than summing across
        # records, which would double-count a single delivered set.
        if wf_records:
            pr_health = None
            for wf_path in wf_records:
                h = _extract_review_health(wf_path)
                if h is not None:
                    pr_health = h
            if pr_health is None:
                health_unmeasured_prs += 1
            else:
                health_measured_prs += 1
                for k in _HEALTH_INT_FIELDS:
                    v = pr_health.get(k)
                    if isinstance(v, int) and not isinstance(v, bool):
                        health_totals[k] += v
                dl = pr_health.get("dimensionsLost")
                if isinstance(dl, list):
                    health_dimensions_lost.update(x for x in dl if isinstance(x, str))
                if pr_health.get("degraded") is True:
                    health_degraded_prs += 1

    stats["delivered_comments"] = total_comments
    if input_proof_measured_prs > 0:
        stats["input_proof"] = dict(input_proof_totals)
        stats["input_proof"]["measured_prs"] = input_proof_measured_prs
        stats["input_proof"]["unmeasured_prs"] = input_proof_unmeasured_prs
    if health_measured_prs > 0:
        stats["health"] = dict(health_totals)
        stats["health"]["measured_prs"] = health_measured_prs
        stats["health"]["unmeasured_prs"] = health_unmeasured_prs
        stats["health"]["degraded_prs"] = health_degraded_prs
        stats["health"]["dimensionsLost"] = sorted(health_dimensions_lost)

    # --- G5: ≥1 delivered comment across the set ---
    if total_comments < 1:
        failures.append("delivered comments across run: 0 (want ≥1)")

    return {"ok": not failures, "failures": failures, "stats": stats}


# Environment-purity receipts (Issue #23): echo identity is primary; workflow-record
# scriptPath remains defense-in-depth when records exist.
PLUGIN_IDENTITY_STRATEGY = "echo_receipt"
