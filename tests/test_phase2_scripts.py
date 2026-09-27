"""Regression tests for the Phase 2 path-safe helper scripts."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STALE_SCRIPT = REPO / "scripts" / "stale_truncate.py"
NUMSTAT_SCRIPT = REPO / "scripts" / "diff_numstat.py"
SHA = "a1b2c3d4"
DEFERRED = (
    "DEFERRED: previously reviewed at the current SHA -- truncation withheld "
    "until the Skip/Review-again answer is known (a Skip must preserve these files)"
)
ARTIFACTS = {
    f"code-gauntlet-findings-{SHA}.json": b"findings bytes",
    f"code-gauntlet-report-{SHA}.md": b"report bytes",
    "code-gauntlet-checkpoint-all-ffffffff.json": b"different sha",
    f"other-report-{SHA}.md": b"no prefix",
}
TRUNCATED = {
    f"code-gauntlet-findings-{SHA}.json",
    f"code-gauntlet-report-{SHA}.md",
}


_QUOTED_PYTHON = re.compile(
    r"""python3\s+-c\s+(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')""",
    re.DOTALL,
)
_FENCED_PYTHON = re.compile(r"```python[^\n]*\n(.*?)```", re.DOTALL)


def _placeholder_program_lines(text: str) -> list[int]:
    """Return the line of each Python program in *text* that embeds a path."""
    programs: list[tuple[int, str]] = []
    for match in _QUOTED_PYTHON.finditer(text):
        group = 1 if match.group(1) is not None else 2
        programs.append((match.start(group), match.group(group)))
    programs.extend(
        (match.start(1), match.group(1)) for match in _FENCED_PYTHON.finditer(text)
    )
    return [
        text.count("\n", 0, start) + 1
        for start, body in programs
        if "{output_dir}" in body or "{plugin_root}" in body
    ]


def _seed_output_dir(output_dir: Path) -> None:
    output_dir.mkdir()
    for name, contents in ARTIFACTS.items():
        (output_dir / name).write_bytes(contents)


def _stale_result(
    output_dir: Path,
    detector_json: str | None = None,
    *,
    head_sha: str = SHA,
    unconditional: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(STALE_SCRIPT),
        "--output-dir",
        str(output_dir),
        "--head-sha",
        head_sha,
    ]
    if unconditional:
        command.append("--unconditional")
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
    return subprocess.run(
        command,
        input=detector_json,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def _assert_seed_state(
    test: unittest.TestCase, output_dir: Path, empty_names: set[str]
) -> None:
    for name, contents in ARTIFACTS.items():
        expected = b"" if name in empty_names else contents
        test.assertEqual((output_dir / name).read_bytes(), expected, name)


class TestStaleTruncate(unittest.TestCase):
    def test_gated_outcomes_truncate_unprotected_artifacts(self):
        outcomes = {
            "no prior review": {},
            "unresolvable prior SHA": {
                "previously_reviewed": True,
                "sha_resolvable": False,
                "last_reviewed_sha": SHA,
                "head_sha": SHA,
            },
            "different resolvable SHA": {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "last_reviewed_sha": "f" * 40,
                "head_sha": SHA,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            for label, detector in outcomes.items():
                with self.subTest(outcome=label):
                    output_dir = Path(temp_dir) / f"o'brien out[1] {label}"
                    _seed_output_dir(output_dir)
                    result = _stale_result(output_dir, json.dumps(detector))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "truncated 2 file(s)\n")
                    self.assertEqual(result.stderr, "")
                    _assert_seed_state(self, output_dir, TRUNCATED)

    def test_gated_current_sha_defers_and_preserves_every_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "o'brien out[1]"
            _seed_output_dir(output_dir)
            detector = {
                "previously_reviewed": True,
                "sha_resolvable": True,
                "last_reviewed_sha": SHA,
                "head_sha": SHA,
            }

            result = _stale_result(output_dir, json.dumps(detector))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"{DEFERRED}\n")
            self.assertEqual(result.stderr, "")
            _assert_seed_state(self, output_dir, set())

    def test_unconditional_mode_truncates_without_stdin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "o'brien out[1]"
            _seed_output_dir(output_dir)
            result = _stale_result(output_dir, unconditional=True, detector_json=None)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "truncated 2 file(s)\n")
            self.assertEqual(result.stderr, "")
            _assert_seed_state(self, output_dir, TRUNCATED)

    def test_invalid_inputs_fail_closed_without_truncation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cases = (
                ("bad SHA", "not-a-sha", "{}", True),
                ("missing directory", SHA, "{}", False),
                ("non-object JSON", SHA, "[]", True),
                ("invalid JSON", SHA, "{", True),
            )
            for label, sha, stdin, make_dir in cases:
                with self.subTest(case=label):
                    output_dir = root / f"o'brien out[1] {label}"
                    if make_dir:
                        _seed_output_dir(output_dir)
                    result = _stale_result(output_dir, stdin, head_sha=sha)

                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(len(result.stderr.splitlines()), 1)
                    self.assertTrue(
                        result.stderr.startswith("stale_truncate: "), result.stderr
                    )
                    if make_dir:
                        _assert_seed_state(self, output_dir, set())
                    else:
                        self.assertFalse(output_dir.exists())


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    return completed.stdout


