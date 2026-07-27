"""Contract: headless-mode.md Headless config echo includes identity receipts."""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEADLESS = (
    REPO_ROOT
    / "skills"
    / "code-gauntlet"
    / "references"
    / "headless-mode.md"
)


class HeadlessEchoIdentityContractTest(unittest.TestCase):
    def test_echo_example_includes_pipeline_version_and_plugin_root(self):
        text = HEADLESS.read_text(encoding="utf-8")
        # The fenced example under "## `Headless config:` echo block"
        m = re.search(
            r"## `Headless config:` echo block.*?```(.*?)```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "echo block example fence missing")
        block = m.group(1)
        self.assertRegex(
            block,
            r"(?m)^[ \t]*pipeline_version=.+ \(bundle\)\s*$",
        )
        self.assertRegex(
            block,
            r"(?m)^[ \t]*plugin_root=.+ \(resolved\)\s*$",
        )


if __name__ == "__main__":
    unittest.main()
