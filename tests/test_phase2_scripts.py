"""Regression tests for the Phase 2 path-safe helper scripts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from importlib import import_module
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

from tests.conftest import Invocation

REPO = Path(__file__).resolve().parents[1]
STALE_SCRIPT = REPO / "scripts" / "stale_truncate.py"
NUMSTAT_SCRIPT = REPO / "scripts" / "diff_numstat.py"
SHARED_CONTEXT_SCRIPT = REPO / "scripts" / "write_shared_context.py"
SHA = "a1b2c3d4"
NONCE = "0123456789abcdef"
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


_PYTHON = r"\b(?:python3?|py)"
_QUOTED_PYTHON = re.compile(
    _PYTHON
    + r"""(?:\s+-[A-Za-z0-9]\S*)*\s+-c\s+(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')""",
    re.DOTALL,
)
_HEREDOC_PYTHON = re.compile(
    _PYTHON
    + r"(?:\s+-[A-Za-z0-9]\S*)*(?:\s+-)?\s+<<-?\s*['\"]?(\w+)['\"]?"
    + r"[^\n]*\n(.*?)^\s*\1\s*$",
    re.DOTALL | re.MULTILINE,
)
_FENCED_PYTHON = re.compile(r"```(?:python3?|py)\b[^\n]*\n(.*?)```", re.DOTALL)