class TestDiffNumstat(unittest.TestCase):
    def test_diff_numstat_matches_git_numstat_corpus(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            patch_dir = root / "o'brien diff[1]"
            patch_dir.mkdir()
            _git(repo, "init", "--quiet")
            _git(repo, "config", "user.name", "Test User")
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "core.autocrlf", "false")
            _git(repo, "config", "diff.renames", "true")

            files = {
                "prefix.txt": b"-- old removal\n++ old addition\n",
                "front-matter.md": b"---\ntitle: before\n+++\nbody before\n",
                "patch.txt": (
                    b"diff --git a/inner.txt b/inner.txt\n"
                    b"--- a/inner.txt\n+++ b/inner.txt\n"
                    b"@@ -1 +1 @@\n-old\n+new\n"
                ),
                "rename-old.txt": b"one\ntwo\nthree\nfour\nfive\n",
                "mode.sh": b"#!/bin/sh\nexit 0\n",
                "no-newline.txt": b"before without newline",
                "crlf.txt": b"first\r\nsecond\r\n",
            }
            for name, contents in files.items():
                (repo / name).write_bytes(contents)
            _git(repo, "add", "--all")
            _git(repo, "commit", "--quiet", "-m", "baseline")

            def edit_prefix() -> None:
                (repo / "prefix.txt").write_bytes(b"-- new removal\n++ new addition\n")

            def edit_front_matter() -> None:
                (repo / "front-matter.md").write_bytes(
                    b"---\ntitle: after\n+++\nbody after\n"
                )

            def edit_patch_file() -> None:
                (repo / "patch.txt").write_bytes(
                    b"diff --git a/inner.txt b/inner.txt\n"
                    b"--- a/inner.txt\n+++ b/inner.txt\n"
                    b"@@ -1 +1 @@\n-before\n+after\n"
                )

            def add_binary() -> None:
                (repo / "binary.dat").write_bytes(b"before\x00binary")
                _git(repo, "add", "binary.dat")

            def rename_with_edit() -> None:
                (repo / "rename-old.txt").rename(repo / "rename-new.txt")
                (repo / "rename-new.txt").write_bytes(
                    b"one\ntwo changed\nthree\nfour\nfive\n"
                )

            def mode_only() -> None:
                _git(repo, "update-index", "--chmod=+x", "mode.sh")

            def no_trailing_newline() -> None:
                (repo / "no-newline.txt").write_bytes(b"after without newline")

            def crlf_lines() -> None:
                (repo / "crlf.txt").write_bytes(b"first\r\nchanged\r\n")

            # Each marker proves the case's patch really carries the shape it names.
            cases = (
                ("dash and plus content prefixes", edit_prefix, b"\n--- old removal\n"),
                ("front matter delimiters", edit_front_matter, b"\n-title: before\n"),
                ("patch as file content", edit_patch_file, b"\n--old\n"),
                ("binary file", add_binary, b"\nBinary files "),
                ("rename with edit", rename_with_edit, b"\nrename from "),
                ("mode only", mode_only, b"\nnew mode 100755\n"),
                ("no trailing newline", no_trailing_newline, b"\n\\ No newline"),
                ("CRLF lines", crlf_lines, b"\n+changed\r\n"),
                ("empty diff", lambda: None, b""),
            )
            for index, (label, edit, marker) in enumerate(cases):
                with self.subTest(case=label):
                    _git(repo, "reset", "--hard", "--quiet", "HEAD")
                    _git(repo, "clean", "-fdq")
                    edit()
                    # Stage every edit so a rename's new side is tracked; with
                    # core.filemode off, add keeps a mode set by update-index.
                    _git(repo, "-c", "core.filemode=false", "add", "--all")
                    patch = _git(repo, "diff", "--cached", "--find-renames", "HEAD")
                    numstat = _git(
                        repo, "diff", "--cached", "--numstat", "--find-renames", "HEAD"
                    ).decode("utf-8")
                    if marker:
                        self.assertIn(marker, patch)
                    else:
                        self.assertEqual(patch, b"")
                    expected_changed = 0
                    expected_binary = 0
                    for row in numstat.splitlines():
                        fields = row.split("\t", 2)
                        added, removed = fields[0], fields[1]
                        if added == "-" or removed == "-":
                            expected_binary += 1
                        else:
                            expected_changed += int(added) + int(removed)

                    patch_path = patch_dir / f"case-{index}.patch"
                    patch_path.write_bytes(patch)
                    result = subprocess.run(
                        [sys.executable, str(NUMSTAT_SCRIPT), str(patch_path)],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout,
                        f"changed_lines={expected_changed}\n"
                        f"binary_files={expected_binary}\n",
                    )
                    self.assertEqual(result.stderr, "")

    def test_multi_file_glab_diff_counts_each_file_once(self):
        # glab mr diff prints no `diff --git` lines, so a scan that leaves a hunk
        # only there counts every later file's ---/+++ headers as changes.
        fixtures = sorted((REPO / "tests" / "fixtures" / "glab_diff").glob("*.diff"))
        self.assertGreater(len(fixtures), 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            patch_dir = Path(temp_dir) / "o'brien glab[1]"
            patch_dir.mkdir()

            def changed_lines(patch: bytes, name: str) -> int:
                patch_path = patch_dir / name
                patch_path.write_bytes(patch)
                result = subprocess.run(
                    [sys.executable, str(NUMSTAT_SCRIPT), str(patch_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                first = result.stdout.splitlines()[0]
                return int(first.removeprefix("changed_lines="))

            parts = [path.read_bytes() for path in fixtures]
            self.assertTrue(all(part.endswith(b"\n") for part in parts))
            singles = [
                changed_lines(part, f"single-{index}.patch")
                for index, part in enumerate(parts)
            ]
            self.assertTrue(all(count > 0 for count in singles), singles)
            self.assertEqual(changed_lines(b"".join(parts), "all.patch"), sum(singles))

    def test_missing_patch_fails_without_stdout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "o'brien diff[1]" / "missing.patch"
            result = subprocess.run(
                [sys.executable, str(NUMSTAT_SCRIPT), str(missing)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertTrue(result.stderr.startswith("diff_numstat: "), result.stderr)


class TestShippedText(unittest.TestCase):
    output_dir: Path

    def _command_after_section(self, label: str, *, pipe: bool) -> str:
        skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        marker = f'echo "=== {label} ==="'
        start = skill.index(marker) + len(marker)
        end = skill.index("```", start)
        section = skill[start:end]
        if pipe:
            command = section[section.index("|") + 1 :].strip()
        else:
            command = next(
                line.strip()
                for line in section.splitlines()
                if line.strip().startswith("python3 ")
            )
            if command.startswith('python3 -c "'):
                source_end = section.index('\n"', section.index(command))
                command = section[section.index(command) : source_end + 2].strip()
        return command

    def _run_shipped(self, command: str, detector_json: str | None = None):
        expanded = (
            command.replace("{plugin_root}", REPO.as_posix())
            .replace("{output_dir}", self.output_dir.as_posix())
            .replace("{head_sha_short}", SHA)
            .replace("$HEAD_SHA_SHORT", SHA)
        )
        parts = shlex.split(expanded, posix=True)
        self.assertTrue(parts and parts[0] == "python3", parts)
        parts[0] = sys.executable
        return subprocess.run(
            parts,
            input=detector_json,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_stale_truncate_shipped_command_handles_quoted_output_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.output_dir = Path(temp_dir) / "o'brien out[1]"
            _seed_output_dir(self.output_dir)
            command = self._command_after_section("stale_truncate", pipe=True)
            self.assertEqual(
                re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", command),
                ["$HEAD_SHA_SHORT"],
            )
            self.assertNotIn("`", command)

            detector = json.dumps({"previously_reviewed": False})
            result = self._run_shipped(command, detector)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "truncated 2 file(s)\n")
            _assert_seed_state(self, self.output_dir, TRUNCATED)

    def test_diff_numstat_shipped_command_handles_quoted_output_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.output_dir = Path(temp_dir) / "o'brien out[1]"
            self.output_dir.mkdir()
            patch_path = self.output_dir / f"code-gauntlet-diff-{SHA}.patch"
            patch_path.write_bytes(
                b"diff --git a/file.txt b/file.txt\n"
                b"--- a/file.txt\n+++ b/file.txt\n"
                b"@@ -1 +1 @@\n--- old content\n+++ new content\n"
            )
            command = self._command_after_section("numstat", pipe=False)
            self.assertEqual(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", command), [])
            self.assertNotIn("`", command)

            result = self._run_shipped(command)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "changed_lines=2\nbinary_files=0\n")

    def test_python_programs_in_skills_and_agents_do_not_embed_path_placeholders(
        self,
    ):
        offender_paths = []
        for root_name in ("skills", "agents"):
            for path in (REPO / root_name).rglob("*.md"):
                text = path.read_text(encoding="utf-8")
                offender_paths.extend(
                    f"{path.relative_to(REPO)}:{line_number}"
                    for line_number in _placeholder_program_lines(text)
                )

        self.assertEqual(offender_paths, [])

    def test_placeholder_scan_flags_program_text_and_spares_argv(self):
        # Each value is the text and the line where its program body starts.
        flagged = {
            "multi-line double-quoted": (
                'echo "$J" | python3 -c "\nimport glob\nroot = \'{output_dir}\'\n"\n',
                1,
            ),
            "single-quoted": ("python3 -c 'print(\"{plugin_root}/x\")'\n", 1),
            "python fence": ("```python\nopen('{output_dir}/a.md')\n```\n", 2),
        }
        for label, (text, line_number) in flagged.items():
            with self.subTest(case=label):
                self.assertEqual(_placeholder_program_lines(text), [line_number])
        argv = 'python3 -c "\nimport sys\nopen(sys.argv[1])\n" "{output_dir}/a.json"\n'
        self.assertEqual(_placeholder_program_lines(argv), [])


if __name__ == "__main__":
    unittest.main()
