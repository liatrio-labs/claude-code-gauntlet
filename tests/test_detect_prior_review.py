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
    made and its exit code drives sha_is_ancestor, and it appends a
    human-readable explanation to errors[] both when the head cannot be
    resolved and when the last-reviewed commit is absent from the clone.
  - build_result is also exercised end-to-end against a REAL resolve_git_facts
    result (git subprocess calls patched), including the non-ancestor
    (rebase/force-push) case, proving build_result's assumed input shape is
    actually compatible with what resolve_git_facts emits — not just assumed
    via the hand-built _git_facts() fixture.
  - GitHub reviews are fetched with --paginate too, mirroring the GitLab
    pagination pin below (PR reviews come back oldest-first at 30/page, so an
    unpaginated fetch would silently lose the newest marker on a busy PR).
  - sanitize_marker: allow-listed echo keys, unknown key NAMES only (values
    never echoed verbatim), and truncation of an oversized payload.
  - run()'s errors="replace" decode: undecodable bytes from gh/glab/git must
    not raise, and the CLI must still exit 0 with valid JSON on stdout.
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
            # Faithful to real git: `git cat-file -e <bad>^{commit}` is a
            # "fatal:" error exiting 128, not a plain 1, and it names the
            # object it could not find. Verified empirically: `git cat-file -e
            # 000...0^{commit}` -> "fatal: Not a valid object name
            # 000...0^{commit}", exit 128.
            return res(rc=0 if resolvable else 128,
                       err="" if resolvable else f"fatal: Not a valid object name {cmd[-1]}")
        if "merge-base" in cmd:
            # Faithful to real git: a plain "not an ancestor" answer from
            # `merge-base --is-ancestor` is silent on stderr and exits 1 — it
            # is a boolean "no", not an error. Verified empirically. (A truly
            # invalid commit would exit 128 with a "fatal:" message, but that
            # branch is unreachable here — merge-base only runs after cat-file
            # has already proven the sha resolvable.)
            return res(rc=0 if ancestor else 1)
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
            # Faithful to real git: an unresolvable rev is a "fatal:" error
            # exiting 128, not 1. Verified empirically: `git rev-parse
            # deadbeef` -> "fatal: ambiguous argument 'deadbeef': unknown
            # revision or path not in the working tree.", exit 128.
            return res(
                err=f"fatal: ambiguous argument '{rev}': unknown revision or "
                    "path not in the working tree.",
                rc=128,
            )
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


class TestFetchEntriesGithubPagination(unittest.TestCase):
    """Mirrors TestFetchEntriesGitlabPagination below: GitHub returns PR
    reviews oldest-first at 30/page (gh's default page size), so an
    unpaginated fetch on any PR with more than 30 reviews would silently lose
    the newest marker — the same class of bug --paginate fixes on the GitLab
    side, previously unpinned on this side."""

    def test_paginate_flag_is_present_in_gh_argv(self):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return SimpleNamespace(stdout="[]", stderr="", returncode=0)

        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            detect_prior_review.fetch_entries_github("o", "r", 5)

        self.assertEqual(len(calls), 1)
        self.assertIn("--paginate", calls[0])
        joined = " ".join(calls[0])
        self.assertIn("pulls/5/reviews", joined)


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
                head_sha, sha_is_ancestor, new_commit_count):
    """Build a git_facts dict from ONLY the six keys resolve_git_facts ever
    emits: head_sha, last_reviewed_sha, last_reviewed_sha_short,
    sha_resolvable, sha_is_ancestor, new_commit_count. head_advanced and
    incremental_safe are NOT among them — those are build_result's OWN
    outputs, computed from these six plus the signal. Fabricating them here
    as fixture inputs would let this fixture drift away from what
    resolve_git_facts actually returns without any test noticing."""
    return {
        "sha_resolvable": sha_resolvable,
        "last_reviewed_sha": last_reviewed_sha,
        "last_reviewed_sha_short": last_reviewed_sha_short,
        "head_sha": head_sha,
        "sha_is_ancestor": sha_is_ancestor,
        "new_commit_count": new_commit_count,
    }


class TestBuildResult(unittest.TestCase):

    def test_found_and_head_advanced(self):
        signal = {
            "sha": FULL_SHA, "signal": "marker", "legacy": False, "source": "review",
            "marker": {"version": "3.0", "findings_count": 4, "sha": FULL_SHA},
        }
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, HEAD_SHA, True, 3)
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
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, FULL_SHA, True, 0)
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
        facts = _git_facts(False, raw_sha, raw_sha[:8], HEAD_SHA, False, None)
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
        facts = _git_facts(True, FULL_SHA, SHORT_SHA, HEAD_SHA, False, 0)
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


