import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.phase2 import (
    ARTIFACTS,
    SHA,
    TRUNCATED,
    seed_artifacts,
    write_context_inputs,
)

REPO = Path(__file__).resolve().parents[1]
NONCE = "0123456789abcdef"

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


def _context_block() -> str:
    skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    start = skill.index("### Write the shared agent context file")
    end = skill.index("\n### ", start + 5)
    section = skill[start:end]
    fence_start = section.index("```bash") + len("```bash")
    return section[fence_start : section.index("```", fence_start)].strip("\n")


def test_shipped_context_paths(tmp_path: Path) -> None:
    skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    collector = re.search(r'collect_project_rules\.py"[^\n]*--out "([^"]+)"', skill)
    assert collector is not None
    start = skill.index('echo "=== diff ==="')
    end = skill.index('echo "=== numstat ==="', start)
    diff_redirect = re.search(
        r'^gh pr diff \{pr_number\} > "([^"]+)"$', skill[start:end], re.M
    )
    assert diff_redirect is not None
    lines = _context_block().splitlines()
    assert lines[0].endswith("<<'CODE_GAUNTLET_TRIAGE_{nonce}'"), lines[0]
    assert lines[-1] == "CODE_GAUNTLET_TRIAGE_{nonce}"
    command = lines[0].split("<<", 1)[0].rstrip()
    assert "$" not in command
    assert "`" not in command
    output_dir = tmp_path / "o'brien out[1]"
    output_dir.mkdir()
    collector_path = collector.group(1)
    diff_path = diff_redirect.group(1)
    for placeholder, value in {
        "{output_dir}": output_dir.as_posix(),
        "{head_sha_short}": SHA,
    }.items():
        collector_path = collector_path.replace(placeholder, value)
        diff_path = diff_path.replace(placeholder, value)
    assert Path(collector_path) == output_dir / f"code-gauntlet-project-rules-{SHA}.md"
    assert Path(diff_path) == output_dir / f"code-gauntlet-diff-{SHA}.patch"
    Path(collector_path).write_bytes(b"rules\n")
    Path(diff_path).write_bytes(b"diff\n")
    command = (
        command.replace("{plugin_root}", REPO.as_posix())
        .replace("{output_dir}", output_dir.as_posix())
        .replace("{head_sha_short}", SHA)
    )
    parts = shlex.split(command, posix=True)
    assert parts and parts[0] == "python3", parts
    parts[0] = sys.executable
    result = subprocess.run(
        parts,
        input=b"Risk: low\nAI-generated-code status: none",
        capture_output=True,
        check=False,
    )
    expected = (
        b"rules\n\n## Risk classification and AI-generated-code status\n\n"
        b"Risk: low\nAI-generated-code status: none\n\n"
        b"## Diff\n\n<untrusted-code-content>\ndiff\n"
        b"</untrusted-code-content>\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'{"contextLines": 12, "contextChars": 167}\n'
    assert result.stderr == b""
    assert (output_dir / f"code-gauntlet-context-{SHA}.md").read_bytes() == expected


@pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="bash is required for the heredoc injection regression",
)
def test_nonce_heredoc_preserves_fixed_delimiter_text_as_data(tmp_path: Path) -> None:
    block = _context_block()
    output_dir = tmp_path / "o'brien out[1]"
    write_context_inputs(output_dir, b"rules\n", b"diff\n")
    output_path = output_dir.as_posix()
    for character in ("'", "[", "]", " "):
        assert character in output_path
    for character in ("$", "`", '"'):
        assert character not in output_path
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    triage = (
        "Risk: low\n"
        "CODE_GAUNTLET_TRIAGE\n"
        "touch injected-marker\n"
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
        ["bash", "-c", block], cwd=cwd, capture_output=True, check=False
    )
    assert not (cwd / "injected-marker").exists()
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    context = (output_dir / f"code-gauntlet-context-{SHA}.md").read_bytes()
    assert (
        b"\n## Risk classification and AI-generated-code status\n\n"
        + triage.encode("utf-8")
        + b"\n\n## Diff\n"
        in context
    )


def _command_after_section(label: str, *, pipe: bool) -> str:
    skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(encoding="utf-8")
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
    return command


