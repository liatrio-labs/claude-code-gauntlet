"""
Tests for scripts/collect_project_rules.py.

The script resolves a reviewed repository's project rules for the shared agent
context file (issue #49). The defect it fixes is that the `Read` tool does not
expand Claude Code's `@path` import directive, while Anthropic's own docs tell
an AGENTS.md-using repo to write a CLAUDE.md that is nothing but such a pointer
— so for three of the five benchmark mirror repos the entire "project rules"
section was an 11-to-40-byte pointer string, silently.

Contract under test:
  * pointers resolve — standalone (`@AGENTS.md`, sentry/grafana) and inline
    mid-sentence (`See @AI-AGENTS.md for all instructions.`, discourse);
  * pointers are NOT followed inside code spans or fenced blocks, and resolve
    relative to the directory of the file containing them;
  * the security boundary holds: absolute/home pointers are refused before any
    `os.path.join` (which silently discards its base on an absolute second
    argument), everything is confined by `realpath` against the repo root with a
    separator-aware check (a bare `startswith` would accept a `repo-evil`
    sibling), and a repo-confined target still has to be `.md` — confinement
    alone does not stop `@.env`;
  * bounds are enforced from `os.stat` BEFORE `open`, so an over-cap file is
    never read at all;
  * disclosure is total: every skip carries a reason, and stdout is EXACTLY one
    line of JSON on every path including failure;
  * "no convention files" is a clean success that still WRITES an empty --out
    file — Phase 2 reads that path unconditionally, so a missing file must mean
    "the collection step never ran" and nothing else.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from scripts.collect_project_rules import (  # noqa: E402
    MAX_FILES,
    MAX_IMPORT_DEPTH,
    PROJECT_RULE_FILENAMES,
    _find_imports,
    _strip_code,
    _within,
    main,
)

SCRIPT = os.path.join(REPO_ROOT, "scripts", "collect_project_rules.py")


class _RepoCase(unittest.TestCase):
    """Builds a real on-disk repo (real files, real symlinks) per test."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="cpr-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.repo = os.path.join(self.base, "repo")
        os.makedirs(self.repo)
        self.out = os.path.join(self.base, "rules.md")

    def write(self, relpath, content, root=None):
        path = os.path.join(root or self.repo, relpath)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w") as handle:
            handle.write(content)
        return path

    def run_script(self, *extra, **kwargs):
        """Run in-process (fast, importable) and return (exit_code, receipt, body)."""
        repo = kwargs.pop("repo", self.repo)
        argv = ["--repo-root", repo, "--out", self.out] + list(extra)
        from io import StringIO

        saved = sys.stdout
        sys.stdout = StringIO()
        try:
            code = main(argv)
            captured = sys.stdout.getvalue()
        finally:
            sys.stdout = saved
        lines = [line for line in captured.split("\n") if line]
        self.assertEqual(
            len(lines), 1,
            "stdout must be EXACTLY one line of JSON; got %d line(s): %r"
            % (len(lines), captured),
        )
        receipt = json.loads(lines[0])
        # "" for a missing file as well as an empty one; the distinction between
        # those two states is load-bearing and is asserted explicitly, on
        # os.path.exists, by test_repo_with_no_convention_files_*.
        body = open(self.out).read() if os.path.exists(self.out) else ""
        return code, receipt, body

    def reasons(self, receipt):
        return sorted({s["reason"] for s in receipt["skipped"]})

    def source_paths(self, receipt):
        return [s["path"] for s in receipt["sources"]]


