import ast
import json
import os
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
from tests.support.shell import bash_executable

REPO = Path(__file__).resolve().parents[1]
NONCE = "0123456789abcdef"

_PYTHON = r"(?:\b(?:python3?|py)|\{python\})"
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
_FENCE = re.compile(r"^([ \t]*)```([^\n]*)\n(.*?)^\1```", re.MULTILINE | re.DOTALL)


def skill_text(path: Path, python: str = "python3") -> str:
    return (
        path.read_text(encoding="utf-8")
        .replace("{python}", python)
        .replace('"${PYTHON[@]}"', python)
    )


def test_skill_text_resolves_only_source_interpreters(tmp_path):
    source = tmp_path / "skill.md"
    source.write_text('{python} a.py\n"${PYTHON[@]}" b.py\n', encoding="utf-8")
    assert skill_text(source, python="py -3") == "py -3 a.py\npy -3 b.py\n"


def test_preflight_shell_blocks_use_the_selected_argv():
    for path in (REPO / "skills/code-gauntlet").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        selector = re.search(r"\bPYTHON\s*=\s*\(", text)
        if selector is None:
            continue
        # Alternate forge snippets belong to the same composite selection scope.
        section_end = text.find("\n### ", selector.start())
        preflight = text[:section_end] if section_end >= 0 else text
        for line, body in _shell_fences(text):
            if (
                re.search(r"\bPYTHON\s*=\s*\(", body)
                or line <= preflight.count("\n") + 1
            ):
                assert "{python}" not in body, f"{path}:{line}"


def test_dispatch_reference_uses_current_command_contract():
    dispatch = (REPO / "skills/code-gauntlet/references/phase3-dispatch.md").read_text(
        encoding="utf-8"
    )
    assert 'await_workflow.py" --python "{python}"' in dispatch
    assert "assembleScriptPath" not in dispatch
    validation = (
        REPO / "skills/code-gauntlet/references/validation-pipeline.md"
    ).read_text(encoding="utf-8")
    assert "{verify.scriptPath}" not in validation
    assert '"{plugin_root}/scripts/verify_findings.py"' in validation


def _has_data_literals(body: str) -> bool:
    try:
        program = ast.parse(body)
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple))
        and any(
            isinstance(child, ast.Constant) and isinstance(child.value, str)
            for child in ast.walk(node)
        )
        for node in ast.walk(program)
    )


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
            or _has_data_literals(body)
        )
    ]


def _shell_fences(text: str) -> list[tuple[int, str]]:
    return [
        (text.count("\n", 0, match.start(3)) + 1, match[3])
        for match in _FENCE.finditer(text)
        if match[2].strip() in {"bash", "sh", "shell", "zsh"}
        or (
            not match[2].strip()
            and re.search(
                r"^\s*(?:python3?\b|\{python\}|Bash\s*\()", match[3], re.MULTILINE
            )
        )
    ]


def _markdown_sources() -> list[tuple[Path, str]]:
    return [
        (
            path.relative_to(REPO),
            skill_text(path),
        )
        for root in ("skills", "agents")
        for path in (REPO / root).rglob("*.md")
    ]


def _unquoted_path_lines(body: str) -> list[int]:
    quote = None
    escaped = False
    offenders = []
    for index, character in enumerate(body):
        if body.startswith(("{output_dir}", "{plugin_root}"), index) and quote != '"':
            offenders.append(body.count("\n", 0, index) + 1)
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
        elif character == quote:
            quote = None
        elif character in {"'", '"'} and quote is None:
            quote = character
    return offenders


def _context_block() -> str:
    skill = skill_text(REPO / "skills/code-gauntlet/references/phase2-preparation.md")
    start = skill.index("## Write the shared agent context file")
    section = skill[start:]
    fence_start = section.index("```bash") + len("```bash")
    return section[fence_start : section.index("```", fence_start)].strip("\n")


def test_shipped_context_paths(tmp_path: Path) -> None:
    skill = skill_text(REPO / "skills/code-gauntlet/references/phase2-preparation.md")
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
    checkout = tmp_path / "review checkout"
    checkout.mkdir()
    (checkout / "layout-marker").write_bytes(b"source")
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
        .replace("{repo_root}", checkout.as_posix())
        .replace("{head_sha_short}", SHA)
        .replace("{target_type}", "local")
        .replace("{head_sha_full}", SHA.ljust(40, "0"))
        .replace("{snapshot_args}", "")
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
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    context = (output_dir / f"code-gauntlet-context-{SHA}.md").read_bytes()
    receipt = json.loads(result.stdout)
    assert receipt["contextLines"] == context.count(b"\n")
    assert receipt["contextChars"] == len(context.decode("utf-8"))
    assert b'"entries": ["layout-marker"], "unavailable": []' in context


