"""tests/test_report_patches.py — scripts/report_patches.py (issue #226).

report_patches.py is the read-only Phase 8 gate: it re-runs the diff-only subset of
delivery's deterministic apply-check (post_review.py's ``_gated_finding`` /
``_suggested_fix_gate``) against the PINNED review diff and renders every patch that
passes into a sibling artifact — never editing the report, never posting, never
mutating ``findings.json``.

Each test below states, in its docstring, the ONE thing it pins and (where
applicable) which mutation of the implementation it must catch. Verified by actually
reverting the fix locally and watching the named test go red — not by reading the
implementation and assuming a green run proves anything.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.post_review as post_review
import scripts.report_patches as report_patches


class ReportPatchesTestBase(unittest.TestCase):
    """Shared tempdir + run/parse helpers for every report_patches.py test below."""

    SHA = "abc1234"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)
        # Each report_patches.main() call resets this state itself — but a test
        # that pokes the gate directly (TestResetRunState below) must not leak
        # into a later test in the same process.
        self.addCleanup(post_review.reset_run_state)
        # report_patches.py bare-imports post_review's helpers (``from post_review
        # import ...``), which — because tests also do ``import scripts.post_review``
        # — makes bare "post_review" and "scripts.post_review" TWO SEPARATE module
        # objects with independent module-level state. reset_run_state on ONE does
        # not touch the other's counters; report_patches.main() reads/writes its own
        # bare-imported copy, so that one needs its own cleanup too.
        self.addCleanup(report_patches.reset_run_state)

    def _findings_path(self, sha=None):
        return os.path.join(self.tmp, f"code-gauntlet-findings-{sha or self.SHA}.json")

    def _diff_path(self, sha=None):
        return os.path.join(self.tmp, f"code-gauntlet-diff-{sha or self.SHA}.patch")

    def _artifact_path(self, sha=None):
        return os.path.join(self.tmp, f"code-gauntlet-patches-{sha or self.SHA}.md")

    def _write_findings(self, findings, sha=None):
        path = self._findings_path(sha)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(findings, fh)
        return path

    def _write_diff(self, text, sha=None):
        path = self._diff_path(sha)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def _write_diff_bytes(self, data, sha=None):
        path = self._diff_path(sha)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _run(self, sha=None, output_dir=None):
        """Run report_patches.main() with stdout/stderr captured.

        Returns (exit_code, receipt_dict_or_None, stdout_text, stderr_text,
        stdout_lines).
        """
        argv = ["--output-dir", output_dir or self.tmp, "--head-sha", sha or self.SHA]
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout_buf),
            contextlib.redirect_stderr(stderr_buf),
        ):
            exit_code = report_patches.main(argv)
        stdout_text = stdout_buf.getvalue()
        stderr_text = stderr_buf.getvalue()
        lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
        receipt = json.loads(lines[0]) if lines else None
        return exit_code, receipt, stdout_text, stderr_text, lines

    def _read_artifact(self, sha=None):
        with open(self._artifact_path(sha), encoding="utf-8") as fh:
            return fh.read()


# ---------------------------------------------------------------------------
# Diff fixtures — hand-written unified diffs, one shape per producer/edge case.
# ---------------------------------------------------------------------------

# A plain `gh pr diff`-shaped single-file diff: a/ b/ prefixed headers.
DIFF_GH_X = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,1 +1,3 @@\n"
    " def f():\n"
    "+    line2\n"
    "+    line3\n"
)


class TestGhShapedDiffKeepsPatch(ReportPatchesTestBase):
    """A gh-shaped (a/ b/ prefixed) diff + a finding whose patch differs from the
    current lines renders a kept patch: heading, fenced block, and a receipt that
    counts it."""

    def test_two_line_patch_on_gh_diff_is_kept_and_rendered(self):
        self._write_diff(DIFF_GH_X)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 3,
                    "title": "Title One",
                    "suggested_fix_code": "    replaced2\n    replaced3",
                }
            ]
        )

        exit_code, receipt, _, _, lines = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["ok"], True)
        self.assertEqual(receipt["oracle"], "ok")
        self.assertEqual(receipt["candidates"], 1)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)

        content = self._read_artifact()
        self.assertIn("## `x.py`:2-3 — Title One", content)
        self.assertIn("```py", content)
        self.assertIn("    replaced2\n    replaced3", content)


class TestGlabVerbatimRealDir(ReportPatchesTestBase):
    """A glab-shaped diff (verbatim paths, no a/ b/ prefix stripping) whose real
    top-level directory happens to be spelled ``a/`` still anchors a finding
    spelled the same way as the header."""

    def test_finding_on_real_a_directory_kept(self):
        diff = (
            "--- a/real/x.py\n"
            "+++ a/real/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " def f():\n"
            "+    original\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "a/real/x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Real Dir",
                    "suggested_fix_code": "    changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)
        self.assertIn("## `a/real/x.py`:2-2 — Real Dir", self._read_artifact())


class TestProducerDetection(ReportPatchesTestBase):
    """The oracle detects a git-shaped producer solely from the FIRST line
    opening with git's default ``a/`` prefix (``diff --git "?a/``), then keys
    the oracle with ``post_review.parse_diff_text`` for that shape — never a
    fixed assumption of one shape, and never a bare ``diff --git`` match that
    would also swallow ``diff.noprefix``/``diff.mnemonicPrefix`` output.

    RED (git-shaped case) when detection is removed and glab-verbatim parsing is
    always assumed: an ``a/``/``b/`` prefixed path would never be stripped, so the
    finding spelled ``x.py`` would fail closed instead of being kept.
    RED (verbatim case) when detection is removed and git-shaped parsing is always
    assumed: a real path spelled ``a/real/x.py`` would have its ``a/`` prefix
    wrongly stripped, so the finding spelled with the prefix would fail closed
    instead of being kept, while the unprefixed spelling would wrongly match.
    RED (noprefix case) when the anchor is widened back to a bare ``diff --git ``:
    a ``diff.noprefix``-shaped diff's first line (no ``a/`` after ``diff --git ``)
    would then be wrongly treated as git-shaped, stripping a leading ``b/`` that
    is not a synthetic prefix at all and colliding two real files onto one key.
    RED (quoted case) when the optional ``"?`` is dropped: a C-quoted first file
    (``diff --git "a/café.py" "b/café.py"``) would then fail the anchor entirely,
    falling to verbatim keying that keeps the ``b/`` prefix on, so a finding
    spelled without it fails closed instead of being kept.
    """

    def test_no_diff_git_line_is_parsed_verbatim_glab_style(self):
        diff = (
            "--- a/real/x.py\n"
            "+++ a/real/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " def f():\n"
            "+    original\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "a/real/x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Verbatim Kept",
                    "suggested_fix_code": "    changed",
                },
                {
                    "file": "real/x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Verbatim Stripped Not In Diff",
                    "suggested_fix_code": "    other",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertEqual(receipt["reasons"], {"range_not_in_diff": 1})
        content = self._read_artifact()
        self.assertIn("Verbatim Kept", content)
        self.assertNotIn("Verbatim Stripped Not In Diff", content)

    def test_body_content_starting_with_diff_git_does_not_flip_detection(self):
        """RED when detection searches the whole text instead of the first line: a
        verbatim diff whose hunk body carries a marker-less (zero-prefixed) context
        line beginning ``diff --git`` would be read as git-shaped, stripping the
        real ``b/`` directory so the key becomes ``real/x.py`` — a path the repo
        does not have — and a finding spelled that way would be KEPT against the
        real ``b/real/x.py``'s lines. Under verbatim keying it fails closed."""
        diff = (
            "--- b/real/x.py\n"
            "+++ b/real/x.py\n"
            "@@ -1,2 +1,3 @@\n"
            "diff --git a/real/x.py b/real/x.py\n"
            " def f():\n"
            "+    original\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "real/x.py",
                    "line": 3,
                    "end_line": 3,
                    "title": "Phantom Stripped Path",
                    "suggested_fix_code": "    changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["reasons"], {"range_not_in_diff": 1})

    def test_diff_git_line_present_is_parsed_git_shaped(self):
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " def f():\n"
            "+    original\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Git Shaped",
                    "suggested_fix_code": "    changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)
        self.assertIn("Git Shaped", self._read_artifact())

    def test_noprefix_shaped_diff_is_not_wrongly_detected_as_git_shaped(self):
        """``diff.noprefix=true`` output opens with ``diff --git b/foo.py
        b/foo.py`` — no synthetic ``a/`` prefix, because the FIRST file here
        genuinely lives under a real top-level ``b/`` directory. That must
        NOT match the git-shaped anchor, so both files parse verbatim: the
        real ``b/foo.py`` keys as itself, and the second file's real
        top-level ``foo.py`` keys as itself — two distinct keys, not one.

        RED when the anchor is widened back to a bare ``diff --git ``: this
        diff's first line WOULD match, so github-style parsing strips a
        leading ``b/`` from each new-side header — stripping the real
        subdirectory off the first file (colliding it onto ``foo.py``, the
        second file's own key) and stripping nothing off the second file
        (which has no ``b/`` to strip) — so the ``b/foo.py`` finding no
        longer has a key to anchor to and fails closed as
        ``range_not_in_diff`` instead of the correct ``no_op_replacement``.
        """
        diff = (
            "diff --git b/foo.py b/foo.py\n"
            "--- b/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+CURRENT_TOP\n"
            "diff --git foo.py foo.py\n"
            "--- foo.py\n"
            "+++ foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+DIFFERENT_TEXT\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "b/foo.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Real Subdir No-Op",
                    "suggested_fix_code": "CURRENT_TOP",
                },
                {
                    "file": "foo.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Top File Kept",
                    "suggested_fix_code": "CHANGED_TEXT",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["candidates"], 2)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertEqual(receipt["reasons"], {"no_op_replacement": 1})
        content = self._read_artifact()
        self.assertIn("Top File Kept", content)
        self.assertNotIn("Real Subdir No-Op", content)

    def test_c_quoted_first_file_is_still_detected_as_git_shaped(self):
        """The FIRST line of this diff is C-quoted (git's octal-escaped
        non-ASCII encoding, e.g. under ``core.quotepath``):
        ``diff --git "a/café.py" "b/café.py"``. The optional ``"?`` in the
        anchor must still recognize this as git-shaped so the ``b/`` prefix
        is stripped and the finding spelled ``café.py`` (without it) anchors.

        RED when the optional ``"?`` is dropped from the anchor: the quote
        immediately after ``diff --git `` makes the anchor fail entirely, so
        this diff falls to verbatim keying, which leaves the key spelled
        ``b/café.py`` — a finding spelled ``café.py`` then fails closed as
        ``range_not_in_diff`` instead of being kept.
        """
        diff = (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "café.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Quoted First File",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)
        self.assertIn("Quoted First File", self._read_artifact())


class TestSiblingBPathAnchorsToItsOwnFile(ReportPatchesTestBase):
    """A git-shaped diff touching both ``foo.py`` and a real top-level ``b/``
    directory's ``b/foo.py`` must key each file under its OWN path, never
    collapse the two together. ``parse_diff_text`` strips exactly ONE leading
    ``b/`` from a new-side header: ``+++ b/foo.py`` keys as ``foo.py``, while
    ``+++ b/b/foo.py`` keys as ``b/foo.py`` — the real subdirectory's ``b/``
    survives the strip. So each finding below is checked against its OWN
    file's actual new-side text, and both are genuine no-ops for the file
    they each name.

    RED under the reverted alias/ambiguity keying, which resolved a header
    path against a computed alias set and dropped ``b/foo.py`` in favor of
    re-resolving it to ``foo.py``: the ``b/foo.py`` finding would then be
    checked against the WRONG file's content (``foo.py``'s ``SOMETHING_ELSE``
    instead of its own ``CURRENT_SUB_TEXT``), failing as a content mismatch
    instead of the correct ``no_op_replacement``.
    """

    def test_patch_targeting_either_real_file_is_judged_against_the_shared_key(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+SOMETHING_ELSE\n"
            "diff --git a/b/foo.py b/b/foo.py\n"
            "--- a/b/foo.py\n"
            "+++ b/b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+CURRENT_SUB_TEXT\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "b/foo.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Sub Path No-Op",
                    "suggested_fix_code": "CURRENT_SUB_TEXT",
                },
                {
                    "file": "foo.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Top Path No-Op",
                    "suggested_fix_code": "SOMETHING_ELSE",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["candidates"], 2)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["downgraded"], 2)
        self.assertEqual(receipt["reasons"], {"no_op_replacement": 2})
        content = self._read_artifact()
        self.assertNotIn("Sub Path No-Op", content)
        self.assertNotIn("Top Path No-Op", content)