class TestPointerResolution(_RepoCase):
    def test_standalone_pointer_resolves_the_sentry_grafana_case(self):
        self.write("CLAUDE.md", "@AGENTS.md\n")
        self.write("AGENTS.md", "# Rules\nRULE-ALPHA: no tabs.\n")
        code, receipt, body = self.run_script()
        self.assertEqual(code, 0)
        self.assertTrue(receipt["ok"])
        self.assertIn("RULE-ALPHA: no tabs.", body)
        self.assertIn("AGENTS.md", self.source_paths(receipt))

    def test_inline_pointer_resolves_the_discourse_case(self):
        # discourse's real CLAUDE.md is a sentence, not a bare pointer line. A
        # "standalone line only" heuristic would silently miss it entirely.
        self.write("CLAUDE.md", "See @AI-AGENTS.md for all instructions.\n")
        self.write("AI-AGENTS.md", "RULE-BETA: prefer composition.\n")
        _, receipt, body = self.run_script()
        self.assertIn("RULE-BETA: prefer composition.", body)
        self.assertIn("AI-AGENTS.md", self.source_paths(receipt))

    def test_symlinked_claude_md_contributes_content_exactly_once(self):
        # cal.com's real layout: CLAUDE.md is a symlink to AGENTS.md. Following
        # it is correct; emitting the same bytes twice under two names is not.
        self.write("AGENTS.md", "RULE-GAMMA: keep it small.\n")
        os.symlink("AGENTS.md", os.path.join(self.repo, "CLAUDE.md"))
        _, receipt, body = self.run_script()
        self.assertEqual(body.count("RULE-GAMMA: keep it small."), 1)
        self.assertIn("duplicate_of", self.reasons(receipt))

    def test_pointer_in_fenced_block_or_code_span_is_not_followed(self):
        # The target must NOT be one of PROJECT_RULE_FILENAMES, or the direct
        # directory scan would collect it anyway and the test would pass without
        # proving anything about import parsing.
        self.write(
            "CLAUDE.md",
            "Mention `@NOTES.md` inline.\n\n```\n@NOTES.md\n```\n",
        )
        self.write("NOTES.md", "RULE-DELTA: never reached.\n")
        _, receipt, body = self.run_script()
        self.assertNotIn("RULE-DELTA", body)
        self.assertNotIn("NOTES.md", self.source_paths(receipt))

    def test_relative_pointer_resolves_against_the_containing_file_not_the_root(self):
        # Three distinct directories, so root-relative and cwd-relative
        # resolution both produce a visibly different (wrong) answer.
        self.write("pkg/AGENTS.md", "See @nested/RULES.md here.\n")
        self.write("pkg/nested/RULES.md", "RULE-NESTED: correct target.\n")
        self.write("nested/RULES.md", "RULE-ROOT: wrong target.\n")
        changed = self.write("../changed.json", json.dumps(["pkg/thing.txt"]))
        _, _, body = self.run_script("--changed-files", changed)
        self.assertIn("RULE-NESTED: correct target.", body)
        self.assertNotIn("RULE-ROOT", body)

    def test_missing_pointer_target_is_disclosed_and_the_run_still_succeeds(self):
        self.write("CLAUDE.md", "@NOPE.md\n")
        code, receipt, _ = self.run_script()
        self.assertEqual(code, 0)
        self.assertTrue(receipt["ok"])
        self.assertIn("missing", self.reasons(receipt))
        # references/phase2-triage.md's Triage Announcement folds gaps[] into a
        # human-readable note, explicitly naming "a missing import target" as
        # one of the things it exists to surface — a skip entry alone is not
        # enough if nothing ever reads skipped[] directly.
        self.assertTrue(
            any("NOPE.md" in g for g in receipt["gaps"]),
            "a missing pointer target must reach the human-readable gaps list",
        )


