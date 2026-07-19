"""Boundary parity: the v3 pipeline's persisted findings schema must carry every
field the two RETAINED Python boundary scripts read, so each consumes the pipeline
output without erroring on a missing field.

Two boundaries, two field vocabularies:
  - verify_findings.py reads the CANONICAL names (file, line_start, line_end,
    description, origin, cross_file_refs, ...) — all via ``.get()`` with defaults,
    so it never errors on an absent field.
  - post_review.py (the retained v2 poster) reads the V2 names: it INDEXES
    ``f["file"]`` and ``f["line"]`` directly (KeyError if absent) and reads
    ``body`` / ``end_line`` via ``.get()``.

The parity contract is therefore that the persisted findings envelope carries the
UNION: the canonical fields for verify + downstream, plus the ``line`` / ``body``
aliases the retained poster indexes. This test builds one representative envelope
and drives it through BOTH scripts (verify positionally; post_review --dry-run with
the read-only CLI calls mocked) asserting neither errors, then documents why the
aliases are load-bearing.
"""

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

# The canonical fields the brief pins as the pipeline schema surface (Task 13 Step 4).
CANONICAL_FIELDS = [
    "id", "title", "description", "dimension", "severity", "confidence",
    "file", "line_start", "line_end", "origin", "cross_file_refs", "report_destination",
]


def canonical_finding():
    """One finding in the persisted pipeline schema: canonical fields (verify +
    downstream) plus the v2 aliases (line, end_line, body) the retained post_review
    boundary indexes."""
    description = "The loop bound uses <= instead of <, reading one past the end of the page buffer."
    return {
        # Canonical pipeline schema — read by verify_findings.py and every downstream stage.
        "id": "F1",
        "title": "Off-by-one in pagination bound",
        "description": description,
        "dimension": "bug",
        "severity": "high",
        "confidence": 90,
        "file": "foo.py",
        "line_start": 2,
        "line_end": 2,
        "origin": "new",
        "cross_file_refs": [],
        "report_destination": "main",
        "evidence": "for i in range(0, n + 1):",
        # v2 aliases — read by the retained post_review.py boundary.
        "line": 2,
        "end_line": 2,
        "body": description,
    }


# A GitHub diff (gh pr diff) making foo.py lines 1 (context) and 2 (added) valid
# for inline comments — shared by the post_review mock and the verify --diff-file.
GH_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " existing\n"
    "+added\n"
)


def _fake_run(diff="", remote="git@github.com:o/r.git\n"):
    """subprocess.run side_effect mocking post_review's read-only CLI calls
    (which / git remote / git rev-parse / gh pr diff). Mirrors test_post_review.py.
    Any other command returns an empty JSON object; in dry-run, post_json
    short-circuits before reaching a POST subprocess."""
    def _run(cmd, *a, **k):
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
    """The persisted finding must carry every field the two boundaries read."""

    def test_canonical_fields_all_present(self):
        f = canonical_finding()
        for field in CANONICAL_FIELDS:
            self.assertIn(field, f, f"pipeline finding must carry canonical field '{field}'")

    def test_v2_aliases_present_for_retained_poster(self):
        # post_review.py indexes f["file"]/f["line"] and reads body/end_line; the
        # persisted schema must carry these so the retained poster never KeyErrors.
        f = canonical_finding()
        for field in ("file", "line", "body", "end_line"):
            self.assertIn(field, f, f"persisted schema must carry v2 alias '{field}' for post_review.py")


class TestVerifyFindingsBoundary(unittest.TestCase):
    """verify_findings.py (positional path) consumes the canonical schema cleanly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verify_consumes_canonical_schema_without_error(self):
        findings_path = os.path.join(self.tmp, "findings.json")
        out_path = os.path.join(self.tmp, "out.json")
        diff_path = os.path.join(self.tmp, "diff.patch")
        with open(findings_path, "w") as fh:
            json.dump({"findings": [canonical_finding()], "base_branch": "main"}, fh)
        with open(diff_path, "w") as fh:
            fh.write(GH_DIFF)

        proc = subprocess.run(
            [
                sys.executable, str(REPO / "scripts" / "verify_findings.py"),
                findings_path, "--diff-file", diff_path, "--output", out_path,
            ],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"verify_findings.py errored on the pipeline schema: {proc.stderr}")

        with open(out_path) as fh:
            envelope = json.load(fh)
        for key in ("verified", "eliminated", "batches", "stats"):
            self.assertIn(key, envelope, f"verify envelope missing '{key}'")


class TestPostReviewBoundary(unittest.TestCase):
    """post_review.py --dry-run consumes the pipeline findings without a missing-field error."""

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
            json.dump({
                "platform": "github", "owner": "o", "repo": "r", "pr_number": 5,
                "review_body": "Deep review summary", "findings": findings,
            }, fh)

    def test_post_review_consumes_pipeline_finding_without_error(self):
        self._write([canonical_finding()])
        with patch.object(sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]), \
             patch("scripts.post_review.subprocess.run", side_effect=_fake_run(diff=GH_DIFF)):
            post_review.main()  # must not raise: every field it reads is present

        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        self.assertTrue(os.path.exists(payload_path), "dry-run payload was not captured")
        with open(payload_path) as fh:
            cap = json.load(fh)
        self.assertEqual(cap["platform"], "github")
        # The canonical finding rendered into exactly one inline comment (file:line valid).
        self.assertEqual(len(cap["payload"]["comments"]), 1)
        self.assertEqual(cap["payload"]["comments"][0]["path"], "foo.py")
        self.assertEqual(cap["payload"]["comments"][0]["line"], 2)

    def test_missing_v2_aliases_would_break_the_retained_poster(self):
        # Documents why the aliases are load-bearing: a canonical-only finding (no
        # `line`) makes post_review's direct index f["line"] raise KeyError. This is
        # the exact boundary the persisted-schema union closes.
        canonical_only = {k: v for k, v in canonical_finding().items() if k not in ("line", "end_line", "body")}
        self._write([canonical_only])
        with patch.object(sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]), \
             patch("scripts.post_review.subprocess.run", side_effect=_fake_run(diff=GH_DIFF)):
            with self.assertRaises(KeyError):
                post_review.main()


if __name__ == "__main__":
    unittest.main()