class TestVerbatimAliasNotKept(ReportPatchesTestBase):
    """A verbatim (no ``diff --git`` line) diff touching only ``a/mod.py`` must
    NOT let a finding spelled ``mod.py`` (the github-style stripped alias) match
    — there is no producer signal here that stripping is correct, and a real
    top-level ``a/`` directory is exactly what plain ``glab mr diff`` output
    looks like."""

    def test_finding_on_the_stripped_alias_is_not_kept(self):
        diff = "--- a/mod.py\n+++ a/mod.py\n@@ -1,1 +1,2 @@\n line1\n+orig\n"
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "mod.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Stripped Alias",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertEqual(receipt["reasons"], {"range_not_in_diff": 1})
        self.assertNotIn("Stripped Alias", self._read_artifact())

    def test_finding_on_a_real_top_level_b_directory_alias_is_not_kept(self):
        """The github-style strip only ever removes a LEADING ``b/`` from the
        NEW-side header (never ``a/``) — so a verbatim fixture using ``a/`` on
        both sides is accidentally immune to a platform mix-up (stripping
        ``b/`` off an ``a/``-prefixed path is a no-op either way). A real
        top-level directory literally named ``b`` is the fixture that
        actually distinguishes "parsed verbatim" from "parsed as
        github-shaped": only the WRONG platform choice would strip it.

        RED when the platform detection is wrong for this shape (forced or
        swapped to "github"): the finding spelled ``mod.py`` would then match
        the wrongly-stripped key and be KEPT instead of downgraded.
        """
        diff = "--- b/mod.py\n+++ b/mod.py\n@@ -1,1 +1,2 @@\n line1\n+orig\n"
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "mod.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Real B Dir Alias",
                    "suggested_fix_code": "changed",
                },
                {
                    "file": "b/mod.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Real B Dir Own Spelling",
                    "suggested_fix_code": "changed2",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertEqual(receipt["reasons"], {"range_not_in_diff": 1})
        content = self._read_artifact()
        self.assertIn("Real B Dir Own Spelling", content)
        self.assertNotIn("Real B Dir Alias", content)


class TestGhAliasMatching(ReportPatchesTestBase):
    """A gh-shaped header ``+++ b/foo.py`` is keyed as ``foo.py`` — the ``b/``
    prefix strip happens at PARSE TIME, inside ``parse_diff_text("github",
    …)`` itself, so there is only ever the one canonical key, never a second
    header-path key recorded alongside it. A finding spelled ``foo.py`` (the
    stripped spelling) anchors to that single key.

    RED when the platform this git-shaped diff is parsed under is forced to
    ``"gitlab"`` instead of the detected ``"github"``: verbatim (unstripped)
    keying then leaves the key spelled ``b/foo.py``, so a finding spelled
    ``foo.py`` fails closed as ``range_not_in_diff`` instead of being kept.
    """

    def test_finding_spelled_without_the_b_prefix_is_kept(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+added\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "foo.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Alias",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)


class TestHeaderPathDecoding(ReportPatchesTestBase):
    """Two header-path wire encodings, both undone before candidate keys are
    computed: a TAB-terminated space path, and a C-quoted non-ASCII path."""

    def test_tab_terminated_space_path_and_c_quoted_path_both_kept(self):
        diff = (
            'diff --git "a/dir with space/x.py" "b/dir with space/x.py"\n'
            "--- a/dir with space/x.py\t\n"
            "+++ b/dir with space/x.py\t\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+added space\n"
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n'
            '+++ "b/caf\\303\\251.py"\n'
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+added cafe\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "dir with space/x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Space Path",
                    "suggested_fix_code": "changed space",
                },
                {
                    "file": "café.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Cafe Path",
                    "suggested_fix_code": "changed cafe",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["candidates"], 2)
        self.assertEqual(receipt["kept"], 2)
        self.assertEqual(receipt["downgraded"], 0)
        content = self._read_artifact()
        self.assertIn("Space Path", content)
        self.assertIn("Cafe Path", content)


class TestCrlfBodyAndInvalidByte(ReportPatchesTestBase):
    """The diff file is read with universal newlines and ``errors="replace"``:
    a raw CRLF-terminated diff and a byte this repo cannot decode as UTF-8 must
    neither crash the run nor corrupt the diff oracle a real patch is judged
    against.

    RED when ``errors="replace"`` is removed from the read: decoding the
    invalid byte under strict UTF-8 raises ``UnicodeDecodeError``, which is not
    an ``OSError`` and is not caught — the run crashes instead of returning
    exit 0.
    """

    def test_crlf_diff_and_invalid_byte_do_not_crash_and_gate_sees_normalized_text(
        self,
    ):
        diff_bytes = (
            b"diff --git a/crlf.py b/crlf.py \xff\r\n"
            b"--- a/crlf.py\r\n"
            b"+++ b/crlf.py\r\n"
            b"@@ -1,1 +1,2 @@\r\n"
            b" line1\r\n"
            b"+orig content\r\n"
        )
        diff_path = self._write_diff_bytes(diff_bytes)
        finding = {
            "file": "crlf.py",
            "line": 2,
            "end_line": 2,
            "title": "CRLF",
            "suggested_fix_code": "changed content",
        }
        self._write_findings([finding])

        exit_code, receipt, _, stderr, lines = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(lines), 1)

        # Read the diff exactly as report_patches.py does, and ask the SAME gate
        # the script uses, to determine the ground truth rather than assuming it.
        with open(diff_path, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
        self.assertNotIn(
            "\r",
            diff_text,
            "universal newlines already strips CR before the gate ever runs",
        )
        valid_lines, line_texts = report_patches._diff_oracle(diff_text)
        ok, reason = post_review._suggested_fix_gate(
            finding,
            apply_range=(2, 2),
            line_texts=line_texts,
            valid_lines=valid_lines,
            path_lookup="crlf.py",
        )
        self.assertTrue(ok, f"expected the differing patch to be kept, got {reason!r}")
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)

    def test_invalid_byte_on_a_content_line_still_yields_a_working_oracle(self):
        """The invalid byte lands INSIDE a hunk's ``+`` content line this
        time, not the header — the read-only oracle report_patches.py builds
        must still work: the byte survives as U+FFFD (``errors="replace"``),
        not a crash, and the SAME decoded text handed directly to
        ``post_review.parse_diff_text`` carries the identical replacement
        character at the identical line — the oracle survives the corruption
        rather than silently losing that line."""
        diff_bytes = (
            b"diff --git a/bad.py b/bad.py\n"
            b"--- a/bad.py\n"
            b"+++ b/bad.py\n"
            b"@@ -1,1 +1,2 @@\n"
            b" line1\n"
            b"+orig \xff content\n"
        )
        diff_path = self._write_diff_bytes(diff_bytes)
        finding = {
            "file": "bad.py",
            "line": 2,
            "end_line": 2,
            "title": "Bad Byte",
            "suggested_fix_code": "changed content",
        }
        self._write_findings([finding])

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)

        with open(diff_path, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
        _, _, _, line_texts = post_review.parse_diff_text("github", diff_text)
        self.assertIn("�", line_texts[("bad.py", 2)])


class TestMissingDiffFailsEveryCandidateClosed(ReportPatchesTestBase):
    """No diff file on disk: the oracle is ``None``/``None``, so every candidate
    fails closed as ``no_diff_oracle`` and the artifact says so — but the run
    still succeeds (exit 0, file written).

    RED when a missing diff is treated as empty dicts (``{}``/``{}``) instead of
    ``None``: an empty ``valid_lines`` dict passes the ``isinstance(..., dict)``
    oracle-presence check, so ``_range_is_valid`` runs and reports
    ``range_not_in_diff`` instead of ``no_diff_oracle``.
    """

    def test_missing_diff_downgrades_every_candidate_as_no_diff_oracle(self):
        self._write_findings(
            [
                {
                    "file": "a.py",
                    "line": 1,
                    "end_line": 1,
                    "title": "A",
                    "suggested_fix_code": "x",
                },
                {
                    "file": "b.py",
                    "line": 5,
                    "end_line": 5,
                    "title": "B",
                    "suggested_fix_code": "y",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["ok"], True)
        self.assertEqual(receipt["oracle"], "missing")
        self.assertEqual(receipt["candidates"], 2)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["downgraded"], 2)
        self.assertEqual(receipt["reasons"], {"no_diff_oracle": 2})
        self.assertIn(
            "The pinned diff file was missing or empty, so every candidate "
            "patch failed closed (`no_diff_oracle`).",
            self._read_artifact(),
        )

    def test_empty_diff_file_downgrades_every_candidate_as_no_diff_oracle(self):
        """A 0-byte diff file — Phase 2's documented diff-producer failure mode —
        must take the exact same disclosed, fail-closed path as a MISSING file,
        not be silently parsed as a present-but-empty diff.

        RED when the post-read emptiness check is removed: an empty string is
        still a valid (if useless) diff to ``_diff_oracle``, so ``oracle_state``
        becomes ``"ok"`` and every candidate downgrades as ``range_not_in_diff``
        instead of ``no_diff_oracle`` — a different, undisclosed reason for the
        same underlying failure.
        """
        self._write_diff("")
        self._write_findings(
            [
                {
                    "file": "a.py",
                    "line": 1,
                    "end_line": 1,
                    "title": "A",
                    "suggested_fix_code": "x",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["ok"], True)
        self.assertEqual(receipt["oracle"], "missing")
        self.assertEqual(receipt["candidates"], 1)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertEqual(receipt["reasons"], {"no_diff_oracle": 1})
        self.assertIn(
            "The pinned diff file was missing or empty, so every candidate "
            "patch failed closed (`no_diff_oracle`).",
            self._read_artifact(),
        )


class TestZeroCandidates(ReportPatchesTestBase):
    """No finding in the persisted array carries ``suggested_fix_code`` at all —
    the artifact says so plainly rather than rendering an empty section."""

    def test_no_suggested_fix_code_anywhere_renders_the_no_patches_line(self):
        self._write_findings(
            [{"file": "a.py", "line": 1, "end_line": 1, "title": "No patch"}]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["candidates"], 0)
        self.assertEqual(receipt["kept"], 0)
        self.assertIn(
            "No finding carried a patch this step could check.",
            self._read_artifact(),
        )


class TestIdempotentRewrite(ReportPatchesTestBase):
    """Running twice over the same inputs produces byte-identical artifact
    content and an identical receipt (the receipt carries no timestamp)."""

    def test_two_runs_produce_byte_identical_output(self):
        self._write_diff(DIFF_GH_X)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 3,
                    "title": "Kept",
                    "suggested_fix_code": "    replaced2\n    replaced3",
                },
                {
                    "file": "x.py",
                    "line": 9,
                    "title": "No end_line",
                    "suggested_fix_code": "z",
                },
            ]
        )

        exit_code1, receipt1, *_ = self._run()
        content1 = self._read_artifact()
        exit_code2, receipt2, *_ = self._run()
        content2 = self._read_artifact()

        self.assertEqual(exit_code1, 0)
        self.assertEqual(exit_code2, 0)
        self.assertEqual(content1, content2)
        self.assertEqual(receipt1, receipt2)


class TestFenceLengthening(ReportPatchesTestBase):
    """A patch payload containing a three-backtick run needs a four-backtick
    fence so the payload cannot close it early — the same rule
    ``post_review._fence_run`` applies to delivery's own fences.

    RED when the fence is fixed at three backticks: the rendered open/close
    lines would then be exactly ``` ``` ``` (three backticks), which this test's
    exact-line check rejects.
    """

    def test_a_backtick_run_in_the_payload_lengthens_the_fence_to_four(self):
        diff = (
            "diff --git a/fence.py b/fence.py\n"
            "--- a/fence.py\n"
            "+++ b/fence.py\n"
            "@@ -1,1 +1,4 @@\n"
            " def f():\n"
            "+    line_a\n"
            "+    line_b\n"
            "+    line_c\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "fence.py",
                    "line": 2,
                    "end_line": 4,
                    "title": "Fence",
                    "suggested_fix_code": "```\nx\ny",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        lines = self._read_artifact().splitlines()
        # The payload's OWN first line is a bare "```" (content, not a delimiter) —
        # so the real assertion is on the FENCE lines specifically: a 4-backtick
        # opener carrying the language tag, and the payload sandwiched between it
        # and a bare 4-backtick closer.
        open_index = lines.index("````py")
        self.assertEqual(lines[open_index + 1 : open_index + 4], ["```", "x", "y"])
        self.assertEqual(lines[open_index + 4], "````")
        self.assertNotIn("```py", lines)


class TestSecretBearingPatchRedacted(ReportPatchesTestBase):
    """A patch whose bytes are credential-shaped (the same prefixed formats
    ``post_review._redact_secrets`` matches) downgrades as ``redacted`` and is
    never rendered — the raw secret and the ``[REDACTED]`` placeholder alike
    are absent from the artifact, since the finding is dropped entirely rather
    than rendered with a substituted body."""

    def test_credential_shaped_patch_is_downgraded_and_never_rendered(self):
        diff = (
            "diff --git a/sec.py b/sec.py\n"
            "--- a/sec.py\n"
            "+++ b/sec.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        secret = "ghp_" + "A" * 24
        self._write_findings(
            [
                {
                    "file": "sec.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Secret",
                    "suggested_fix_code": f"token={secret}",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["candidates"], 1)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertEqual(receipt["reasons"], {"redacted": 1})
        content = self._read_artifact()
        self.assertNotIn(secret, content)
        self.assertNotIn("[REDACTED]", content)


class TestWarnLabel(ReportPatchesTestBase):
    """A downgrade warning on report_patches.py's own gate call is labelled
    ``report-patch``, not delivery's ``suggested-fix`` — so the two records stay
    distinguishable in a run's combined stderr.

    RED when the default label is used (the ``warn_label="report-patch"``
    keyword is dropped from the ``_gated_finding`` call): the warning would read
    ``suggested-fix downgraded: …`` instead.
    """

    def test_downgrade_warning_is_labelled_report_patch(self):
        self._write_findings(
            [
                {
                    "file": "foo.py",
                    "line": 3,
                    "end_line": 3,
                    "title": "Empty",
                    "suggested_fix_code": "",
                }
            ]
        )

        exit_code, receipt, _, stderr, _ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["downgraded"], 1)
        self.assertIn("report-patch downgraded: foo.py:3", stderr)
        self.assertNotIn("suggested-fix downgraded", stderr)


class TestRenderedDisclosures(ReportPatchesTestBase):
    """The literal sentences this artifact promises its reader — pinned as
    LITERAL STRINGS, never the module's own constants (a self-referential
    ``assertIn(report_patches._SOME_LINE, ...)`` proves nothing about what the
    text actually says, only that it round-trips)."""

    def test_kept_run_carries_every_disclosure_and_not_the_no_oracle_sentence(self):
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Kept",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        content = self._read_artifact()
        self.assertIn(f"code-gauntlet-diff-{self.SHA}.patch", content)
        self.assertIn("not the current working tree or branch", content)
        self.assertIn("high-confidence findings only", content)
        self.assertIn(f"# Apply-checked patches (against {self.SHA})", content)
        self.assertIn("Platform render-site constraints are not applied here", content)
        self.assertNotIn(
            "The pinned diff file was missing or empty, so every candidate "
            "patch failed closed (`no_diff_oracle`).",
            content,
        )

    def test_two_reason_downgrade_tally_lists_the_higher_count_first(self):
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig_x\n"
            "diff --git a/y.py b/y.py\n"
            "--- a/y.py\n"
            "+++ b/y.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig_y\n"
        )
        self._write_diff(diff)
        secret = "ghp_" + "A" * 24
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "No-op X",
                    "suggested_fix_code": "orig_x",
                },
                {
                    "file": "y.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "No-op Y",
                    "suggested_fix_code": "orig_y",
                },
                {
                    "file": "z.py",
                    "line": 1,
                    "end_line": 1,
                    "title": "Secret Z",
                    "suggested_fix_code": f"token={secret}",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 0)
        self.assertEqual(receipt["downgraded"], 3)
        self.assertEqual(receipt["reasons"], {"no_op_replacement": 2, "redacted": 1})
        self.assertIn(
            "Downgraded: 3 — reason tally: no_op_replacement (2), redacted (1)",
            self._read_artifact(),
        )

    def test_title_falls_back_to_id_then_to_finding(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig_a\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig_b\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "a.py",
                    "line": 2,
                    "end_line": 2,
                    "id": "F-42",
                    "suggested_fix_code": "changed_a",
                },
                {
                    "file": "b.py",
                    "line": 2,
                    "end_line": 2,
                    "suggested_fix_code": "changed_b",
                },
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 2)
        content = self._read_artifact()
        self.assertIn("— F-42", content)
        self.assertIn("— finding", content)

    def test_whitespace_only_title_falls_through_to_finding_not_a_dangling_dash(
        self,
    ):
        """A title that is present but collapses to nothing once rendered (all
        whitespace) is truthy as a RAW value, so a naive ``title or id or
        "finding"`` chain (evaluated before ``_one_line`` runs) would pick it
        and leave a dangling ``— `` with no text after it.

        RED when ``_one_line`` is applied once at the end instead of to each
        fallback candidate before the ``or`` chain: the heading would then end
        with a bare ``— `` (whitespace collapsed to nothing) instead of
        falling through to ``"finding"``.
        """
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig_a\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "a.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "   \n  ",
                    "suggested_fix_code": "changed_a",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        content = self._read_artifact()
        heading = next(ln for ln in content.splitlines() if ln.startswith("## "))
        self.assertTrue(
            heading.endswith("— finding"),
            f"expected the heading to fall through to '— finding', got {heading!r}",
        )

    def test_lone_surrogate_title_renders_as_a_question_mark_not_u_fffd(self):
        """A title carrying a lone (unpaired) UTF-16 surrogate is valid JSON but
        cannot be encoded back out as UTF-8 — ``_one_line``'s encode/decode
        round trip through ``errors="replace"`` must survive it, and the
        character that reaches the heading is a plain ASCII ``?`` (what
        ``str.encode(..., "replace")`` substitutes for an unencodable
        character), not U+FFFD (which is a *decode*-side substitution).

        RED when the encode/decode round trip is removed from ``_one_line``:
        the raw lone surrogate would reach ``write_text_atomic``'s strict-UTF-8
        write and raise ``UnicodeEncodeError``, so the run would fail instead
        of writing the artifact.
        """
        diff = (
            "diff --git a/s.py b/s.py\n"
            "--- a/s.py\n"
            "+++ b/s.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        findings_json = (
            '[{"file": "s.py", "line": 2, "end_line": 2, '
            '"title": "oops_\\ud800_surrogate", "suggested_fix_code": "changed"}]'
        )
        with open(self._findings_path(), "w", encoding="utf-8") as fh:
            fh.write(findings_json)

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        content = self._read_artifact()
        self.assertIn("oops_?_surrogate", content)
        self.assertNotIn("�", content)

    def test_receipt_warnings_equal_the_stderr_downgrade_lines(self):
        self._write_findings(
            [
                {
                    "file": "foo.py",
                    "line": 3,
                    "end_line": 3,
                    "title": "Empty",
                    "suggested_fix_code": "",
                },
                {
                    "file": "bar.py",
                    "line": 4,
                    "end_line": 4,
                    "title": "AlsoEmpty",
                    "suggested_fix_code": "   ",
                },
            ]
        )

        exit_code, receipt, _, stderr, _ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["downgraded"], 2)
        stderr_warning_lines = [
            ln[len("WARNING: ") :]
            for ln in stderr.splitlines()
            if ln.startswith("WARNING: ")
        ]
        self.assertEqual(receipt["warnings"], stderr_warning_lines)
        self.assertEqual(len(receipt["warnings"]), 2)

    def test_code_span_lengthens_the_run_over_a_backtick_in_the_path(self):
        self.assertEqual(report_patches._code_span("a`b"), "``a`b``")
        self.assertEqual(report_patches._code_span("no ticks here"), "`no ticks here`")