class TestSecurityBoundary(_RepoCase):
    def _canary_outside(self):
        return self.write("outside.md", "OUTSIDE-CANARY\n", root=self.base)

    def test_traversal_pointer_is_refused(self):
        self._canary_outside()
        self.write("CLAUDE.md", "@../outside.md\n")
        _, receipt, body = self.run_script()
        self.assertNotIn("OUTSIDE-CANARY", body)
        self.assertIn("outside_repo", self.reasons(receipt))

    def test_absolute_pointer_is_refused_before_any_path_join(self):
        # os.path.join(base, "/abs/x.md") returns "/abs/x.md" — the base is
        # silently discarded, with no exception. This must be caught by an
        # explicit pre-join check, which is why it gets its own test.
        # The payload ends in .md so it is a real candidate and actually reaches
        # the join logic under test, rather than being dropped earlier.
        outside = self.write("secret.md", "ABS-CANARY\n", root=self.base)
        self.write("CLAUDE.md", "@%s\n" % outside)
        _, receipt, body = self.run_script()
        self.assertNotIn("ABS-CANARY", body)
        self.assertIn("absolute_path", self.reasons(receipt))

    def test_home_relative_pointer_is_refused(self):
        self.write("CLAUDE.md", "@~/secrets.md\n")
        _, receipt, _ = self.run_script()
        self.assertIn("absolute_path", self.reasons(receipt))

    def test_sibling_directory_sharing_the_root_name_prefix_is_refused(self):
        # A bare startswith() check would accept /base/repo-evil for /base/repo.
        evil = os.path.join(self.base, "repo-evil")
        os.makedirs(evil)
        self.write("pwn.md", "PREFIX-CANARY\n", root=evil)
        self.write("CLAUDE.md", "@../repo-evil/pwn.md\n")
        _, receipt, body = self.run_script()
        self.assertNotIn("PREFIX-CANARY", body)
        self.assertIn("outside_repo", self.reasons(receipt))

    def test_symlink_escaping_the_repo_is_refused(self):
        self._canary_outside()
        os.symlink(os.path.join(self.base, "outside.md"),
                   os.path.join(self.repo, "escape.md"))
        self.write("CLAUDE.md", "@escape.md\n")
        _, receipt, body = self.run_script()
        self.assertNotIn("OUTSIDE-CANARY", body)
        self.assertIn("outside_repo", self.reasons(receipt))

    def test_first_class_source_that_is_an_escaping_symlink_is_refused(self):
        # Confinement must cover named sources too, not only pointers: cal.com
        # proves a symlinked CLAUDE.md is a real-world shape, so it is also the
        # shape an attacker would reach for.
        self._canary_outside()
        os.symlink(os.path.join(self.base, "outside.md"),
                   os.path.join(self.repo, "CLAUDE.md"))
        _, receipt, body = self.run_script()
        self.assertNotIn("OUTSIDE-CANARY", body)
        self.assertIn("outside_repo", self.reasons(receipt))

    def test_non_markdown_pointer_never_reads_the_target(self):
        # Confinement proves "inside the repo", not "is a rules file". Reading a
        # named CLAUDE.md can only ever open one known filename per directory;
        # pointer indirection is what first makes an attacker-chosen filename
        # reachable, so a committed .env inside the repo needs its own control.
        self.write(".env", "AWS_SECRET=INSIDE-CANARY\n")
        self.write("CLAUDE.md", "@.env\n")
        _, receipt, body = self.run_script()
        self.assertNotIn("INSIDE-CANARY", body)
        self.assertIn("not_markdown", self.reasons(receipt))

    def test_md_named_symlink_to_an_in_repo_secret_is_refused(self):
        # The extension filter alone is not the control: a pointer CAN end in
        # .md and still resolve to something else. Naming a symlink `rules.md`
        # and aiming it at a committed .env is the attack that defeats a
        # name-only check, so the check runs on the REALPATH.
        self.write(".env", "AWS_SECRET=SYMLINK-CANARY\n")
        os.symlink(".env", os.path.join(self.repo, "rules.md"))
        self.write("CLAUDE.md", "@rules.md\n")
        _, receipt, body = self.run_script()
        self.assertNotIn("SYMLINK-CANARY", body)
        self.assertIn("not_markdown", self.reasons(receipt))

    def test_refusals_are_surfaced_as_gaps_not_only_as_skip_entries(self):
        self.write("CLAUDE.md", "@../outside.md\n")
        self.write("outside.md", "OUTSIDE\n", root=self.base)
        _, receipt, _ = self.run_script()
        self.assertTrue(
            any("project_rules_refused" in gap for gap in receipt["gaps"]),
            "a security refusal must reach the human-readable gaps list",
        )

    def test_prose_at_signs_are_not_treated_as_pointers(self):
        # discourse's real AI-AGENTS.md contains "Specify the @type." Refusing
        # such tokens is safe but noisy, and a refusal line on every run is a
        # disclosure channel people learn to ignore.
        self.write("CLAUDE.md",
                   "Specify the @type. Use @param and @Override and @media.\n")
        _, receipt, _ = self.run_script()
        self.assertEqual(
            [], [s for s in receipt["skipped"] if s["reason"] != "duplicate_of"],
            "prose at-signs must not produce skip entries: %r" % receipt["skipped"],
        )
        self.assertEqual(
            [], [g for g in receipt["gaps"] if "refused" in g],
            "prose at-signs must not produce refusal gaps: %r" % receipt["gaps"],
        )


