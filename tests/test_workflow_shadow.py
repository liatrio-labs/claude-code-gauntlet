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


def run_capture(cwd, config, *, headless="0", pwd=None):
    env = dict(
        os.environ, CLAUDE_CONFIG_DIR=str(config), CODE_GAUNTLET_HEADLESS=headless
    )
    if pwd is not None:
        env["PWD"] = str(pwd)
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


@pytest.mark.parametrize("character", ["é", "😀"])
@pytest.mark.parametrize("over_limit", [False, True])
def test_local_file_limit_counts_bytes_before_decoding(
    workspace, character, over_limit
):
    from gauntlet.workflow_shadow import WORKFLOW_SCRIPT_CAP

    repo, cwd, config = workspace
    glyph = character.encode("utf-8")
    payload = (workflow() + "//").encode("utf-8")
    payload += glyph * ((WORKFLOW_SCRIPT_CAP - len(payload)) // len(glyph))
    payload += b" " * (WORKFLOW_SCRIPT_CAP - len(payload))
    if over_limit:
        payload += glyph
    assert len(payload.decode("utf-8").encode("utf-16-le")) // 2 < WORKFLOW_SCRIPT_CAP
    path = put(config / "workflows", "")
    path.write_bytes(payload)
    result = run_capture(cwd, config)
    if over_limit:
        assert_policy(result)
    else:
        assert result.returncode == 1
        assert result.stdout == ""
        assert str(path) in result.stderr


@pytest.mark.parametrize("escape", ["\\٣", "\\²", "\\0٣", "\\0²"])
def test_non_ascii_digit_escapes_cannot_skip_a_shadow(workspace, escape):
    repo, cwd, config = workspace
    source = workflow().replace("description: 'Probe'", f"description: '{escape}'")
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr
    assert "shadows the plugin pipeline" in result.stderr


@pytest.mark.parametrize(
    "number",
    [
        "0xff",
        "0XFF",
        "0o77",
        "0O77",
        "0b10",
        "0B10",
        "1_000",
        "1_000.5",
        "1e1_0",
        "123n",
        "0xfn",
        "0o7n",
        "0b1n",
        ".5",
        "-.5",
        "- 1",
    ],
)
def test_unknown_numeric_literals_refuse_instead_of_skipping(workspace, number):
    repo, cwd, config = workspace
    source = workflow().replace("name:", f"extra: {number}, name:")
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr
    assert "possible pipeline shadow" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        workflow().replace("name:", "extra: [0xff], name:"),
        workflow().replace("name:", "extra: {nested: 1_000}, name:"),
        workflow().replace("name:", "extra: 1_000n, name:"),
        workflow().replace("name:", "extra: 0xF_F, name:"),
    ],
)
def test_partial_literals_in_nested_metadata_refuse(workspace, source):
    repo, cwd, config = workspace
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr


@pytest.mark.parametrize("terminator", ["\n", "\r", "\u2028", "\u2029", "\r\n"])
def test_all_hashbang_line_terminators_preserve_shadow_detection(workspace, terminator):
    repo, cwd, config = workspace
    path = put(config / "workflows", "#!workflow" + terminator + workflow())
    result = run_capture(cwd, config)
    assert result.returncode == 1
    assert result.stdout == ""
    assert str(path) in result.stderr


@pytest.mark.parametrize("shadow", [False, True])
def test_removed_pwd_is_ignored_without_skipping_real_cwd(workspace, shadow):
    repo, cwd, config = workspace
    stale = repo / "removed"
    stale.mkdir()
    stale.rmdir()
    if shadow:
        path = put(cwd / ".claude/workflows", workflow())
    result = run_capture(cwd, config, pwd=stale)
    if shadow:
        assert result.returncode == 1
        assert result.stdout == ""
        assert str(path) in result.stderr
    else:
        assert_policy(result)


@pytest.mark.parametrize(
    "name,body,refused",
    [
        ("other", "return 1;", False),
        ("other", r"return 'a\nb';", True),
    ],
)
def test_unknown_meta_can_refuse_only_after_the_name_prefilter(
    workspace, name, body, refused
):
    repo, cwd, config = workspace
    source = metadata(name).replace("name:", "extra: 0xff, name:") + ";\n" + body
    path = put(config / "workflows", source)
    result = run_capture(cwd, config)
    assert result.returncode == int(refused)
    if refused:
        assert result.stdout == ""
        assert str(path) in result.stderr
    else:
        assert_policy(result)


@pytest.mark.parametrize(
    "extra",
    [
        "value: +1",
        "value: +.5",
        "value: undefined",
        "value: NaN",
        "value: Infinity",
        "value: getValue()",
        "__proto__: {}",
        "constructor: {}",
        "prototype: {}",
        "['value']: 1",
        "...extra",
        r"value: '\08'",
        r"value: '\8'",
        r"value: '\07'",
        r"value: '\xgg'",
        r"value: '\u00'",
        r"value: '\u{110000}'",
        "value: `a${1}`",
        "value: 'line\nbreak'",
    ],
)
def test_confirmed_host_rejections_are_ignored(workspace, extra):
    repo, cwd, config = workspace
    source = workflow().replace("name:", extra + ", name:")
    put(config / "workflows", source)
    assert_policy(run_capture(cwd, config))


@pytest.mark.parametrize(
    "source",
    [
        "#!workflow " + NAME,
        workflow().replace("export const", "export let"),
        workflow().replace("const meta", "const other"),
        "export const meta; // " + NAME,
        "export const meta = []; // " + NAME,
        "export const meta = {name: '', description: 'Probe', extra: '" + NAME + "'};",
        workflow().replace("description: 'Probe'", "description: null"),
        workflow().split("description:")[0] + "description: 'unterminated",
        workflow().split("description:")[0] + "description: '\\u{",
    ],
)
def test_nonregistration_return_paths_are_safe(workspace, source):
    repo, cwd, config = workspace
    put(config / "workflows", source)
    assert_policy(run_capture(cwd, config))


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
        (" != other;", False),
        (" !== other;", False),
        (" = other;", False),
        (" ? true : false;", False),
        (" % 2;", False),
        (" ** 2;", False),
        (" & other;", False),
        (" | other;", False),
        (" ^ other;", False),
        (" < other;", False),
        (" > other;", False),
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