class TestReceiptContract(ReportPatchesTestBase):
    """Shape and side-effect guarantees the receipt and the run itself must hold
    regardless of which branch produced them."""

    def test_exactly_one_stdout_line_with_every_receipt_key(self):
        self._write_diff(DIFF_GH_X)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 3,
                    "title": "Kept",
                    "suggested_fix_code": "    replaced2\n    replaced3",
                }
            ]
        )

        exit_code, receipt, stdout_text, _, lines = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            len(lines), 1, f"expected exactly one stdout line: {stdout_text!r}"
        )
        self.assertEqual(
            set(receipt),
            {
                "ok",
                "path",
                "oracle",
                "candidates",
                "kept",
                "downgraded",
                "reasons",
                "filtered_earlier",
                "findings",
                "warnings",
                "errors",
            },
        )

    def test_findings_file_bytes_and_mtime_are_untouched_by_the_run(self):
        self._write_diff(DIFF_GH_X)
        path = self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 3,
                    "title": "Kept",
                    "suggested_fix_code": "    replaced2\n    replaced3",
                }
            ]
        )
        with open(path, "rb") as fh:
            before_bytes = fh.read()
        before_mtime = os.stat(path).st_mtime_ns

        self._run()

        with open(path, "rb") as fh:
            after_bytes = fh.read()
        after_mtime = os.stat(path).st_mtime_ns
        self.assertEqual(before_bytes, after_bytes)
        self.assertEqual(before_mtime, after_mtime)

    def test_missing_findings_file_is_ok_false_exit_1(self):
        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["ok"], False)
        self.assertTrue(receipt["errors"])
        # A pre-oracle failure — the diff was never even opened — is reported
        # distinctly from "the diff file could not be read".
        self.assertEqual(receipt["oracle"], "unattempted")

    def test_findings_file_as_json_object_instead_of_array_is_ok_false_exit_1(self):
        with open(self._findings_path(), "w", encoding="utf-8") as fh:
            json.dump({"not": "an array"}, fh)

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["ok"], False)
        self.assertTrue(receipt["errors"])
        self.assertEqual(receipt["oracle"], "unattempted")

    def test_malformed_json_findings_file_is_ok_false_exit_1(self):
        with open(self._findings_path(), "w", encoding="utf-8") as fh:
            fh.write("{not valid json")

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["ok"], False)
        self.assertTrue(receipt["errors"])
        self.assertIn("invalid JSON", receipt["errors"][0])

    def test_malformed_head_sha_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            report_patches.main(["--output-dir", self.tmp, "--head-sha", "a/b"])
        self.assertEqual(ctx.exception.code, 2)


