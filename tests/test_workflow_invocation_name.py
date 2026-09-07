"""Pin the registered workflow name across the skill's dispatch surfaces."""

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
WORKFLOW_FILES = (
    SKILLS_ROOT / "code-gauntlet" / "SKILL.md",
    SKILLS_ROOT / "code-gauntlet" / "references" / "phase3-dispatch.md",
    SKILLS_ROOT / "code-gauntlet" / "references" / "crash-recovery.md",
)
FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
WORKFLOW_CALL_RE = re.compile(r"\bWorkflow\s*\(")


def _workflow_blocks(text):
    return [
        block
        for block in FENCED_BLOCK_RE.findall(text)
        if WORKFLOW_CALL_RE.search(block)
    ]


def _registered_workflow_name():
    plugin = json.loads(
        (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    source = (REPO_ROOT / "workflows" / "src" / "pipeline_entry.js").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"export\s+const\s+meta\s*=\s*\{\s*name:\s*['\"]([^'\"]+)['\"]",
        source,
    )
    if match is None:
        raise AssertionError("pipeline entry has no readable meta.name literal")
    return f"{plugin['name']}:{match.group(1)}"


class WorkflowInvocationNameTest(unittest.TestCase):
    def test_dispatch_blocks_use_derived_registered_name(self):
        registered_name = _registered_workflow_name()
        expected = f'name: "{registered_name}"'
        for path in WORKFLOW_FILES:
            blocks = _workflow_blocks(path.read_text(encoding="utf-8"))
            self.assertTrue(blocks, f"no Workflow( fenced block in {path}")
            for block in blocks:
                self.assertIn(expected, block, path)

    def test_skill_workflow_blocks_never_pass_pipeline_script_path(self):
        offenders = []
        for path in SKILLS_ROOT.rglob("*.md"):
            for block in _workflow_blocks(path.read_text(encoding="utf-8")):
                if "scriptPath" in block and "pipeline.js" in block:
                    offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
