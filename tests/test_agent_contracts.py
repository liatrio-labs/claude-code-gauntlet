"""Agent .md contract guards (V3.1 c8 / live-run L10).

The v2 NDJSON emission contract was scrubbed from the 7 discovery agents: v3
returns findings BY VALUE via StructuredOutput, so any surviving printf/NDJSON/
validator instruction burns failed tool calls in every live run (9 of 10 zsh
Bash failures in the PR-310 run were printf emission attempts) and double-emits
findings. These tests pin the scrub so the residue cannot return.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DISCOVERY_AGENTS = [
    "bug-detector", "security-reviewer", "cross-file-impact", "test-analyzer",
    "conventions-and-intent", "type-design-analyzer", "code-simplifier",
]

# Emission-mechanics markers that must never reappear in a discovery agent
# contract. 'Bash' is included: the tool was granted solely for NDJSON emission
# ("Bash is available ONLY for writing findings"), so its grant goes with it.
RESIDUE = re.compile(r"printf|ndjson|validate_ndjson|Bash", re.IGNORECASE)


class TestDiscoveryAgentEmissionScrub(unittest.TestCase):
    def test_no_ndjson_emission_residue_in_discovery_agents(self):
        offenders = {}
        for name in DISCOVERY_AGENTS:
            text = (REPO / "agents" / f"{name}.md").read_text()
            hits = sorted(set(RESIDUE.findall(text)))
            if hits:
                offenders[name] = hits
        self.assertEqual(offenders, {},
                         f"v2 NDJSON emission residue returned: {offenders}")

    def test_discovery_agents_keep_by_value_contract_and_exclusions(self):
        for name in DISCOVERY_AGENTS:
            text = (REPO / "agents" / f"{name}.md").read_text()
            self.assertIn("by-value return", text, name)
            self.assertIn("{ findings, complete, total_seen }", text, name)
            # The intentionally-duplicated false-positive exclusion block survives.
            self.assertIn("False-positive exclusions", text, name)

    def test_non_discovery_agents_are_untouched_by_the_scrub_rule(self):
        # The executor legitimately keeps Bash (it runs the pinned verify command);
        # the scrub rule is scoped to the 7 discovery contracts only.
        executor = (REPO / "agents" / "executor.md").read_text()
        self.assertIn("Bash", executor)


if __name__ == "__main__":
    unittest.main()