@pytest.mark.skipif(
    shutil.which(bash_executable()) is None,
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
        "{repo_root}": cwd.as_posix(),
        "{head_sha_short}": SHA,
        "{nonce}": NONCE,
        "{target_type}": "local",
        "{head_sha_full}": SHA.ljust(40, "0"),
        "{snapshot_args}": "",
        "{risk classification (2e) and AI-generated-code status (2k)}": triage,
    }
    for placeholder, value in replacements.items():
        block = block.replace(placeholder, value)
    block = block.replace(
        "python3 ", f"{shlex.quote(Path(sys.executable).as_posix())} ", 1
    )
    result = subprocess.run(
        [bash_executable(), "-c", block], cwd=cwd, capture_output=True, check=False
    )
    assert not (cwd / "injected-marker").exists()
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    context = (output_dir / f"code-gauntlet-context-{SHA}.md").read_bytes()
    assert (
        b"\n## Risk classification and AI-generated-code status\n\n"
        + triage.encode("utf-8")
        + b"\n\n## Repository layout:"
        in context
    )


def _command_after_section(label: str, *, pipe: bool) -> str:
    skill = skill_text(REPO / "skills/code-gauntlet/references/phase2-preparation.md")
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
        .replace("{target_type}", "local")
        .replace("{head_sha_full}", SHA.ljust(40, "0"))
        .replace("{snapshot_args}", "")
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
    skill = skill_text(REPO / "skills/code-gauntlet/SKILL.md")
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


