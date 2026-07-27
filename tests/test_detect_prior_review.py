"""
Tests for scripts/detect_prior_review.py (Issue #39).

Written FROM THE DESIGN SPEC ALONE, not by reading scripts/detect_prior_review.py —
see tests/test_review_marker.py's module docstring for the double-entry rationale.

Covers:
  - Pure collectors: collect_entries_github / collect_entries_gitlab map raw
    GitHub/GitLab API payloads to the {body, timestamp, source, id} entry shape.
    GitHub scans exactly one surface (pulls/{n}/reviews) — issues/{n}/comments
    was dropped entirely; a test pins that fetch_entries_github issues exactly
    one call and that call never touches issues/.
  - GitLab notes are fetched with --paginate (post_gitlab posts the marker
    summary note FIRST, so an unpaginated fetch can miss it past page 1).
  - build_result(signal, git_facts): the branches (found+advanced,
    found+not-advanced, found+sha-unresolvable, found+resolvable-but-not-an-
    ancestor, not-found), pinning incremental_safe == sha_resolvable and
    head_advanced in every branch, and head_advanced additionally requiring
    sha_is_ancestor.
  - resolve_git_facts: the `git merge-base --is-ancestor <sha> <head>` call is
    made and its exit code drives sha_is_ancestor.
  - CLI end-to-end via --bodies-file with git subprocess calls patched: stdout
    parses as exactly one JSON object, exit 0, fields match. Non-ASCII content
    (a marker's `findings` extension slot, a non-ASCII bodies-file path) still
    prints parseable, ASCII-safe JSON (ensure_ascii=True).
  - remote_slug() accepts scp-style (git@host:owner/repo) and any-scheme
    (https, http, ssh, git, git+ssh) remote URLs, with optional user@, :port,
    and trailing slash, and keeps GitLab subgroup paths intact in *repo*.
  - Fetch failure: GitHub and GitLab each scan exactly one surface now, so a
    fetch failure degrades straight to previously_reviewed: false with
    errors[] populated and exit 0 — there is no second surface to fall back to.
  - Argparse usage errors: a missing --number (with no --bodies-file, and an
    unparseable remote) is a RECOVERABLE outcome — exit 0, valid JSON, errors[]
    populated. Only a genuinely malformed flag (missing --platform, invalid
    --platform choice, an unknown flag) is a non-zero argparse exit.

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


def _fake_git_run(resolvable=True, full_sha=FULL_SHA, head_sha=HEAD_SHA, commit_count=3,
                   ancestor=True):
    """A ``subprocess.run`` side_effect mocking the read-only git calls the SHA
    resolution step makes: ``git cat-file -e {sha}^{commit}``, ``git merge-base
    --is-ancestor {sha} {head}``, ``git rev-parse {sha}``/``git rev-parse HEAD``,
    and ``git rev-list --count {sha}..HEAD``.

    *ancestor* drives the merge-base exit code (0 => is-an-ancestor => True) and
    defaults to True — the common case in these tests is a normal forward-moving
    branch where the previously-reviewed commit is still an ancestor of head.
    """
    def _run(cmd, *a, **k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)
        if "cat-file" in cmd:
            return res(rc=0 if resolvable else 1,
                       err="" if resolvable else "fatal: Not a valid object name")
        if "merge-base" in cmd:
            return res(rc=0 if ancestor else 1,
                       err="" if ancestor else "fatal: Not an ancestor")
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


def _fake_gh_glab_and_git_run(reviews=None, notes=None,
                               reviews_rc=0, notes_rc=0,
                               git_run=None):
    """A combined side_effect for the fetch + SHA-resolution subprocess calls.

    Only two API surfaces exist post-contract-change: github `pulls/{n}/reviews`
    and gitlab `merge_requests/{n}/notes`. There is no `issues/{n}/comments`
    branch to fake here — that surface was dropped from the script entirely
    (nothing ever wrote the signal there, and any read-access user could post a
    forged marker to it).
    """
    git_run = git_run or _fake_git_run()

    def _run(cmd, *a, **k):
        def res(out="", err="", rc=0):
            return SimpleNamespace(stdout=out, stderr=err, returncode=rc)
        joined = " ".join(cmd)
        if "pulls" in joined and "reviews" in joined:
            if reviews_rc != 0:
                return res(err="gh: fetch failed", rc=reviews_rc)
            return res(out=json.dumps(reviews if reviews is not None else []))
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
    """collect_entries_github now takes a single argument (payload_reviews) —
    the issues/{n}/comments surface was dropped."""

    def test_maps_reviews_to_entry_shape(self):
        reviews = [{"id": 1, "body": "review body one", "submitted_at": "2026-01-01T00:00:00Z"}]
        entries = detect_prior_review.collect_entries_github(reviews)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["body"], "review body one")
        self.assertEqual(entries[0]["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(entries[0]["source"], "review")
        self.assertEqual(entries[0]["id"], 1)

    def test_multiple_reviews_all_mapped(self):
        reviews = [
            {"id": 1, "body": "r1", "submitted_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "body": "r2", "submitted_at": "2026-02-01T00:00:00Z"},
        ]
        entries = detect_prior_review.collect_entries_github(reviews)
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["source"] == "review" for e in entries))

    def test_empty_payload_produces_no_entries(self):
        self.assertEqual(detect_prior_review.collect_entries_github([]), [])


class TestFetchEntriesGithubSingleSurface(unittest.TestCase):
    """Pins the security decision behind dropping issues/{n}/comments: nothing
    ever wrote the signal there, while any user with read access could post to
    it, and since the newest signal wins, a forged marker there could aim a
    rerun at an attacker-chosen SHA. fetch_entries_github must therefore issue
    exactly one fetch, and it must be the reviews endpoint."""

    def test_issues_exactly_one_fetch_and_it_is_the_reviews_endpoint(self):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return SimpleNamespace(stdout="[]", stderr="", returncode=0)

        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            entries, errors = detect_prior_review.fetch_entries_github("o", "r", 5)

        self.assertEqual(len(calls), 1, "exactly one fetch must be issued")
        joined = " ".join(calls[0])
        self.assertIn("pulls/", joined)
        self.assertIn("reviews", joined)
        self.assertNotIn("issues/", joined)
        self.assertEqual(entries, [])
        self.assertEqual(errors, [])


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


class TestFetchEntriesGitlabPagination(unittest.TestCase):
    """Pins a real shipped bug: GitLab returns 20 notes/page and post_gitlab
    posts the marker-bearing summary note FIRST, so without --paginate the
    marker is invisible on any MR with more than 20 notes."""

    def test_paginate_flag_is_present_in_glab_argv(self):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return SimpleNamespace(stdout="[]", stderr="", returncode=0)

        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            detect_prior_review.fetch_entries_gitlab("o", "r", 5)

        self.assertEqual(len(calls), 1)
        self.assertIn("--paginate", calls[0])
        joined = " ".join(calls[0])
        self.assertIn("merge_requests/5/notes", joined)


# ---------------------------------------------------------------------------
# build_result — the branches.
# ---------------------------------------------------------------------------

def _git_facts(sha_resolvable, last_reviewed_sha, last_reviewed_sha_short,
                head_sha, sha_is_ancestor, head_advanced, new_commit_count,
                incremental_safe):
    return {
        "sha_resolvable": sha_resolvable,
        "last_reviewed_sha": last_reviewed_sha,
        "last_reviewed_sha_short": last_reviewed_sha_short,
        "head_sha": head_sha,
        "sha_is_ancestor": sha_is_ancestor,
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
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, HEAD_SHA, True, True, 3, True)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["signal"], "marker")
        self.assertEqual(result["source"], "review")
        self.assertFalse(result["legacy"])
        self.assertEqual(result["last_reviewed_sha"], FULL_SHA)
        self.assertEqual(result["last_reviewed_sha_short"], SHORT_SHA)
        self.assertTrue(result["sha_resolvable"])
        self.assertTrue(result["sha_is_ancestor"])
        self.assertEqual(result["head_sha"], HEAD_SHA)
        self.assertTrue(result["head_advanced"])
        self.assertEqual(result["new_commit_count"], 3)
        self.assertTrue(result["incremental_safe"])
        self.assertEqual(result["marker"], signal["marker"])
        self.assertEqual(
            result["incremental_safe"], result["sha_resolvable"] and result["head_advanced"],
        )

    def test_found_and_head_not_advanced(self):
        # Same sha as head: previously-reviewed commit is trivially its own
        # ancestor, but there is nothing new to review.
        signal = {
            "sha": FULL_SHA, "signal": "footer", "legacy": False, "source": "issue_comment",
            "marker": None,
        }
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, FULL_SHA, True, False, 0, False)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertTrue(result["sha_resolvable"])
        self.assertTrue(result["sha_is_ancestor"])
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
        facts = _git_facts(False, raw_sha, raw_sha[:8], HEAD_SHA, False, False, None, False)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertFalse(result["sha_resolvable"])
        self.assertFalse(result["sha_is_ancestor"])
        self.assertFalse(result["head_advanced"])
        self.assertFalse(result["incremental_safe"])
        self.assertEqual(result["last_reviewed_sha"], raw_sha, "raw value is kept when unresolvable")
        self.assertIsNone(result["new_commit_count"])
        self.assertTrue(result["legacy"])
        self.assertEqual(
            result["incremental_safe"], result["sha_resolvable"] and result["head_advanced"],
        )

    def test_found_resolvable_but_not_ancestor_head_not_advanced(self):
        """A branch force-pushed BACKWARDS: the old commit is still present in
        the object DB (sha_resolvable True) but is no longer an ancestor of
        head. An inequality test alone would call this "advanced" with
        new_commit_count 0 — head_advanced and incremental_safe must both stay
        False even though sha_resolvable is True and the shas differ."""
        signal = {
            "sha": FULL_SHA, "signal": "marker", "legacy": False, "source": "review",
            "marker": {"version": "3.0", "findings_count": 1, "sha": FULL_SHA},
        }
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, HEAD_SHA, False, False, 0, False)
        result = detect_prior_review.build_result(signal, facts)

        self.assertTrue(result["sha_resolvable"])
        self.assertFalse(result["sha_is_ancestor"])
        self.assertFalse(result["head_advanced"])
        self.assertFalse(result["incremental_safe"])
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
        self.assertFalse(result["sha_is_ancestor"])
        self.assertFalse(result["incremental_safe"])


# ---------------------------------------------------------------------------
# resolve_git_facts — proves the merge-base --is-ancestor call is made and its
# exit code drives sha_is_ancestor.
# ---------------------------------------------------------------------------

class TestResolveGitFactsMergeBase(unittest.TestCase):

    def _tracked(self, **kwargs):
        calls = []
        inner = _fake_git_run(**kwargs)

        def _run(cmd, *a, **k):
            calls.append(cmd)
            return inner(cmd, *a, **k)
        return _run, calls

    def test_is_ancestor_call_made_with_sha_and_head_and_true_exit_sets_field_true(self):
        fake_run, calls = self._tracked(resolvable=True, ancestor=True)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            facts = detect_prior_review.resolve_git_facts(FULL_SHA, HEAD_SHA)

        merge_base_calls = [c for c in calls if "merge-base" in c]
        self.assertEqual(len(merge_base_calls), 1, "merge-base --is-ancestor must be called exactly once")
        self.assertIn("--is-ancestor", merge_base_calls[0])
        self.assertIn(FULL_SHA, merge_base_calls[0])
        self.assertIn(HEAD_SHA, merge_base_calls[0])
        self.assertTrue(facts["sha_is_ancestor"])

    def test_is_ancestor_false_exit_sets_field_false(self):
        fake_run, calls = self._tracked(resolvable=True, ancestor=False)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            facts = detect_prior_review.resolve_git_facts(FULL_SHA, HEAD_SHA)

        merge_base_calls = [c for c in calls if "merge-base" in c]
        self.assertEqual(len(merge_base_calls), 1)
        self.assertFalse(facts["sha_is_ancestor"])


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


class TestNonAsciiOutputIsAsciiSafe(_CliTestBase):
    """Output is printed with json.dumps(result, indent=2) and the default
    ensure_ascii=True, so non-ASCII content anywhere in the payload — the
    marker's `findings` extension slot, or gh/glab stderr text surfaced into
    errors[] — cannot raise UnicodeEncodeError under an ASCII stdout. Pinned by
    asserting stdout contains only ASCII codepoints yet still round-trips
    through json.loads to the original unicode values."""

    def test_marker_findings_with_non_ascii_prints_parseable_ascii_safe_json(self):
        findings_payload = [{"title": "café bug \U0001f41b"}]
        marker_body = review_marker.build_marker(FULL_SHA, 1, findings=findings_payload)
        entries = [{"body": marker_body, "timestamp": "2026-01-01T00:00:00Z",
                    "source": "review", "id": 1}]
        bodies_path = self._bodies_file(entries)
        argv = self._base_argv(bodies_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run(resolvable=True)):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        raw = out.strip()
        self.assertTrue(all(ord(c) < 128 for c in raw),
                         "stdout must be pure ASCII under ensure_ascii=True")
        result = json.loads(raw)
        self.assertEqual(result["marker"]["findings"], findings_payload)

    def test_bodies_file_read_error_with_non_ascii_path_is_ascii_safe(self):
        missing_path = os.path.join(self.tmp, "café-missing.json")
        argv = self._base_argv(missing_path)

        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run()):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        raw = out.strip()
        self.assertTrue(all(ord(c) < 128 for c in raw),
                         "stdout must be pure ASCII under ensure_ascii=True")
        result = json.loads(raw)
        self.assertFalse(result["previously_reviewed"])
        self.assertTrue(result["errors"])


# ---------------------------------------------------------------------------
# remote_slug() — accepted URL forms.
# ---------------------------------------------------------------------------

class TestRemoteSlug(unittest.TestCase):
    """remote_slug() parses the 'origin' remote URL. Accepted forms: scp-style
    git@host:owner/repo(.git), and any scheme with optional user@ and :port —
    https://, http://, ssh://, git://, git+ssh:// — plus an optional trailing
    slash. A GitLab subgroup path keeps its subgroups in *repo*."""

    def _slug_for(self, url):
        def fake_run(cmd, *a, **k):
            return SimpleNamespace(stdout=url + "\n", stderr="", returncode=0)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            return detect_prior_review.remote_slug()

    def test_accepted_url_forms(self):
        cases = [
            ("git@github.com:o/r.git", ("o", "r")),
            ("https://github.com/o/r.git", ("o", "r")),
            ("https://github.com/o/r", ("o", "r")),
            ("ssh://git@github.com/o/r.git", ("o", "r")),
            ("ssh://git@github.com:2222/o/r.git", ("o", "r")),
            ("git://host/o/r.git", ("o", "r")),
            ("git+ssh://git@host/o/r", ("o", "r")),
            ("https://gitlab.com/group/sub/proj.git", ("group", "sub/proj")),
            ("not a remote url at all", (None, None)),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(self._slug_for(url), expected)


# ---------------------------------------------------------------------------
# Fetch failures — D8: detection never blocks a review.
# ---------------------------------------------------------------------------

class TestFetchFailureDegradation(_CliTestBase):
    """GitHub and GitLab each scan exactly one surface now (the github
    issues/{n}/comments surface was dropped). 'One surface failing' therefore
    means the whole platform fetch failed — there is no second surface left to
    fall back to. These replace the old multi-surface-fallback assertions with
    the single-surface reality; coverage of "a fetch failure degrades to exit 0
    with errors populated" is retained for both platforms."""

    def test_github_fetch_failing_yields_exit_zero_with_errors_and_no_signal(self):
        argv = ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_gh_glab_and_git_run(
                       reviews_rc=1, git_run=_fake_git_run(resolvable=True))):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertFalse(result["previously_reviewed"])
        self.assertIsNone(result["signal"])
        self.assertFalse(result["incremental_safe"])
        self.assertEqual(len(result["errors"]), 1, "the single github surface's failure must be recorded")
        self.assertIn("scanned", result)

    def test_gitlab_fetch_failing_yields_exit_zero_with_errors_and_no_signal(self):
        argv = ["--platform", "gitlab", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_gh_glab_and_git_run(
                       notes_rc=1, git_run=_fake_git_run(resolvable=True))):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertFalse(result["previously_reviewed"])
        self.assertIsNone(result["signal"])
        self.assertFalse(result["incremental_safe"])
        self.assertEqual(len(result["errors"]), 1, "the single gitlab surface's failure must be recorded")
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
# Argparse usage errors vs. recoverable outcomes.
# ---------------------------------------------------------------------------