class TestBounds(_RepoCase):
    def test_over_cap_file_is_never_opened(self):
        # Asserting only on the output would pass an implementation that reads
        # the file and then discards it. The bound must come off os.stat, so the
        # proof is that open() is never called for that path at all.
        self.write("CLAUDE.md", "x" * 5000)
        import builtins

        real_open = builtins.open
        opened = []

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        builtins.open = spy
        try:
            _, receipt, _ = self.run_script("--max-file-bytes", "100")
        finally:
            builtins.open = real_open
        self.assertIn("too_large", self.reasons(receipt))
        self.assertFalse(
            [p for p in opened if p.endswith("CLAUDE.md")],
            "an over-cap file must never be opened; open() was called on it",
        )

    def test_total_cap_truncates_and_discloses(self):
        self.write("CLAUDE.md", "a" * 400)
        self.write("AGENTS.md", "b" * 400)
        _, receipt, _ = self.run_script("--max-total-bytes", "500")
        self.assertTrue(receipt["truncated"])
        self.assertIn("total_cap_reached", self.reasons(receipt))
        self.assertTrue(any("project_rules_truncated" in g for g in receipt["gaps"]))

    def test_file_count_cap_bounds_the_walk_even_when_no_bytes_accumulate(self):
        # The byte caps do NOT bound this: every one of these files is empty, so
        # total_bytes never moves no matter how many are walked. This test
        # exists because the constant was once deleted as apparent dead code —
        # nothing referenced it and nothing failed.
        self.write("CLAUDE.md",
                   "\n".join("@f%d.md" % i for i in range(MAX_FILES + 20)) + "\n")
        for i in range(MAX_FILES + 20):
            self.write("f%d.md" % i, "")
        _, receipt, _ = self.run_script()
        # Only the pointer list itself contributes bytes; the 84 targets are all
        # empty, so the byte caps are nowhere near tripping and cannot be what
        # stopped the walk. Only the file-count bound can have.
        self.assertLess(receipt["total_bytes"], 2000)
        self.assertLessEqual(len(receipt["sources"]), MAX_FILES)
        self.assertIn("file_cap_reached", self.reasons(receipt))
        self.assertTrue(receipt["truncated"])

    def test_import_depth_cap_matches_the_real_product_and_is_disclosed(self):
        # Claude Code resolves at most four hops; matching that keeps this
        # script's view of a repo identical to the harness's.
        self.write("CLAUDE.md", "@d1.md\n")
        for i in range(1, MAX_IMPORT_DEPTH + 1):
            self.write("d%d.md" % i, "RULE-D%d\n@d%d.md\n" % (i, i + 1))
        self.write("d%d.md" % (MAX_IMPORT_DEPTH + 1), "RULE-TOO-DEEP\n")
        _, receipt, body = self.run_script()
        self.assertIn("RULE-D%d" % MAX_IMPORT_DEPTH, body)
        self.assertNotIn("RULE-TOO-DEEP", body)
        self.assertIn("depth_exceeded", self.reasons(receipt))

    def test_import_cycle_terminates_and_is_disclosed(self):
        self.write("CLAUDE.md", "@a.md\n")
        self.write("a.md", "RULE-A\n@b.md\n")
        self.write("b.md", "RULE-B\n@a.md\n")
        code, receipt, body = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("RULE-A", body)
        self.assertIn("RULE-B", body)
        self.assertTrue({"cycle", "duplicate_of"} & set(self.reasons(receipt)))
        # Same phase2-triage.md contract as the missing-target case above: "a
        # cycle" is one of the reasons explicitly named as belonging in gaps[].
        self.assertTrue(
            any("cycle" in g for g in receipt["gaps"]),
            "an import cycle must reach the human-readable gaps list: %r" % receipt["gaps"],
        )


class TestDiscovery(_RepoCase):
    def test_changed_files_pull_in_directory_level_rules(self):
        self.write("CLAUDE.md", "ROOT-RULE\n")
        self.write("pkg/storage/AGENTS.md", "DIR-RULE\n")
        changed = self.write("../changed.json",
                             json.dumps(["pkg/storage/impl.go"]))
        _, _, body = self.run_script("--changed-files", changed)
        self.assertIn("ROOT-RULE", body)
        self.assertIn("DIR-RULE", body)

    def test_directory_rules_outside_the_changed_set_are_not_pulled_in(self):
        self.write("CLAUDE.md", "ROOT-RULE\n")
        self.write("other/AGENTS.md", "UNRELATED-RULE\n")
        changed = self.write("../changed.json", json.dumps(["pkg/impl.go"]))
        _, _, body = self.run_script("--changed-files", changed)
        self.assertNotIn("UNRELATED-RULE", body)

    def test_all_declared_source_filenames_are_collected(self):
        for name in PROJECT_RULE_FILENAMES:
            self.write(name, "RULE-FROM-%s\n" % name.replace(".md", ""))
        _, _, body = self.run_script()
        for name in PROJECT_RULE_FILENAMES:
            self.assertIn("RULE-FROM-%s" % name.replace(".md", ""), body)

    def test_review_md_is_deliberately_not_a_source(self):
        # REVIEW.md has its own structured parse path and precedence semantics;
        # collecting it as free rule text here would give one file two meanings.
        self.assertNotIn("REVIEW.md", PROJECT_RULE_FILENAMES)
        self.write("REVIEW.md", "REVIEW-CONTENT\n")
        _, _, body = self.run_script()
        self.assertNotIn("REVIEW-CONTENT", body)


