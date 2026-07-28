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

# Every agent that opens a file, and so is exposed to an unannounced partial Read
# (issue #48). The 7 discovery agents plus the three other file-readers: validator and
# change-summarizer are handed the shared context path by the same stage inputs the
# discovery agents are; challenger is not, but it opens the code under review itself.
COMPLETE_READ_AGENTS = DISCOVERY_AGENTS + ["validator", "challenger", "change-summarizer"]

COMPLETE_READ_CANON = (
    "skills/code-gauntlet/references/complete-read-contract.md"
)
COMPLETE_READ_MARKER = (
    "<!-- Canonical source: references/complete-read-contract.md"
    " — keep all agent copies in sync -->"
)


def _canonical_complete_read_block():
    """The block between the BEGIN/END sentinels in the canonical reference."""
    text = (REPO / COMPLETE_READ_CANON).read_text()
    body = text.split("<!-- BEGIN CANONICAL BLOCK -->")[1]
    return body.split("<!-- END CANONICAL BLOCK -->")[0].strip("\n")

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

    def test_schema_declared_extras_are_omit_not_null(self):
        # Bugbot PR-20 wave 1: hidden_errors / invalid_state_example are typed
        # `string` in schemaExtra; a contract that says "otherwise null" makes agents
        # emit null against a string-typed schema — the same StructuredOutput
        # retry-storm class as string-typed confidence. Not-applicable extras must be
        # OMITTED. (claude_md_rule is not schema-declared, so its null is fine.)
        for name, field in [("bug-detector", "hidden_errors"),
                            ("type-design-analyzer", "invalid_state_example")]:
            text = (REPO / "agents" / f"{name}.md").read_text()
            self.assertIn("OMIT this field", text, name)
            self.assertNotIn(f'"{field}":null', text,
                             f"{name} example emits null for schema-declared {field}")
            self.assertNotIn("otherwise null", text.split(field)[1][:120],
                             f"{name} contract still offers a null branch for {field}")


class TestCompleteReadContract(unittest.TestCase):
    """The read-completeness contract (issue #48).

    On run wf_cef39739-577 every one of the 7 discovery agents' FIRST Read of the
    95,057-byte / 2,028-line shared context file returned 58,145 chars ending at line
    1083, and NONE of the 7 tool results carried a truncation notice. Six agents
    paginated to the file's end anyway; security-reviewer did not, and reviewed roughly
    half the diff while returning complete=true. Nothing in the run's artifacts, report
    or transcript distinguished that from a clean empty result.

    The primary fix is arithmetic (contextReadPlan in workflows/src/stages.js enumerates
    the exact Read calls, pinned by workflows/test/stages_context_read.test.js). These
    tests pin the agent-side backstop: every file-reading agent carries the rule, and
    all 10 copies stay byte-identical to the canonical source.
    """

    def test_canonical_source_exists_and_lists_every_copy(self):
        canon = REPO / COMPLETE_READ_CANON
        self.assertTrue(canon.is_file(), f"missing canonical source: {COMPLETE_READ_CANON}")
        text = canon.read_text()
        for name in COMPLETE_READ_AGENTS:
            self.assertIn(f"`agents/{name}.md`", text,
                          f"{name} is not listed in the canonical file's duplication contract")

    def test_every_file_reading_agent_carries_the_block_byte_identically(self):
        # Byte-identity is the point: a copy that drifts is a copy that stops saying the
        # thing that keeps a partial read from passing as a whole one.
        block = _canonical_complete_read_block()
        self.assertGreater(len(block), 400, "canonical block looks truncated")
        offenders = {}
        for name in COMPLETE_READ_AGENTS:
            text = (REPO / "agents" / f"{name}.md").read_text()
            problems = []
            if text.count(COMPLETE_READ_MARKER) != 1:
                problems.append(f"canonical-source comment appears {text.count(COMPLETE_READ_MARKER)}x (need 1)")
            if block not in text:
                problems.append("block missing or not byte-identical to the canonical source")
            if problems:
                offenders[name] = problems
        self.assertEqual(offenders, {},
                         f"complete-read contract drifted or is missing: {offenders}")

    def test_the_block_states_the_three_load_bearing_facts(self):
        # Requirement 2: the fix must not depend on the Read tool emitting a truncation
        # notice — none of the 7 profiled first-reads carried one. Assert the block says
        # so explicitly, names the shared context file, and calls out the silent-failure
        # consequence. Worded against the canonical source only; the byte-identity test
        # above propagates it to all 10 copies.
        block = _canonical_complete_read_block()
        self.assertIn("no", block.lower())
        self.assertIn("truncation notice", block)
        self.assertIn("shared context file is mandatory reading in full", block)
        self.assertIn("silent failure", block)
        self.assertIn("offset", block)

    def test_the_block_trips_no_existing_discovery_agent_guard(self):
        # The scrub guard above forbids printf/ndjson/validate_ndjson/Bash in a discovery
        # agent contract. A new block that reintroduced any of them would pass its own
        # test and fail the scrub — assert the two contracts are compatible directly.
        self.assertEqual(sorted(set(RESIDUE.findall(_canonical_complete_read_block()))), [])

    def test_the_skill_documents_measuring_and_stamping_the_context_size(self):
        # The agent-side block is a backstop. The primary mechanism is the measurement
        # the skill stamps — if Phase 2 stops stamping it, every prompt silently falls
        # back to the count-free wording and the arithmetic fix is gone with no failure
        # anywhere. Pin the producer-side documentation that keeps the two in step.
        skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text()
        triage = (REPO / "skills/code-gauntlet/references/phase2-triage.md").read_text()
        for doc, label in [(skill, "SKILL.md"), (triage, "phase2-triage.md")]:
            self.assertIn("contextLines", doc, f"{label} does not document the contextLines waist field")
            self.assertIn("contextChars", doc, f"{label} does not document the contextChars waist field")

    def test_the_workflow_validates_and_consumes_the_stamped_size(self):
        # The Python suite owns no JS behavior, but it can pin that the two halves of the
        # waist still exist: the skill's docs (above) promise a field the workflow must
        # still accept and use. A rename on either side fails here.
        args_js = (REPO / "workflows/src/args.js").read_text()
        stages_js = (REPO / "workflows/src/stages.js").read_text()
        self.assertIn("contextLines", args_js, "args.js no longer validates contextLines")
        self.assertIn("contextReadPlan", stages_js, "stages.js no longer builds a read plan")
        # The bundle is generated; test_bundle_fresh.py proves it matches src, so a
        # presence check here catches a build that silently dropped the stage.
        bundle = (REPO / "workflows/pipeline.js").read_text()
        self.assertIn("contextReadPlan", bundle, "the shipped bundle carries no read plan")


if __name__ == "__main__":
    unittest.main()