@pytest.mark.parametrize(
    "quoted_name",
    [
        "y`touch MARKER`'portable",
        pytest.param(
            "y`touch MARKER`\"both'",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Windows filenames cannot contain quotes",
            ),
        ),
    ],
)
def test_shipped_incremental_diff_keeps_paths_as_data(
    quoted_name: str, tmp_path: Path, monkeypatch
) -> None:
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    listed = ["x$(touch MARKER)", quoted_name, "literal[1]"]
    all_files = [*listed, "literal1", "unrelated"]
    for argv in (["git", "init", "-q"], ["git", "config", "commit.gpgsign", "false"]):
        subprocess.run(argv, cwd=tmp_path, capture_output=True, check=True, timeout=10)
    since = ""
    for contents in (b"before\n", b"after\xff\n"):
        for name in all_files:
            (tmp_path / name).write_bytes(contents)
        for argv in (["git", "add", "--all"], ["git", "commit", "-qm", "change"]):
            subprocess.run(
                argv, cwd=tmp_path, capture_output=True, check=True, timeout=10
            )
        if not since:
            since = (
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=tmp_path,
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                .stdout.decode()
                .strip()
            )
    skill = skill_text(REPO / "skills/code-gauntlet/references/phase2-preparation.md")
    command = next(line for line in skill.splitlines() if "incremental_diff.py" in line)
    output_dir = tmp_path / "out [1]"
    output_dir.mkdir()
    (output_dir / f"code-gauntlet-files-{SHA}.json").write_text(
        json.dumps(listed), encoding="utf-8"
    )
    for placeholder, value in {
        "{plugin_root}": REPO.as_posix(),
        "{output_dir}": output_dir.as_posix(),
        "{head_sha_short}": SHA,
        "{last_reviewed_sha}": since,
    }.items():
        command = command.replace(placeholder, value)
    parts = shlex.split(command)
    assert parts[0] == "python3"
    parts[0] = sys.executable
    assert parts[-2] == ">"
    patch = Path(parts[-1])
    with patch.open("wb") as destination:
        result = subprocess.run(
            parts[:-2],
            cwd=tmp_path,
            stdout=destination,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert not (tmp_path / "MARKER").exists()
    assert patch.read_bytes().count(b"+after\xff\n") == len(listed)
    numstat = subprocess.run(
        ["git", "apply", "--numstat", "-z", str(patch)],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    assert sorted(
        row.split(b"\t", 2)[2].decode() for row in numstat.split(b"\0") if row
    ) == sorted(listed)


def test_skills_do_not_interpolate_changed_paths_in_shell_commands() -> None:
    offenders = []
    for path in (REPO / "skills").rglob("*.md"):
        for number, line in enumerate(
            skill_text(path).splitlines(),
            1,
        ):
            if re.search(r"git (?:diff|log)\b[^`\n]*-- <", line):
                offenders.append(f"{path.relative_to(REPO)}:{number}")
    assert offenders == []


def test_skills_do_not_interpolate_branch_names_in_git_commands() -> None:
    offenders = []
    for path in (REPO / "skills").rglob("*.md"):
        for number, line in enumerate(
            skill_text(path).splitlines(),
            1,
        ):
            if re.search(
                r"git\s+[^`\n]*(?:<(?:branch|base)>|\{(?:branch|base_branch|baseBranch)\})",
                line,
            ):
                offenders.append(f"{path.relative_to(REPO)}:{number}")
    assert offenders == []


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_shipped_branch_resolver_keeps_names_on_stdin(
    ending: bytes, tmp_path: Path, monkeypatch
) -> None:
    for key, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)
    branch = "topic/$(id)`id`'caf\u00e9)("
    for argv in (
        ["git", "init", "-q"],
        ["git", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "tip"],
        ["git", "check-ref-format", "--branch", branch],
        ["git", "branch", branch],
    ):
        subprocess.run(argv, cwd=tmp_path, capture_output=True, check=True, timeout=10)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout
    reference = skill_text(REPO / "skills/code-gauntlet/references/phase2-triage.md")
    block = re.search(
        r"```bash\n(python3 \S+/resolve_branch\.py.*?)\n```", reference, re.S
    )
    assert block is not None
    lines = block[1].splitlines()
    assert lines[0].endswith("<<'CODE_GAUNTLET~BRANCH'")
    assert lines[1:] == [
        "<branch name exactly as the user gave it>",
        "CODE_GAUNTLET~BRANCH",
    ]
    parts = shlex.split(
        lines[0].split("<<", 1)[0].replace("{plugin_root}", REPO.as_posix())
    )
    assert parts == ["python3", (REPO / "scripts/resolve_branch.py").as_posix()]
    parts[0] = sys.executable
    monkeypatch.setenv("PYTHONIOENCODING", "latin-1")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sentinel = fake_bin / "id"
    sentinel.write_text("#!/bin/sh\nprintf ran > MARKER\n", encoding="utf-8")
    sentinel.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(
        parts,
        input=branch.encode("utf-8") + ending,
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == sha
    assert result.stderr == b""
    # macOS ships bash 3.2, which re-parses a heredoc body nested in $( ).
    bash = (
        "/bin/bash"
        if sys.platform != "win32" and os.path.exists("/bin/bash")
        else shutil.which(bash_executable())
    )
    if bash:
        shell_block = (
            block[1]
            .replace("<branch name exactly as the user gave it>", branch)
            .replace("{plugin_root}", REPO.as_posix())
            .replace("python3 ", shlex.quote(Path(sys.executable).as_posix()) + " ", 1)
        )
        shell_result = subprocess.run(
            [bash, "-c", shell_block], cwd=tmp_path, capture_output=True, timeout=10
        )
        assert shell_result.returncode == 0, shell_result.stderr
        assert shell_result.stdout == sha
        assert shell_result.stderr == b""
    assert not (tmp_path / "MARKER").exists()


def test_skill_heredocs_are_never_nested_in_command_substitution() -> None:
    offenders = []
    for path in [*(REPO / "skills").rglob("*.md"), *(REPO / "agents").rglob("*.md")]:
        text = skill_text(path)
        for match in re.finditer(r"[$<]\([^)\n]*<<", text):
            offenders.append(
                f"{path.relative_to(REPO)}:{text.count(chr(10), 0, match.start()) + 1}"
            )
    assert offenders == []


def test_python_programs_in_skills_and_agents_do_not_embed_path_placeholders() -> None:
    offender_paths: list[str] = []
    for path, text in _markdown_sources():
        offender_paths.extend(
            f"{path}:{line_number}" for line_number in _placeholder_program_lines(text)
        )
    assert offender_paths == []


def test_shell_fences_in_skills_and_agents_do_not_echo_variables() -> None:
    offenders: list[str] = []
    for path, text in _markdown_sources():
        for start, body in _shell_fences(text):
            for number, line in enumerate(body.splitlines(), start):
                if re.search(r'\becho\s+(?:-[A-Za-z]+\s+)*"\$(?:\w|\{)', line):
                    offenders.append(f"{path}:{number}")
    assert offenders == []


def test_shell_fences_in_skills_and_agents_double_quote_paths() -> None:
    offenders: list[str] = []
    for path, text in _markdown_sources():
        for start, body in _shell_fences(text):
            offenders.extend(
                f"{path}:{start + number - 1}" for number in _unquoted_path_lines(body)
            )
    assert offenders == []


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
        pytest.param(
            '''python3 -c "findings = [{'body': 'User input in SQL'}]"''',
            [1],
            id="quoted-finding-data",
        ),
        pytest.param(
            "python3 - <<'EOF'\nfindings = [{'body': 'User input in SQL'}]\nEOF\n",
            [2],
            id="heredoc-finding-data",
        ),
    ],
)
def test_placeholder_scan_flags_program_text_and_spares_argv(
    text: str, expected: list[int]
) -> None:
    assert _placeholder_program_lines(text) == expected