class TestBuildResultWithRealResolveGitFacts(unittest.TestCase):
    """End-to-end: a REAL resolve_git_facts() result (git subprocess calls
    patched) piped directly into build_result — proving the two functions'
    dict shapes are actually compatible, rather than assumed via the
    hand-built _git_facts() fixture used by TestBuildResult above. Includes
    the non-ancestor (rebase/force-push) case, which nothing previously
    exercised end to end."""

    def _signal(self, sha):
        return {
            "sha": sha, "signal": "marker", "legacy": False, "source": "review",
            "marker": {"version": "3.0", "findings_count": 1, "sha": sha},
        }

    def test_forward_moving_branch_is_incremental_safe(self):
        fake_run = _fake_git_run(resolvable=True, full_sha=FULL_SHA, head_sha=HEAD_SHA,
                                  commit_count=3, ancestor=True)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            git_facts = detect_prior_review.resolve_git_facts(FULL_SHA, HEAD_SHA)

        result = detect_prior_review.build_result(self._signal(FULL_SHA), git_facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertTrue(result["sha_resolvable"])
        self.assertTrue(result["sha_is_ancestor"])
        self.assertTrue(result["head_advanced"])
        self.assertTrue(result["incremental_safe"])
        self.assertEqual(result["new_commit_count"], 3)

    def test_non_ancestor_rebase_or_force_push_is_not_incremental_safe(self):
        """The reviewed commit still exists in the object DB (sha_resolvable
        True) but a backwards force-push/rebase has moved it off head's
        ancestry. TestBuildResult covers this branch against a hand-built
        fixture; this proves the SAME outcome against resolve_git_facts'
        actual output — the case the module docstring calls out as never
        exercised end to end."""
        fake_run = _fake_git_run(resolvable=True, full_sha=FULL_SHA, head_sha=HEAD_SHA,
                                  commit_count=0, ancestor=False)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            git_facts = detect_prior_review.resolve_git_facts(FULL_SHA, HEAD_SHA)

        result = detect_prior_review.build_result(self._signal(FULL_SHA), git_facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertTrue(result["sha_resolvable"])
        self.assertFalse(result["sha_is_ancestor"])
        self.assertFalse(result["head_advanced"])
        self.assertFalse(result["incremental_safe"])

    def test_sha_unresolvable_is_not_incremental_safe(self):
        fake_run = _fake_git_run(resolvable=False, head_sha=HEAD_SHA)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            git_facts = detect_prior_review.resolve_git_facts(FULL_SHA, HEAD_SHA)

        result = detect_prior_review.build_result(self._signal(FULL_SHA), git_facts)

        self.assertTrue(result["previously_reviewed"])
        self.assertFalse(result["sha_resolvable"])
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


class TestResolveGitFactsErrorMessages(unittest.TestCase):
    """resolve_git_facts appends a human-readable explanation to errors[] in
    two situations: the head commit cannot be resolved at all, and the
    last-reviewed commit is absent from this clone. Both strings feed the
    caller's degradation disclosure and were previously unpinned."""

    def test_head_unresolvable_appends_explanation(self):
        def fake_run(cmd, *a, **k):
            if cmd[:2] == ["git", "rev-parse"]:
                return SimpleNamespace(stdout="", stderr="fatal: not a git repository",
                                        returncode=128)
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            errors = []
            facts = detect_prior_review.resolve_git_facts(None, None, errors)

        self.assertEqual(facts["head_sha"], "unknown")
        self.assertTrue(
            any("could not resolve the head commit" in e for e in errors), errors)

    def test_last_reviewed_commit_absent_appends_explanation(self):
        fake_run = _fake_git_run(resolvable=False)
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            errors = []
            facts = detect_prior_review.resolve_git_facts(FULL_SHA, HEAD_SHA, errors)

        self.assertFalse(facts["sha_resolvable"])
        self.assertTrue(
            any("is not present in this clone" in e for e in errors), errors)
        self.assertTrue(
            any(FULL_SHA[:8] in e for e in errors),
            "the short sha should be named in the explanation")


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


class TestGithubFetch(_CliTestBase):
    """GitHub happy-path coverage, mirroring TestGitlabFetch above — until now
    GitHub (the primary platform) had only failure-path coverage in this file."""

    def test_github_reviews_are_fetched_and_a_signal_is_recovered(self):
        review_body = "## Summary\nSome pre-existing narrative text.\n"
        review_body += review_marker.build_footer(3, FULL_SHA, body=review_body)
        reviews = [{"id": 9, "body": review_body, "submitted_at": "2026-01-01T00:00:00Z"}]

        argv = ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run",
                   side_effect=_fake_gh_glab_and_git_run(
                       reviews=reviews, git_run=_fake_git_run(resolvable=True))):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["signal"], "marker")
        self.assertEqual(result["source"], "review")
        self.assertEqual(result["last_reviewed_sha"], FULL_SHA)

    def test_paginated_concatenated_json_arrays_are_flattened_and_newest_marker_wins(self):
        """``gh api --paginate`` emits one JSON array per page, concatenated back
        to back in the raw stdout — not one merged array (see
        detect_prior_review._parse_json_array's own docstring). Build the fake
        stdout as two literal concatenated arrays, an older marker on 'page 1'
        and a newer one on 'page 2', and prove both pages' entries are flattened
        into the scan and the timestamp-newest signal wins regardless of which
        page carried it."""
        older_sha = "c" * 40
        newer_sha = "d" * 40
        page1 = json.dumps([
            {"id": 1, "body": review_marker.build_marker(older_sha, 1),
             "submitted_at": "2026-01-01T00:00:00Z"},
        ])
        page2 = json.dumps([
            {"id": 2, "body": review_marker.build_marker(newer_sha, 2),
             "submitted_at": "2026-06-01T00:00:00Z"},
        ])
        concatenated = page1 + page2  # exactly what --paginate emits: arrays back to back

        def fake_run(cmd, *a, **k):
            joined = " ".join(cmd)
            if "pulls" in joined and "reviews" in joined:
                return SimpleNamespace(stdout=concatenated, stderr="", returncode=0)
            if cmd and cmd[0] == "git":
                return _fake_git_run(resolvable=True)(cmd, *a, **k)
            return SimpleNamespace(stdout="{}", stderr="", returncode=0)

        argv = ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch("scripts.detect_prior_review.subprocess.run", side_effect=fake_run):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        result = json.loads(out.strip())
        self.assertTrue(result["previously_reviewed"])
        self.assertEqual(result["last_reviewed_sha"], newer_sha)
        self.assertEqual(result["scanned"], {"review": 2}, "both pages' entries must be counted")