class TestEncodingPathsEmitReceipt(ReportPatchesTestBase):
    """Every I/O path this script touches must still emit exactly one receipt
    line and exit cleanly — never an unhandled traceback with no receipt at
    all — when the on-disk bytes are not clean UTF-8, or when the findings
    JSON is syntactically valid but encodes something Unicode cannot
    round-trip (a lone UTF-16 surrogate)."""

    def test_raw_invalid_byte_inside_a_title_string_does_not_crash(self):
        """A raw 0xff byte inside a JSON string value is read with
        ``errors="replace"`` (becoming U+FFFD) rather than raising
        ``UnicodeDecodeError`` — the run still succeeds.

        RED when ``errors="replace"`` is removed from ``_load_findings``'s
        ``open()`` call: decoding 0xff under strict UTF-8 raises
        ``UnicodeDecodeError``, an uncaught exception with no receipt at all.
        """
        raw = b'[{"file": "a.py", "line": 1, "end_line": 1, "title": "T\xffX"}]'
        with open(self._findings_path(), "wb") as fh:
            fh.write(raw)

        exit_code, receipt, _, _, lines = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(lines), 1)
        self.assertEqual(receipt["ok"], True)

    def test_lone_surrogate_in_a_kept_patch_body_is_a_reported_error_not_a_crash(
        self,
    ):
        """A lone UTF-16 surrogate (``\\ud800`` unpaired) is VALID JSON but
        cannot be encoded back out as UTF-8 — the failure surfaces only once
        the patch has passed the gate and the artifact write is attempted.

        RED when the gate+render+write section's ``try/except`` is removed:
        the raised ``UnicodeEncodeError`` propagates unhandled, so the run
        never gets to emit a receipt at all.
        """
        diff = (
            "diff --git a/s.py b/s.py\n"
            "--- a/s.py\n"
            "+++ b/s.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        findings_json = (
            '[{"file": "s.py", "line": 2, "end_line": 2, "title": "Lone Surrogate", '
            '"suggested_fix_code": "text_\\ud800_here"}]'
        )
        with open(self._findings_path(), "w", encoding="utf-8") as fh:
            fh.write(findings_json)

        exit_code, receipt, _, _, lines = self._run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["ok"], False)
        self.assertEqual(len(lines), 1)
        self.assertTrue(
            receipt["errors"] and receipt["errors"][0].startswith("UnicodeEncodeError"),
            f"expected errors[0] to start with UnicodeEncodeError, got "
            f"{receipt['errors']!r}",
        )
        self.assertFalse(os.path.exists(self._artifact_path()))

    def test_emit_receipt_fallback_survives_a_broken_json_dumps(self):
        """``_emit_receipt`` itself is defended: a broken ``json.dumps`` must
        still produce exactly one valid-JSON stdout line, never an unhandled
        exception.

        RED when the ``try/except`` inside ``_emit_receipt`` is removed: the
        patched ``TypeError`` propagates unhandled instead of falling back to
        the hand-built minimal receipt string.
        """
        stdout_buf = io.StringIO()
        with (
            patch("json.dumps", side_effect=TypeError("boom")),
            contextlib.redirect_stdout(stdout_buf),
        ):
            report_patches._emit_receipt({"ok": True})

        lines = [ln for ln in stdout_buf.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["ok"], False)
        self.assertIsNone(parsed["oracle"])


