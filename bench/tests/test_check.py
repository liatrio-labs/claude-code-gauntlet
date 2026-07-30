"""Tests for the mechanical functional-smoke checker (Issue #28).

stdlib only, no network, no judge. Fixtures mirror real harness artifact names:
bare findings lists, ``workflows/wf_*.json`` (not fabricated ``raw.json`` tool_uses),
``code-gauntlet-checkpoint-all-*.json``, and ``run.json`` ``pr_urls`` completeness.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench import run  # noqa: E402
from bench.runner import check, invoke  # noqa: E402

PIPELINE = str(REPO_ROOT / "workflows" / "pipeline.js")


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _ok_payload(n_comments=1):
    comments = [
        {"path": "a.py", "line": 10 + i, "body": "finding {}".format(i)}
        for i in range(n_comments)
    ]
    return {
        "platform": "github",
        "endpoint": "repos/o/r/pulls/1/reviews",
        "method": "POST",
        "payload": {
            "event": "COMMENT",
            "body": "deep-review (dry-run)",
            "comments": comments,
        },
        "skipped": [],
    }


def _ok_gitlab_payload(n_discussions=1):
    """Minimal GitLab dry-run payload shape (platform + discussions)."""
    discussions = [
        {
            "body": "finding {}".format(i),
            "position": {
                "position_type": "text",
                "new_path": "a.py",
                "new_line": 10 + i,
                "old_path": "a.py",
            },
        }
        for i in range(n_discussions)
    ]
    return {
        "platform": "gitlab",
        "discussions": discussions,
        "skipped": [],
    }


def _ok_finding(origin="new"):
    # Real persist output is a bare list of union-schema findings.
    return {
        "id": "f1",
        "title": "Null deref",
        "description": "Possible null dereference.",
        "body": "Possible null dereference.",
        "dimension": "bug",
        "severity": "high",
        "confidence": 0.9,
        "file": "a.py",
        "line_start": 10,
        "line_end": 10,
        "line": 10,
        "end_line": 10,
        "origin": origin,
        "cross_file_refs": [],
        "report_destination": "inline",
    }


def _wf_record(script_path=PIPELINE, *, include_verify=True):
    """Shape of a per-child Workflow record (the real scriptPath carrier).

    Real skill runs also persist ``args.verify.scriptPath`` → verify_findings.py;
    G4 must ignore that nested path and only grade the top-level Workflow path.
    """
    rec = {
        "runId": "wf_test-0001",
        "scriptPath": script_path,
        "status": "completed",
    }
    if include_verify:
        # Absolute form mirrors SKILL.md's ``{plugin_root}/scripts/verify_findings.py``.
        rec["args"] = {
            "verify": {
                "scriptPath": str(REPO_ROOT / "scripts" / "verify_findings.py"),
                "inputPathBase": "/tmp/in",
                "outputPathBase": "/tmp/out",
            }
        }
    return rec


def _wf_record_with_input_proof(input_proof=None, script_path=PIPELINE, *, timestamp=None):
    """A wf record carrying a compact-return ``result.stats.inputProof`` block
    (issue #25 PR3). ``input_proof=None`` omits ``stats.inputProof`` entirely,
    modeling a record recorded before PR3 landed — the "not measured" case.
    ``timestamp`` sets the record's own top-level ``timestamp`` field (the
    field ``_select_pr_input_proof_snapshot`` orders candidate records by —
    same issue #85 currency problem as health).
    """
    rec = _wf_record(script_path)
    stats = {}
    if input_proof is not None:
        stats["inputProof"] = input_proof
    rec["result"] = {"ok": True, "gaps": [], "stats": stats}
    if timestamp is not None:
        rec["timestamp"] = timestamp
    return rec


def _wf_record_with_health(health=None, script_path=PIPELINE, *, timestamp=None):
    """A wf record carrying a compact-return ``result.stats.health`` block
    (issue #25 reqs 7-9). ``health=None`` omits ``stats.health`` entirely,
    modeling a record recorded before this landed — the "not measured" case.
    ``timestamp`` sets the record's own top-level ``timestamp`` field (the
    field ``_select_pr_health_snapshot`` orders candidate records by, since
    ``wf_*.json`` filenames are random and glob order is not chronological —
    issue #85); omitted by default, matching a record with no usable one.
    """
    rec = _wf_record(script_path)
    stats = {}
    if health is not None:
        stats["health"] = health
    rec["result"] = {"ok": True, "gaps": [], "stats": stats}
    if timestamp is not None:
        rec["timestamp"] = timestamp
    return rec


HEALTH_BANNER_SENTINEL = "<!-- code-gauntlet:health:begin -->"


def _report_with_banner(body="# Report\n\nAll good.\n"):
    """A persisted report body carrying the health-degradation banner
    sentinel, mirroring what applyHealthBanner() prepends in stages.js. Only
    the begin sentinel matters to the checker (see _report_has_health_banner),
    but both are included for fidelity to the real shape.
    """
    return (
        "{}\n"
        "> [!WARNING]\n"
        "> ## This review is degraded\n"
        "<!-- code-gauntlet:health:end -->\n\n"
        "{}"
    ).format(HEALTH_BANNER_SENTINEL, body)


def _banner_review_body():
    """The banner text as it rides review_body — same sentinel, no report
    wrapper prose needed since only the sentinel substring is checked.
    """
    return _report_with_banner(body="")


def _write_post_review(pr_dir, review_body=None, *, sha="deadbeef", with_identity=True,
                       health_banner=None):
    """A persisted ``code-gauntlet-post-review-*.json`` — the SECOND delivery
    surface (issue #25 req 7). ``with_identity=True`` writes the PR-identity
    wrapper shape ``{owner, repo, pr_number, sha, health_banner, review_body,
    findings}`` (live-run L3); ``with_identity=False`` writes the bare-array
    shape a run with no PR identity persists, which carries neither field.

    ``health_banner`` is where the pipeline writes the banner today;
    ``review_body`` is the caller's own narrative slot and the pre-split
    carrier. Both are settable here because the check must find the banner in
    either — see ``_HEALTH_BANNER_FIELDS``.
    """
    path = Path(pr_dir) / "code-gauntlet-post-review-{}.json".format(sha)
    if with_identity:
        _write_json(
            path,
            {
                "owner": "example", "repo": "repo", "pr_number": 1, "sha": sha,
                "health_banner": health_banner or "",
                "review_body": review_body or "", "findings": [],
            },
        )
    else:
        _write_json(path, [])


def _identity_echo_block(*, plugin_root=None, pipeline_version=None):
    """Headless config identity lines for raw.json / report carriers."""
    root = str(plugin_root if plugin_root is not None else REPO_ROOT)
    ver = pipeline_version
    if ver is None:
        ver = invoke.read_pipeline_version(REPO_ROOT)
    return (
        "Review complete.\n\n"
        "Headless config:\n"
        "  pipeline_version={} (bundle)\n"
        "  plugin_root={} (resolved)\n"
    ).format(ver, root)


def _plant_raw_identity(pr_dir, *, plugin_root=None, pipeline_version=None):
    raw_path = Path(pr_dir) / "raw.json"
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    data["result"] = _identity_echo_block(
        plugin_root=plugin_root,
        pipeline_version=pipeline_version,
    )
    _write_json(raw_path, data)


def _build_ok_run(
    run_dir,
    *,
    n_prs=1,
    n_comments=1,
    origin="new",
    script_path=PIPELINE,
    pr_urls=None,
    completed_prs=None,
    include_findings=True,
    include_workflow=True,
):
    """Populate ``run_dir`` with a checker-passing synthetic run (realistic names).

    ``pr_urls`` is what ``run.json`` declares. ``completed_prs`` (default: all of
    ``pr_urls``) controls which PRs get state files + artifact dirs — use a shorter
    list to model a mid-run kill.
    """
    urls = pr_urls
    if urls is None:
        urls = [
            "https://github.com/example/repo/pull/{}".format(i + 1) for i in range(n_prs)
        ]
    done = list(urls if completed_prs is None else completed_prs)
    _write_json(
        run_dir / "run.json",
        {
            "run_id": run_dir.name,
            "tier": "smoke",
            "anchor": None,
            "pr_urls": list(urls),
        },
    )
    state = run_dir / "state"
    state.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(done):
        _write_json(
            state / "pr-{}.json".format(i + 1),
            {"url": url, "status": "ok", "detail": {}, "ts": "2026-07-23T00:00:00Z"},
        )
        pr_dir = run_dir / "pr-example-repo-{}".format(i + 1)
        pr_dir.mkdir(parents=True, exist_ok=True)
        _write_json(pr_dir / "post-review-payload.json", _ok_payload(n_comments))
        if include_findings:
            # Bare list — the real writeArtifacts shape.
            _write_json(
                pr_dir / "code-gauntlet-findings-deadbeef.json",
                [_ok_finding(origin=origin)],
            )
        if include_workflow:
            _write_json(pr_dir / "workflows" / "wf_test-0001.json", _wf_record(script_path))
        # Result envelope only — no tool_uses / scriptPath (matches production raw.json).
        _write_json(
            pr_dir / "raw.json",
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Review complete.",
                "total_cost_usd": 1.0,
                "session_id": "fake-session-0001",
            },
        )
        (pr_dir / "code-gauntlet-report-deadbeef.md").write_text(
            "# Report\n\nAll good.\n", encoding="utf-8"
        )
        _write_json(
            pr_dir / "code-gauntlet-checkpoint-all-deadbeef.json",
            {"phases": {}, "gaps": []},
        )


class CheckRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-check-")
        self.run_dir = Path(self.tmp) / "smoke-20260723-000000-abc1234"
        self.run_dir.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_happy_path_passes(self):
        _build_ok_run(self.run_dir)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["failures"], [])
        self.assertGreaterEqual(result["stats"]["delivered_comments"], 1)
        self.assertGreaterEqual(result["stats"]["workflow_records"], 1)

    def test_gitlab_payload_happy_path_passes(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "post-review-payload.json",
            _ok_gitlab_payload(n_discussions=2),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["stats"]["delivered_comments"], 2)

    def test_gitlab_payload_missing_position_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_gitlab_payload()
        del bad["discussions"][0]["position"]["new_path"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "post-review-payload.json",
            bad,
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("new_path/new_line" in f for f in result["failures"])
        )

    def test_dict_wrapped_findings_accepted(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            {"findings": [_ok_finding()]},
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_bare_list_unknown_origin_fails_g3(self):
        _build_ok_run(self.run_dir, origin="unknown")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("origin=unknown" in f for f in result["failures"]))

    def test_missing_payload_fails_g2(self):
        _build_ok_run(self.run_dir)
        (self.run_dir / "pr-example-repo-1" / "post-review-payload.json").unlink()
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing post-review-payload" in f for f in result["failures"]))

    def test_missing_findings_fails_g2(self):
        _build_ok_run(self.run_dir, include_findings=False)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("missing code-gauntlet-findings" in f for f in result["failures"])
        )
        self.assertEqual(result["stats"]["findings_files"], 0)

    def test_partial_run_missing_checkpoint_fails_g1(self):
        # run.json declares 3 PRs; only 1 has state + artifacts (mid-run kill).
        urls = [
            "https://github.com/example/repo/pull/1",
            "https://github.com/example/repo/pull/2",
            "https://github.com/example/repo/pull/3",
        ]
        _build_ok_run(self.run_dir, pr_urls=urls, completed_prs=urls[:1])
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no checkpoint" in f for f in result["failures"]))
        self.assertTrue(any("declares 3 PR" in f for f in result["failures"]))

    def test_checkpoint_all_degrade_fails_g3(self):
        _build_ok_run(self.run_dir)
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-checkpoint-all-deadbeef.json",
            {
                "gaps": [
                    "writeArtifacts: writer echo did not account for all four "
                    "planned artifact paths (no write proof) — artifacts not "
                    "persisted (partial-artifacts)"
                ]
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("checkpoint-all" in f and "no-write-proof" in f for f in result["failures"])
        )

    def test_report_degrade_fails_g3(self):
        _build_ok_run(self.run_dir)
        report = (
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-report-deadbeef.md"
        )
        report.write_text(
            "# Report\n\ngaps: writeArtifacts: no write proof — partial-artifacts\n",
            encoding="utf-8",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("code-gauntlet-report" in f for f in result["failures"]))

    def test_workflow_return_partial_artifacts_gap_fails_g3(self):
        """writeArtifacts gaps land on the compact return, not report/checkpoint."""
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        # Clean secondary carriers so only the compact-return path can fire.
        (pr / "code-gauntlet-report-deadbeef.md").write_text("# Report\n", encoding="utf-8")
        _write_json(pr / "code-gauntlet-checkpoint-all-deadbeef.json", {"phases": {}})
        _write_json(
            pr / "workflows" / "wf_test-0001.json",
            {
                "runId": "wf_test-0001",
                "scriptPath": PIPELINE,
                "status": "completed",
                "result": {
                    "ok": True,
                    "partial": True,
                    "artifactPaths": {
                        "findings": None,
                        "report": None,
                        "postReview": None,
                        "checkpoints": None,
                    },
                    "gaps": [
                        "writeArtifacts: writer echo did not account for all four "
                        "planned artifact paths (no write proof) — artifacts not "
                        "persisted (partial-artifacts)"
                    ],
                },
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("workflows/wf_" in f and "partial-artifacts" in f for f in result["failures"])
        )

    def test_raw_json_result_partial_artifacts_gap_fails_g3(self):
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        (pr / "code-gauntlet-report-deadbeef.md").write_text("# Report\n", encoding="utf-8")
        _write_json(pr / "code-gauntlet-checkpoint-all-deadbeef.json", {"phases": {}})
        # Skill often echoes the compact return into the envelope .result text.
        _write_json(
            pr / "raw.json",
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": (
                    "Review complete. Workflow return: "
                    '{"ok":true,"gaps":["writeArtifacts: no write proof — '
                    'artifacts not persisted (partial-artifacts)"]}'
                ),
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("raw.json" in f and "partial-artifacts" in f for f in result["failures"]))

    def _clean_secondary_carriers(self, pr):
        """Blank the report/checkpoint carriers so only the wf record can fire."""
        (pr / "code-gauntlet-report-deadbeef.md").write_text("# Report\n", encoding="utf-8")
        _write_json(pr / "code-gauntlet-checkpoint-all-deadbeef.json", {"phases": {}})

    def test_wf_script_field_bundle_literals_do_not_fail_g3(self):
        """Issue #52: a wf record echoes the whole pipeline bundle into its
        ``script`` field, and that bundle's source carries the degrade
        sentinels as ordinary substrings (literals and comments). A run whose
        ``result.gaps`` is clean must not be flagged because of them.
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        self._clean_secondary_carriers(pr)
        _write_json(
            pr / "workflows" / "wf_test-0001.json",
            {
                "runId": "wf_test-0001",
                "scriptPath": PIPELINE,
                "status": "completed",
                # Escaped quotes matter: a naive "script"\s*:\s*"[^"]*" stops at
                # the first \" and leaves the rest of the bundle in the scan.
                "script": (
                    'function writeArtifacts() {\n'
                    '  return fail("writer echo did not cover all three primary '
                    'artifact paths (no write proof)");\n'
                    '}\n'
                    'const note = "he said \\"partial-artifacts\\" and moved on";\n'
                    'gaps.push("artifacts not persisted (partial-artifacts)");\n'
                ),
                "result": {
                    "ok": True,
                    "partial": False,
                    "artifactPaths": {
                        "findings": "code-gauntlet-findings-deadbeef.json",
                        "report": "code-gauntlet-report-deadbeef.md",
                        "postReview": "code-gauntlet-post-review-deadbeef.json",
                        "checkpoints": "code-gauntlet-checkpoint-all-deadbeef.json",
                    },
                    "gaps": [],
                },
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["failures"], [])

    def test_wf_script_field_bundle_literals_with_genuine_gap_fails_g3(self):
        """The same contaminated ``script`` field, but a genuine degrade in
        ``result.gaps`` — the fix must not overcorrect into a false negative.
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        self._clean_secondary_carriers(pr)
        _write_json(
            pr / "workflows" / "wf_test-0001.json",
            {
                "runId": "wf_test-0001",
                "scriptPath": PIPELINE,
                "status": "completed",
                "script": (
                    'const note = "he said \\"partial-artifacts\\" and moved on";\n'
                    'gaps.push("(no write proof)");\n'
                ),
                "result": {
                    "ok": True,
                    "partial": True,
                    "artifactPaths": {
                        "findings": None,
                        "report": None,
                        "postReview": None,
                        "checkpoints": None,
                    },
                    "gaps": [
                        "writeArtifacts: writer echo did not account for all four "
                        "planned artifact paths (no write proof) — artifacts not "
                        "persisted (partial-artifacts)"
                    ],
                },
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("workflows/wf_" in f and "partial-artifacts" in f for f in result["failures"])
        )

    def test_unparseable_wf_record_bundle_literals_only_does_not_fail_g3(self):
        """A wf record truncated mid-write has no structure to consult, so it
        falls back to a raw-text scan — with the bundle-bearing ``script``
        field blanked first, including when its closing quote never arrived.
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        self._clean_secondary_carriers(pr)
        wf_path = pr / "workflows" / "wf_test-0001.json"
        wf_path.parent.mkdir(parents=True, exist_ok=True)
        wf_path.write_text(
            '{\n'
            '  "runId": "wf_test-0001",\n'
            '  "scriptPath": ' + json.dumps(PIPELINE) + ',\n'
            '  "script": "const note = \\"partial-artifacts\\";\\n'
            'return fail(\\"... (no write proof)\\");\\n',
            encoding="utf-8",
        )
        with open(wf_path, encoding="utf-8") as fh:
            self.assertRaises(json.JSONDecodeError, json.load, fh)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(
            any("writer degrade" in f for f in result["failures"]), result["failures"]
        )

    def test_unparseable_wf_record_with_genuine_gap_fails_g3(self):
        """Same truncated record, but the degrade phrase survives outside the
        ``script`` field — the raw-text fallback must still catch it.
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        self._clean_secondary_carriers(pr)
        wf_path = pr / "workflows" / "wf_test-0001.json"
        wf_path.parent.mkdir(parents=True, exist_ok=True)
        wf_path.write_text(
            '{\n'
            '  "runId": "wf_test-0001",\n'
            '  "script": "const note = \\"harmless\\";\\n",\n'
            '  "result": {\n'
            '    "gaps": ["writeArtifacts: writer echo did not cover all three '
            'primary artifact paths (no write proof) — artifacts not persisted '
            '(partial-artifacts)"',
            encoding="utf-8",
        )
        with open(wf_path, encoding="utf-8") as fh:
            self.assertRaises(json.JSONDecodeError, json.load, fh)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("workflows/wf_" in f and "partial-artifacts" in f for f in result["failures"])
        )

    def test_structured_carrier_clean_gaps_ignores_stray_bytes(self):
        """For a structured carrier the parsed ``gaps`` array is authoritative:
        a sentinel elsewhere in the document does not make the run degraded.
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr / "code-gauntlet-checkpoint-all-deadbeef.json",
            {
                "gaps": [],
                "phases": {
                    "challenge": {
                        "note": "challenger quoted 'partial-artifacts' from the source"
                    }
                },
            },
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_text_carrier_keeps_prose_after_truncated_script_field(self):
        """A TEXT carrier's bytes are scanned as-is (issue #52).

        Script-blanking is for structured carriers only: the unterminated-field
        pattern runs to end-of-text, so applying it here would swallow the
        genuine prose that follows a truncated ``script`` field.
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        (pr / "raw.json").write_text(
            '{"type": "result", "meta": {"script": "a snippet that never closes\n'
            "MEANWHILE elsewhere in the prose: no write proof for findings.json\n",
            encoding="utf-8",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("raw.json" in f and "no-write-proof" in f for f in result["failures"]),
            result["failures"],
        )

    def test_degrade_carrier_policy_covers_every_scan_pattern(self):
        """The scanned patterns and their policies cannot desync (issue #52)."""
        self.assertEqual(
            set(check._DEGRADE_SCAN_PATTERNS), set(check._DEGRADE_CARRIER_POLICY)
        )
        self.assertTrue(check._DEGRADE_SCAN_PATTERNS)
        for pattern in check._DEGRADE_SCAN_PATTERNS:
            self.assertIn(
                check._DEGRADE_CARRIER_POLICY[pattern],
                (check._DEGRADE_STRUCTURED, check._DEGRADE_TEXT),
            )

    def test_stale_marketplace_script_path_fails_g4(self):
        _build_ok_run(
            self.run_dir,
            script_path="/home/user/.claude/plugins/cache/stale/workflows/pipeline.js",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("scriptPath" in f for f in result["failures"]))

    def test_g4_echo_identity_mismatch_fails(self):
        """Clean scriptPath but stale identity receipt in raw.json .result fails G4."""
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        _plant_raw_identity(
            pr,
            plugin_root="/home/user/.claude/plugins/cache/stale",
            pipeline_version="0.0.1",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                "plugin_root" in f
                or "identity" in f.lower()
                or "pipeline_version" in f
                for f in result["failures"]
            ),
            result["failures"],
        )
        self.assertFalse(any("scriptPath" in f for f in result["failures"]))

    def test_g4_clean_echo_and_clean_scriptpath_passes(self):
        _build_ok_run(self.run_dir)
        _plant_raw_identity(self.run_dir / "pr-example-repo-1")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        identity_failures = [
            f
            for f in result["failures"]
            if "scriptPath" in f
            or "plugin_root" in f
            or "pipeline_version" in f
            or "identity" in f.lower()
        ]
        self.assertEqual(identity_failures, [])

    def test_g4_clean_echo_stale_scriptpath_still_fails(self):
        """Defense-in-depth: clean identity echo must not override stale scriptPath."""
        stale = "/home/user/.claude/plugins/cache/stale/workflows/pipeline.js"
        _build_ok_run(self.run_dir, script_path=stale)
        _plant_raw_identity(self.run_dir / "pr-example-repo-1")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"], result["failures"])
        self.assertTrue(
            any("scriptPath" in f for f in result["failures"]),
            result["failures"],
        )
        identity_failures = [
            f
            for f in result["failures"]
            if "plugin_root" in f
            or "pipeline_version" in f
            or "identity" in f.lower()
        ]
        self.assertEqual(identity_failures, [], result["failures"])

    def test_nested_verify_script_path_ignored_by_g4(self):
        """Healthy runs carry args.verify.scriptPath → verify_findings.py; must pass."""
        _build_ok_run(self.run_dir)
        # Explicitly assert the fixture planted a nested non-pipeline scriptPath.
        wf = self.run_dir / "pr-example-repo-1" / "workflows" / "wf_test-0001.json"
        data = json.loads(wf.read_text(encoding="utf-8"))
        nested = data["args"]["verify"]["scriptPath"]
        self.assertTrue(nested.endswith("verify_findings.py"), nested)
        self.assertNotEqual(nested, data["scriptPath"])
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        # Extractor returns only the Workflow invocation path.
        extracted = check._extract_script_paths(wf)
        self.assertEqual(extracted, [PIPELINE])

    def test_extract_script_paths_skips_nested_under_args(self):
        wf = self.run_dir / "wf_nested.json"
        _write_json(
            wf,
            {
                "scriptPath": PIPELINE,
                "args": {
                    "verify": {"scriptPath": "/plugin/scripts/verify_findings.py"},
                    "other": {"scriptPath": "/should/ignore.js"},
                },
            },
        )
        self.assertEqual(check._extract_script_paths(wf), [PIPELINE])

    def test_extract_script_paths_accepts_wrapped_tool_input(self):
        wf = self.run_dir / "wf_wrapped.json"
        _write_json(
            wf,
            {
                "runId": "wf_wrap",
                "input": {
                    "scriptPath": PIPELINE,
                    "args": {
                        "verify": {"scriptPath": "/plugin/scripts/verify_findings.py"},
                    },
                },
            },
        )
        self.assertEqual(check._extract_script_paths(wf), [PIPELINE])

    def test_missing_workflow_records_fails_g4(self):
        """Without an echo identity receipt, missing wf records still hard-fail G4."""
        _build_ok_run(self.run_dir, include_workflow=False)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no workflows/wf_" in f for f in result["failures"]))
        # raw.json must NOT be treated as a scriptPath source.
        self.assertFalse(any("raw.json" in f and "scriptPath" in f for f in result["failures"]))

    def test_g4_valid_echo_without_wf_records_passes(self):
        """Complete valid echo receipt is sufficient when no wf records were collected."""
        _build_ok_run(self.run_dir, include_workflow=False)
        _plant_raw_identity(self.run_dir / "pr-example-repo-1")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertFalse(any("no workflows/wf_" in f for f in result["failures"]))

    def test_g4_echo_identity_reads_raw_json_with_preamble(self):
        """raw.json may carry stderr/preamble; tolerant parse must still find the receipt."""
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        raw_path = pr / "raw.json"
        envelope = json.loads(raw_path.read_text(encoding="utf-8"))
        envelope["result"] = _identity_echo_block(
            plugin_root="/home/user/.claude/plugins/cache/stale",
            pipeline_version="0.0.1",
        )
        # Merge-style file: CLI warning text ahead of the result envelope.
        raw_path.write_text(
            "warn: background noise from child CLI\n" + json.dumps(envelope) + "\n",
            encoding="utf-8",
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"], result["failures"])
        self.assertTrue(
            any(
                "plugin_root" in f
                or "identity" in f.lower()
                or "pipeline_version" in f
                for f in result["failures"]
            ),
            result["failures"],
        )

    # --- input_proof: reported stat, not a gate (issue #25 PR3) ---

    def test_input_proof_present_nonzero_aggregates(self):
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr / "workflows" / "wf_test-0001.json",
            _wf_record_with_input_proof({
                "slices": 4, "proven": 2, "unproven": 0,
                "recovered": 1, "rewritten": 1, "degraded": 1,
            }),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(
            result["stats"]["input_proof"],
            {
                "slices": 4, "proven": 2, "unproven": 0,
                "recovered": 1, "rewritten": 1, "degraded": 1,
                "measured_prs": 1, "unmeasured_prs": 0,
            },
        )

    def test_input_proof_present_all_zero_is_distinct_from_absent(self):
        """An all-zero measured object must not collapse into "not measured"."""
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr / "workflows" / "wf_test-0001.json",
            _wf_record_with_input_proof({
                "slices": 0, "proven": 0, "unproven": 0,
                "recovered": 0, "rewritten": 0, "degraded": 0,
            }),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(
            result["stats"]["input_proof"],
            {
                "slices": 0, "proven": 0, "unproven": 0,
                "recovered": 0, "rewritten": 0, "degraded": 0,
                "measured_prs": 1, "unmeasured_prs": 0,
            },
        )

    def test_input_proof_absent_reports_not_measured(self):
        """A pre-PR3 run (no ``result.stats.inputProof`` anywhere) must report
        ``None`` — never a zeroed dict, which would claim a measurement that
        never happened.
        """
        _build_ok_run(self.run_dir)  # default wf record carries no `result` key
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertIsNone(result["stats"]["input_proof"])

    def test_input_proof_mixed_measured_and_unmeasured_prs(self):
        """One PR measured, one not — the run-level aggregate must reflect
        only the measured PR's counters while still surfacing the split.
        """
        urls = [
            "https://github.com/example/repo/pull/1",
            "https://github.com/example/repo/pull/2",
        ]
        _build_ok_run(self.run_dir, pr_urls=urls)
        pr1 = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr1 / "workflows" / "wf_test-0001.json",
            _wf_record_with_input_proof({
                "slices": 3, "proven": 3, "unproven": 0,
                "recovered": 0, "rewritten": 0, "degraded": 0,
            }),
        )
        # pr-example-repo-2 keeps the default _wf_record() (no `result` at
        # all) planted by _build_ok_run — the "not measured" PR.
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(
            result["stats"]["input_proof"],
            {
                "slices": 3, "proven": 3, "unproven": 0,
                "recovered": 0, "rewritten": 0, "degraded": 0,
                "measured_prs": 1, "unmeasured_prs": 1,
            },
        )

    def test_extract_input_proof_returns_none_for_missing_stats(self):
        wf = self.run_dir / "wf_no_stats.json"
        _write_json(
            wf,
            {"runId": "wf_x", "scriptPath": PIPELINE, "result": {"ok": True, "gaps": []}},
        )
        self.assertIsNone(check._extract_input_proof(wf))

    def test_extract_input_proof_returns_dict_when_present(self):
        wf = self.run_dir / "wf_with_stats.json"
        proof = {
            "slices": 2, "proven": 2, "unproven": 0,
            "recovered": 0, "rewritten": 0, "degraded": 0,
        }
        _write_json(
            wf,
            {"runId": "wf_x", "scriptPath": PIPELINE, "result": {"stats": {"inputProof": proof}}},
        )
        self.assertEqual(check._extract_input_proof(wf), proof)

    def test_input_proof_uses_newest_record_not_sum_of_retries(self):
        """Each wf record's inputProof is a full verify-stage snapshot. Summing
        every record on a retried PR double-counts; pick the newest by
        timestamp, same currency fix as health (issue #85).
        """
        _build_ok_run(self.run_dir)
        pr = self.run_dir / "pr-example-repo-1"
        # Alphabetically-last filename is the OLDER attempt — glob order must
        # not win. Precondition so the fixture cannot quietly stop exercising
        # the bug if filenames change.
        old_name = "wf_z_sorts_last_but_is_oldest.json"
        new_name = "wf_a_sorts_first_but_is_newest.json"
        self.assertGreater(old_name, new_name)
        _write_json(
            pr / "workflows" / old_name,
            _wf_record_with_input_proof(
                {"slices": 4, "proven": 1, "unproven": 0,
                 "recovered": 0, "rewritten": 0, "degraded": 3},
                timestamp="2026-07-29T18:00:00Z",
            ),
        )
        _write_json(
            pr / "workflows" / new_name,
            _wf_record_with_input_proof(
                {"slices": 4, "proven": 4, "unproven": 0,
                 "recovered": 1, "rewritten": 0, "degraded": 0},
                timestamp="2026-07-29T19:30:00Z",
            ),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(
            result["stats"]["input_proof"],
            {
                "slices": 4, "proven": 4, "unproven": 0,
                "recovered": 1, "rewritten": 0, "degraded": 0,
                "measured_prs": 1, "unmeasured_prs": 0,
            },
        )

    # --- G3 banner-pairing: unclassified findings must be disclosed ---
    # (issue #25 req 7)

    def test_unclassified_finding_with_banner_passes(self):
        """An unclassified finding (origin present but not 'new'/'surfaced',
        and deliberately NOT the literal 'unknown' G3 already scans for) whose
        report DOES carry the health banner must not fail — the run discloses
        exactly what it should.
        """
        _build_ok_run(self.run_dir, origin="stale")
        report = self.run_dir / "pr-example-repo-1" / "code-gauntlet-report-deadbeef.md"
        report.write_text(_report_with_banner(), encoding="utf-8")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["stats"]["unclassified_findings"], 1)
        self.assertEqual(result["stats"]["unknown_origin"], 0)
        self.assertFalse(
            any("health-degradation banner" in f for f in result["failures"])
        )

    def test_unclassified_finding_without_banner_fails(self):
        """Same unclassified finding, but the persisted report carries no
        banner — the exact silent-degradation defect req 7 exists to prevent.
        """
        _build_ok_run(self.run_dir, origin="stale")  # default report has no banner
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stats"]["unclassified_findings"], 1)
        self.assertTrue(
            any(
                "unclassified finding(s)" in f and "health-degradation banner" in f
                for f in result["failures"]
            )
        )

    def test_all_classified_findings_no_banner_is_clean(self):
        """A healthy run (all origins classified) needs no banner at all."""
        _build_ok_run(self.run_dir, origin="new")  # default report has no banner
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["stats"]["unclassified_findings"], 0)
        self.assertFalse(
            any("health-degradation banner" in f for f in result["failures"])
        )

    def test_missing_origin_key_counts_as_unclassified(self):
        """A finding with no ``origin`` key at all must count as unclassified
        (mirrors isClassified in stages.js, which _is_classified deliberately
        matches). It also independently fails G2's union-schema origin-
        required check — that is expected and unrelated; this test asserts
        the NEW pairing signal specifically, in both directions.
        """
        _build_ok_run(self.run_dir)
        finding = _ok_finding()
        del finding["origin"]
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_json(pr_dir / "code-gauntlet-findings-deadbeef.json", [finding])

        # Banner absent (default report): both G2 (missing origin field) and
        # the new pairing check fire.
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stats"]["unclassified_findings"], 1)
        self.assertEqual(result["stats"]["unknown_origin"], 0)
        self.assertTrue(
            any("missing required field group origin" in f for f in result["failures"])
        )
        self.assertTrue(
            any(
                "unclassified finding(s)" in f and "health-degradation banner" in f
                for f in result["failures"]
            )
        )

        # Banner present: the pairing check is satisfied even though G2 still
        # fails for its own, unrelated reason.
        report = pr_dir / "code-gauntlet-report-deadbeef.md"
        report.write_text(_report_with_banner(), encoding="utf-8")
        result2 = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(
            any("missing required field group origin" in f for f in result2["failures"])
        )
        self.assertFalse(
            any("health-degradation banner" in f for f in result2["failures"])
        )

    def test_unclassified_finding_disclosed_via_post_review_only_passes(self):
        """EITHER surface satisfies the pairing check, not BOTH: a
        pr_comments-only delivery never shows report.md to anyone, so the
        banner riding ONLY on the post-review review_body must be sufficient.
        Also matches the pipeline's own empty-report path, where the report
        artifact is deliberately not persisted and only review_body carries
        the banner (see the EITHER-not-BOTH comment at the check site).
        """
        _build_ok_run(self.run_dir, origin="stale")  # default report has no banner
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_post_review(pr_dir, _banner_review_body())
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertFalse(
            any("health-degradation banner" in f for f in result["failures"])
        )

    def test_unclassified_finding_disclosed_via_health_banner_field_passes(self):
        """The field the PIPELINE actually writes. ``writerPayload`` /
        ``persistPlan`` put the banner in ``health_banner``, not in
        ``review_body`` — that slot belongs to Phase 8's narrative, and
        scripts/post_review.py prepends the two at post time. A check that
        looked only at ``review_body`` would read every real degraded run as
        undisclosed while G3 quietly passed on report.md alone.
        """
        _build_ok_run(self.run_dir, origin="stale")  # default report has no banner
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_post_review(pr_dir, review_body="", health_banner=_banner_review_body())
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])
        self.assertFalse(
            any("health-degradation banner" in f for f in result["failures"])
        )

    def test_a_lost_dimension_with_no_banner_fails_even_with_zero_findings(self):
        """THE case the findings-derived trigger can never see.

        A lost review dimension leaves NO trace in the findings file: the agent
        that would have produced those findings never returned, so there is
        nothing to count and ``pr_unclassified`` is 0. The review is still
        materially degraded — nothing was reviewed for that dimension — and the
        pipeline correctly bands it (``stages_health_banner.test.js``, "the
        worst false-clean"). Until the gate also keyed on the pipeline's own
        ``health.degraded``, that disclosure path had no bench-level
        enforcement at all: the trigger could not fire, so the banner could
        have silently stopped rendering and every smoke would still be green.
        """
        _build_ok_run(self.run_dir, origin="new")  # every finding classified
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr_dir / "workflows" / "wf_test-0001.json",
            _wf_record_with_health({
                "delivered": 0, "notChallenged": 0, "unclassified": 0,
                "dimensionsLost": ["security"], "evidenceIsFresh": True,
                "degraded": True,
            }),
        )
        _write_post_review(pr_dir, review_body="", health_banner="")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"], "a lost dimension with no banner must fail")
        self.assertTrue(
            any("health-degradation banner" in f and "security" in f
                for f in result["failures"]),
            result["failures"],
        )

    def test_a_lost_dimension_that_IS_disclosed_passes(self):
        """The other half: the gate must not fire on a degraded run that did
        band itself, or it would just be noise on correct behaviour."""
        _build_ok_run(self.run_dir, origin="new")
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr_dir / "workflows" / "wf_test-0001.json",
            _wf_record_with_health({
                "delivered": 0, "notChallenged": 0, "unclassified": 0,
                "dimensionsLost": ["security"], "evidenceIsFresh": True,
                "degraded": True,
            }),
        )
        _write_post_review(pr_dir, review_body="", health_banner=_banner_review_body())
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_unclassified_finding_neither_surface_disclosed_fails(self):
        """Report has no banner AND the persisted post-review review_body is
        present but empty (the healthy-run shape) — still a silent failure.
        """
        _build_ok_run(self.run_dir, origin="stale")
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_post_review(pr_dir, review_body="")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any(
                "health-degradation banner" in f and "code-gauntlet-post-review" in f
                for f in result["failures"]
            )
        )

    def test_post_review_bare_array_shape_has_no_review_body_surface(self):
        """A run with no PR identity persists a bare findings array for
        post-review (no review_body key at all) — that must read as "no
        banner here", not as a parse error masking a real disclosure gap.
        """
        _build_ok_run(self.run_dir, origin="stale")
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_post_review(pr_dir, with_identity=False)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("health-degradation banner" in f for f in result["failures"])
        )

    def test_pairing_check_is_isolated_per_pr(self):
        """A bug that scans across the whole run dir instead of scoping to
        each PR's own directory — or that lets one PR's banner satisfy a
        DIFFERENT PR's unclassified finding — must not pass. One PR is
        degraded-and-disclosed, the other degraded-and-silent; the failure
        must be attributed to exactly the silent one.
        """
        urls = [
            "https://github.com/example/repo/pull/1",
            "https://github.com/example/repo/pull/2",
        ]
        _build_ok_run(self.run_dir, pr_urls=urls, origin="stale")
        pr1 = self.run_dir / "pr-example-repo-1"
        pr2 = self.run_dir / "pr-example-repo-2"
        # PR1: degraded and disclosed (report banner).
        (pr1 / "code-gauntlet-report-deadbeef.md").write_text(
            _report_with_banner(), encoding="utf-8"
        )
        # PR2: degraded and silent — default report (no banner), no post-review file.

        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])

        pr1_failures = [f for f in result["failures"] if f.startswith(pr1.name + ":")]
        pr2_failures = [f for f in result["failures"] if f.startswith(pr2.name + ":")]
        self.assertFalse(
            any("health-degradation banner" in f for f in pr1_failures), pr1_failures
        )
        self.assertTrue(
            any("health-degradation banner" in f for f in pr2_failures), pr2_failures
        )
        # Both PRs' unclassified findings were still counted at the run level.
        self.assertEqual(result["stats"]["unclassified_findings"], 2)

    # --- health: reported stat, not a gate (issue #25 reqs 7-9) ---

    def test_health_snapshot_picks_the_NEWEST_record_not_the_last_by_filename(self):
        """A retried PR must report its live attempt, not a superseded one.

        ``wf_*.json`` filenames are ``wf_<random>``, so sorted-glob order says
        nothing about which attempt is current — this fixture uses the real
        filenames and timestamps from smoke-20260729-193917-f08d4f6, where the
        record that sorts LAST is the OLDEST of the set by 90 minutes. Picking
        by glob order there reports a dead attempt's health as the run's, which
        is the same class of defect issue #85 files against
        ``_iter_workflow_records``. The assertion is deliberately written so it
        FAILS under the glob-order implementation rather than merely passing
        under the timestamp one.
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)

            def record(name, ts, unclassified):
                path = d / name
                path.write_text(json.dumps({
                    "timestamp": ts,
                    "result": {"stats": {"health": {
                        "unclassified": unclassified, "degraded": unclassified > 0,
                    }}},
                }), encoding="utf-8")
                return path

            newest = record("wf_744f098e-8c6.json", "2026-07-29T21:44:17.304Z", 0)
            middle = record("wf_b081a6b5-340.json", "2026-07-29T20:56:25.962Z", 5)
            oldest = record("wf_a62f7348-a3f.json", "2026-07-29T20:12:04.222Z", 99)

            records = sorted([newest, middle, oldest])
            # Precondition the whole test rests on: glob order is not chronological.
            self.assertEqual(records[-1].name, "wf_b081a6b5-340.json")

            picked = check._select_pr_health_snapshot(records)
            self.assertEqual(picked["unclassified"], 0, "must be the newest record")
            self.assertFalse(picked["degraded"])

    def test_health_snapshot_without_timestamps_prefers_a_degraded_record(self):
        """With nothing to order by, under-reporting degradation is the wrong
        direction to fail in for a disclosure signal, so a degraded record wins."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)

            def untimestamped(name, unclassified):
                path = d / name
                path.write_text(json.dumps({
                    "result": {"stats": {"health": {
                        "unclassified": unclassified, "degraded": unclassified > 0,
                    }}},
                }), encoding="utf-8")
                return path

            clean = untimestamped("wf_aaa.json", 0)
            degraded = untimestamped("wf_zzz.json", 4)
            picked = check._select_pr_health_snapshot([clean, degraded])
            self.assertTrue(picked["degraded"])
            self.assertEqual(picked["unclassified"], 4)

    def test_health_present_aggregates_across_prs(self):
        urls = [
            "https://github.com/example/repo/pull/1",
            "https://github.com/example/repo/pull/2",
        ]
        _build_ok_run(self.run_dir, pr_urls=urls, origin="new")
        pr1 = self.run_dir / "pr-example-repo-1"
        pr2 = self.run_dir / "pr-example-repo-2"
        _write_json(
            pr1 / "workflows" / "wf_test-0001.json",
            _wf_record_with_health({
                "delivered": 5, "notChallenged": 1, "unclassified": 2,
                "dimensionsLost": ["security"], "evidenceIsFresh": True,
                "degraded": True,
            }),
        )
        _write_json(
            pr2 / "workflows" / "wf_test-0001.json",
            _wf_record_with_health({
                "delivered": 3, "notChallenged": 0, "unclassified": 0,
                "dimensionsLost": [], "evidenceIsFresh": True,
                "degraded": False,
            }),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertEqual(
            result["stats"]["health"],
            {
                "delivered": 8, "notChallenged": 1, "unclassified": 2,
                "verifySlicesDegraded": 0, "inputUnproven": 0, "inputRecovered": 0,
                "measured_prs": 2, "unmeasured_prs": 0, "degraded_prs": 1,
                "dimensionsLost": ["security"],
            },
        )

    def test_health_absent_reports_not_measured(self):
        """A run whose wf records carry no ``result.stats.health`` at all
        (e.g. recorded before this landed) must report ``None`` — never a
        zeroed dict, which would claim a measurement that never happened.
        """
        _build_ok_run(self.run_dir)  # default wf record carries no `result` key
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertIsNone(result["stats"]["health"])

    def test_extract_review_health_returns_none_for_missing_stats(self):
        wf = self.run_dir / "wf_no_stats.json"
        _write_json(
            wf,
            {"runId": "wf_x", "scriptPath": PIPELINE, "result": {"ok": True, "gaps": []}},
        )
        self.assertIsNone(check._extract_review_health(wf))

    def test_extract_review_health_returns_dict_when_present(self):
        wf = self.run_dir / "wf_with_health.json"
        health = {
            "delivered": 4, "notChallenged": 0, "unclassified": 0,
            "dimensionsLost": [], "evidenceIsFresh": True, "degraded": False,
        }
        _write_json(
            wf,
            {"runId": "wf_x", "scriptPath": PIPELINE, "result": {"stats": {"health": health}}},
        )
        self.assertEqual(check._extract_review_health(wf), health)

    # --- health snapshot selection: timestamp, not glob order (issue #85) ---
    #
    # wf_*.json filenames are `wf_<random>`, so sorted glob order is arbitrary
    # and NOT chronological — issue #85 already recorded a superseded record
    # winning a different gate's verdict this same way. These tests pin that
    # _select_pr_health_snapshot orders by the record's own `timestamp`
    # instead, with a disclosure-safe fallback when no timestamp is usable.

    def test_record_timestamp_reads_iso8601_string(self):
        wf = self.run_dir / "wf_ts.json"
        _write_json(wf, {"runId": "wf_x", "timestamp": "2026-07-29T21:44:17Z"})
        self.assertEqual(check._record_timestamp(wf), "2026-07-29T21:44:17Z")

    def test_record_timestamp_none_when_absent_or_invalid(self):
        no_ts = self.run_dir / "wf_no_ts.json"
        _write_json(no_ts, {"runId": "wf_x"})
        self.assertIsNone(check._record_timestamp(no_ts))

        not_a_string = self.run_dir / "wf_bad_type.json"
        _write_json(not_a_string, {"runId": "wf_x", "timestamp": 12345})
        self.assertIsNone(check._record_timestamp(not_a_string))

        not_iso = self.run_dir / "wf_bad_shape.json"
        _write_json(not_iso, {"runId": "wf_x", "timestamp": "not-a-date"})
        self.assertIsNone(check._record_timestamp(not_iso))

    def test_select_pr_health_snapshot_picks_newest_by_timestamp_not_glob_order(self):
        """Mirrors the measured smoke-20260729-193917-f08d4f6 shape: the
        record that sorts LAST alphabetically is actually the OLDEST by 90
        minutes. Picking by glob order would silently report the superseded
        attempt's counts.
        """
        # The NEWEST record deliberately sorts FIRST and is passed FIRST. If it
        # sorted (or were passed) last, a "take the last one" implementation would
        # give the same answer as a correct one and this test would pass against
        # the very bug it names — which is how it was originally written.
        new = self.run_dir / "wf_a_sorts_first_but_is_newest.json"
        old = self.run_dir / "wf_z_sorts_last_but_is_oldest.json"
        _write_json(
            old,
            _wf_record_with_health(
                {"delivered": 1, "notChallenged": 0, "unclassified": 1,
                 "dimensionsLost": [], "evidenceIsFresh": True, "degraded": True},
                timestamp="2026-07-29T20:12:04Z",
            ),
        )
        _write_json(
            new,
            _wf_record_with_health(
                {"delivered": 9, "notChallenged": 0, "unclassified": 0,
                 "dimensionsLost": [], "evidenceIsFresh": True, "degraded": False},
                timestamp="2026-07-29T21:44:17Z",
            ),
        )
        # Glob-ascending order, which now puts the NEWEST first and the OLDEST last.
        snapshot = check._select_pr_health_snapshot([new, old])
        self.assertEqual(snapshot["delivered"], 9)
        self.assertFalse(snapshot["degraded"])

    def test_select_pr_health_snapshot_falls_back_to_degraded_when_untimestamped(self):
        """No candidate has a usable timestamp: ordering is impossible, so
        the fallback must prefer disclosure over silence — a record already
        reporting degraded:true wins over a quieter, healthier-looking one.
        """
        healthy = self.run_dir / "wf_healthy_no_ts.json"
        degraded = self.run_dir / "wf_degraded_no_ts.json"
        _write_json(
            healthy,
            _wf_record_with_health({
                "delivered": 5, "notChallenged": 0, "unclassified": 0,
                "dimensionsLost": [], "evidenceIsFresh": True, "degraded": False,
            }),
        )
        _write_json(
            degraded,
            _wf_record_with_health({
                "delivered": 5, "notChallenged": 0, "unclassified": 2,
                "dimensionsLost": [], "evidenceIsFresh": True, "degraded": True,
            }),
        )
        # Degraded FIRST: with it last, "take the last one" would agree with the
        # correct answer and the fallback would go untested.
        snapshot = check._select_pr_health_snapshot([degraded, healthy])
        self.assertTrue(snapshot["degraded"])
        self.assertEqual(snapshot["unclassified"], 2)

    def test_select_pr_health_snapshot_returns_none_without_any_health(self):
        no_health = self.run_dir / "wf_no_health.json"
        _write_json(no_health, _wf_record())
        self.assertIsNone(check._select_pr_health_snapshot([no_health]))

    def test_health_stat_uses_newest_record_not_glob_order(self):
        """End-to-end through check_run(): a PR with two wf records where the
        alphabetically-last filename is actually the older attempt. The
        reported stats.health must reflect the NEWER attempt.
        """
        _build_ok_run(self.run_dir, origin="new")
        pr_dir = self.run_dir / "pr-example-repo-1"
        _write_json(
            pr_dir / "workflows" / "wf_z_sorts_last_but_is_oldest.json",
            _wf_record_with_health(
                {"delivered": 1, "notChallenged": 0, "unclassified": 1,
                 "dimensionsLost": ["security"], "evidenceIsFresh": True, "degraded": True},
                timestamp="2026-07-29T20:12:04Z",
            ),
        )
        _write_json(
            pr_dir / "workflows" / "wf_a_sorts_first_but_is_newest.json",
            _wf_record_with_health(
                {"delivered": 9, "notChallenged": 0, "unclassified": 0,
                 "dimensionsLost": [], "evidenceIsFresh": True, "degraded": False},
                timestamp="2026-07-29T21:44:17Z",
            ),
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertEqual(result["stats"]["health"]["delivered"], 9)
        self.assertEqual(result["stats"]["health"]["degraded_prs"], 0)

    def test_zero_comments_fails_g5(self):
        _build_ok_run(self.run_dir, n_comments=0)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("delivered comments" in f for f in result["failures"]))

    def test_naive_anchor_refused(self):
        _build_ok_run(self.run_dir)
        manifest = json.loads((self.run_dir / "run.json").read_text())
        manifest["anchor"] = "naive"
        _write_json(self.run_dir / "run.json", manifest)
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result.get("refused"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("naive" in f for f in result["failures"]))

    def test_union_schema_missing_description_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["description"]
        del bad["body"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("description/body" in f for f in result["failures"]))

    def test_union_schema_missing_line_identity_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["line_start"]
        del bad["line"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("line identity" in f for f in result["failures"]))

    def test_union_schema_missing_file_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["file"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing required field group file" in f for f in result["failures"]))

    def test_union_schema_missing_origin_fails_g2(self):
        _build_ok_run(self.run_dir)
        bad = _ok_finding()
        del bad["origin"]
        _write_json(
            self.run_dir / "pr-example-repo-1" / "code-gauntlet-findings-deadbeef.json",
            [bad],
        )
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertFalse(result["ok"])
        self.assertTrue(
            any("missing required field group origin" in f for f in result["failures"])
        )

    def test_relative_pipeline_script_path_accepted(self):
        _build_ok_run(self.run_dir, script_path="workflows/pipeline.js")
        result = check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertTrue(result["ok"], result["failures"])

    def test_does_not_import_score_module(self):
        import importlib

        if "bench.runner.score" in sys.modules:
            del sys.modules["bench.runner.score"]
        importlib.reload(check)
        self.assertNotIn("bench.runner.score", sys.modules)
        _build_ok_run(self.run_dir)
        check.check_run(self.run_dir, repo_root=REPO_ROOT)
        self.assertNotIn("bench.runner.score", sys.modules)


class WorkflowRecordCollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-wf-collect-")
        self.home = Path(self.tmp) / "claude-home"
        self.pr_dir = Path(self.tmp) / "pr-example-repo-1"
        self.pr_dir.mkdir()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, rel, payload, mtime_ns=None):
        path = self.home / "config" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        if mtime_ns is not None:
            import os

            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def test_collects_only_new_or_changed_records(self):
        old = self._plant(
            "projects/slug/session/workflows/wf_old.json",
            _wf_record("/old/workflows/pipeline.js"),
        )
        baseline = invoke.snapshot_workflow_records(self.home)
        self.assertIn(str(old.resolve()), baseline)

        self._plant(
            "projects/slug/session/workflows/wf_new.json",
            _wf_record(PIPELINE),
        )
        # Change the old record so it is re-copied.
        self._plant(
            "projects/slug/session/workflows/wf_old.json",
            _wf_record(PIPELINE),
        )

        copied = invoke.collect_workflow_records(self.home, self.pr_dir, baseline)
        self.assertEqual(set(copied), {"wf_new.json", "wf_old.json"})
        dest = self.pr_dir / "workflows"
        self.assertTrue((dest / "wf_new.json").is_file())
        self.assertTrue((dest / "wf_old.json").is_file())
        data = json.loads((dest / "wf_new.json").read_text())
        self.assertEqual(data["scriptPath"], PIPELINE)

    def test_unchanged_baseline_copies_nothing(self):
        self._plant(
            "projects/slug/session/workflows/wf_old.json",
            _wf_record(PIPELINE),
        )
        baseline = invoke.snapshot_workflow_records(self.home)
        copied = invoke.collect_workflow_records(self.home, self.pr_dir, baseline)
        self.assertEqual(copied, [])
        self.assertFalse((self.pr_dir / "workflows").exists())

    def test_filename_collision_gets_numeric_suffix(self):
        # Same basename from two project slugs — second copy must not overwrite.
        self._plant(
            "projects/slug-a/session/workflows/wf_same.json",
            _wf_record(PIPELINE),
        )
        self._plant(
            "projects/slug-b/session/workflows/wf_same.json",
            _wf_record("/other/workflows/pipeline.js"),
        )
        copied = invoke.collect_workflow_records(self.home, self.pr_dir, {})
        self.assertEqual(set(copied), {"wf_same.json", "wf_same-2.json"})
        dest = self.pr_dir / "workflows"
        self.assertTrue((dest / "wf_same.json").is_file())
        self.assertTrue((dest / "wf_same-2.json").is_file())
        paths = {
            json.loads((dest / name).read_text())["scriptPath"]
            for name in copied
        }
        self.assertEqual(paths, {PIPELINE, "/other/workflows/pipeline.js"})


class CheckCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-check-cli-")
        self.runs_root = Path(self.tmp) / "runs"
        self.runs_root.mkdir()
        self.run_dir = self.runs_root / "smoke-20260723-000000-abc1234"
        self.run_dir.mkdir()
        _build_ok_run(self.run_dir)
        self._runs_patch = patch.object(run, "RUNS_ROOT", self.runs_root)
        self._runs_patch.start()

    def tearDown(self):
        self._runs_patch.stop()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_cli_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run.main(["--check", self.run_dir.name])
        self.assertEqual(rc, 0)

    def test_check_cli_fails_on_gate(self):
        (self.run_dir / "pr-example-repo-1" / "post-review-payload.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = run.main(["--check", self.run_dir.name])
        self.assertEqual(rc, 1)

    def test_check_missing_run_is_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = run.main(["--check", "does-not-exist"])
        self.assertEqual(rc, 2)

    def test_check_mutex_with_score_only(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = run.main(["--check", "x", "--score-only", "y"])
        self.assertEqual(rc, 2)
        self.assertIn("--check", err.getvalue())

    def test_check_rejects_tier_flag(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = run.main(["--check", self.run_dir.name, "--tier", "smoke"])
        self.assertEqual(rc, 2)
        self.assertIn("does not accept", err.getvalue())

    def test_check_naive_is_exit_2(self):
        manifest = json.loads((self.run_dir / "run.json").read_text())
        manifest["anchor"] = "naive"
        _write_json(self.run_dir / "run.json", manifest)
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = run.main(["--check", self.run_dir.name])
        self.assertEqual(rc, 2)


class MiniResolutionTest(unittest.TestCase):
    def test_tier_mini_resolves_six(self):
        subsets = json.loads((REPO_ROOT / "bench/golden/subsets.json").read_text())
        shas = json.loads((REPO_ROOT / "bench/golden/shas.json").read_text())
        urls = run._resolve_tier("mini", subsets, shas)
        self.assertEqual(len(urls), 6)
        self.assertEqual(urls, subsets["mini"])

    def test_prs_mini_alias_expands(self):
        subsets = json.loads((REPO_ROOT / "bench/golden/subsets.json").read_text())
        args = run.parse_args(["--prs", "mini"])
        self.assertEqual(args.prs, subsets["mini"])


if __name__ == "__main__":
    unittest.main()