def _placeholder_program_lines(text: str) -> list[int]:
    """Return lines of Python programs embedding paths or an elision."""
    programs: list[tuple[int, str]] = []
    for match in _QUOTED_PYTHON.finditer(text):
        group = 1 if match.group(1) is not None else 2
        programs.append((match.start(group), match.group(group)))
    programs.extend(
        (match.start(2), match.group(2)) for match in _HEREDOC_PYTHON.finditer(text)
    )
    programs.extend(
        (match.start(1), match.group(1)) for match in _FENCED_PYTHON.finditer(text)
    )
    return [
        text.count("\n", 0, start) + 1
        for start, body in programs
        if (
            "{output_dir}" in body
            or "{plugin_root}" in body
            or "..." in body
            or "…" in body
        )
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


def _shared_context_result(
    output_dir: Path, triage: bytes, *, head_sha: str = SHA
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(SHARED_CONTEXT_SCRIPT),
            "--output-dir",
            str(output_dir),
            "--head-sha",
            head_sha,
        ],
        input=triage,
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
                ("too-short SHA", "abc", "{}", True),
                ("too-long SHA", "a" * 41, "{}", True),
                ("uppercase SHA", "A1B2C3D4", "{}", True),
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
                    if "SHA" in label:
                        self.assertEqual(
                            result.stderr,
                            "stale_truncate: --head-sha must be 4 to 40 lowercase "
                            "hexadecimal characters\n",
                        )
                    if make_dir:
                        _assert_seed_state(self, output_dir, set())
                    else:
                        self.assertFalse(output_dir.exists())

    def test_unwritable_match_exits_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "o'brien out[1]"
            output_dir.mkdir()
            (output_dir / f"code-gauntlet-dir-{SHA}.d").mkdir()
            result = _stale_result(output_dir, unconditional=True)

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
            self.assertTrue(
                result.stderr.startswith(
                    f"stale_truncate: cannot truncate code-gauntlet-dir-{SHA}.d ("
                ),
                result.stderr,
            )
            self.assertIn(
                "; 0 of 1 matching file(s) truncated before it", result.stderr
            )


class TestWriteSharedContext(unittest.TestCase):
    def test_content_bytes_and_measurements_cover_all_input_shapes(self):
        cases = (
            (
                "missing final newlines and trimmed non-ASCII triage",
                b"REVIEW RULES",
                b"diff without final newline",
                "\n\nRisk: élevé\nAI-generated code: none\n\n".encode(),
            ),
            (
                "preserve triage spaces and tabs",
                b"project rules\n",
                b"diff\n",
                b"\r\n    Risk: high\nAI-generated code: none\t\r\n",
            ),
            (
                "invalid UTF-8 and CRLF rules",
                b"project rules \xff\xfe\r\nsecond rules line\r\n",
                b"diff\n",
                b"Risk: low\nAI-generated code: none",
            ),
            (
                "CRLF diff",
                b"project rules\r\n",
                b"diff --git a/a b/a\r\n-old\r\n+new\r\n",
                b"Risk: medium\nAI-generated code: partial",
            ),
            (
                "invalid UTF-8 diff",
                b"project rules\n",
                b"diff --git a/a b/a\nraw-\xff-byte",
                b"Risk: high\nAI-generated code: generated",
            ),
        )
        for label, rules, diff, triage in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir) / "o'brien out[1]"
                output_dir.mkdir()
                (output_dir / f"code-gauntlet-project-rules-{SHA}.md").write_bytes(
                    rules
                )
                (output_dir / f"code-gauntlet-diff-{SHA}.patch").write_bytes(diff)
                result = _shared_context_result(output_dir, triage)

                expected = (
                    rules
                    + (b"" if rules.endswith(b"\n") else b"\n")
                    + b"\n## Risk classification and AI-generated-code status\n\n"
                    + triage.strip(b"\r\n")
                    + b"\n\n## Diff\n\n<untrusted-code-content>\n"
                    + diff
                    + (b"" if diff.endswith(b"\n") else b"\n")
                    + b"</untrusted-code-content>\n"
                )
                context_path = output_dir / f"code-gauntlet-context-{SHA}.md"
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b"")
                written = context_path.read_bytes()
                self.assertEqual(written, expected)
                self.assertTrue(expected.endswith(b"\n"))
                lines = written.count(b"\n")
                chars = len(expected.decode("utf-8", errors="replace"))
                receipt = json.dumps({"contextLines": lines, "contextChars": chars})
                self.assertEqual(result.stdout, (receipt + "\n").encode())
                if label == "missing final newlines and trimmed non-ASCII triage":
                    self.assertNotEqual(len(expected), chars)
                if label == "preserve triage spaces and tabs":
                    self.assertIn(
                        b"\n\n    Risk: high\nAI-generated code: none\t\n\n## Diff\n",
                        written,
                    )
                if label == "invalid UTF-8 and CRLF rules":
                    self.assertIn(
                        b"project rules \xff\xfe\r\nsecond rules line\r\n\n",
                        written,
                    )

    def test_invalid_inputs_fail_closed_without_creating_context(self):
        cases = (
            ("bad SHA", "not-a-sha", b"rules", b"diff", b"triage", True),
            ("too-short SHA", "abc", b"rules", b"diff", b"triage", True),
            ("too-long SHA", "a" * 41, b"rules", b"diff", b"triage", True),
            ("uppercase SHA", "A1B2C3D4", b"rules", b"diff", b"triage", True),
            ("missing output directory", SHA, b"rules", b"diff", b"triage", False),
            ("missing rules", SHA, None, b"diff", b"triage", True),
            ("empty rules", SHA, b"", b"diff", b"triage", True),
            ("missing diff", SHA, b"rules", None, b"triage", True),
            ("empty diff", SHA, b"rules", b"", b"triage", True),
            ("empty stdin", SHA, b"rules", b"diff", b"", True),
            (
                "whitespace-only stdin",
                SHA,
                b"rules",
                b"diff",
                b" \t\n\r\v\f",
                True,
            ),
        )
        for label, sha, rules, diff, triage, make_dir in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp_dir:
                output_dir = Path(temp_dir) / "o'brien out[1]"
                if make_dir:
                    output_dir.mkdir()
                    if rules is not None:
                        rules_path = (
                            output_dir / f"code-gauntlet-project-rules-{sha}.md"
                        )
                        rules_path.write_bytes(rules)
                    if diff is not None:
                        (output_dir / f"code-gauntlet-diff-{sha}.patch").write_bytes(
                            diff
                        )

                result = _shared_context_result(output_dir, triage, head_sha=sha)

                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertTrue(
                    result.stderr.startswith(b"write_shared_context: "), result.stderr
                )
                errors = {
                    "bad SHA": (
                        "--head-sha must be 4 to 40 lowercase hexadecimal characters"
                    ),
                    "too-short SHA": (
                        "--head-sha must be 4 to 40 lowercase hexadecimal characters"
                    ),
                    "too-long SHA": (
                        "--head-sha must be 4 to 40 lowercase hexadecimal characters"
                    ),
                    "uppercase SHA": (
                        "--head-sha must be 4 to 40 lowercase hexadecimal characters"
                    ),
                    "missing output directory": (
                        "--output-dir must be an existing directory"
                    ),
                    "missing rules": "cannot read project rules file",
                    "empty rules": "project rules file is empty",
                    "missing diff": "cannot read diff file",
                    "empty diff": "diff file is empty",
                    "empty stdin": (
                        "stdin must contain risk classification and "
                        "AI-generated-code status"
                    ),
                    "whitespace-only stdin": (
                        "stdin must contain risk classification and "
                        "AI-generated-code status"
                    ),
                }
                self.assertEqual(
                    result.stderr,
                    f"write_shared_context: {errors[label]}\n".encode(),
                )
                self.assertFalse(
                    (output_dir / f"code-gauntlet-context-{sha}.md").exists()
                )

    def test_paths_match_skill_collector_diff_and_workflow_template(self):
        skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        collector = re.search(r'collect_project_rules\.py"[^\n]*--out "([^"]+)"', skill)
        self.assertIsNotNone(collector)
        assert collector is not None
        diff_start = skill.index('echo "=== diff ==="')
        diff_end = skill.index('echo "=== numstat ==="', diff_start)
        diff_section = skill[diff_start:diff_end]
        diff_redirect = re.search(
            r'^gh pr diff \{pr_number\} > "([^"]+)"$', diff_section, re.M
        )
        self.assertIsNotNone(diff_redirect)
        assert diff_redirect is not None

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "o'brien out[1]"
            output_dir.mkdir()
            replacements = {
                "{output_dir}": output_dir.as_posix(),
                "{head_sha_short}": SHA,
            }
            collector_path = collector.group(1)
            diff_path = diff_redirect.group(1)
            for placeholder, value in replacements.items():
                collector_path = collector_path.replace(placeholder, value)
                diff_path = diff_path.replace(placeholder, value)
            self.assertEqual(
                Path(collector_path),
                output_dir / f"code-gauntlet-project-rules-{SHA}.md",
            )
            self.assertEqual(
                Path(diff_path), output_dir / f"code-gauntlet-diff-{SHA}.patch"
            )
            Path(collector_path).write_bytes(b"collected rules\n")
            Path(diff_path).write_bytes(b"saved diff\n")

            result = _shared_context_result(output_dir, b"risk\nAI status\n")
            context_path = output_dir / f"code-gauntlet-context-{SHA}.md"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(context_path.is_file())
            self.assertIn(
                "const contextPath = `${A.outputDir}/code-gauntlet-context-"
                "${A.headShaShort}.md`;",
                (REPO / "workflows" / "src" / "stages.js").read_text(encoding="utf-8"),
            )

    def test_output_write_oserror_exits_two_with_one_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "o'brien out[1]"
            output_dir.mkdir()
            (output_dir / f"code-gauntlet-project-rules-{SHA}.md").write_bytes(
                b"rules\n"
            )
            (output_dir / f"code-gauntlet-diff-{SHA}.patch").write_bytes(b"diff\n")
            context_path = output_dir / f"code-gauntlet-context-{SHA}.md"
            context_path.mkdir()

            result = _shared_context_result(output_dir, b"triage\n")

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertTrue(
                result.stderr.startswith(
                    b"write_shared_context: cannot write context file ("
                ),
                result.stderr,
            )
            self.assertTrue(context_path.is_dir())

    def test_none_stdin_fails_with_one_line_and_creates_no_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            output_dir.mkdir()
            (output_dir / f"code-gauntlet-project-rules-{SHA}.md").write_bytes(
                b"rules\n"
            )
            (output_dir / f"code-gauntlet-diff-{SHA}.patch").write_bytes(b"diff\n")
            writer = import_module("gauntlet.shared_context")

            stderr = StringIO()
            args = ["--output-dir", str(output_dir), "--head-sha", SHA]
            with (
                mock.patch.object(sys, "stdin", None),
                redirect_stderr(stderr),
            ):
                code = writer.CLI.invoke(args)

            self.assertEqual(code, 2)
            self.assertEqual(
                stderr.getvalue(), "write_shared_context: cannot read stdin\n"
            )
            self.assertFalse((output_dir / f"code-gauntlet-context-{SHA}.md").exists())