class TestEmitReceiptStdoutEncoding(ReportPatchesTestBase):
    """``_emit_receipt`` must never leave stdout empty even when the HOST's
    stdout is opened with a codec narrower than UTF-8 — the exact scenario an
    in-process ``io.StringIO`` capture (every other test in this file) cannot
    observe, because ``StringIO`` has no codec at all. Only a real subprocess,
    with ``PYTHONIOENCODING`` actually controlling ``sys.stdout``'s encoding,
    can prove this.

    RED (both tests) when ``ensure_ascii=True`` is reverted to ``False`` and
    the ``sys.stdout.write`` call is pulled back out of the same ``try`` as
    ``json.dumps`` (the pre-fix shape): a receipt whose bytes are not pure
    ASCII then hits ``UnicodeEncodeError`` on the unguarded write, under
    strict ``PYTHONIOENCODING=ascii`` — stdout ends up completely empty,
    indistinguishable from a dead executor.
    """

    SCRIPT = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "report_patches.py"
    )

    def _run_subprocess(self, output_dir, sha, env_overrides):
        env = dict(os.environ)
        env.update(env_overrides)
        return subprocess.run(
            [
                sys.executable,
                self.SCRIPT,
                "--output-dir",
                output_dir,
                "--head-sha",
                sha,
            ],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_non_ascii_path_in_a_downgrade_warning_survives_ascii_stdout(self):
        """No diff file at all (oracle missing) forces every candidate to
        downgrade as ``no_diff_oracle``, which puts the finding's raw ``file``
        value — ``src/unié.py``, carrying a non-ASCII character — straight
        into a ``report-patch downgraded: ...`` warning line that lands in
        the receipt's ``warnings`` array.
        """
        self._write_findings(
            [
                {
                    "file": "src/unié.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Non-ASCII path",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        result = self._run_subprocess(self.tmp, self.SHA, {"PYTHONIOENCODING": "ascii"})

        self.assertEqual(
            result.returncode,
            0,
            f"stderr: {result.stderr!r}, stdout: {result.stdout!r}",
        )
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        self.assertEqual(
            len(lines), 1, f"expected exactly one stdout line, got {result.stdout!r}"
        )
        receipt = json.loads(lines[0])
        self.assertEqual(len(receipt["warnings"]), 1)
        self.assertIn("unié", receipt["warnings"][0])

    def test_lone_surrogate_in_a_findings_file_path_never_leaves_stdout_empty(self):
        """A lone (unpaired) UTF-16 surrogate inside the findings JSON's
        ``file`` value is valid JSON but cannot be encoded back out as UTF-8 —
        the same shape ``TestEncodingPathsEmitReceipt`` covers in-process, but
        this run's downgrade path routes the raw value into a stderr warning
        AND the receipt's ``warnings`` array before any UTF-8 write is even
        attempted, so this exercises a second, earlier surface for the same
        underlying hazard.
        """
        findings_json = (
            '[{"file": "mo\\ud800d.py", "line": 1, "end_line": 1, '
            '"title": "Surrogate Path", "suggested_fix_code": "changed"}]'
        )
        findings_path = os.path.join(
            self.tmp, f"code-gauntlet-findings-{self.SHA}.json"
        )
        with open(findings_path, "w", encoding="utf-8") as fh:
            fh.write(findings_json)

        result = self._run_subprocess(self.tmp, self.SHA, {"PYTHONIOENCODING": "ascii"})

        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        self.assertEqual(
            len(lines), 1, f"expected exactly one stdout line, got {result.stdout!r}"
        )
        self.assertIn(result.returncode, (0, 1))
        json.loads(lines[0])  # must be valid JSON regardless of ok/errors


class TestOperationalHygiene(ReportPatchesTestBase):
    """A grab-bag of guarantees that don't fit an existing themed class:
    symlink confinement, the fence info-string regex, write-failure recovery,
    the atomic-write call path, and how many files a run actually creates."""

    def test_confined_symlink_escape_is_refused_and_the_outside_file_is_untouched(
        self,
    ):
        """A symlink AT the artifact's own path, pointing outside
        --output-dir, must be refused rather than followed —
        ``os.path.realpath`` resolving through it is exactly the case
        ``_confined`` exists to catch.

        RED when ``_confined`` stops calling ``os.path.realpath`` (e.g.
        ``os.path.abspath`` instead, which does not resolve symlinks): the
        escape would go undetected and the outside file would be silently
        overwritten.
        """
        outside_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_tmp.cleanup)
        outside_path = os.path.join(outside_tmp.name, "victim.md")
        with open(outside_path, "w", encoding="utf-8") as fh:
            fh.write("PRE-EXISTING CONTENT\n")

        os.symlink(outside_path, self._artifact_path())

        self._write_findings(
            [{"file": "a.py", "line": 1, "end_line": 1, "title": "No patch"}]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["ok"], False)
        self.assertTrue(receipt["errors"])
        self.assertEqual(receipt["oracle"], "unattempted")
        with open(outside_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "PRE-EXISTING CONTENT\n")

    def test_ext_re_rejects_backticks_but_accepts_a_plain_extension(self):
        """Direct regex pin: ``_EXT_RE`` must reject an extension carrying
        backticks (which would smuggle extra fence markup into the info
        string) and accept an ordinary one."""
        self.assertIsNone(report_patches._EXT_RE.match("a`b`c"))
        self.assertIsNotNone(report_patches._EXT_RE.match("py"))

    def test_a_backtick_laden_extension_renders_a_bare_fence_opener(self):
        diff = (
            "diff --git a/x.a```b b/x.a```b\n"
            "--- a/x.a```b\n"
            "+++ b/x.a```b\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "x.a```b",
                    "line": 2,
                    "end_line": 2,
                    "title": "Backtick Ext",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        fence_lines = [ln for ln in self._read_artifact().splitlines() if ln == "```"]
        self.assertEqual(
            len(fence_lines),
            2,
            "the fence's info string must be empty (bare open+close), not the "
            "raw backtick-laden extension",
        )

    def test_write_failure_is_ok_false_with_sorted_reasons_present(self):
        """A permission-denied output directory makes ``write_text_atomic``
        raise ``OSError`` — the broad ``except`` in ``main()`` must still
        emit a full receipt, with the downgrade reasons this run accumulated
        reported ALPHABETICALLY even though they were inserted in a
        different order (finding order: redacted, then empty) — proving
        ``_receipt()`` itself sorts rather than merely passing through
        insertion order."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses permission bits")
        secret = "ghp_" + "A" * 24
        self._write_findings(
            [
                {
                    "file": "z.py",
                    "line": 1,
                    "end_line": 1,
                    "title": "Secret",
                    "suggested_fix_code": f"token={secret}",
                },
                {
                    "file": "a.py",
                    "line": 1,
                    "end_line": 1,
                    "title": "Empty",
                    "suggested_fix_code": "",
                },
            ]
        )
        os.chmod(self.tmp, 0o500)
        try:
            exit_code, receipt, *_ = self._run()
        finally:
            os.chmod(self.tmp, 0o700)

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["ok"], False)
        self.assertTrue(receipt["errors"])
        self.assertEqual(receipt["downgraded"], 2)
        self.assertEqual(list(receipt["reasons"].keys()), ["empty", "redacted"])

    def test_write_goes_through_write_text_atomic(self):
        """RED when the write is replaced by a plain ``open()``/``write()``:
        the wrapped mock would then never be called."""
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Kept",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        with patch(
            "scripts.report_patches.write_text_atomic",
            wraps=report_patches.write_text_atomic,
        ) as mock_write:
            exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        mock_write.assert_called_once()
        self.assertEqual(mock_write.call_args[0][0], self._artifact_path())

    def test_run_creates_exactly_one_new_file(self):
        """R3 structural: RED if the script ever writes a second artifact
        (e.g. a stray ``post-review-payload.json``) — this script owns
        exactly one output path."""
        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Kept",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        before = set(os.listdir(self.tmp))
        exit_code, receipt, *_ = self._run()
        after = set(os.listdir(self.tmp))

        self.assertEqual(exit_code, 0)
        self.assertEqual(after - before, {f"code-gauntlet-patches-{self.SHA}.md"})

    def test_render_defense_in_depth_redacts_a_secret_shaped_kept_payload(self):
        """``_render`` calls ``_redact_secrets`` a SECOND time on kept patch
        text — defense in depth against a finding that reached "kept" some
        way other than the gate (the gate itself already refuses to keep a
        credential-shaped patch — see TestSecretBearingPatchRedacted).
        Calling ``_render`` directly with a hand-built "kept" list is the
        only way to exercise that second call at all, which is what makes
        this assertion non-vacuous.

        NOTE: no AWS ``AKIA...`` pattern exists in
        ``post_review._redact_secrets`` — only prefixed GitHub/GitLab token
        shapes are implemented — so this test uses the GitHub shape that is
        actually there rather than asserting against a pattern the code does
        not have.

        RED when the ``_redact_secrets(text)`` call inside ``_render`` is
        removed: the raw secret would then appear in the rendered fence.
        """
        secret = "ghp_" + "A" * 24
        finding = {
            "file": "leak.py",
            "line": 1,
            "end_line": 1,
            "title": "Leak",
            "suggested_fix_code": f"token={secret}",
        }

        content = report_patches._render([finding], 1, 0, "ok", self.SHA)

        self.assertNotIn(secret, content)
        self.assertIn("[REDACTED]", content)


class TestHtmlCommentNeutralization(ReportPatchesTestBase):
    """A stray ``<!--`` in a finding's title would open an HTML comment that
    swallows the rest of the rendered document — so heading text is neutralized
    (``<!--`` -> ``&lt;!--``). A kept patch's OWN fenced content is exempt: it
    already passed the gate's redaction check, and rewriting bytes inside a
    committable patch would make the artifact lie about what was verified.

    RED when neutralization is applied to fence payloads too: the patch body's
    own ``<!--`` would then also become ``&lt;!--``, and this test's raw-body
    assertion would fail to find it.
    """

    def test_title_neutralized_fence_payload_untouched(self):
        diff = (
            "diff --git a/note.py b/note.py\n"
            "--- a/note.py\n"
            "+++ b/note.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+    original text\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "note.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Bad <!-- title",
                    "suggested_fix_code": "    replaced <!-- marker line",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        content = self._read_artifact()
        self.assertIn("&lt;!-- title", content)
        self.assertNotIn("Bad <!-- title", content)
        self.assertIn("replaced <!-- marker line", content)


class TestHeadingInjectionNeutralized(ReportPatchesTestBase):
    """A title carrying embedded newlines and fence/heading-shaped text — the
    payload ``_one_line`` exists to defuse — must render on ONE physical
    markdown line, so none of its embedded ``` ``` `` fence markers or its
    ``## Injected`` line can land at the START of a rendered line and forge
    extra document structure.

    RED when ``_one_line`` is skipped for ``title``: the raw multi-line title
    would split across several physical lines, putting a bare ``` at the
    start of one line (opening a REAL fence), ``STOLEN`` as its body, a
    closing ``` at the start of the next, and ``## Injected`` as a REAL H2
    heading — exactly what this test's line-start assertions catch.
    """

    def test_multiline_title_collapses_to_one_heading_no_forged_structure(self):
        diff = (
            "diff --git a/inject.py b/inject.py\n"
            "--- a/inject.py\n"
            "+++ b/inject.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+orig\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "inject.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "oops\n```\nSTOLEN\n```\n## Injected",
                    "suggested_fix_code": "changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        content = self._read_artifact()
        lines = content.splitlines()

        heading_lines = [ln for ln in lines if ln.startswith("## ")]
        self.assertEqual(
            len(heading_lines),
            1,
            f"expected exactly one rendered H2 heading, got {heading_lines!r}",
        )
        self.assertIn("STOLEN", heading_lines[0])
        self.assertIn("## Injected", heading_lines[0])
        self.assertEqual(
            [ln for ln in lines if ln == "## Injected"],
            [],
            "the embedded title text must never itself become a heading LINE",
        )

        fence_lines = [ln for ln in lines if re.fullmatch(r"`{3,}\w*", ln)]
        self.assertEqual(
            len(fence_lines),
            2,
            "one kept patch must render exactly one fence pair (open+close), "
            f"got {fence_lines!r}",
        )

    def test_file_with_embedded_newline_renders_on_one_line(self):
        finding = {
            "file": "plain.py\ninjected/path.py",
            "line": 2,
            "end_line": 2,
            "title": "Multiline File",
            "suggested_fix_code": "changed",
        }
        content = report_patches._render([finding], 1, 0, "ok", self.SHA)
        lines = content.splitlines()

        heading_lines = [ln for ln in lines if ln.startswith("## ")]
        self.assertEqual(len(heading_lines), 1)
        self.assertIn("plain.py injected/path.py", heading_lines[0])
        stray = [
            ln for ln in lines if ln not in heading_lines and "injected/path.py" in ln
        ]
        self.assertEqual(stray, [], "the file path must not spill onto a second line")


class TestG3ScaffoldingAbsent(ReportPatchesTestBase):
    """This artifact is never bench-scanned as a G3 degrade carrier — but the
    scaffolding text it emits must not accidentally contain either G3 sentinel
    phrase regardless, at 0 kept and at 3 kept patches."""

    def test_zero_kept_patches_carries_neither_g3_phrase(self):
        self._write_findings(
            [{"file": "a.py", "line": 1, "end_line": 1, "title": "No patch"}]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 0)
        content = self._read_artifact().lower()
        self.assertNotIn("no write proof", content)
        self.assertNotIn("partial-artifacts", content)

    def test_three_kept_patches_carries_neither_g3_phrase(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,1 +1,2 @@\n"
            " x\n"
            "+orig_a\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,1 +1,2 @@\n"
            " x\n"
            "+orig_b\n"
            "diff --git a/c.py b/c.py\n"
            "--- a/c.py\n"
            "+++ b/c.py\n"
            "@@ -1,1 +1,2 @@\n"
            " x\n"
            "+orig_c\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": name,
                    "line": 2,
                    "end_line": 2,
                    "title": name,
                    "suggested_fix_code": f"changed_{name}",
                }
                for name in ("a.py", "b.py", "c.py")
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 3)
        content = self._read_artifact().lower()
        self.assertNotIn("no write proof", content)
        self.assertNotIn("partial-artifacts", content)


class TestFilteredEarlier(ReportPatchesTestBase):
    """A finding carrying ``suggested_fix_code_removed_by`` (an earlier pipeline
    stage already stripped its patch) is not a candidate here — it is counted
    separately as ``filtered_earlier`` and disclosed in the artifact rather than
    silently dropped."""

    def test_removed_by_stamp_counts_as_filtered_earlier_not_a_candidate(self):
        self._write_findings(
            [
                {
                    "file": "x.py",
                    "line": 1,
                    "end_line": 1,
                    "title": "Filtered",
                    "suggested_fix_code_removed_by": "filter_findings.py",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["candidates"], 0)
        self.assertEqual(receipt["filtered_earlier"], 1)
        content = self._read_artifact()
        self.assertIn(
            "1 patch(es) were removed earlier by the pipeline's content filter",
            content,
        )


class TestResetRunState(ReportPatchesTestBase):
    """``report_patches.main()`` calls ``post_review.reset_run_state()`` before
    gating its own candidates, so a prior gate call earlier in the same process
    (as a delivery run before it would leave) cannot leak into this run's
    receipt.

    RED when the ``reset_run_state()`` call is removed from
    ``report_patches.main``: the poisoned ``downgraded``/``reasons`` state from
    the pre-dirtying call below would still be present in this run's receipt.

    Dirties the gate through ``report_patches._gated_finding`` — the SAME
    bare-imported name ``report_patches.main()`` itself calls — not
    ``post_review._gated_finding``: bare ``post_review`` (report_patches.py's
    import) and ``scripts.post_review`` (this test module's import) are two
    separate module objects with independent state, so poisoning the latter
    would silently miss the counters the run under test actually reads.
    """

    def test_stale_state_from_a_prior_gate_call_does_not_leak_into_the_receipt(self):
        poison = {"file": "poison.py", "line": 1, "suggested_fix_code": ""}
        report_patches._gated_finding(poison, (1, 1), {}, {})
        self.assertEqual(report_patches._FIX_COUNTS["downgraded"], 1)  # sanity: dirtied

        diff = (
            "diff --git a/clean.py b/clean.py\n"
            "--- a/clean.py\n"
            "+++ b/clean.py\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+    original\n"
        )
        self._write_diff(diff)
        self._write_findings(
            [
                {
                    "file": "clean.py",
                    "line": 2,
                    "end_line": 2,
                    "title": "Clean",
                    "suggested_fix_code": "    changed",
                }
            ]
        )

        exit_code, receipt, *_ = self._run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["kept"], 1)
        self.assertEqual(receipt["downgraded"], 0)
        self.assertEqual(receipt["reasons"], {})


if __name__ == "__main__":
    unittest.main()
