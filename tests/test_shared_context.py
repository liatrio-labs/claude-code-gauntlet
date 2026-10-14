import sys
from collections.abc import Callable
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest
from gauntlet import shared_context

from tests.conftest import Invocation
from tests.support.phase2 import SHA, write_context_inputs


@pytest.mark.parametrize(
    "rules, diff, triage, expected, receipt",
    [
        pytest.param(
            b"REVIEW RULES",
            b"diff without final newline",
            "\n\nRisk: \u00e9lev\u00e9\nAI-generated code: none\n\n".encode(),
            (
                b"REVIEW RULES\n\n"
                b"## Risk classification and AI-generated-code status\n\n"
                b"Risk: \xc3\xa9lev\xc3\xa9\nAI-generated code: none\n\n"
                b"## Diff\n\n<untrusted-code-content>\n"
                b"diff without final newline\n</untrusted-code-content>\n"
            ),
            b'{"contextLines": 12, "contextChars": 191}\n',
            id="absent-lf-non-ascii-trimmed-triage",
        ),
        pytest.param(
            b"project rules\n",
            b"diff\n",
            b"\r\n    Risk: high\nAI-generated code: none\t\r\n",
            (
                b"project rules\n\n"
                b"## Risk classification and AI-generated-code status\n\n"
                b"    Risk: high\nAI-generated code: none\t\n\n"
                b"## Diff\n\n<untrusted-code-content>\ndiff\n"
                b"</untrusted-code-content>\n"
            ),
            b'{"contextLines": 12, "contextChars": 174}\n',
            id="present-lf-preserved-spaces-tabs",
        ),
        pytest.param(
            b"project rules \xff\xfe\r\nsecond rules line\r\n",
            b"diff --git a/a b/a\r\nraw-\xff-byte\r\n",
            b"Risk: low\nAI-generated code: none",
            (
                b"project rules \xff\xfe\r\nsecond rules line\r\n\n"
                b"## Risk classification and AI-generated-code status\n\n"
                b"Risk: low\nAI-generated code: none\n\n"
                b"## Diff\n\n<untrusted-code-content>\n"
                b"diff --git a/a b/a\r\nraw-\xff-byte\r\n"
                b"</untrusted-code-content>\n"
            ),
            b'{"contextLines": 14, "contextChars": 218}\n',
            id="raw-invalid-utf8-and-crlf-in-both-inputs",
        ),
    ],
)
def test_context_bytes(
    rules: bytes,
    diff: bytes,
    triage: bytes,
    expected: bytes,
    receipt: bytes,
    tmp_path: Path,
    invoke: Callable[..., Invocation],
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    write_context_inputs(output_dir, rules, diff)
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(output_dir), "--head-sha", SHA],
        tmp_path,
        triage,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert (output_dir / f"code-gauntlet-context-{SHA}.md").read_bytes() == expected
    assert result.stdout == receipt


@pytest.mark.parametrize(
    "sha, make_dir, rules, diff, triage, error",
    [
        pytest.param(
            "not-a-sha",
            True,
            b"rules",
            b"diff",
            b"triage",
            "--head-sha must be 4 to 40 lowercase hexadecimal characters",
            id="bad-sha",
        ),
        pytest.param(
            SHA,
            False,
            b"rules",
            b"diff",
            b"triage",
            "--output-dir must be an existing directory",
            id="missing-directory",
        ),
        pytest.param(
            SHA,
            True,
            None,
            b"diff",
            b"triage",
            "cannot read project rules file",
            id="missing-rules",
        ),
        pytest.param(
            SHA,
            True,
            b"",
            b"diff",
            b"triage",
            "project rules file is empty",
            id="empty-rules",
        ),
        pytest.param(
            SHA,
            True,
            b"rules",
            None,
            b"triage",
            "cannot read diff file",
            id="missing-diff",
        ),
        pytest.param(
            SHA,
            True,
            b"rules",
            b"",
            b"triage",
            "diff file is empty",
            id="empty-diff",
        ),
        pytest.param(
            SHA,
            True,
            b"rules",
            b"diff",
            b"",
            "stdin must contain risk classification and AI-generated-code status",
            id="empty-stdin",
        ),
    ],
)
def test_context_refusal(
    sha: str,
    make_dir: bool,
    rules: bytes | None,
    diff: bytes | None,
    triage: bytes,
    error: str,
    tmp_path: Path,
    invoke: Callable[..., Invocation],
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    if make_dir:
        write_context_inputs(output_dir, rules, diff, sha=sha)
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(output_dir), "--head-sha", sha],
        tmp_path,
        triage,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == f"write_shared_context: {error}\n".encode()
    assert not (output_dir / f"code-gauntlet-context-{sha}.md").exists()


def test_output_write_oserror_exits_two_with_one_line(
    tmp_path: Path, invoke: Callable[..., Invocation]
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    write_context_inputs(output_dir, b"rules\n", b"diff\n")
    context_path = output_dir / f"code-gauntlet-context-{SHA}.md"
    context_path.mkdir()
    result = invoke(
        "write_shared_context",
        ["--output-dir", str(output_dir), "--head-sha", SHA],
        tmp_path,
        b"triage\n",
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert len(result.stderr.splitlines()) == 1
    assert result.stderr.startswith(
        b"write_shared_context: cannot write context file ("
    )
    assert context_path.is_dir()


def test_none_stdin_fails_with_one_line_and_creates_no_context(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    write_context_inputs(output_dir, b"rules\n", b"diff\n")
    stderr = StringIO()
    args = ["--output-dir", str(output_dir), "--head-sha", SHA]
    with mock.patch.object(sys, "stdin", None), redirect_stderr(stderr):
        code = shared_context.CLI.invoke(args)
    assert code == 2
    assert stderr.getvalue() == "write_shared_context: cannot read stdin\n"
    assert not (output_dir / f"code-gauntlet-context-{SHA}.md").exists()