class TestSanitizeMarker(unittest.TestCase):
    """sanitize_marker allow-lists echoed marker keys, lists unknown key NAMES
    only (values never echoed), and truncates an oversized payload. The
    parsed marker is attacker-controllable — anyone with read access can post
    a comment carrying one — and the orchestrator is told to consume the
    `marker` object, so an unbounded verbatim echo would pipe arbitrary text
    straight into a model's context."""

    def test_normal_payload_round_trips_unchanged(self):
        marker = {
            "version": "3.0", "findings_count": 2, "sha": FULL_SHA,
            "_token": "code-gauntlet-findings", "_legacy": False,
        }
        self.assertEqual(detect_prior_review.sanitize_marker(marker), marker)

    def test_unknown_keys_appear_by_name_only_value_not_echoed(self):
        marker = {
            "version": "3.0", "sha": FULL_SHA,
            "attacker_field": "SECRET_PAYLOAD_VALUE_SHOULD_NOT_APPEAR",
        }
        out = detect_prior_review.sanitize_marker(marker)
        self.assertIn("unknown_keys", out)
        self.assertIn("attacker_field", out["unknown_keys"])
        self.assertNotIn("attacker_field", out)
        self.assertNotIn("SECRET_PAYLOAD_VALUE_SHOULD_NOT_APPEAR", json.dumps(out))

    def test_huge_value_is_truncated_not_echoed_verbatim(self):
        """Assert the OUTCOME (the payload is bounded) rather than which branch
        produced it: values are clipped individually before the whole-payload cap
        is consulted, so a single huge value never reaches the `truncated`
        fallback. Pinning the branch would fail on that strictly better bound."""
        huge = "X" * 100000
        marker = {"version": "3.0", "findings_count": 1, "sha": FULL_SHA, "findings": huge}
        out = detect_prior_review.sanitize_marker(marker)
        dumped = json.dumps(out)
        self.assertNotIn(huge, dumped)
        self.assertLessEqual(len(dumped), detect_prior_review._MARKER_ECHO_MAX_CHARS)
        self.assertEqual(out["sha"], FULL_SHA)
        self.assertEqual(out["version"], "3.0")
        self.assertEqual(out["findings_count"], 1)

    def test_allowlisted_key_values_are_bounded_individually(self):
        """An allow-listed key is still attacker-controlled — `version` passing
        the key filter must not let 60 KB of prose through into the
        orchestrator's context."""
        marker = {"version": "INJECT " * 9000, "findings_count": 1,
                  "sha": FULL_SHA, "_token": "code-gauntlet-findings", "_legacy": False}
        out = detect_prior_review.sanitize_marker(marker)
        self.assertLessEqual(
            len(json.dumps(out)), detect_prior_review._MARKER_ECHO_MAX_CHARS)
        self.assertLess(len(str(out["version"])), 1000)
        self.assertEqual(out["sha"], FULL_SHA)

    def test_whole_payload_cap_still_fires_for_many_bounded_values(self):
        """The `truncated` fallback stays reachable: many individually-bounded
        values can still exceed the total cap."""
        marker = {"version": "v" * 400, "findings_count": 1, "sha": FULL_SHA,
                  "findings": ["f" * 400] * 40,
                  "_token": "code-gauntlet-findings", "_legacy": False}
        out = detect_prior_review.sanitize_marker(marker)
        self.assertLessEqual(
            len(json.dumps(out)), detect_prior_review._MARKER_ECHO_MAX_CHARS)
        self.assertEqual(out["sha"], FULL_SHA)

    def test_non_dict_marker_returns_none(self):
        self.assertIsNone(detect_prior_review.sanitize_marker(None))
        self.assertIsNone(detect_prior_review.sanitize_marker("not a dict"))
        self.assertIsNone(detect_prior_review.sanitize_marker([1, 2, 3]))