class TestArgparseUsageErrors(unittest.TestCase):

    def test_missing_required_platform_is_nonzero_exit(self):
        _, code = _run_main(["--owner", "o", "--repo", "r", "--number", "5"])
        self.assertNotEqual(code, 0)

    def test_missing_number_without_bodies_file_degrades_to_exit_zero_with_errors(self):
        """A missing --number (no --bodies-file either) is a RECOVERABLE
        outcome, not an argparse usage error: exit 0, valid JSON on stdout,
        previously_reviewed false, and a non-empty errors[]. gather_entries no
        longer calls parser.error for this case."""
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_git_run()):
            out, code = _run_main(["--platform", "github"])

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertFalse(result["previously_reviewed"])
        self.assertTrue(result["errors"])

    def test_invalid_platform_choice_is_nonzero_exit(self):
        _, code = _run_main(["--platform", "bitbucket", "--owner", "o",
                              "--repo", "r", "--number", "5"])
        self.assertNotEqual(code, 0)

    def test_unknown_flag_is_nonzero_exit(self):
        """A genuinely malformed flag is still an argparse usage error — only
        the recoverable, data-dependent failures (missing --number, an
        unparseable remote) were moved to exit-0-with-errors."""
        _, code = _run_main(["--platform", "github", "--nope", "wat"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
