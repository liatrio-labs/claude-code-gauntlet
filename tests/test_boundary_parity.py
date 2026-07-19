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

The parity contract is that the persisted findings envelope carries the UNION: the
canonical fields for verify + downstream, plus the ``line`` / ``end_line`` / ``body``
aliases the retained poster indexes. writeArtifacts applies these aliases at the
persist boundary. This test drives REAL persisted pipeline output (produced by running
the wired stages through the node recorder) through BOTH scripts — verify positionally,
post_review --dry-run with the read-only CLI calls mocked — asserting neither errors,
then documents why the aliases are load-bearing via a KeyError negative control.
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

RECORDER = REPO / "workflows" / "test" / "tools" / "emit_persisted_findings.mjs"

# The canonical fields the brief pins as the pipeline schema surface (Task 13 Step 4).
CANONICAL_FIELDS = [
    "id", "title", "description", "dimension", "severity", "confidence",
    "file", "line_start", "line_end", "origin", "cross_file_refs", "report_destination",
]
# The v2 aliases the retained post_review poster indexes/reads.
V2_ALIAS_FIELDS = ["file", "line", "body", "end_line"]


def load_pipeline_findings():
    """Run the wired pipeline (via the node recorder) and return its REAL persisted
    high-confidence findings — v2-aliased at the writeArtifacts boundary."""
    tmp = tempfile.mkdtemp()
    try:
        out = os.path.join(tmp, "persisted.json")
        proc = subprocess.run(
            ["node", str(RECORDER), out],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
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
            self.assertIn(field, f, f"persisted pipeline finding must carry canonical field '{field}'")

    def test_v2_aliases_present_for_retained_poster(self):
        f = PERSISTED_FINDINGS[0]
        for field in V2_ALIAS_FIELDS:
            self.assertIn(field, f, f"persisted schema must carry v2 alias '{field}' for post_review.py")

    def test_aliases_mirror_canonical_values(self):
        f = PERSISTED_FINDINGS[0]
        self.assertEqual(f["line"], f["line_start"])
        self.assertEqual(f["end_line"], f["line_end"])
        self.assertEqual(f["body"], f["description"])


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
                sys.executable, str(REPO / "scripts" / "verify_findings.py"),
                findings_path, "--diff-file", diff_path, "--output", out_path,
            ],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"verify_findings.py errored on the persisted schema: {proc.stderr}")

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
            json.dump({
                "platform": "github", "owner": "o", "repo": "r", "pr_number": 5,
                "review_body": "Deep review summary", "findings": findings,
            }, fh)

    def test_post_review_consumes_pipeline_findings_without_error(self):
        diff = build_gh_diff(PERSISTED_FINDINGS)
        self._write(PERSISTED_FINDINGS)
        with patch.object(sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]), \
             patch("scripts.post_review.subprocess.run", side_effect=_fake_run(diff=diff)):
            post_review.main()  # must not raise: every field it reads is present

        payload_path = os.path.join(self.tmp, "post-review-payload.json")
        self.assertTrue(os.path.exists(payload_path), "dry-run payload was not captured")
        with open(payload_path) as fh:
            cap = json.load(fh)
        self.assertEqual(cap["platform"], "github")
        # Every persisted finding rendered into an inline comment (file:line valid).
        self.assertEqual(len(cap["payload"]["comments"]), len(PERSISTED_FINDINGS))

    def test_missing_v2_aliases_would_break_the_retained_poster(self):
        # Documents why the aliases are load-bearing: strip them from the REAL findings
        # and post_review's direct index f["line"] raises KeyError. This is the exact
        # boundary the persisted-schema union (writeArtifacts aliasing) closes.
        stripped = [
            {k: v for k, v in f.items() if k not in ("line", "end_line", "body")}
            for f in PERSISTED_FINDINGS
        ]
        self._write(stripped)
        with patch.object(sys, "argv", ["post_review.py", self.findings_path, "--dry-run"]), \
             patch("scripts.post_review.subprocess.run", side_effect=_fake_run(diff=build_gh_diff(PERSISTED_FINDINGS))):
            with self.assertRaises(KeyError):
                post_review.main()


if __name__ == "__main__":
    unittest.main()