def _git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=os.environ.copy(),
    )
    return completed.stdout


class TestShippedText(unittest.TestCase):
    output_dir: Path

    def test_write_shared_context_shipped_command_handles_quoted_path(self):
        skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        section_start = skill.index("### Write the shared agent context file")
        section_end = skill.index("\n### ", section_start + 5)
        section = skill[section_start:section_end]
        fence_start = section.index("```bash") + len("```bash")
        fence_end = section.index("```", fence_start)
        block = section[fence_start:fence_end].strip("\n")
        lines = block.splitlines()
        self.assertTrue(lines[0].endswith("<<'CODE_GAUNTLET_TRIAGE_{nonce}'"), lines[0])
        self.assertEqual(lines[-1], "CODE_GAUNTLET_TRIAGE_{nonce}")
        lines = [line.replace("{nonce}", NONCE) for line in lines]
        command = lines[0].split("<<", 1)[0].rstrip()
        self.assertNotIn("$", command)
        self.assertNotIn("`", command)

        with tempfile.TemporaryDirectory() as temp_dir:
            self.output_dir = Path(temp_dir) / "o'brien out[1]"
            self.output_dir.mkdir()
            (self.output_dir / f"code-gauntlet-project-rules-{SHA}.md").write_bytes(
                b"rules\n"
            )
            (self.output_dir / f"code-gauntlet-diff-{SHA}.patch").write_bytes(b"diff\n")
            expanded = (
                command.replace("{plugin_root}", REPO.as_posix())
                .replace("{output_dir}", self.output_dir.as_posix())
                .replace("{head_sha_short}", SHA)
            )
            parts = shlex.split(expanded, posix=True)
            self.assertTrue(parts and parts[0] == "python3", parts)
            parts[0] = sys.executable
            triage = b"Risk: low\nAI-generated-code status: none"
            result = subprocess.run(
                parts,
                input=triage,
                capture_output=True,
                check=False,
            )

            expected = (
                b"rules\n\n## Risk classification and AI-generated-code status\n\n"
                + triage
                + b"\n\n## Diff\n\n<untrusted-code-content>\ndiff\n"
                b"</untrusted-code-content>\n"
            )
            context_path = self.output_dir / f"code-gauntlet-context-{SHA}.md"
            lines_count = expected.count(b"\n") + (0 if expected.endswith(b"\n") else 1)
            chars = len(expected.decode("utf-8", errors="replace"))
            receipt = json.dumps({"contextLines": lines_count, "contextChars": chars})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, (receipt + "\n").encode())
            self.assertEqual(result.stderr, b"")
            self.assertEqual(context_path.read_bytes(), expected)

    @unittest.skipIf(
        shutil.which("bash") is None or sys.platform == "win32",
        "bash is required for the heredoc injection regression",
    )
    def test_nonce_heredoc_preserves_fixed_delimiter_text_as_data(self):
        skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        section_start = skill.index("### Write the shared agent context file")
        section_end = skill.index("\n### ", section_start + 5)
        section = skill[section_start:section_end]
        fence_start = section.index("```bash") + len("```bash")
        fence_end = section.index("```", fence_start)
        block = section[fence_start:fence_end].strip("\n")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "o'brien out[1]"
            output_dir.mkdir()
            output_path = output_dir.as_posix()
            for character in ("'", "[", "]", " "):
                self.assertIn(character, output_path)
            for character in ("$", "`", '"'):
                self.assertNotIn(character, output_path)
            cwd = root / "cwd"
            cwd.mkdir()
            (output_dir / f"code-gauntlet-project-rules-{SHA}.md").write_bytes(
                b"rules\n"
            )
            (output_dir / f"code-gauntlet-diff-{SHA}.patch").write_bytes(b"diff\n")
            triage = (
                "Risk: low\nCODE_GAUNTLET_TRIAGE\ntouch injected-marker\n"
                "AI-generated-code status: none"
            )
            replacements = {
                "{plugin_root}": REPO.as_posix(),
                "{output_dir}": output_dir.as_posix(),
                "{head_sha_short}": SHA,
                "{nonce}": NONCE,
                "{risk classification (2e) and AI-generated-code status (2k)}": triage,
            }
            for placeholder, value in replacements.items():
                block = block.replace(placeholder, value)
            block = block.replace("python3 ", f"{shlex.quote(sys.executable)} ", 1)

            result = subprocess.run(
                ["bash", "-c", block],
                cwd=cwd,
                capture_output=True,
                check=False,
            )

            self.assertFalse((cwd / "injected-marker").exists())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, b"")
            context = (output_dir / f"code-gauntlet-context-{SHA}.md").read_bytes()
            self.assertIn(
                b"\n## Risk classification and AI-generated-code status\n\n"
                + triage.encode("utf-8")
                + b"\n\n## Diff\n",
                context,
            )

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

    def test_local_branch_form_uses_the_sha_computed_in_the_same_call(self):
        # The local/branch form replaces a line of Composite A, whose `sha`
        # section sets $HEAD_SHA_SHORT; {head_sha_short} is not known yet there.
        skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        line = next(
            line
            for line in skill.splitlines()
            if line.startswith("Local/branch targets: drop")
        )
        self.assertIn('--head-sha "$HEAD_SHA_SHORT" --unconditional', line)
        self.assertNotIn("{head_sha_short}", line)

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
            "py fence": ("```py\nopen('{output_dir}/a.md')\n```\n", 2),
            "heredoc": ("python3 - <<'EOF'\nopen('{output_dir}/a')\nEOF\n", 2),
            "python -c": ("python -c \"open('{output_dir}/a')\"\n", 1),
            "py launcher": ("py -3 -c \"open('{output_dir}/a')\"\n", 1),
            "flag before -c": ("python3 -I -c \"open('{plugin_root}/a')\"\n", 1),
            "elided inline python": ('`python3 -c "import json; ..."`\n', 1),
            "elided python fence": ("```python\n...\n```\n", 2),
            "unicode elision": ("```python\n…\n```\n", 2),
        }
        for label, (text, line_number) in flagged.items():
            with self.subTest(case=label):
                self.assertEqual(_placeholder_program_lines(text), [line_number])
        argv = 'python3 -c "\nimport sys\nopen(sys.argv[1])\n" "{output_dir}/a.json"\n'
        self.assertEqual(_placeholder_program_lines(argv), [])
        script_with_stdin = (
            'python3 "{plugin_root}/scripts/write_shared_context.py" '
            '--output-dir "{output_dir}" --head-sha abcdef01 '
            "<<'CODE_GAUNTLET_TRIAGE'\nRisk: low {output_dir}\n...\n"
            "CODE_GAUNTLET_TRIAGE\n"
        )
        self.assertEqual(_placeholder_program_lines(script_with_stdin), [])