class TestDisclosureContract(_RepoCase):
    def test_repo_with_no_convention_files_succeeds_and_writes_an_empty_file(self):
        # Load-bearing: Phase 2 reads --out unconditionally, so "empty" must mean
        # "collected, found nothing" and "missing" must mean "never ran".
        code, receipt, body = self.run_script()
        self.assertEqual(code, 0)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["sources"], [])
        self.assertTrue(os.path.exists(self.out), "--out must exist even when empty")
        self.assertEqual(body, "")
        self.assertTrue(any("project_rules_absent" in g for g in receipt["gaps"]))

    def test_failure_still_emits_exactly_one_receipt_line(self):
        code, receipt, _ = self.run_script(repo=os.path.join(self.base, "nope"))
        self.assertEqual(code, 1)
        self.assertFalse(receipt["ok"])
        self.assertTrue(receipt["gaps"])

    def test_every_skip_entry_carries_a_reason(self):
        self.write("CLAUDE.md", "@../outside.md\n@.env\n@nope.md\n")
        self.write(".env", "x\n")
        _, receipt, _ = self.run_script()
        self.assertTrue(receipt["skipped"])
        for entry in receipt["skipped"]:
            self.assertTrue(entry.get("reason"), "silent skip: %r" % entry)

    def test_subprocess_invocation_keeps_stdout_to_one_json_line(self):
        # The in-process helper asserts this too, but Phase 2 invokes the script
        # as a subprocess and parses stdout, so pin the real boundary as well.
        self.write("CLAUDE.md", "@AGENTS.md\n")
        self.write("AGENTS.md", "RULE\n")
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--repo-root", self.repo, "--out", self.out],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.strip().split("\n")), 1)
        self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_skipped_paths_never_leak_an_absolute_host_path(self):
        self.write("outside.md", "x\n", root=self.base)
        self.write("CLAUDE.md", "@/etc/passwd\n@../outside.md\n")
        _, receipt, _ = self.run_script()
        for entry in receipt["skipped"]:
            self.assertFalse(
                entry["path"].startswith("/"),
                "receipt leaked an absolute host path: %r" % entry["path"],
            )


class TestPureHelpers(unittest.TestCase):
    def test_strip_code_blanks_fences_and_spans(self):
        text = "a `@x.md` b\n```\n@y.md\n```\n@z.md\n"
        stripped = _strip_code(text)
        self.assertNotIn("@x.md", stripped)
        self.assertNotIn("@y.md", stripped)
        self.assertIn("@z.md", stripped)

    def test_find_imports_handles_inline_and_trailing_punctuation(self):
        self.assertEqual(
            _find_imports("See @AI-AGENTS.md for all instructions."),
            ["AI-AGENTS.md"],
        )

    def test_find_imports_ignores_mid_word_at_signs(self):
        self.assertEqual(_find_imports("mail me at foo@bar.md please"), [])

    def test_find_imports_ignores_bare_decorator_tokens_without_a_dot(self):
        # @param/@Override/@media/@type have no extension at all and are pure
        # prose noise (discourse's real file says "Specify the @type."). A
        # token that DOES contain a dot, like @.env, must NOT be filtered
        # here -- it has to reach _resolve_pointer so a genuine non-markdown
        # pointer is refused AND DISCLOSED as `not_markdown`, rather than
        # silently vanishing before it is ever classified.
        self.assertEqual(_find_imports("@type @param @Override @media"), [])
        self.assertEqual(_find_imports("@.env"), [".env"])

    def test_find_imports_deduplicates_preserving_order(self):
        self.assertEqual(_find_imports("@b.md @a.md @b.md"), ["b.md", "a.md"])

    def test_within_is_separator_aware(self):
        self.assertTrue(_within("/base/repo", "/base/repo"))
        self.assertTrue(_within("/base/repo/x.md", "/base/repo"))
        self.assertFalse(_within("/base/repo-evil/x.md", "/base/repo"))


if __name__ == "__main__":
    unittest.main()
