"""Contract: headless-mode.md Headless config echo includes identity receipts."""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
HEADLESS = REPO_ROOT / "skills" / "code-gauntlet" / "references" / "headless-mode.md"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.runner import invoke  # noqa: E402

DOC_ECHO_VALUES = {
    **invoke.EXPECTED_ECHO,
    "delivery_tier": "all",
    "pipeline_version": "3.1.3",
    "plugin_root": "/absolute/path/to/claude-code-gauntlet",
}


class HeadlessEchoIdentityContractTest(unittest.TestCase):
    def _echo_block(self):
        text = HEADLESS.read_text(encoding="utf-8")
        # The fenced example under "## `Headless config:` echo block"
        m = re.search(
            r"## `Headless config:` echo block.*?```(.*?)```",
            text,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "echo block example fence missing")
        return m.group(1)

    def test_the_bench_shaped_example_satisfies_the_runners_echo_matcher(self):
        """T-ECHO: the bench-configured example satisfies the runner's eight-key receipt."""
        block = self._echo_block()
        self.assertTrue(invoke._echo_in_text(block))

        # Keep the two identity receipts pinned as well; they are outside EXPECTED_ECHO but
        # are part of the documented eleven-line block.
        self.assertRegex(
            block,
            r"(?m)^[ \t]*pipeline_version=.+ \(bundle\)\s*$",
        )
        self.assertRegex(
            block,
            r"(?m)^[ \t]*plugin_root=.+ \(resolved\)\s*$",
        )

    def test_every_knob_line_in_the_doc_block_satisfies_the_line_anchor(self):
        """T-ECHO: all eleven doc keys satisfy the runner's exact line anchor.

        The expected values are substituted into a copy of the doc block and into the runner's
        expectation map. This checks indentation, ``key=value``, the source suffix, and one line
        per knob without pinning the doc's values to anything except the bench-shaped assertion
        above.
        """
        block = self._echo_block()
        substituted = []
        seen = set()
        for line in block.splitlines():
            knob = next(
                (key for key in DOC_ECHO_VALUES if f"{key}=" in line),
                None,
            )
            if knob is None:
                substituted.append(line)
                continue
            self.assertNotIn(knob, seen, f"duplicate doc knob: {knob}")
            seen.add(knob)
            before, after = line.split(f"{knob}=", 1)
            _, source = after.split(" (", 1)
            substituted.append(f"{before}{knob}={DOC_ECHO_VALUES[knob]} ({source}")

        self.assertEqual(set(DOC_ECHO_VALUES), seen)
        with patch.object(invoke, "EXPECTED_ECHO", DOC_ECHO_VALUES):
            self.assertTrue(invoke._echo_in_text("\n".join(substituted)))


if __name__ == "__main__":
    unittest.main()