POSIX_FAKES = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake python3 and forge CLIs are shebang scripts",
)


@POSIX_FAKES
@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_eligibility_shipped_producer_passes_config_as_stdin(
    platform, exit_code, tmp_path
):
    text = skill_text(REPO / "skills/code-gauntlet/references/phase1-preflight.md")
    pattern = (
        r'if ! ELIGIBILITY_JSON=\$\(printf [^\n]*check_eligibility\.py" --platform '
        + platform
        + r" --pr \{pr_number\}\); then\n.*?\nfi\nprintf [^\n]+"
    )
    matches = re.findall(pattern, text, re.DOTALL)
    assert len(matches) == 1
    plugin = tmp_path / "plugin with spaces"
    plugin.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps([sys.argv[1:], sys.stdin.read()]), encoding='utf-8')\n"
        f'print(\'{{"verdict":"proceed"}}\')\nsys.exit({exit_code})\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    payload = json.dumps(
        {
            "mode": "headless",
            "resolved": {"draft_policy": "skip"},
            "title": "'\n$(touch sentinel) `touch sentinel` {python} ${PYTHON[@]}",
        }
    )
    capture = tmp_path / "capture.json"
    env = dict(
        os.environ,
        CONFIG_JSON=payload,
        CAPTURE=str(capture),
        PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
    )
    command = (
        matches[0].replace("{plugin_root}", str(plugin)).replace("{pr_number}", "7")
    )
    result = subprocess.run(
        [bash_executable(), "-c", command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert json.loads(capture.read_text(encoding="utf-8")) == [
        [
            str(plugin / "scripts/check_eligibility.py"),
            "--platform",
            platform,
            "--pr",
            "7",
        ],
        payload,
    ]
    assert not (tmp_path / "sentinel").exists()
    assert result.returncode == exit_code
    assert result.stdout == (
        '{"verdict":"proceed"}\n' if not exit_code else "eligibility: FAILED\n"
    )


@POSIX_FAKES
@pytest.mark.parametrize(
    "platform,outcome",
    [
        ("github", "complete"),
        ("gitlab", "complete"),
        ("gitlab", "incomplete"),
        ("github", "failure"),
        ("gitlab", "failure"),
    ],
)
def test_shipped_file_producer(platform, outcome, tmp_path):
    from tests.support.pr_files import FIXTURES, gitlab_view

    text = skill_text(REPO / "skills/code-gauntlet/references/phase2-preparation.md")
    commands = re.findall(
        r'if ! python3 "[^\n]+/scripts/list_pr_files\.py" --platform '
        + platform
        + r" --pr [^\n]+; then\n.*?\nfi",
        text,
        re.DOTALL,
    )
    assert len(commands) == 1
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    view = (
        {
            "state": "OPEN",
            "isDraft": False,
            "url": "https://github.com/upstream/project/pull/7",
        }
        if platform == "github"
        else gitlab_view()
    )
    if platform == "gitlab" and outcome == "incomplete":
        view["changes_count"] = "1000+"
    listing = (
        '"render.py"\n'
        if platform == "github"
        else (FIXTURES / "glab_mr_diffs.ndjson").read_text(encoding="utf-8")
    )
    fake = fake_bin / ("gh" if platform == "github" else "glab")
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'a', encoding='utf-8') as capture:\n"
        "    capture.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] in ('pr', 'mr'):\n"
        "    print(os.environ['VIEW'])\n"
        "elif os.environ['OUTCOME'] == 'failure':\n"
        "    print('forbidden', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    sys.stdout.write(os.environ['LISTING'])\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    command = (
        commands[0]
        .replace("{plugin_root}", str(REPO))
        .replace("{pr_number}", "7")
        .replace("{output_dir}", str(tmp_path))
        .replace("{head_sha_short}", "abc12345")
    )
    capture = tmp_path / "capture.jsonl"
    env = dict(
        os.environ,
        PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
        CAPTURE=str(capture),
        VIEW=json.dumps(view),
        LISTING=listing,
        OUTCOME=outcome,
    )
    result = subprocess.run(
        [bash_executable(), "-c", command + "\nprintf 'continued\\n'"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    calls = [
        json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()
    ]
    assert calls == (
        [
            ["pr", "view", "7", "--json", "state,isDraft,url,headRefOid"],
            [
                "api",
                "--hostname",
                "github.com",
                "repos/upstream/project/pulls/7/files?per_page=100",
                "--paginate",
                "--jq",
                ".[].filename | @json",
            ],
        ]
        if platform == "github"
        else [
            ["mr", "view", "7", "--output", "json"],
            [
                "api",
                "projects/cdr-group%2Fprobe/merge_requests/1/diffs?per_page=100",
                "--paginate",
                "--output",
                "ndjson",
            ],
        ]
    )
    if outcome == "failure":
        assert result.returncode == 1
        assert result.stdout == "files: FAILED\n"
        assert "list_pr_files: FAILED:" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "continued\n"
        assert json.loads(
            (tmp_path / "code-gauntlet-files-abc12345.json").read_text(encoding="utf-8")
        ) == ["render.py"]
        assert bool(result.stderr) == (outcome == "incomplete")


@pytest.mark.parametrize(
    "source",
    [
        "skills/code-gauntlet/references/phase2-preparation.md",
        "skills/code-gauntlet/references/phase2-triage.md",
    ],
)
@pytest.mark.parametrize("revision_range", ["HEAD", "{base_sha}...{target_sha}"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_shipped_git_file_producer(source, revision_range, outcome, tmp_path):
    text = skill_text(REPO / source, python=shlex.quote(sys.executable))
    commands = [
        body.strip()
        for _, body in _shell_fences(text)
        if f'--git-range "{revision_range}"' in body and "list_pr_files.py" in body
    ]
    assert len(commands) == 1
    sha = "a" * 40
    if outcome == "success":
        for args in (
            ["init", "-q"],
            [
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "-qm",
                "base",
            ],
        ):
            subprocess.run(
                ["git", *args], cwd=tmp_path, check=True, capture_output=True
            )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    output_dir = tmp_path / "output space"
    output_dir.mkdir()
    command = (
        commands[0]
        .replace("{plugin_root}", REPO.as_posix())
        .replace("{base_sha}", sha)
        .replace("{target_sha}", sha)
        .replace("{output_dir}", output_dir.as_posix())
        .replace("{head_sha_short}", "abc12345")
    )
    result = subprocess.run(
        [bash_executable(), "-c", command + "\nprintf 'continued\\n'"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if outcome == "failure":
        assert result.returncode == 1
        assert result.stdout == "files: FAILED\n"
        assert "list_pr_files: FAILED:" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "[]\ncontinued\n"
        assert result.stderr == ""
        assert (
            json.loads(
                (output_dir / "code-gauntlet-files-abc12345.json").read_text(
                    encoding="utf-8"
                )
            )
            == []
        )


def test_skill_and_reference_shell_blocks_have_no_jq():
    offenders = []
    root = REPO / "skills/code-gauntlet"
    for path in [root / "SKILL.md", *(root / "references").glob("*.md")]:
        for line, body in enumerate(skill_text(path).splitlines(), 1):
            if re.search(r"\bjq\b", body):
                offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert offenders == []


@pytest.mark.parametrize("platform", ["github", "gitlab"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
@POSIX_FAKES
def test_shipped_forge_status_and_url(platform, outcome, tmp_path, monkeypatch):
    source = "references/phase2-preparation.md"
    for rel in ("phase1-preflight.md", "phase2-triage.md"):
        assert "single capture/status recipe" in skill_text(
            REPO / "skills/code-gauntlet/references" / rel
        )
    from tests.support.git_diff import patch_repo
    from tests.support.pr_files import gitlab_view

    _, sha = patch_repo(tmp_path, monkeypatch)
    text = skill_text(
        REPO / "skills/code-gauntlet" / source, python=shlex.quote(sys.executable)
    )
    bodies = [body for _, body in _shell_fences(text)]
    status_index = next(
        index
        for index, body in enumerate(bodies)
        if 'echo "=== status ==="' in body
        and f'pr_status.py" --platform {platform}' in body
    )
    status = bodies[status_index]
    if 'echo "=== pr_url ==="' in status:
        url = status
    elif 'echo "=== pr_url ==="' in bodies[status_index + 1]:
        url = bodies[status_index + 1]
    else:
        url = next(body for body in bodies if 'echo "=== pr_url ==="' in body)
    status = status.split('echo "=== checkout ==="')[0]
    if 'echo "=== pr_url ==="' in status:
        status = status.split('echo "=== pr_url ==="')[0]
    url = url[url.index('echo "=== pr_url ==="') :].split(
        'echo "=== prior_review ==="'
    )[0]
    command = status + "\n" + url + '\nprintf "fields=%s|%s\\n" "$TARGET_SHA" "$PR_URL"'
    view = (
        dict(
            gitlab_view(),
            sha=sha,
            web_url="https://gitlab.com/group/sub/r/-/merge_requests/7",
            description="synthetic",
        )
        if platform == "gitlab"
        else {
            "headRefOid": sha,
            "url": "https://github.com/upstream/project/pull/7",
            "body": "synthetic",
        }
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / ("gh" if platform == "github" else "glab")
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'a', encoding='utf-8') as capture:\n"
        "    capture.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(os.environ['VIEW'])\n"
        "sys.exit(int(os.environ['FAIL']))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    command = command.replace("{plugin_root}", REPO.as_posix()).replace(
        "{pr_number}", "7"
    )
    command = command.replace("{output_dir}", tmp_path.as_posix()).replace(
        "{nonce}", NONCE
    )
    capture = tmp_path / "capture"
    result = subprocess.run(
        [bash_executable(), "-c", command],
        cwd=tmp_path,
        env=dict(
            os.environ,
            PATH=str(fake_bin) + os.pathsep + os.environ["PATH"],
            VIEW=json.dumps(view),
            FAIL=str(int(outcome == "failure")),
            CAPTURE=str(capture),
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert json.loads(capture.read_text(encoding="utf-8")) == (
        ["pr", "view", "7", "--json", "state,isDraft,url,headRefOid,body"]
        if platform == "github"
        else ["mr", "view", "7", "--output", "json"]
    )
    if outcome == "failure":
        assert result.returncode == 1
        assert result.stdout == "=== status ===\n"
        assert "pr_status: cannot capture validated PR/MR description" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        expected_url = view["url" if platform == "github" else "web_url"]
        assert result.stdout.endswith(f"fields={sha}|{expected_url}\n")
        assert result.stderr == ""


@pytest.mark.parametrize(
    "source", ["references/phase2-preparation.md", "references/phase2-triage.md"]
)
@pytest.mark.parametrize("mode", ["branch", "local"])
@pytest.mark.parametrize("outcome", ["success", "empty", "failure"])
def test_shipped_patch_producer(source, mode, outcome, tmp_path, monkeypatch):
    from tests.support.git_diff import assert_standard_patch, patch_repo

    base, target = patch_repo(tmp_path, monkeypatch)
    if outcome == "empty":
        base = target
        (tmp_path / "file").write_text("new\n", encoding="utf-8", newline="\n")
    elif outcome == "failure":
        base = target = "a" * 40
        (tmp_path / ".git").rename(tmp_path / "not-git")
    revision_range = "HEAD" if mode == "local" else "{base_sha}...{target_sha}"
    bodies = _shell_fences(
        skill_text(
            REPO / "skills/code-gauntlet" / source, python=shlex.quote(sys.executable)
        )
    )
    commands = [
        body.strip()
        for _, body in bodies
        if "save_git_diff.py" in body and f'--git-range "{revision_range}"' in body
    ]
    assert len(commands) == 1
    output = tmp_path / "o'brien out"
    output.mkdir()
    command = (
        commands[0]
        .replace("{plugin_root}", REPO.as_posix())
        .replace("{base_sha}", base)
        .replace("{target_sha}", target)
        .replace("{output_dir}", output.as_posix())
        .replace("{head_sha_short}", target[:8])
    )
    result = subprocess.run(
        [bash_executable(), "-c", command + "\nprintf 'continued\\n'"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    patch = output / f"code-gauntlet-diff-{target[:8]}.patch"
    if outcome == "success":
        assert result.returncode == 0, result.stderr
        assert result.stdout == "continued\n"
        assert_standard_patch(patch.read_bytes())
    else:
        assert result.returncode == 1
        assert result.stdout == "diff: FAILED\n"
        assert "save_git_diff: " in result.stderr
        assert not patch.exists()


@pytest.mark.parametrize(
    "outcome", ["checkout", "failure", "headless-match", "headless-mismatch"]
)
def test_shipped_branch_composite(outcome, tmp_path, monkeypatch):
    from tests.support.git_diff import git, patch_repo

    base, target = patch_repo(tmp_path, monkeypatch)
    (tmp_path / "file").write_text("new\n", encoding="utf-8", newline="\n")
    branch = "topic/$(id)`id`'end)("
    git(tmp_path, "branch", branch, target)
    git(
        tmp_path,
        "checkout",
        "-q",
        "--detach",
        target if outcome == "headless-match" else base,
    )
    monkeypatch.setenv(
        "CODE_GAUNTLET_HEADLESS", "1" if outcome.startswith("headless") else ""
    )
    bodies = [
        body
        for _, body in _shell_fences(
            skill_text(
                REPO / "skills/code-gauntlet/references/phase2-preparation.md",
                python=shlex.quote(sys.executable),
            )
        )
    ]
    output = tmp_path / "o'brien out"
    output.mkdir()
    stale = output / f"code-gauntlet-report-{target[:8]}.md"
    stale.write_text("stale", encoding="utf-8")

    def run(body):
        expanded = (
            body.replace("{plugin_root}", REPO.as_posix())
            .replace("<base name exactly as chosen>", base)
            .replace("<branch name exactly as the user gave it>", branch)
            .replace("{target_sha}", target)
            .replace("{output_dir}", output.as_posix())
        )
        return subprocess.run(
            [bash_executable(), "-c", expanded],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    for marker, sha in [("CODE_GAUNTLET~BASE", base), ("CODE_GAUNTLET~BRANCH", target)]:
        block = next(
            body for body in bodies if marker in body and "--checkout" not in body
        )
        result = run(block)
        assert result.returncode == 0, result.stderr
        assert result.stdout == sha + "\n"
    if outcome == "failure":
        git(tmp_path, "branch", "-D", branch)
    composite = next(body for body in bodies if 'resolve_branch.py" --checkout' in body)
    assert re.search(r"\$\([^\n]*<<", composite) is None
    result = run(composite)
    if outcome in {"checkout", "headless-match"}:
        assert result.returncode == 0, result.stderr
        assert git(tmp_path, "rev-parse", "HEAD") == target
        assert f"head_sha_short={target[:8]}\nhead_sha_full={target}\n" in result.stdout
        assert stale.read_bytes() == b""
        if outcome == "checkout":
            assert git(tmp_path, "symbolic-ref", "HEAD") == f"refs/heads/{branch}"
    else:
        assert result.returncode == 1
        assert "=== sha ===" not in result.stdout
        assert "=== stale_truncate ===" not in result.stdout
        assert git(tmp_path, "rev-parse", "HEAD") == base
        assert stale.read_text(encoding="utf-8") == "stale"


@pytest.mark.parametrize(
    "statuses,selected,visited",
    [
        ((0, 0, 0), "python3", ["python3"]),
        ((49, 0, 0), "python", ["python3", "python"]),
        ((49, 49, 0), "py -3", ["python3", "python", "py"]),
        ((39, 0, 0), "python", ["python3", "python"]),
        ((49, 49, 49), None, ["python3", "python", "py"]),
    ],
)
def test_shipped_interpreter_selection(statuses, selected, visited, tmp_path):
    if shutil.which(bash_executable()) is None:
        pytest.skip("Bash is required to execute the shipped preflight")
    skill = (REPO / "skills/code-gauntlet/references/phase1-preflight.md").read_text(
        encoding="utf-8"
    )
    selector = skill[
        skill.index("PYTHON=()") : skill.index('echo "=== output_dir ==="')
    ]
    fake_bin = tmp_path / "stub bin"
    fake_bin.mkdir()
    trace = tmp_path / "visited"
    for command, status in zip(("python3", "python", "py"), statuses, strict=True):
        body = f"printf '%s\\n' {command} >> {shlex.quote(trace.as_posix())}\n"
        if command == "py":
            body += '[ "$1" = -3 ] || exit 88\nshift\n'
        if status == 0:
            body += f'exec {shlex.quote(Path(sys.executable).as_posix())} "$@"\n'
        elif status == 39:
            body += (
                f"exec {shlex.quote(Path(sys.executable).as_posix())} -c "
                + shlex.quote(
                    "import sys; sys.version_info = (3, 9); exec(sys.argv[1])"
                )
                + ' "$2"\n'
            )
        else:
            body += f"exit {status}\n"
        stub = fake_bin / command
        stub.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    result = subprocess.run(
        [bash_executable(), "-c", selector],
        env={
            **os.environ,
            "PATH": fake_bin.as_posix() + os.pathsep + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == (0 if selected else 1), (result.stdout, result.stderr)
    assert trace.read_text(encoding="utf-8").splitlines() == visited
    assert result.stdout == (
        f"python={selected}\n"
        if selected
        else "Install Python 3.10+ reachable as python3, python, or py -3.\n"
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="native Windows argv and py launcher require Windows",
)
@pytest.mark.parametrize("python", ["python", "py -3"])
def test_windows_emitted_commands_through_git_bash(python, tmp_path):
    from gauntlet import awaiting

    # Paths with spaces require native argv quoting as well as Bash word quoting.
    output = tmp_path / "native output"
    output.mkdir()
    node_program = r"""
import { writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [root, outputDir, python] = process.argv.slice(1);
const load = (path) => import(pathToFileURL(`${root}/${path}`).href);
const { verifyStage } = await load('workflows/src/verifyStage.js');
const { verifyInlineBudget } = await load('workflows/src/capacity.js');
const { verifyCtx, verifyInput, deltaEnvelope } = await load('workflows/test/helpers/verifyDelta.js');
const { writeArtifacts } = await load('workflows/src/persist.js');
const { persistCtx, persistInput } = await load('workflows/test/helpers/persistFixtures.js');
const { parseWriterPayload } = await load('workflows/src/persist.js');
const input = { python, pluginRoot: root, nonce: 'n-1', headShaShort: 'abc123',
  verify: { scriptPath: '/untrusted.py', inputPathBase: `${outputDir}/input`, outputPathBase: `${outputDir}/verified` } };
const finding = { id: 'F1', file: 'source.py', line_start: 1, origin: 'new', description: '' };
const budget = verifyInlineBudget(input);
// A long path survives projection and fills the Windows argv budget.
finding.file = 'x'.repeat(budget - 200);
const ctx = verifyCtx(() => deltaEnvelope([finding], { nonce: 'n-1.0' }));
await verifyStage(ctx, verifyInput([finding], input));
const verify = ctx.calls[0].prompt.split('\n').at(-1);
const persist = persistCtx();
await writeArtifacts(persist, persistInput({ python, pluginRoot: root, outputDir, persist: {} }));
for (const entry of parseWriterPayload(persist.calls[0].prompt)) writeFileSync(entry.path, entry.text, 'utf8');
console.log(JSON.stringify({verify, assemble: persist.calls[1].prompt.split('\n').at(-1), budget}));
"""
    generated = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            node_program,
            REPO.as_posix(),
            output.as_posix(),
            python,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    commands = json.loads(generated.stdout)
    assert 28000 < len(commands["verify"]) < 32767 - 1800
    (tmp_path / "source.py").write_text("answer = 42\n", encoding="utf-8")
    # A script file keeps Windows command-line quoting out of what Bash parses.
    for name in ("verify", "assemble"):
        script = tmp_path / f"{name}.sh"
        script.write_text(commands[name] + "\n", encoding="utf-8", newline="\n")
        result = subprocess.run(
            [bash_executable(), script.name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, (name, result.stdout, result.stderr)
    envelope = json.loads((output / "verified.slice0.json").read_text(encoding="utf-8"))
    assert envelope["status"] == "ok"
    assert envelope["receipt"]["inline_checksum"].startswith("fnv1a32:")
    target = output / "retry.output"
    args = awaiting.CLI.parser.parse_args(
        [
            target.as_posix(),
            "--python",
            python,
            "--timeout-seconds",
            "0",
            "--max-attempts",
            "2",
        ]
    )
    retry = awaiting.build_next_command(args, None, 0)
    target.write_text('{"ok":true,"phaseReached":"report"}', encoding="utf-8")
    (tmp_path / "retry.sh").write_text(retry + "\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [bash_executable(), "retry.sh"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True, "phaseReached": "report"}