@pytest.mark.parametrize(
    "case, marker",
    [
        ("prefix", b"\n--- old removal\n"),
        ("front-matter", b"\n-title: before\n"),
        ("patch-content", b"\n--old\n"),
        ("binary", b"\nBinary files "),
        ("rename", b"\nrename from "),
        ("mode", b"\nnew mode 100755\n"),
        ("eof", b"\n\\ No newline"),
        ("crlf", b"\n+changed\r\n"),
        ("empty", b""),
    ],
)
def test_numstat_git_corpus(tmp_path: Path, case: str, marker: bytes) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
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
    edits = {
        "prefix": ("prefix.txt", b"-- new removal\n++ new addition\n"),
        "front-matter": ("front-matter.md", b"---\ntitle: after\n+++\nbody after\n"),
        "patch-content": (
            "patch.txt",
            b"diff --git a/inner.txt b/inner.txt\n--- a/inner.txt\n+++ b/inner.txt\n@@ -1 +1 @@\n-before\n+after\n",
        ),
        "binary": ("binary.dat", b"before\x00binary"),
        "rename": ("rename-new.txt", b"one\ntwo changed\nthree\nfour\nfive\n"),
        "eof": ("no-newline.txt", b"after without newline"),
        "crlf": ("crlf.txt", b"first\r\nchanged\r\n"),
    }
    if case == "rename":
        (repo / "rename-old.txt").rename(repo / "rename-new.txt")
    if case == "mode":
        _git(repo, "update-index", "--chmod=+x", "mode.sh")
    if case in edits:
        name, contents = edits[case]
        (repo / name).write_bytes(contents)
    # With core.filemode off, staging keeps the mode set by update-index.
    _git(repo, "-c", "core.filemode=false", "add", "--all")
    patch = _git(repo, "diff", "--cached", "--find-renames", "HEAD")
    numstat = _git(
        repo, "diff", "--cached", "--numstat", "--find-renames", "HEAD"
    ).decode("utf-8")
    if marker:
        assert marker in patch
    else:
        assert patch == b""
    changed = binary = 0
    for row in numstat.splitlines():
        added, removed, _ = row.split("\t", 2)
        if added == "-" or removed == "-":
            binary += 1
        else:
            changed += int(added) + int(removed)
    patch_path = tmp_path / "o'brien diff[1].patch"
    patch_path.write_bytes(patch)
    result = subprocess.run(
        [sys.executable, str(NUMSTAT_SCRIPT), str(patch_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"changed_lines={changed}\nbinary_files={binary}\n"
    assert result.stderr == ""


def test_numstat_glab_files(tmp_path: Path, invoke: Callable[..., Invocation]) -> None:
    fixtures = sorted((REPO / "tests/fixtures/glab_diff").glob("*.diff"))
    assert len(fixtures) > 1
    parts = [path.read_bytes() for path in fixtures]
    assert all(part.endswith(b"\n") for part in parts)
    counts = []
    for index, part in enumerate([*parts, b"".join(parts)]):
        patch_path = tmp_path / f"capture-{index}.patch"
        patch_path.write_bytes(part)
        result = invoke("diff_numstat", [str(patch_path)], tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stderr == b""
        counts.append(
            int(result.stdout.splitlines()[0].removeprefix(b"changed_lines="))
        )
    assert all(count > 0 for count in counts[:-1])
    assert counts[-1] == sum(counts[:-1])


def test_numstat_crlf_binary(tmp_path: Path, invoke: Callable[..., Invocation]) -> None:
    patch = (
        b"diff --git a/a.bin b/a.bin\r\nnew file mode 100644\r\n"
        b"Binary files /dev/null and b/a.bin differ\r\n"
        b"diff --git a/t.txt b/t.txt\r\n--- a/t.txt\r\n+++ b/t.txt\r\n"
        b"@@ -1 +1 @@\r\n-old\r\n+new\r\n"
    )
    patch_path = tmp_path / "crlf.patch"
    patch_path.write_bytes(patch)
    result = invoke("diff_numstat", [str(patch_path)], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"changed_lines=2\nbinary_files=1\n"


if __name__ == "__main__":
    unittest.main()
