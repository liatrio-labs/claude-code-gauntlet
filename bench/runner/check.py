"""Mechanical functional-smoke checker for completed bench runs (Issue #28).

Inspects a finished run directory and returns a pass/fail verdict for the
functional smoke gates. Never invokes the judge, adjudicator, or ``score_run``.

Gates (aligned with ``bench/MEASUREMENT.md``):

  G1  Completeness — every ``run.json`` ``pr_urls`` entry has terminal status ``ok``
  G2  Payload parse + adapter-required fields + union-schema findings check
      (requires ≥1 findings artifact per PR)
  G3  Zero ``origin=unknown`` findings; no writer no-write-proof / partial-artifacts
  G4  Plugin identity — Headless config echo receipts (``pipeline_version``,
      ``plugin_root``) are primary; a complete valid receipt is sufficient when
      no ``workflows/wf_*.json`` records were collected. When records exist,
      top-level Workflow ``scriptPath`` is also checked (defense-in-depth).
      Without a complete echo receipt, collected workflow records are required
      (scriptPath-only fallback).
  G5  ≥1 delivered inline comment across the run set

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

_SCRIPT_PATH_RE = re.compile(r'"scriptPath"\s*:\s*"([^"]+)"')
# Hyphen-only ``partial-artifacts``: the pipeline emits that spelling exclusively
# (stages.js / pipeline.js gap strings and one block comment). A former ``partial.artifacts``
# alternative treated ``.`` as any character and false-positived on TEXT-carrier
# prose such as "partial artifacts" (#57). G3 is the *writer* degrade gate — not
# Phase 8 timeout prose ("deliver whatever partial artifacts exist") whose
# structural signal is ``workflow-timeout`` in ``gaps[]`` when present.
_DEGRADE_RE = re.compile(
    r"(no write proof|partial-artifacts)",
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
# ``_DEGRADE_RE`` matches only the hyphenated pipeline literals (``no write
# proof``, ``partial-artifacts``). Structured carriers consult parsed ``gaps[]``
# whose values are those exact emit strings; TEXT carriers are free model prose
# over an arbitrary reviewed repo, so a broader separator class (e.g. matching
# "partial artifacts") would false-positive (#57). G3 is the *writer* degrade
# gate — Phase 8 timeout partial delivery is a different failure whose
# structural token is ``workflow-timeout`` in ``gaps[]``, not this regex.
#
# STRUCTURED carriers own a real ``gaps`` array, so that parsed array is the
# authoritative — and only — signal consulted; their raw bytes are never
# regex-scanned:
#   workflows/wf_*.json  carries the compact return at ``result.gaps``. It also
#       echoes the whole workflows/pipeline.js bundle into its
#       ``script`` field, and that bundle's source contains the sentinels as
#       ordinary substrings ("no write proof" x3 string/template literals —
#       writeArtifacts's four-path gap, writeArtifactsDerived's three-primary gap,
#       and assemble's derived-path receipt check; "partial-artifacts" x2 — 1
#       string literal + 1 block comment) — so a raw
#       scan matches on EVERY collected record, degraded or not.
#       ``workflows/superseded/`` (archived prior attempts; #85) is invisible:
#       ``_iter_workflow_records`` / degrade globs are non-recursive.
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
        p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("pr-")
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
        return [f"{label}: finding is not an object"]
    for group in _CANONICAL_OR_ALIAS:
        if not any(finding.get(k) not in (None, "") for k in group):
            failures.append(
                "{}: missing required field group {}".format(label, "/".join(group))
            )
    if not any(finding.get(k) not in (None, "") for k in _LINE_FIELDS):
        failures.append(f"{label}: missing line identity (line_start or line)")
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
    """G2 adapter-required field checks for one payload."""
    failures = []
    platform = payload.get("platform")
    if platform not in ("github", "gitlab"):
        failures.append(
            f"{label}: unrecognized payload platform {platform!r} (expected 'github' or 'gitlab')"
        )
        return failures
    if platform == "github":
        comments = (payload.get("payload") or {}).get("comments")
        if comments is None:
            failures.append(f"{label}: github payload missing payload.comments")
            return failures
        if not isinstance(comments, list):
            failures.append(f"{label}: github payload.comments must be a list")
            return failures
        for i, c in enumerate(comments):
            if not isinstance(c, dict):
                failures.append(f"{label}: comment[{i}] is not an object")
                continue
            for key in ("body", "path", "line"):
                if key not in c:
                    failures.append(
                        f"{label}: comment[{i}] missing required field {key!r}"
                    )
    else:  # gitlab
        discussions = payload.get("discussions")
        if discussions is None:
            failures.append(f"{label}: gitlab payload missing discussions")
            return failures
        if not isinstance(discussions, list):
            failures.append(f"{label}: gitlab discussions must be a list")
            return failures
        for i, d in enumerate(discussions):
            if not isinstance(d, dict):
                failures.append(f"{label}: discussion[{i}] is not an object")
                continue
            if "body" not in d:
                failures.append(f"{label}: discussion[{i}] missing body")
            position = d.get("position") or {}
            if "new_path" not in position or "new_line" not in position:
                failures.append(
                    f"{label}: discussion[{i}] position missing new_path/new_line"
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
            f"{label}: cannot read expected PIPELINE_VERSION from workflows/pipeline.js"
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
        failures.append(f"{label}: identity receipt missing plugin_root")
        return failures, False
    try:
        got_root = Path(plugin_root).resolve()
        exp_root = Path(repo_root).resolve()
    except OSError as exc:
        failures.append(f"{label}: identity plugin_root resolve failed: {exc}")
        return failures, False
    if got_root != exp_root:
        failures.append(
            f"{label}: identity plugin_root {str(got_root)!r} != expected {str(exp_root)!r}"
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
        label = (
            str(path.relative_to(pr_dir)) if path.is_relative_to(pr_dir) else path.name
        )
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
        "workflow_records": 0,
    }

    if not run_dir.is_dir():
        return {
            "ok": False,
            "failures": [f"run directory does not exist: {run_dir}"],
            "stats": stats,
        }

    if repo_root is None:
        # bench/runner/check.py -> repo root is parents[2]
        repo_root = Path(__file__).resolve().parents[2]
    else:
        repo_root = Path(repo_root)
    expected_pipeline = (
        Path(plugin_pipeline)
        if plugin_pipeline is not None
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
                f"precondition: PR {url} has no checkpoint (pending / mid-run kill)"
            )
        elif status != "ok":
            failures.append(f"precondition: PR {url} status is {status!r} (want 'ok')")
    # Also flag leftover non-ok state rows not in pr_urls (defensive).
    for url, status in sorted(statuses.items()):
        if url not in declared and status != "ok":
            failures.append(f"precondition: PR {url} status is {status!r} (want 'ok')")

    pr_dirs = _pr_dirs(run_dir)
    stats["pr_dirs"] = len(pr_dirs)
    if not pr_dirs:
        failures.append(f"no pr-* artifact directories found under {run_dir}")
        return {"ok": False, "failures": failures, "stats": stats}

    if declared and len(pr_dirs) < len(declared):
        failures.append(
            f"precondition: run.json declares {len(declared)} PR(s) but only {len(pr_dirs)} pr-* dir(s) exist"
        )

    total_comments = 0

    for pr_dir in pr_dirs:
        label = pr_dir.name

        # --- G2: payload ---
        payload_path = pr_dir / "post-review-payload.json"
        if not payload_path.is_file():
            failures.append(f"{label}: missing post-review-payload.json")
            payload = None
        else:
            try:
                payload = _load_json(payload_path)
            except (json.JSONDecodeError, OSError) as exc:
                failures.append(
                    f"{label}: post-review-payload.json not parseable: {exc}"
                )
                payload = None
            else:
                failures.extend(_validate_payload_fields(payload, label))
                total_comments += _count_delivered_comments(payload)

        # --- G2: findings required + union schema ---
        findings_files = _iter_findings_files(pr_dir)
        if not findings_files:
            failures.append(
                f"{label}: missing code-gauntlet-findings-*.json "
                "(union-schema / origin gates require a persisted findings artifact)"
            )
        for findings_path in findings_files:
            stats["findings_files"] += 1
            try:
                data = _load_json(findings_path)
            except (json.JSONDecodeError, OSError) as exc:
                failures.append(f"{label}: {findings_path.name} not parseable: {exc}")
                continue
            findings = _findings_list(data)
            if findings is None:
                failures.append(
                    f"{label}: {findings_path.name} must be a list or {{findings: [...]}}"
                )
                continue
            for i, finding in enumerate(findings):
                flabel = f"{label}:{findings_path.name}[{i}]"
                failures.extend(_check_union_schema(finding, flabel))
                # --- G3: origin=unknown ---
                if isinstance(finding, dict) and finding.get("origin") == "unknown":
                    stats["unknown_origin"] += 1
                    failures.append(f"{flabel}: origin=unknown (verify/slice degrade)")

        # --- G3: writer no-write-proof / partial-artifacts ---
        for hit in _scan_degrade_text(pr_dir):
            failures.append(
                f"{label}: writer degrade signal in {hit} (no-write-proof / partial-artifacts)"
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
                    f"{label}: no workflows/wf_*.json records collected "
                    "(cannot verify plugin scriptPath; stale-plugin contamination "
                    "cannot be ruled out)"
                )
        else:
            script_paths = []
            for wf_path in wf_records:
                script_paths.extend(_extract_script_paths(wf_path))
            stats["script_paths"] += len(script_paths)
            if not script_paths:
                failures.append(
                    f"{label}: workflows/wf_*.json present but no scriptPath field found"
                )
            for sp in script_paths:
                if not _script_path_ok(sp, expected_pipeline, repo_root=repo_root):
                    failures.append(
                        f"{label}: scriptPath {sp!r} is not under {str(expected_pipeline)!r}"
                    )

    stats["delivered_comments"] = total_comments

    # --- G5: ≥1 delivered comment across the set ---
    if total_comments < 1:
        failures.append("delivered comments across run: 0 (want ≥1)")

    return {"ok": not failures, "failures": failures, "stats": stats}


# Environment-purity receipts (Issue #23): echo identity is primary; workflow-record
# scriptPath remains defense-in-depth when records exist.
PLUGIN_IDENTITY_STRATEGY = "echo_receipt"
