"""Boundary parity: the v3 pipeline's persisted findings schema must carry every
field the two RETAINED Python boundary scripts read, so each consumes the pipeline
output without erroring on a missing field.

Two boundaries, two field vocabularies:
  - verify_findings.py reads the CANONICAL names (file, line_start, line_end,
    description, origin, cross_file_refs, ...) — all via ``.get()`` with defaults,
    so it never errors on an absent field.
  - post_review.py (the retained v2 poster) reads the V2 names: a finding with a
    ``line`` INDEXES ``f["file"]`` directly (KeyError if absent), reads ``line``
    itself and ``body`` / ``end_line`` via ``.get()``. A finding with no ``line``
    at all degrades into the trailing "could not be anchored inline" section
    instead of indexing anything (issue #192) — it is never fatal.

The parity contract is that the persisted findings envelope carries the UNION: the
canonical fields for verify + downstream, plus the ``line`` / ``end_line`` / ``body``
aliases the retained poster reads. writeArtifacts applies these aliases at the
persist boundary. This test drives REAL persisted pipeline output (produced by running
the wired stages through the node recorder) through BOTH scripts — verify positionally,
post_review --dry-run with the read-only CLI calls mocked — asserting neither errors,
then documents why the ``file`` alias is load-bearing (for a finding that DOES have a
line) via a KeyError negative control, and that a missing ``line`` alias degrades
gracefully rather than aborting the run.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import scripts.post_review as post_review  # noqa: E402
import scripts.report_patches as report_patches  # noqa: E402
from bench.runner import invoke  # noqa: E402

RECORDER = REPO / "workflows" / "test" / "tools" / "emit_persisted_findings.mjs"

# The canonical fields the brief pins as the pipeline schema surface (Task 13 Step 4).
CANONICAL_FIELDS = [
    "id",
    "title",
    "description",
    "dimension",
    "severity",
    "confidence",
    "file",
    "line_start",
    "line_end",
    "origin",
    "cross_file_refs",
    "report_destination",
]
# The v2 aliases the retained post_review poster indexes/reads.
V2_ALIAS_FIELDS = ["file", "line", "body", "end_line"]

# The fields issue #47 added to the schema. Before that change every one of them was
# instructed by an agent contract, declared by no schema, and therefore discarded at the
# discovery StructuredOutput boundary — so nothing downstream, including this boundary,
# ever saw them. The recorder seeds them onto the findings it drives through the WIRED
# pipeline, which makes the assertions below an end-to-end carriage proof rather than a
# restatement of the registry: if any stage between discovery and persist starts
# reconstructing findings field-by-field, they vanish here.
ISSUE_47_FIELDS = [
    "suggestion",
    "claude_md_rule",
    "spec_text",
    "criticality",
    "failure_scenario",
]


def load_pipeline_findings():
    """Run the wired pipeline (via the node recorder) and return its REAL persisted
    high-confidence findings — v2-aliased at the writeArtifacts boundary."""
    tmp = tempfile.mkdtemp()
    try:
        out = os.path.join(tmp, "persisted.json")
        proc = subprocess.run(
            ["node", str(RECORDER), out],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"recorder failed: {proc.stderr}")
        with open(out) as fh:
            return json.load(fh)["findings"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Recorded once for the module — the pipeline persist output is deterministic.
PERSISTED_FINDINGS = load_pipeline_findings()


def build_gh_diff(findings):
    """A unified diff (as `gh pr diff` would emit) making every finding's (file, line)
    a valid inline-comment anchor — derived from the REAL findings, not hard-coded."""
    max_line = {}
    for f in findings:
        line = int(f.get("line") or f.get("line_start") or 1)
        max_line[f["file"]] = max(max_line.get(f["file"], 0), line)
    parts = []
    for path, ml in max_line.items():
        n = ml + 1  # new-side lines 1..n (>= the finding's line)
        parts.append(f"diff --git a/{path} b/{path}\n")
        parts.append(f"--- a/{path}\n")
        parts.append(f"+++ b/{path}\n")
        parts.append(f"@@ -1,1 +1,{n} @@\n")
        parts.append(" context\n")
        for i in range(2, n + 1):
            parts.append(f"+added {i}\n")
    return "".join(parts)


def _fake_run(diff="", remote="git@github.com:o/r.git\n"):
    """subprocess.run side_effect mocking post_review's read-only CLI calls
    (which / git remote / git rev-parse / gh pr diff). Any other command returns an
    empty JSON object; in dry-run, post_json short-circuits before a POST subprocess."""

    def _run(cmd, *_a, **_k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)

        if cmd[0] == "which":
            return res(out="/usr/bin/" + cmd[1])
        if cmd[:3] == ["git", "remote", "get-url"]:
            return res(out=remote)
        if cmd[:2] == ["git", "rev-parse"]:
            return res(out="deadbeefcafe\n")
        if cmd[:3] == ["gh", "pr", "diff"]:
            return res(out=diff)
        return res(out="{}", rc=0)

    return _run


class TestSchemaCarriesBoundaryFields(unittest.TestCase):
    """The REAL persisted finding carries every field the two boundaries read."""

    def test_canonical_fields_all_present(self):
        f = PERSISTED_FINDINGS[0]
        for field in CANONICAL_FIELDS:
            self.assertIn(
                field,
                f,
                f"persisted pipeline finding must carry canonical field '{field}'",
            )

    def test_v2_aliases_present_for_retained_poster(self):
        f = PERSISTED_FINDINGS[0]
        for field in V2_ALIAS_FIELDS:
            self.assertIn(
                field,
                f,
                f"persisted schema must carry v2 alias '{field}' for post_review.py",
            )

    def test_aliases_mirror_canonical_values(self):
        f = PERSISTED_FINDINGS[0]
        self.assertEqual(f["line"], f["line_start"])
        self.assertEqual(f["end_line"], f["line_end"])
        self.assertEqual(f["body"], f["description"])

    def test_issue_47_fields_survive_the_whole_pipeline_to_persist(self):
        # Discovery -> merge -> verify echo -> validate -> filter -> challenge -> persist.
        # Every hop must carry these; the two schema hops (discovery dispatch and the verify
        # echo) are the ones that used to drop them.
        seen = {field for f in PERSISTED_FINDINGS for field in f}
        missing = [field for field in ISSUE_47_FIELDS if field not in seen]
        self.assertEqual(
            missing,
            [],
            f"fields lost somewhere between discovery and persist: {missing}",
        )

    def test_issue_47_field_values_are_not_hollowed_out(self):
        # Present-but-empty is the failure mode a presence check alone would pass: a stage
        # that reconstructs findings through an under-declared schema tends to keep the key
        # and drop the content (exactly what the verify executor once did to `description`).
        values = {}
        for f in PERSISTED_FINDINGS:
            for field in ISSUE_47_FIELDS:
                if field in f:
                    values.setdefault(field, []).append(f[field])
        for field, vals in values.items():
            self.assertTrue(
                any(v not in (None, "", []) for v in vals),
                f"'{field}' survived to persist but every value is empty",
            )


class TestVerifyFindingsBoundary(unittest.TestCase):
    """verify_findings.py (positional path) consumes the persisted schema cleanly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verify_consumes_pipeline_findings_without_error(self):
        findings_path = os.path.join(self.tmp, "findings.json")
        out_path = os.path.join(self.tmp, "out.json")
        diff_path = os.path.join(self.tmp, "diff.patch")
        with open(findings_path, "w") as fh:
            json.dump({"findings": PERSISTED_FINDINGS, "base_branch": "main"}, fh)
        with open(diff_path, "w") as fh:
            fh.write(build_gh_diff(PERSISTED_FINDINGS))

        proc = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_findings.py"),
                findings_path,
                "--diff-file",
                diff_path,
                "--output",
                out_path,
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"verify_findings.py errored on the persisted schema: {proc.stderr}",
        )

        with open(out_path) as fh:
            envelope = json.load(fh)
        for key in ("verified", "eliminated", "batches", "stats"):
            self.assertIn(key, envelope, f"verify envelope missing '{key}'")


