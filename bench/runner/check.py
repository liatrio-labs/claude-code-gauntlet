"""Mechanical functional-smoke checker for completed bench runs (Issue #28).

Inspects a finished run directory and returns a pass/fail verdict for the
functional smoke gates. Never invokes the judge, adjudicator, or ``score_run``.

Gates (smoke gates 2–5 from the measurement-policy issue):

  G1  Payload parse + adapter-required fields + union-schema findings check
  G2  Zero ``origin=unknown`` findings; no writer no-write-proof / partial-artifacts
  G3  Child ``scriptPath`` under the repo's ``workflows/pipeline.js``
  G4  ≥1 delivered inline comment across the run set

Stdlib-only (CLAUDE.md).
"""

from __future__ import annotations

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


def _findings_list(data):
    """Normalize a findings artifact to a list of finding dicts."""
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


def _extract_script_paths(raw_path):
    """Return every scriptPath string found in a raw.json artifact."""
    text = Path(raw_path).read_text(encoding="utf-8", errors="replace")
    paths = list(_SCRIPT_PATH_RE.findall(text))
    # Also walk the JSON tree when parseable — catches non-stringified nests.
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return paths
    seen = set(paths)

    def walk(node):
        if isinstance(node, dict):
            sp = node.get("scriptPath")
            if isinstance(sp, str) and sp not in seen:
                seen.add(sp)
                paths.append(sp)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return paths


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
    """Return matching degrade snippets from report/gap-bearing artifacts."""
    hits = []
    patterns = (
        "deep-review-report.md",
        "code-gauntlet-report-*.md",
        "code-gauntlet-checkpoints-*.json",
        "code-gauntlet-progress-*.json",
    )
    files = []
    for pat in patterns:
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


def check_run(run_dir, *, repo_root=None, plugin_pipeline=None):
    """Run functional smoke gates against ``run_dir``.

    Returns ``{"ok": bool, "failures": [str, ...], "stats": {...}}``.
    Does not raise on gate failures — callers use ``ok`` / exit codes.
    """
    run_dir = Path(run_dir)
    failures = []
    stats = {
        "pr_dirs": 0,
        "delivered_comments": 0,
        "findings_files": 0,
        "script_paths": 0,
        "unknown_origin": 0,
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

    # Precondition: every checkpointed PR is ok (when state exists).
    statuses = _checkpoint_statuses(run_dir)
    for url, status in sorted(statuses.items()):
        if status != "ok":
            failures.append(
                "precondition: PR {} status is {!r} (want 'ok')".format(url, status)
            )

    pr_dirs = _pr_dirs(run_dir)
    stats["pr_dirs"] = len(pr_dirs)
    if not pr_dirs:
        failures.append("no pr-* artifact directories found under {}".format(run_dir))
        return {"ok": False, "failures": failures, "stats": stats}

    total_comments = 0

    for pr_dir in pr_dirs:
        label = pr_dir.name

        # --- G1: payload ---
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

        # --- G1: findings union schema (when present) ---
        for findings_path in _iter_findings_files(pr_dir):
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
                # --- G2: origin=unknown ---
                if isinstance(finding, dict) and finding.get("origin") == "unknown":
                    stats["unknown_origin"] += 1
                    failures.append(
                        "{}: origin=unknown (verify/slice degrade)".format(flabel)
                    )

        # --- G2: writer no-write-proof / partial-artifacts ---
        for hit in _scan_degrade_text(pr_dir):
            failures.append(
                "{}: writer degrade signal in {} (no-write-proof / partial-artifacts)".format(
                    label, hit
                )
            )

        # --- G3: scriptPath identity ---
        raw_path = pr_dir / "raw.json"
        if not raw_path.is_file():
            failures.append(
                "{}: missing raw.json (cannot verify plugin scriptPath)".format(label)
            )
        else:
            script_paths = _extract_script_paths(raw_path)
            stats["script_paths"] += len(script_paths)
            if not script_paths:
                failures.append(
                    "{}: no scriptPath found in raw.json "
                    "(stale-plugin contamination cannot be ruled out)".format(label)
                )
            for sp in script_paths:
                if not _script_path_ok(sp, expected_pipeline, repo_root=repo_root):
                    failures.append(
                        "{}: scriptPath {!r} is not under {!r}".format(
                            label, sp, str(expected_pipeline)
                        )
                    )

    stats["delivered_comments"] = total_comments

    # --- G4: ≥1 delivered comment across the set ---
    if total_comments < 1:
        failures.append(
            "delivered comments across run: 0 (want ≥1)"
        )

    return {"ok": not failures, "failures": failures, "stats": stats}


# Upgrade hook for environment-purity receipts (Issue #23): when
# pipeline_version / plugin_root appear in the headless config echo, prefer
# those over the interim scriptPath grep above. Keep this constant as the
# documented switch point for that swap.
PLUGIN_IDENTITY_STRATEGY = "scriptPath_grep"  # future: "echo_receipt"