def _run_shipped(
    output_dir: Path, command: str, detector_json: str | None = None
) -> subprocess.CompletedProcess[str]:
    expanded = (
        command.replace("{plugin_root}", REPO.as_posix())
        .replace("{output_dir}", output_dir.as_posix())
        .replace("{head_sha_short}", SHA)
        .replace("$HEAD_SHA_SHORT", SHA)
    )
    parts = shlex.split(expanded, posix=True)
    assert parts and parts[0] == "python3", parts
    parts[0] = sys.executable
    return subprocess.run(
        parts,
        input=detector_json,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "platform,url_key,url",
    [
        ("github", "url", "https://github.com/o/r/pull/5"),
        ("gitlab", "web_url", "https://gitlab.com/group/sub/r/-/merge_requests/5"),
    ],
)
def test_shipped_pr_identity_keeps_author_text_on_stdin(
    platform: str, url_key: str, url: str, tmp_path: Path, monkeypatch
) -> None:
    skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    start = skill.index("<!-- generated-from-registry-identity:pr_identity_fields")
    end = skill.index(
        "<!-- /generated-from-registry-identity:pr_identity_fields", start
    )
    command = next(
        line[1:-1]
        for line in skill[start:end].splitlines()
        if line.startswith("`")
        and "resolve_pr_identity.py" in line
        and f"--platform {platform}" in line
    )
    assert "--title" not in command
    assert "--url" not in command
    assert set(re.findall(r"\{[^}]+\}", command)) == {"{pr_number}", "{plugin_root}"}
    producer, consumer = command.split(" | ")
    assert producer == (
        "gh pr view {pr_number} --json url,title"
        if platform == "github"
        else "glab mr view {pr_number} --output json"
    )
    sha = "a" * 40
    parts = shlex.split(
        consumer.replace("{plugin_root}", REPO.as_posix()).replace(
            '"$(git rev-parse HEAD)"', sha
        )
    )
    assert parts == [
        "python3",
        (REPO / "scripts/resolve_pr_identity.py").as_posix(),
        "--platform",
        platform,
        "--sha",
        sha,
    ]
    parts[0] = sys.executable
    title = "$(touch MARKER) `touch MARKER` both \" and '\ncaf\u00e9\r\nend"
    payload = {url_key: url, "title": title}
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    result = subprocess.run(
        parts,
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert not (tmp_path / "MARKER").exists()
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    identity = json.loads(result.stdout)
    assert identity["title"].encode("utf-8") == title.encode("utf-8")
    assert identity["platform"] == platform
    assert identity["pr_number"] == 5


def test_stale_truncate_shipped_command_handles_quoted_output_path(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    seed_artifacts(output_dir)
    command = _command_after_section("stale_truncate", pipe=True)
    assert re.findall("\\$[A-Za-z_][A-Za-z0-9_]*", command) == ["$HEAD_SHA_SHORT"]
    assert "`" not in command
    detector = json.dumps({"previously_reviewed": False})
    result = _run_shipped(output_dir, command, detector)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "truncated 2 file(s)\n"
    for name, contents in ARTIFACTS.items():
        assert (output_dir / name).read_bytes() == (
            b"" if name in TRUNCATED else contents
        ), name


def test_diff_numstat_shipped_command_handles_quoted_output_path(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "o'brien out[1]"
    output_dir.mkdir()
    patch_path = output_dir / f"code-gauntlet-diff-{SHA}.patch"
    patch_path.write_bytes(
        b"diff --git a/file.txt b/file.txt\n"
        b"--- a/file.txt\n"
        b"+++ b/file.txt\n"
        b"@@ -1 +1 @@\n"
        b"--- old content\n"
        b"+++ new content\n"
    )
    command = _command_after_section("numstat", pipe=False)
    assert re.findall("\\$[A-Za-z_][A-Za-z0-9_]*", command) == []
    assert "`" not in command
    result = _run_shipped(output_dir, command)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "changed_lines=2\nbinary_files=0\n"


def test_local_branch_form_uses_the_sha_computed_in_the_same_call() -> None:
    skill = (REPO / "skills" / "code-gauntlet" / "SKILL.md").read_text(encoding="utf-8")
    line = next(
        line
        for line in skill.splitlines()
        if line.startswith("Local/branch targets: drop")
    )
    assert '--head-sha "$HEAD_SHA_SHORT" --unconditional' in line
    assert "{head_sha_short}" not in line


def test_python_programs_in_skills_and_agents_do_not_embed_path_placeholders() -> None:
    offender_paths: list[str] = []
    for root_name in ("skills", "agents"):
        for path in (REPO / root_name).rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            offender_paths.extend(
                f"{path.relative_to(REPO)}:{line_number}"
                for line_number in _placeholder_program_lines(text)
            )
    assert offender_paths == []


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            'python3 -c "\nimport sys\nopen(sys.argv[1])\n" "{output_dir}/a.json"\n',
            [],
            id="quoted-safe-argv",
        ),
        pytest.param(
            'echo "$J" | python3 -c "\nimport glob\nroot = \'{output_dir}\'\n"\n',
            [1],
            id="quoted-path-embedding",
        ),
        pytest.param(
            "python3 - <<'EOF'\nprint('data')\nEOF\n",
            [],
            id="heredoc-safe",
        ),
        pytest.param(
            "python3 - <<'EOF'\nopen('{plugin_root}/a')\nEOF\n",
            [2],
            id="heredoc-path-embedding",
        ),
        pytest.param("```python\nprint('data')\n```\n", [], id="fence-safe"),
        pytest.param("```python\n...\n```\n", [2], id="fence-elision"),
    ],
)
def test_placeholder_scan_flags_program_text_and_spares_argv(
    text: str, expected: list[int]
) -> None:
    assert _placeholder_program_lines(text) == expected