class TestPostReviewBoundary(unittest.TestCase):
    """post_review.py --dry-run consumes the persisted findings without a missing-field error."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.findings_path = os.path.join(self.tmp, "findings.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        post_review.DRY_RUN = False
        post_review._CAPTURED.clear()
        post_review._SKIP_WARNINGS.clear()

    def _write(self, findings):
        with open(self.findings_path, "w") as fh:
            json.dump(
                {
                    "platform": "github",
                    "owner": "o",
                    "repo": "r",
                    "pr_number": 5,
                    "review_body": "Code gauntlet summary",
                    "findings": findings,
                },
                fh,
            )

    def test_post_review_consumes_pipeline_findings_without_error(self):
        diff = build_gh_diff(PERSISTED_FINDINGS)
        self._write(PERSISTED_FINDINGS)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run", side_effect=_fake_run(diff=diff)
            ),
        ):
            post_review.main()  # must not raise: every field it reads is present

        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        self.assertTrue(
            os.path.exists(payload_path), "dry-run payload was not captured"
        )
        with open(payload_path) as fh:
            cap = json.load(fh)
        self.assertEqual(cap["platform"], "github")
        # Every persisted finding rendered into an inline comment (file:line valid).
        self.assertEqual(len(cap["payload"]["comments"]), len(PERSISTED_FINDINGS))

    def test_the_posted_comment_renders_the_suggestion_and_cited_rule(self):
        # The end of the chain issue #47 is about: a reviewer reads a PR comment, not
        # findings.json. This drives the REAL persisted findings through the REAL poster and
        # asserts the rendered comment bodies actually carry the prose fix and the cited
        # rule — the promise report-format.md's inline-comment template makes.
        diff = build_gh_diff(PERSISTED_FINDINGS)
        self._write(PERSISTED_FINDINGS)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run", side_effect=_fake_run(diff=diff)
            ),
        ):
            post_review.main()

        with open(os.path.join(self.tmp, "post-review-payload.json")) as fh:
            comments = json.load(fh)["payload"]["comments"]
        bodies = "\n".join(c["body"] for c in comments)

        self.assertIn(
            "**Suggested fix:**",
            bodies,
            "the prose suggestion never reached the posted comment",
        )
        self.assertIn(
            "**Cited rule:**", bodies, "the cited rule never reached the posted comment"
        )
        # The actual VALUES, not just the headings — a heading with an empty body would
        # satisfy the assertions above while telling the reviewer nothing.
        for finding in PERSISTED_FINDINGS:
            if finding.get("suggestion"):
                self.assertIn(finding["suggestion"], bodies)
            rule = finding.get("claude_md_rule") or finding.get("spec_text")
            if rule:
                self.assertIn(rule, bodies)
        # And the identity trailer reaches the wire through the REAL poster — one per
        # delivered comment, last line, pinned by codepoint rather than by glyph.
        trailer = "\u2694\ufe0f *Code Gauntlet*"
        for comment in comments:
            self.assertEqual(comment["body"].splitlines()[-1], trailer)
            self.assertEqual(comment["body"].count(trailer), 1)

    def test_the_posted_comment_omits_the_artifact_only_fields(self):
        # criticality / failure_scenario ride the schema end to end but are deliberately
        # kept out of the comment body (issue #47). Asserted against REAL pipeline output so
        # the scoping decision is pinned where it actually takes effect.
        diff = build_gh_diff(PERSISTED_FINDINGS)
        self._write(PERSISTED_FINDINGS)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run", side_effect=_fake_run(diff=diff)
            ),
        ):
            post_review.main()

        with open(os.path.join(self.tmp, "post-review-payload.json")) as fh:
            comments = json.load(fh)["payload"]["comments"]
        bodies = "\n".join(c["body"] for c in comments)
        for finding in PERSISTED_FINDINGS:
            if finding.get("failure_scenario"):
                self.assertNotIn(finding["failure_scenario"], bodies)

    def test_missing_file_alias_would_break_the_retained_poster(self):
        # Documents why the `file` alias is load-bearing for a finding that DOES carry
        # a line: strip it from the REAL findings and post_review's direct index
        # f["file"] raises KeyError. This is the exact boundary the persisted-schema
        # union (writeArtifacts aliasing) closes.
        stripped = [
            {k: v for k, v in f.items() if k != "file"} for f in PERSISTED_FINDINGS
        ]
        self._write(stripped)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=build_gh_diff(PERSISTED_FINDINGS)),
            ),
            self.assertRaises(KeyError),
        ):
            post_review.main()

    def test_missing_line_alias_degrades_gracefully_not_a_crash(self):
        # Issue #192: a finding with no `line` alias at all must NOT crash the whole
        # poster — it degrades into the skipped section instead. Unlike the `file`
        # alias above, `line`/`end_line`/`body` are no longer load-bearing for
        # crash-avoidance; this pins that this stripped shape used to raise KeyError
        # and now does not.
        stripped = [
            {k: v for k, v in f.items() if k not in ("line", "end_line", "body")}
            for f in PERSISTED_FINDINGS
        ]
        self._write(stripped)
        with (
            patch.object(
                sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]
            ),
            patch(
                "scripts.post_review.subprocess.run",
                side_effect=_fake_run(diff=build_gh_diff(PERSISTED_FINDINGS)),
            ),
        ):
            post_review.main()  # must not raise

        with open(os.path.join(self.tmp, "post-review-payload.json")) as fh:
            payload = json.load(fh)["payload"]
        self.assertEqual(len(payload["comments"]), 0)
        self.assertIn("could not be anchored inline", payload["body"])


class TestReportPatchesBoundary(unittest.TestCase):
    """report_patches.py --output-dir/--head-sha (issue #226) consumes the SAME
    persisted findings envelope post_review.py --dry-run consumes above, without
    erroring on a missing/mismatched field. No diff file is needed here — the
    receipt's ``findings`` count reports the whole persisted array regardless of
    the (missing) diff oracle."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        # bare "post_review" (report_patches.py's own import) and "scripts.post_review"
        # (this test module's import) are two SEPARATE module objects with independent
        # state — see ReportPatchesTestBase's docstring in tests/test_report_patches.py.
        # This class's own gate-driving tests dirty the bare-imported one; other classes
        # in this file dirty scripts.post_review directly. Both need resetting.
        post_review.reset_run_state()
        report_patches.reset_run_state()

    def test_report_patches_consumes_pipeline_findings_without_error(self):
        sha = "abc1234"
        findings_path = os.path.join(self.tmp, f"code-gauntlet-findings-{sha}.json")
        with open(findings_path, "w") as fh:
            json.dump(PERSISTED_FINDINGS, fh)

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            exit_code = report_patches.main(
                ["--output-dir", self.tmp, "--head-sha", sha]
            )

        self.assertEqual(
            exit_code,
            0,
            f"report_patches.py errored on the persisted schema: {stdout_buf.getvalue()}",
        )
        receipt = json.loads(stdout_buf.getvalue().strip().splitlines()[-1])
        self.assertEqual(receipt["findings"], len(PERSISTED_FINDINGS))

    def _run_report_patches(self, findings, sha="abc1234", diff=None):
        findings_path = os.path.join(self.tmp, f"code-gauntlet-findings-{sha}.json")
        with open(findings_path, "w") as fh:
            json.dump(findings, fh)
        if diff is not None:
            diff_path = os.path.join(self.tmp, f"code-gauntlet-diff-{sha}.patch")
            with open(diff_path, "w") as fh:
                fh.write(diff)

        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            exit_code = report_patches.main(
                ["--output-dir", self.tmp, "--head-sha", sha]
            )
        self.assertEqual(exit_code, 0, stdout_buf.getvalue())
        return json.loads(stdout_buf.getvalue().strip().splitlines()[-1])

    def test_every_persisted_finding_with_a_patch_is_kept_against_a_matching_diff(
        self,
    ):
        """Every REAL persisted finding, given a ``suggested_fix_code`` and a
        diff built to make its (file, line) a real, DIFFERING anchor, passes
        the SAME gate delivery runs — this drives the gate with the actual
        pipeline schema rather than a hand-built fixture."""
        patched = [
            dict(f, suggested_fix_code="const patched = true;")
            for f in PERSISTED_FINDINGS
        ]
        receipt = self._run_report_patches(
            patched, diff=build_gh_diff(PERSISTED_FINDINGS)
        )
        self.assertEqual(receipt["kept"], len(patched))
        self.assertEqual(receipt["reasons"], {})

    def test_dropping_end_line_from_every_finding_downgrades_all_as_missing_end_line(
        self,
    ):
        """RED against the alias-drop bug the module docstring names: an
        ``end_line``-less v2 finding must downgrade with the gate's OWN
        ``missing_end_line`` reason, not silently pass or fail some other way.
        """
        patched = []
        for f in PERSISTED_FINDINGS:
            copy = dict(f, suggested_fix_code="const patched = true;")
            copy.pop("end_line", None)
            patched.append(copy)
        receipt = self._run_report_patches(
            patched, diff=build_gh_diff(PERSISTED_FINDINGS)
        )
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["reasons"], {"missing_end_line": len(patched)})


