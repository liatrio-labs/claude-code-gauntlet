"""Policy capture refuses pipeline substitution without blocking unrelated workflows."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/capture_policy_env.py"
NAME = "code-gauntlet:code-gauntlet-pipeline"


def metadata(name=NAME):
    return f"export const meta = {{name: '{name}', description: 'Probe'}}"


def workflow(name=NAME):
    return metadata(name) + ";\nreturn {marker: 'probe'};\n"


@pytest.fixture
def workspace(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "package" / "src"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    config = tmp_path / "personal config"
    config.mkdir()
    return repo, nested, config


def run_capture(cwd, config, *, headless="0"):
    env = dict(
        os.environ, CLAUDE_CONFIG_DIR=str(config), CODE_GAUNTLET_HEADLESS=headless
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


def put(directory, source):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "unrelated-filename.js"
    path.write_text(source, encoding="utf-8")
    return path


def assert_policy(result):
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert set(json.loads(result.stdout)) == {
        "subagentModel",
        "provider",
        "gateway",
        "mappedAliases",
    }


@pytest.mark.parametrize("location", ["cwd", "parent", "repo", "above-repo", "user"])
def test_policy_capture_refuses_each_resolver_location(workspace, location):
    repo, cwd, config = workspace
    directories = {
        "cwd": cwd / ".claude/workflows",
        "parent": cwd.parent / ".claude/workflows",
        "repo": repo / ".claude/workflows",
        "above-repo": repo.parent / ".claude/workflows",
        "user": config / "workflows",
    }
    path = put(directories[location], workflow())
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr
    assert "shadows the plugin pipeline" in result.stderr
    assert len(result.stderr.splitlines()) == 1


def test_default_user_directory_is_checked(workspace, monkeypatch, invoke):
    repo, cwd, config = workspace
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: config)
    path = put(config / ".claude/workflows", workflow())
    result = invoke("capture_policy_env", [], cwd)
    assert result.returncode == 1
    assert result.stdout == b""
    assert str(path).encode() in result.stderr


@pytest.mark.parametrize("headless", ["0", "1"])
def test_policy_capture_checks_again_after_preparation(workspace, headless):
    repo, cwd, config = workspace
    assert_policy(run_capture(cwd, config, headless=headless))
    path = put(repo / ".claude/workflows", workflow())
    result = run_capture(cwd, config, headless=headless)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        None,
        workflow("other"),
        metadata("other") + "\nreturn 1;",
        "const helper = " + json.dumps(NAME) + ";\n",
        "'use strict';\n" + workflow(),
        "import './helper.js';\n" + workflow(),
        "// " + NAME + "\nconst helper = 'a\\nb';\n",
        metadata("other") + ";\n" + ("// unrelated \\\n" * 30_000),
        metadata("other") + ";\n" + ("// unrelated \\\n" * 90_000),
        metadata("other") + "\nreturn 'a\\nb';\n",
        workflow(NAME.upper()),
        workflow(" " + NAME),
        workflow(NAME + "-extra"),
    ],
    ids=[
        "empty",
        "other",
        "documented-style",
        "helper",
        "directive-first",
        "import-first",
        "helper-with-escapes",
        "large-body-with-escapes",
        "oversized-body-with-escapes",
        "body-escapes",
        "case-sensitive",
        "no-trimming",
        "exact-name",
    ],
)
def test_unrelated_and_unregistered_files_pass(workspace, source):
    repo, cwd, config = workspace
    if source is not None:
        put(repo / ".claude/workflows", source)
    assert_policy(run_capture(cwd, config))


def test_non_utf8_file_that_cannot_spell_name_passes(workspace):
    repo, cwd, config = workspace
    path = put(config / "workflows", "")
    path.write_bytes(b"\xff\xfe not a workflow")
    assert_policy(run_capture(cwd, config))


def test_oversized_pipeline_definition_is_not_registered(workspace):
    from gauntlet.workflow_shadow import WORKFLOW_SCRIPT_CAP

    repo, cwd, config = workspace
    source = workflow() + " " * WORKFLOW_SCRIPT_CAP
    put(config / "workflows", source)
    assert_policy(run_capture(cwd, config))
    build = (ROOT / "workflows/build.js").read_text(encoding="utf-8")
    match = re.search(r"export const WORKFLOW_SCRIPT_CAP = ([\d_]+);", build)
    assert match is not None
    assert int(match[1].replace("_", "")) == WORKFLOW_SCRIPT_CAP


@pytest.mark.parametrize(
    "suffix,registered",
    [
        (";\nreturn 1;", True),
        ("\nreturn 1;", True),
        ("\n/* comment */ return 1;", True),
        (" /* comment\n */ return 1;", True),
        ("\n!true;", True),
        ("\n++counter;", True),
        ("\n--counter;", True),
        ("\n~value;", True),
        ("\n{ log(); }", True),
        ("\n inSomething();", True),
        ("\n inΩ();", True),
        (" || {};", False),
        (" && {};", False),
        ("\n+value;", False),
        ("\n-value;", False),
        ("\n/regex/.test('x');", False),
        ("\n({});", False),
        ("\n[0];", False),
        ("\n`tag`;", False),
        (".name;", False),
        (", second = {};", False),
        ("\n in {};", False),
        ("\n instanceof Object;", False),
    ],
)
def test_declaration_boundaries_match_host_parser(workspace, suffix, registered):
    repo, cwd, config = workspace
    path = put(config / "workflows", metadata() + suffix)
    result = run_capture(cwd, config)
    if registered:
        assert result.returncode == 1
        assert result.stdout == ""
        assert str(path) in result.stderr
    else:
        assert_policy(result)


@pytest.mark.parametrize(
    "name",
    [
        NAME,
        r"\u0063ode-gauntlet:code-gauntlet-pipeline",
        r"\x63ode-gauntlet:code-gauntlet-pipeline",
        r"\u{63}ode-gauntlet:code-gauntlet-pipeline",
        "`" + NAME + "`",
    ],
)
def test_literal_spellings_cannot_hide_pipeline_name(workspace, name):
    repo, cwd, config = workspace
    source = workflow(name)
    if name.startswith("`"):
        source = source.replace("'" + name + "'", name)
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        workflow().replace("meta =", r"m\u0065ta ="),
        workflow().replace("name:", r"na\u006de:"),
        workflow().replace("description:", "extra: /probe/, description:"),
    ],
)
def test_ambiguous_candidate_metadata_refuses_with_file(workspace, source):
    repo, cwd, config = workspace
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr
    assert "possible pipeline shadow" in result.stderr


@pytest.mark.parametrize(
    "source,refused",
    [
        (workflow().replace("name:", r"'na\u006de':"), True),
        (workflow().replace("name:", "/* name: 'other' */ name:"), True),
        (workflow().replace("description:", "name: 'other', description:"), False),
        (
            workflow().replace(
                "description:", "extra: [null, true, false, -1.5e2], description:"
            ),
            True,
        ),
        (workflow().replace("description:", "__proto__: {}, description:"), False),
        (workflow().replace(NAME, r"other\07"), False),
        (workflow().replace(NAME, r"other\u{110000}"), False),
        (workflow().replace(NAME, r"other\u00"), False),
        (workflow().replace(NAME, "other\nname"), False),
        (workflow().replace("name:", "['name']:"), False),
        (workflow().replace("description:", "...extra, description:"), False),
        (workflow().replace("'" + NAME + "'", "getName()"), False),
        (workflow().replace("'" + NAME + "'", "`other${suffix}`"), False),
    ],
)
def test_literal_and_nonliteral_metadata(workspace, source, refused):
    repo, cwd, config = workspace
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == int(refused), result.stderr
    if refused:
        assert result.stdout == ""
        assert str(path) in result.stderr
    else:
        assert_policy(result)


def test_only_direct_js_files_are_workflow_sources(workspace):
    repo, cwd, config = workspace
    put(config / "workflows/nested", workflow())
    (config / "workflows/ignored.ts").write_text(workflow(), encoding="utf-8")
    put(repo.parent / "additional" / ".claude/workflows", workflow())
    assert_policy(run_capture(cwd, config))


@pytest.mark.parametrize("name", ["other", NAME])
def test_linked_workflow_is_inspected_like_host(workspace, symlink_or_skip, name):
    repo, cwd, config = workspace
    target = put(repo / "elsewhere", workflow(name))
    directory = config / "workflows"
    directory.mkdir()
    path = directory / "linked.js"
    path.symlink_to(target)
    result = run_capture(cwd, config)
    assert result.returncode == int(name == NAME), result.stderr
    if name == NAME:
        assert str(path) in result.stderr


def test_capture_is_the_existing_dispatch_and_resume_dependency():
    references = ROOT / "skills/code-gauntlet/references"
    triage = (references / "phase2-triage.md").read_text(encoding="utf-8")
    assert "capture_policy_env.py` and stamp its four keys verbatim" in triage
    recovery = (references / "crash-recovery.md").read_text(encoding="utf-8")
    assert "re-run `scripts/capture_policy_env.py` and stop if it refuses" in recovery
    assert not (ROOT / "scripts/check_workflow_shadow.py").exists()
