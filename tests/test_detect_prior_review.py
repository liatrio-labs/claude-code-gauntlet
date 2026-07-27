"""
Tests for scripts/detect_prior_review.py (Issue #39).

Written FROM THE DESIGN SPEC ALONE, not by reading scripts/detect_prior_review.py —
see tests/test_review_marker.py's module docstring for the double-entry rationale.

Covers:
  - Pure collectors: collect_entries_github / collect_entries_gitlab map raw
    GitHub/GitLab API payloads to the {body, timestamp, source, id} entry shape.
  - build_result(signal, git_facts): the four branches (found+advanced,
    found+not-advanced, found+unresolvable, not-found), pinning
    incremental_safe == sha_resolvable and head_advanced in every branch.
  - CLI end-to-end via --bodies-file with git subprocess calls patched: stdout
    parses as exactly one JSON object, exit 0, fields match.
  - Fetch failure on one surface still returns the other surface's signal and
    records errors[]; all fetches failing still exits 0 with previously_reviewed:
    false and errors populated.
  - Argparse usage errors (missing/invalid --platform) are the only non-zero exit.

No network: git and gh/glab calls are all patched via
``scripts.detect_prior_review.subprocess.run``.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.detect_prior_review as detect_prior_review
import scripts.review_marker as review_marker

# Hex-only fixed SHAs (valid under review_marker.SHA_RE regardless of context).
FULL_SHA = "a" * 40
HEAD_SHA = "b" * 40
SHORT_SHA = FULL_SHA[:8]


def _run_main(argv):
    """Run detect_prior_review.main() with *argv*, capturing stdout.

    Returns (stdout, exit_code). main() may return None (implicit success) or
    raise SystemExit (argparse errors, or an explicit exit call) — either way
    this normalizes to a plain (stdout, code) pair so callers don't need to
    know which convention the implementation picked.
    """
    stdout = io.StringIO()
    code = 0
    with patch.object(sys, "argv", ["detect_prior_review.py"] + list(argv)), \
         contextlib.redirect_stdout(stdout):
        try:
            detect_prior_review.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return stdout.getvalue(), code


def _fake_git_run(resolvable=True, full_sha=FULL_SHA, head_sha=HEAD_SHA, commit_count=3):
    """A ``subprocess.run`` side_effect mocking the read-only git calls the SHA
    resolution step makes: ``git cat-file -e {sha}^{commit}``, ``git rev-parse
    {sha}``/``git rev-parse HEAD``, and ``git rev-list --count {sha}..HEAD``.
    """
    def _run(cmd, *a, **k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)
        joined = " ".join(cmd)
        if "cat-file" in cmd:
            return res(rc=0 if resolvable else 1,
                       err="" if resolvable else "fatal: Not a valid object name")
        if "rev-parse" in cmd and cmd[-1] == "HEAD":
            return res(out=head_sha + "\n")
        if "rev-parse" in cmd:
            # Faithful to real git: `rev-parse` echoes an already-full object id
            # back unchanged and only *expands* an abbreviated one. A fake that
            # rewrote every rev to full_sha would hide which rev the caller
            # actually passed — exactly what an explicit --head-sha must prove.
            rev = cmd[-1]
            if len(rev) == 40:
                return res(out=rev + "\n")
            if resolvable:
                return res(out=full_sha + "\n")
            return res(err="fatal: ambiguous argument", rc=1)
        if "rev-list" in cmd:
            return res(out=f"{commit_count}\n")
        return res(out="{}")
    return _run


def _fake_gh_glab_and_git_run(reviews=None, comments=None, notes=None,
                               reviews_rc=0, comments_rc=0, notes_rc=0,
                               git_run=None):
    """A combined side_effect for the fetch + SHA-resolution subprocess calls."""
    git_run = git_run or _fake_git_run()

    def _run(cmd, *a, **k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)
        joined = " ".join(cmd)
        if "pulls" in joined and "reviews" in joined:
            if reviews_rc != 0:
                return res(err="gh: fetch failed", rc=reviews_rc)
            return res(out=json.dumps(reviews if reviews is not None else []))
        if "issues" in joined and "comments" in joined:
            if comments_rc != 0:
                return res(err="gh: fetch failed", rc=comments_rc)
            return res(out=json.dumps(comments if comments is not None else []))
        if "merge_requests" in joined and "notes" in joined:
            if notes_rc != 0:
                return res(err="glab: fetch failed", rc=notes_rc)
            return res(out=json.dumps(notes if notes is not None else []))
        if cmd and cmd[0] == "git":
            return git_run(cmd, *a, **k)
        return res(out="{}")
    return _run


# ---------------------------------------------------------------------------
# Pure collectors
# ---------------------------------------------------------------------------

class TestCollectEntriesGithub(unittest.TestCase):

    def test_maps_reviews_and_issue_comments_to_entry_shape(self):
        reviews = [{"id": 1, "body": "review body one", "submitted_at": "2026-01-01T00:00:00Z"}]
        comments = [{"id": 2, "body": "comment body two", "created_at": "2025-06-01T00:00:00Z"}]
        entries = detect_prior_review.collect_entries_github(reviews, comments)

        by_source = {e["source"]: e for e in entries}
        self.assertIn("review", by_source)
        self.assertIn("issue_comment", by_source)

        self.assertEqual(by_source["review"]["body"], "review body one")
        self.assertEqual(by_source["review"]["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(by_source["review"]["id"], 1)

        self.assertEqual(by_source["issue_comment"]["body"], "comment body two")
        self.assertEqual(by_source["issue_comment"]["timestamp"], "2025-06-01T00:00:00Z")
        self.assertEqual(by_source["issue_comment"]["id"], 2)

    def test_multiple_reviews_and_comments_all_mapped(self):
        reviews = [
            {"id": 1, "body": "r1", "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "body": "r2", "submitted_at": "2026-02-01T00:00:00Z"},
        ]
        comments = [{"id": 3, "body": "c1", "created_at": "2026-03-01T00:00:00Z"}]
        entries = detect_prior_review.collect_entries_github(reviews, comments)
        self.assertEqual(len(entries), 3)
        reviews_only = [e for e in entries if e["source"] == "review"]
        self.assertEqual(len(reviews_only), 2)

    def test_empty_payloads_produce_no_entries(self):
        self.assertEqual(detect_prior_review.collect_entries_github([], []), [])


class TestCollectEntriesGitlab(unittest.TestCase):

    def test_maps_notes_to_entry_shape(self):
        notes = [{"id": 3, "body": "note body three", "created_at": "2026-02-02T00:00:00Z"}]
        entries = detect_prior_review.collect_entries_gitlab(notes)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["body"], "note body three")
        self.assertEqual(entries[0]["timestamp"], "2026-02-02T00:00:00Z")
        self.assertEqual(entries[0]["source"], "note")
        self.assertEqual(entries[0]["id"], 3)

    def test_empty_notes_produce_no_entries(self):
        self.assertEqual(detect_prior_review.collect_entries_gitlab([]), [])


# ---------------------------------------------------------------------------
# build_result — the four branches.
# ---------------------------------------------------------------------------

def _git_facts(sha_resolvable, last_reviewed_sha, last_reviewed_sha_short,
                head_sha, head_advanced, new_commit_count, incremental_safe):
    return {
        "sha_resolvable": sha_resolvable,
        "last_reviewed_sha": last_reviewed_sha,
        "last_reviewed_sha_short": last_reviewed_sha_short,
        "head_sha": head_sha,
        "head_advanced": head_advanced,
        "new_commit_count": new_commit_count,
        "incremental_safe": incremental_safe,
    }


class TestBuildResult(unittest.TestCase):

    def test_found_and_head_advanced(self):
        signal = {
            "sha": FULL_SHA, "signal": "marker", "legacy": False, "source": "review",
            "marker": {"version": "3.0", "findings_count": 4, "sha": FULL_SHA},
        }
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, HEAD_SHA, True, 3, True)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["signal"], "marker")
        self.assertEqual(result["source"], "review")
        self.assertFalse(result["legacy"])
        self.assertEqual(result["last_reviewed_sha"], FULL_SHA)
        self.assertEqual(result["last_reviewed_sha_short"], SHORT_SHA)
        self.assertTrue(result["sha_resolvable"])
        self.assertEqual(result["head_sha"], HEAD_SHA)
        self.assertTrue(result["head_advanced"])
        self.assertEqual(result["new_commit_count"], 3)
        self.assertTrue(result["incremental_safe"])
        self.assertEqual(result["marker"], signal["marker"])
        self.assertEqual(
            result["incremental_safe"], result["sha_resolvable"] and result["head_advanced"],
        )

    def test_found_and_head_not_advanced(self):
        signal = {
            "sha": FULL_SHA, "signal": "footer", "legacy": False, "source": "issue_comment",
            "marker": None,
        }
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, FULL_SHA, False, 0, False)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertTrue(result["sha_resolvable"])
        self.assertFalse(result["head_advanced"])
        self.assertFalse(result["incremental_safe"])
        self.assertEqual(
            result["incremental_safe"], result["sha_resolvable"] and result["head_advanced"],
        )

    def test_found_but_sha_unresolvable(self):
        raw_sha = "c" * 40
        signal = {
            "sha": raw_sha, "signal": "marker", "legacy": True, "source": "note",
            "marker": {"version": "3.0", "sha": raw_sha},
        }
        facts = _git_facts(False, raw_sha, raw_sha[:8], HEAD_SHA, False, None, False)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertFalse(result["sha_resolvable"])
        self.assertFalse(result["head_advanced"])
        self.assertFalse(result["incremental_safe"])
        self.assertEqual(result["last_reviewed_sha"], raw_sha, "raw value is kept when unresolvable")
        self.assertIsNone(result["new_commit_count"])
        self.assertTrue(result["legacy"])
        self.assertEqual(
            result["incremental_safe"], result["sha_resolvable"] and result["head_advanced"],
        )

    def test_not_found(self):
        result = detect_prior_review.build_result(None, None)

        self.assertFalse(result["previously_reviewed"])
        self.assertIsNone(result["signal"])
        self.assertIsNone(result["source"])
        self.assertIsNone(result["marker"])
        self.assertIsNone(result["last_reviewed_sha"])
        self.assertFalse(result["incremental_safe"])


# ---------------------------------------------------------------------------
# CLI end-to-end — --bodies-file, git calls patched.
# ---------------------------------------------------------------------------

class _CliTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bodies_file(self, entries):
        path = os.path.join(self.tmp, "bodies.json")
        with open(path, "w") as f:
            json.dump(entries, f)
        return path

    def _base_argv(self, bodies_path, platform="github", **extra):
        argv = ["--platform", platform, "--owner", "o", "--repo", "r",
                "--number", "5", "--bodies-file", bodies_path]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return argv


class TestCliBodiesFile(_CliTestBase):

    def test_found_and_advanced_exits_zero_with_matching_fields(self):
        marker_body = review_marker.build_marker(FULL_SHA, 4)
        entries = [{"body": marker_body, "timestamp": "2026-01-01T00:00:00Z",
                    "source": "review", "id": 101}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run(resolvable=True)):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["signal"], "marker")
        self.assertEqual(result["last_reviewed_sha"], FULL_SHA)
        self.assertTrue(result["sha_resolvable"])
        self.assertEqual(result["head_sha"], HEAD_SHA)
        self.assertTrue(result["head_advanced"])
        self.assertTrue(result["incremental_safe"])
        self.assertEqual(result["errors"], [])

    def test_found_but_head_not_advanced(self):
        # Marker's sha is the same as HEAD — nothing new to review.
        marker_body = review_marker.build_marker(HEAD_SHA, 2)
        entries = [{"body": marker_body, "timestamp": "2026-01-01T00:00:00Z",
                    "source": "review", "id": 1}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run(resolvable=True, full_sha=HEAD_SHA,
                                              head_sha=HEAD_SHA, commit_count=0)):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertFalse(result["head_advanced"])
        self.assertFalse(result["incremental_safe"])

    def test_found_but_sha_unresolvable(self):
        marker_body = review_marker.build_marker(FULL_SHA, 1)
        entries = [{"body": marker_body, "timestamp": "2026-01-01T00:00:00Z",
                    "source": "review", "id": 1}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run(resolvable=False)):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertFalse(result["sha_resolvable"])
        self.assertFalse(result["incremental_safe"])

    def test_nothing_found_reports_previously_reviewed_false(self):
        entries = [{"body": "just a plain unrelated comment", "timestamp": "2026-01-01T00:00:00Z",
                    "source": "issue_comment", "id": 1}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run()):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertFalse(result["previously_reviewed"])
        self.assertIsNone(result["signal"])
        self.assertIsNone(result["source"])
        self.assertIsNone(result["marker"])
        self.assertIsNone(result["last_reviewed_sha"])
        self.assertFalse(result["incremental_safe"])

    def test_no_bodies_at_all_reports_previously_reviewed_false(self):
        bodies_path = self._bodies_file([])
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run()):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertFalse(result["previously_reviewed"])

    def test_head_sha_override_is_used_instead_of_git_rev_parse_head(self):
        override_head = "d" * 40
        marker_body = review_marker.build_marker(FULL_SHA, 1)
        entries = [{"body": marker_body, "timestamp": "2026-01-01T00:00:00Z",
                    "source": "review", "id": 1}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path, **{"head_sha": override_head})

        # The fake's "git rev-parse HEAD" branch would return HEAD_SHA (== b*40),
        # which differs from override_head — if the result matches override_head,
        # the flag was honored rather than shelling out for HEAD.
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run(resolvable=True)):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertEqual(result["head_sha"], override_head)

    def test_stdout_is_exactly_one_json_object(self):
        entries = [{"body": review_marker.build_marker(FULL_SHA, 1),
                    "timestamp": "2026-01-01T00:00:00Z", "source": "review", "id": 1}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run()):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        # A single json.loads over the *entire* stripped stdout must succeed and
        # must not leave trailing content — i.e. exactly one JSON object, no more.
        decoder = json.JSONDecoder()
        obj, end = decoder.raw_decode(out.strip())
        self.assertIsInstance(obj, dict)
        self.assertEqual(end, len(out.strip()))


# ---------------------------------------------------------------------------
# Fetch failures — D8: detection never blocks a review.
# ---------------------------------------------------------------------------

class TestFetchFailureDegradation(_CliTestBase):

    def test_one_surface_failing_still_returns_the_others_signal(self):
        marker_body = review_marker.build_marker(FULL_SHA, 2)
        comments = [{"id": 9, "body": marker_body, "created_at": "2026-01-01T00:00:00Z"}]

        argv = ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_gh_glab_and_git_run(
                       reviews_rc=1, comments=comments, comments_rc=0,
                       git_run=_fake_git_run(resolvable=True))):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["last_reviewed_sha"], FULL_SHA)
        self.assertTrue(result["errors"], "the failed surface must be recorded in errors")

    def test_all_surfaces_failing_exits_zero_with_no_signal(self):
        argv = ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_gh_glab_and_git_run(
                       reviews_rc=1, comments_rc=1,
                       git_run=_fake_git_run(resolvable=True))):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertFalse(result["previously_reviewed"])
        self.assertIsNone(result["signal"])
        self.assertFalse(result["incremental_safe"])
        self.assertTrue(result["errors"])
        self.assertGreaterEqual(len(result["errors"]), 2,
                                "both failed surfaces must be recorded")
        self.assertIn("scanned", result)


class TestGitlabFetch(_CliTestBase):

    def test_gitlab_notes_are_fetched_and_a_signal_is_recovered(self):
        marker_body = review_marker.build_marker(FULL_SHA, 1)
        notes = [{"id": 5, "body": marker_body, "created_at": "2026-01-01T00:00:00Z"}]

        argv = ["--platform", "gitlab", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_gh_glab_and_git_run(
                       notes=notes, git_run=_fake_git_run(resolvable=True))):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["last_reviewed_sha"], FULL_SHA)


# ---------------------------------------------------------------------------
# Argparse usage errors — the only non-zero-exit path (D8).
# ---------------------------------------------------------------------------

class TestArgparseUsageErrors(unittest.TestCase):

    def test_missing_required_platform_is_nonzero_exit(self):
        _, code = _run_main(["--owner", "o", "--repo", "r", "--number", "5"])
        self.assertNotEqual(code, 0)

    def test_missing_required_owner_repo_number_is_nonzero_exit(self):
        _, code = _run_main(["--platform", "github"])
        self.assertNotEqual(code, 0)

    def test_invalid_platform_choice_is_nonzero_exit(self):
        _, code = _run_main(["--platform", "bitbucket", "--owner", "o",
                              "--repo", "r", "--number", "5"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