class TestReportMethodologyRuntimeParity(unittest.TestCase):
    """The Python receipt consumers accept the real JS renderer output in both modes."""

    def _render(self, mode):
        if mode == "headless":
            echo = {
                "model_tier": {"value": "optimized", "source": "env"},
                "delivery": {"value": "pr_comments,markdown", "source": "env"},
                "post_mode": {"value": "dry-run", "source": "env"},
                "pr_comment_cap": {"value": "25", "source": "env"},
                "delivery_tier": {"value": "all", "source": "default"},
                "draft_policy": {"value": "review", "source": "env"},
                "reviewed_policy": {"value": "full", "source": "env"},
                "pr_not_found_policy": {"value": "error", "source": "env"},
                "trivial_scope": {"value": "full", "source": "env"},
            }
            expected = dict(invoke.EXPECTED_ECHO)
        else:
            echo = {
                "model_tier": {"value": "optimized", "source": "fixed"},
                "delivery_tier": {"value": "all", "source": "default"},
                "pr_comment_cap": {"value": "null", "source": "default"},
                "review_md": {"value": "absent", "source": "discovery"},
            }
            expected = {
                "model_tier": "optimized",
                "delivery_tier": "all",
                "pr_comment_cap": "null",
                "review_md": "absent",
            }
        fixture = {
            "mode": mode,
            "configEcho": echo,
            "pluginRoot": "/absolute/path/to/claude-code-gauntlet",
            "pipelineVersion": "3.26.0",
            "reviewScope": {
                "kind": "full",
                "since": None,
                "commits": None,
                "reason": None,
            },
            "policy": {"tier": "optimized", "provider": "firstParty", "gateway": False},
            "deliveryTier": "all",
            "deliveryCap": None if mode == "interactive" else 25,
            "gapCount": 0,
            "summary": "summary",
            "findings": [],
            "unverified": [],
            "dimensions": {"dispatched": [], "degraded": []},
            "stats": {
                "discovered": 0,
                "validate": {},
                "filter": {},
                "challenge": {},
                "merge": {},
            },
        }
        node = (
            "import('./workflows/src/renderReport.js').then(m => "
            "process.stdout.write(m.renderReport(" + json.dumps(fixture) + ")))"
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout, expected

    def test_both_receipt_modes_cross_the_bench_parsers(self):
        for mode in ("headless", "interactive"):
            report, expected = self._render(mode)
            with patch.object(invoke, "EXPECTED_ECHO", expected):
                self.assertTrue(invoke._echo_in_text(report), mode)
            identity = invoke._IDENTITY_LINE_RE.findall(report)
            self.assertEqual(
                {key: value for key, value in identity},
                {
                    "pipeline_version": "3.26.0",
                    "plugin_root": "/absolute/path/to/claude-code-gauntlet",
                },
                mode,
            )


if __name__ == "__main__":
    unittest.main()
