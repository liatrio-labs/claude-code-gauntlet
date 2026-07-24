"""Mechanical functional-smoke checker for completed bench runs (Issue #28).

Inspects a finished run directory and returns a pass/fail verdict for the
functional smoke gates. Never invokes the judge, adjudicator, or ``score_run``.

Gates (aligned with ``bench/MEASUREMENT.md``):

  G1  Completeness — every ``run.json`` ``pr_urls`` entry has terminal status ``ok``
  G2  Payload parse + adapter-required fields + union-schema findings check
      (requires ≥1 findings artifact per PR)
  G3  Zero ``origin=unknown`` findings; no writer no-write-proof / partial-artifacts
  G4  Child ``scriptPath`` under the repo's ``workflows/pipeline.js``
      (from collected ``pr_dir/workflows/wf_*.json``, not ``raw.json``)
  G5  ≥1 delivered inline comment across the run set

Stdlib-only (CLAUDE.md).
"""

import json
import re
from pathlib import Path

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
_DEGRADE_RE = re.compile(
    r"(no write proof|partial-artifacts|partial.artifacts)",
    re.IGNORECASE,
)

# Expected pipeline entry relative to the plugin/repo root.
PIPELINE_REL = Path("workflows") / "pipeline.js"

# Artifact names written by writeArtifacts (workflows/src/stages.js plannedArtifactPaths).
# Do not include bench-only fixture names such as deep-review-report.md.
_DEGRADE_SCAN_PATTERNS = (
    "code-gauntlet-report-*.md",
    "code-gauntlet-checkpoint-all-*.json",
)


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
    (``scripts/verify_findings.py``) are intentionally ignored — a recursive
    walk would false-fail healthy skill runs.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Partial/corrupt JSON: first top-level-ish match only (not every hit).
        m = _SCRIPT_PATH_RE.search(text)
        return [m.group(1)] if m else []
    if not isinstance(data, dict):
        return []
    sp = data.get("scriptPath")
    if isinstance(sp, str) and sp:
        return [sp]
    # Some runtimes nest the tool input under a single wrapper key.
    for key in ("input", "toolInput", "parameters"):
        nested = data.get(key)
        if isinstance(nested, dict):
            sp = nested.get("scriptPath")
            if isinstance(sp, str) and sp:
                return [sp]
    return []


def _script_path_ok(script_path, expected_pipeline, repo_root=None):
    """True when scriptPath is the repo's ``workflows/pipeline.js``.

    Stale marketplace copies also end in ``workflows/pipeline.js`` but live under
    ``~/.claude/plugins/cache/...`` — those must fail. Accept only paths that
    resolve to ``expected_pipeline`` or whose normalized string equals that path
    (artifact may outlive the on-disk resolve target).
    """
    expected = Path(expected_pipeline).resolve()
    normalized = script_path.replace("\\", "/").rstrip("/")
    expected_norm = str(expected).replace("\\", "/")
    if normalized == expected_norm:
        return True
    candidate = Path(script_path)
    try:
        if candidate.is_absolute() and candidate.resolve() == expected:
            return True
    except OSError:
        pass
    # Relative form used in some records: "workflows/pipeline.js" under plugin root.
    if repo_root is not None and normalized in (
        "workflows/pipeline.js",
        "./workflows/pipeline.js",
    ):
        return True
    return False


def _scan_degrade_text(pr_dir):
    """Return matching degrade snippets from report/checkpoint artifacts."""
    hits = []
    files = []
    for pat in _DEGRADE_SCAN_PATTERNS:
        if "*" in pat:
            files.extend(pr_dir.glob(pat))
        else:
            p = pr_dir / pat
            if p.is_file():
                files.append(p)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _DEGRADE_RE.search(text):
            hits.append(path.name)
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

        # --- G3: writer no-write-proof / partial-artifacts ---
        for hit in _scan_degrade_text(pr_dir):
            failures.append(
                "{}: writer degrade signal in {} (no-write-proof / partial-artifacts)".format(
                    label, hit
                )
            )

        # --- G4: scriptPath from collected workflow records (not raw.json) ---
        wf_records = _iter_workflow_records(pr_dir)
        stats["workflow_records"] += len(wf_records)
        if not wf_records:
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

    stats["delivered_comments"] = total_comments

    # --- G5: ≥1 delivered comment across the set ---
    if total_comments < 1:
        failures.append("delivered comments across run: 0 (want ≥1)")

    return {"ok": not failures, "failures": failures, "stats": stats}


# Upgrade hook for environment-purity receipts (Issue #23): when
# pipeline_version / plugin_root appear in the headless config echo, prefer
# those over the interim workflow-record scriptPath grep above.
PLUGIN_IDENTITY_STRATEGY = "workflow_record_scriptPath"  # future: "echo_receipt"