class TestRunSurvivesNonUtf8Stdout(unittest.TestCase):
    """run() passes errors='replace' to subprocess.run, so a single
    undecodable byte in gh/glab/git's stdout — a UnicodeDecodeError, which is
    a ValueError, not an OSError — cannot escape run()'s except clauses and
    break the always-exit-0 contract the caller degrades on. Both tests drive
    the REAL subprocess module with a python3 child process that writes
    invalid UTF-8 bytes, not the process-wide `scripts.detect_prior_review.
    subprocess.run` mock used elsewhere in this file, so the assertion is
    against actual OS decode behavior rather than a fake that could lie about
    it. No network: the child process and the one real `git rev-parse HEAD`
    call the end-to-end test makes are both local-only."""

    def test_run_wrapper_never_raises_and_replaces_undecodable_bytes(self):
        cmd = [sys.executable, "-c",
               r"import sys; sys.stdout.buffer.write(b'garbage \xff\xfe bytes')"]
        stdout, stderr, rc = detect_prior_review.run(cmd, timeout=10)
        self.assertEqual(rc, 0)
        self.assertIn("garbage", stdout)
        self.assertIn("bytes", stdout)
        self.assertIn("�", stdout,
                       "undecodable bytes must be replaced, not silently dropped")

    def test_cli_end_to_end_non_utf8_fetch_still_exits_zero_with_valid_json(self):
        """fetch_entries_github is monkeypatched to route through the REAL
        run() wrapper against a python3 child that writes undecodable bytes,
        so main()'s exit code and stdout validity are proven against real
        decode behavior end to end, not a mock that bypasses run()'s own
        subprocess.run call entirely."""

        def fake_fetch_entries_github(owner, repo, number):
            cmd = [sys.executable, "-c",
                   r"import sys; sys.stdout.buffer.write(b'\xff\xfe' + b'not json')"]
            stdout, _, _ = detect_prior_review.run(cmd, timeout=10)
            items = detect_prior_review._parse_json_array(stdout)
            if items is None:
                return [], [f"github reviews: response was not JSON: {stdout[:60]!r}"]
            return detect_prior_review.collect_entries_github(items), []

        argv = ["--platform", "github", "--owner", "o", "--repo", "r", "--number", "5"]
        with patch.object(detect_prior_review, "fetch_entries_github",
                           side_effect=fake_fetch_entries_github):
            out, code = _run_main(argv)

        self.assertEqual(code, 0)
        decoder = json.JSONDecoder()
        obj, end = decoder.raw_decode(out.strip())
        self.assertIsInstance(obj, dict)
        self.assertEqual(end, len(out.strip()))
        self.assertFalse(obj["previously_reviewed"])
        self.assertTrue(obj["errors"])


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
