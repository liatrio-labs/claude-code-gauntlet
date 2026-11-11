import json
import re
from pathlib import Path

import pytest

from tests.support.repository import workflow_header

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
WORKFLOW_FILES = (
    SKILLS_ROOT / "code-gauntlet" / "SKILL.md",
    SKILLS_ROOT / "code-gauntlet" / "references" / "phase3-dispatch.md",
    SKILLS_ROOT / "code-gauntlet" / "references" / "crash-recovery.md",
)
SKILL_FILES = tuple(sorted(SKILLS_ROOT.rglob("*.md")))
FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
WORKFLOW_CALL_RE = re.compile(r"\bWorkflow\s*\(")
WORKFLOW_ALLOW_RULE_RE = re.compile(r"\bWorkflow\s*\(\s*([^()\s]+)\s*\)")


def _workflow_blocks(text):
    return [
        block
        for block in FENCED_BLOCK_RE.findall(text)
        if WORKFLOW_CALL_RE.search(WORKFLOW_ALLOW_RULE_RE.sub("", block))
    ]


def _registered_workflow_name():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    return f"{plugin['name']}:{workflow_header(REPO_ROOT)['name']}"


def test_shadow_guard_checks_the_registered_pipeline_name():
    from gauntlet.workflow_shadow import PIPELINE_NAME

    assert _registered_workflow_name() == PIPELINE_NAME


@pytest.mark.parametrize(
    "path",
    SKILL_FILES,
    ids=[str(path.relative_to(SKILLS_ROOT)) for path in SKILL_FILES],
)
def test_workflow_names_match_registration(path):
    registered_name = _registered_workflow_name()
    text = path.read_text(encoding="utf-8")
    for match in WORKFLOW_ALLOW_RULE_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        assert match[1] == registered_name, f"{path}:{line}: {match[0]}"
    blocks = _workflow_blocks(text)
    if path in WORKFLOW_FILES:
        assert blocks, f"no Workflow( fenced block in {path}"
    for block in blocks:
        assert f'name: "{registered_name}"' in block, path


def test_skill_workflow_blocks_never_pass_pipeline_script_path():
    offenders = [
        str(path)
        for path in SKILLS_ROOT.rglob("*.md")
        for block in _workflow_blocks(path.read_text(encoding="utf-8"))
        if "scriptPath" in block and "pipeline.js" in block
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "header, expected",
    [
        pytest.param(
            "export const meta = { name: 'first-workflow', description: 'First' };",
            {"name": "first-workflow", "description": "First"},
            id="first-metadata-name",
        ),
        pytest.param(
            "export const meta = { name: 'second-workflow', description: 'Second' };",
            {"name": "second-workflow", "description": "Second"},
            id="distinct-metadata-name",
        ),
    ],
)
def test_workflow_header_reads_runtime_metadata(tmp_path, header, expected):
    (tmp_path / "workflows").mkdir()
    # A body with top-level return must never be imported as module metadata.
    bundle = header + "\nreturn await run();\n"
    (tmp_path / "workflows/build.js").write_text(
        "export function build() { return " + json.dumps(bundle) + "; }\n",
        encoding="utf-8",
    )
    assert workflow_header(tmp_path) == expected
