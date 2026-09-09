"""Tests for the registry-driven configuration resolver."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import generate_contract_requirements as generator
from scripts import resolve_config as resolver

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "resolve_config.py"
SKILL_ROOT = REPO / "skills" / "code-gauntlet"


def clean_environment(**overrides):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODE_GAUNTLET_")
    }
    env.update(overrides)
    return env


def expected_rule_values(row, mode="headless"):
    rule = row["rule"]
    if "kind" not in rule:
        rule = rule[mode]
    if rule["kind"] == "positive_digits":
        return "positive integer"
    if rule["kind"] == "digits_or_null":
        return "digits or null"
    return ",".join(rule["values"])


class TestRuleInterpreter(unittest.TestCase):
    def test_rules_are_exact_and_fail_closed(self):
        enum = {"kind": "enum", "values": ["Chat"]}
        csv = {"kind": "csv_subset", "values": ["chat", "markdown"]}
        positive = {"kind": "positive_digits"}
        digits = {"kind": "digits_or_null"}
        self.assertTrue(resolver.matches_rule(enum, "Chat", "headless"))
        self.assertFalse(resolver.matches_rule(enum, "chat", "headless"))
        self.assertTrue(resolver.matches_rule(csv, "chat,markdown", "headless"))
        for value in ("", "chat,,markdown", "chat,chat", "chat, markdown", "Chat"):
            self.assertFalse(resolver.matches_rule(csv, value, "headless"))
        self.assertTrue(resolver.matches_rule(positive, "1", "headless"))
        self.assertFalse(resolver.matches_rule(positive, "0", "headless"))
        self.assertTrue(resolver.matches_rule(digits, "0", "interactive"))
        self.assertTrue(resolver.matches_rule(digits, "null", "interactive"))
        self.assertFalse(resolver.matches_rule(digits, "00", "interactive"))
        self.assertFalse(resolver.matches_rule(digits, "01", "interactive"))
        self.assertFalse(
            resolver.matches_rule(digits, "9007199254740992", "interactive")
        )
        self.assertFalse(
            resolver.matches_rule(digits, "\N{ARABIC-INDIC DIGIT ONE}", "interactive")
        )
        self.assertFalse(resolver.matches_rule({"kind": "bogus"}, "x", "headless"))
        self.assertFalse(
            resolver.matches_rule(
                {"headless": {"kind": "enum", "values": ["x"]}},
                "x",
                "interactive",
            )
        )
        self.assertFalse(
            resolver.matches_rule({"kind": "enum", "values": [1]}, 1, "headless")
        )


class TestDefaultDeliveryParser(unittest.TestCase):
    def test_heading_termination_and_comments(self):
        cases = {
            "## Default Delivery\nchat,pr_comments\n## Ignore\nmarkdown": "chat,pr_comments",
            "## Default Delivery\nchat\n### Ignore\nmarkdown": "chat",
            "## Default Delivery\nchat\n```yaml\nmarkdown\n```:": "chat",
            "## Default Delivery\nchat\n~~~\nmarkdown\n~~~": "chat",
            "## Default Delivery\n<!-- chat,\npr_comments -->\nmarkdown": "markdown",
            "## Default Delivery\n<!-- chat,pr_comments -->": None,
            "## Default Delivery\nprose about chat": None,
            "## Default Delivery\nchat, pr_comments": None,
            "## Default Delivery\nChat": None,
            "## Default Delivery\nchat,,markdown": None,
            "# Default Delivery\nchat": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(resolver.parse_default_delivery(text), expected)

    def test_universal_newlines_and_root_heading(self):
        for newline in ("\n", "\r\n", "\r"):
            self.assertEqual(
                resolver.parse_default_delivery(
                    f"prefix{newline}## Default Delivery{newline}{newline}  chat{newline}"
                ),
                "chat",
            )

    def test_root_heading_ends_the_section(self):
        text = "## Default Delivery\n<!--\n# Ignore\n-->\nchat\n"
        self.assertIsNone(resolver.parse_default_delivery(text))

    def test_unicode_line_separator_does_not_split_a_value_line(self):
        self.assertIsNone(
            resolver.parse_default_delivery("## Default Delivery\nchat\u2028markdown\n")
        )

    def test_fenced_value_is_not_read_after_a_blank_body(self):
        text = "## Default Delivery\n\n```yaml\nchat\n```\n"
        self.assertEqual(
            resolver._default_delivery_body(re.split(r"\r\n|\r|\n", text)), [""]
        )
        self.assertIsNone(resolver.parse_default_delivery(text))

    def test_shipped_root_scaffold_is_unset_and_uses_the_default(self):
        spec = (REPO / "skills/code-gauntlet/references/review-md-spec.md").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"### Root REVIEW\.md template\n\n(````markdown\n.*?\n````)",
            spec,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        scaffold = match.group(1)
        result = resolver.resolve("headless", {}, scaffold, "pr")
        self.assertEqual(
            result["configEcho"]["delivery"],
            {"value": "markdown", "source": "default"},
        )


class TestResolvePureFunctions(unittest.TestCase):
    def test_empty_environment_defaults_have_one_wire_copy(self):
        interactive = resolver.resolve("interactive", {}, None, "pr")
        self.assertEqual(
            interactive,
            {
                "configEcho": {
                    "model_tier": {"value": "optimized", "source": "fixed"},
                    "pr_comment_cap": {"value": "null", "source": "default"},
                    "delivery_tier": {"value": "all", "source": "default"},
                },
                "waist": {
                    "configEcho": {
                        "model_tier": {"value": "optimized", "source": "fixed"},
                        "pr_comment_cap": {"value": "null", "source": "default"},
                        "delivery_tier": {"value": "all", "source": "default"},
                    }
                },
                "resolved": {"model_tier": "optimized"},
            },
        )
        headless = resolver.resolve("headless", {}, None, "pr")
        self.assertEqual(
            list(headless["configEcho"]),
            [
                "model_tier",
                "delivery",
                "post_mode",
                "pr_comment_cap",
                "delivery_tier",
                "draft_policy",
                "reviewed_policy",
                "pr_not_found_policy",
                "trivial_scope",
            ],
        )
        self.assertEqual(headless["configEcho"]["pr_comment_cap"]["value"], "6")
        self.assertEqual(headless["resolved"]["delivery"], ["markdown"])
        self.assertNotIn("pr_comment_cap", headless["resolved"])
        self.assertEqual(headless["waist"], {"configEcho": headless["configEcho"]})

    def test_resolved_contains_only_hand_typed_gate_inputs(self):
        expected = {
            "headless": {
                "model_tier",
                "delivery",
                "post_mode",
                "draft_policy",
                "reviewed_policy",
                "pr_not_found_policy",
            },
            "interactive": {"model_tier"},
        }
        for mode, keys in expected.items():
            with self.subTest(mode=mode):
                result = resolver.resolve(mode, {}, None, "pr")
                self.assertEqual(set(result["resolved"]), keys)

    def test_delivery_precedence_is_env_then_review_then_default(self):
        review = "## Default Delivery\nchat,pr_comments\n## Ignore\n"
        from_review = resolver.resolve("headless", {}, review, "pr")
        self.assertEqual(
            from_review["configEcho"]["delivery"],
            {"value": "chat,pr_comments", "source": "review_md"},
        )
        from_default = resolver.resolve(
            "headless", {}, "## Default Delivery\n<!-- chat -->", "pr"
        )
        self.assertEqual(
            from_default["configEcho"]["delivery"],
            {"value": "markdown", "source": "default"},
        )
        from_env = resolver.resolve(
            "headless",
            {"CODE_GAUNTLET_DELIVERY": "markdown"},
            review,
            "pr",
        )
        self.assertEqual(
            from_env["configEcho"]["delivery"], {"value": "markdown", "source": "env"}
        )

    def test_delivery_precedence_uses_each_distinct_value(self):
        review = "## Default Delivery\nchat,pr_comments\n"
        env = {"CODE_GAUNTLET_DELIVERY": "chat"}
        from_env = resolver.resolve("headless", env, review, "pr")
        self.assertEqual(
            from_env["configEcho"]["delivery"],
            {"value": "chat", "source": "env"},
        )
        from_review = resolver.resolve("headless", {}, review, "pr")
        self.assertEqual(
            from_review["configEcho"]["delivery"],
            {"value": "chat,pr_comments", "source": "review_md"},
        )
        from_default = resolver.resolve("headless", {}, None, "pr")
        self.assertEqual(
            from_default["configEcho"]["delivery"],
            {"value": "markdown", "source": "default"},
        )

    def test_interactive_model_pin_is_validated_but_fixed(self):
        result = resolver.resolve(
            "interactive",
            {"CODE_GAUNTLET_MODEL_TIER": "optimized"},
            "## Default Delivery\nchat",
            "pr",
        )
        self.assertEqual(
            result["configEcho"]["model_tier"],
            {"value": "optimized", "source": "fixed"},
        )
        with self.assertRaises(resolver.ResolverError):
            resolver.resolve(
                "interactive",
                {"CODE_GAUNTLET_MODEL_TIER": "standard"},
                None,
                "pr",
            )

    def test_local_delivery_rejects_pr_comments_from_env_or_review(self):
        with self.assertRaisesRegex(
            resolver.ResolverError,
            r"HEADLESS CONFIG ERROR: CODE_GAUNTLET_DELIVERY=chat,pr_comments not in \{chat,markdown\}",
        ):
            resolver.resolve(
                "headless",
                {"CODE_GAUNTLET_DELIVERY": "chat,pr_comments"},
                None,
                "local",
            )
        with self.assertRaisesRegex(
            resolver.ResolverError,
            r"HEADLESS CONFIG ERROR: REVIEW\.md default_delivery=chat,pr_comments not in \{chat,markdown\}",
        ):
            resolver.resolve(
                "headless", {}, "## Default Delivery\nchat,pr_comments", "local"
            )

    def test_pr_mr_and_unset_targets_accept_pr_comments(self):
        review = "## Default Delivery\nchat,pr_comments"
        for target in ("pr", "mr", None):
            with self.subTest(source="env", target=target):
                result = resolver.resolve(
                    "headless",
                    {"CODE_GAUNTLET_DELIVERY": "chat,pr_comments"},
                    None,
                    target,
                )
                self.assertEqual(
                    result["configEcho"]["delivery"]["value"], "chat,pr_comments"
                )
                self.assertEqual(result["configEcho"]["delivery"]["source"], "env")
            with self.subTest(source="review_md", target=target):
                result = resolver.resolve("headless", {}, review, target)
                self.assertEqual(
                    result["configEcho"]["delivery"]["value"], "chat,pr_comments"
                )
                self.assertEqual(
                    result["configEcho"]["delivery"]["source"], "review_md"
                )


class TestResolverCli(unittest.TestCase):
    def run_cli(self, *args, **values):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=REPO,
            env=clean_environment(**values),
            capture_output=True,
            text=True,
        )

    def test_success_shape_identity_and_stderr_block(self):
        proc = self.run_cli("--target", "pr", CODE_GAUNTLET_HEADLESS="1")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.stdout.endswith("\n"))
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["target"], "pr")
        self.assertNotIn("configEcho", payload)
        self.assertEqual(
            set(payload), {"mode", "target", "block", "waist", "resolved", "identity"}
        )
        # The release commit bumps plugin.json and the bundle together; the manifest is
        # the oracle the resolver does not read, so a wrong bundle read goes red here.
        manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(payload["identity"]["pipeline_version"], manifest["version"])
        self.assertEqual(payload["identity"]["plugin_root"], str(REPO))
        self.assertEqual(payload["block"] + "\n", proc.stderr)
        self.assertTrue(proc.stderr.startswith("Headless config:\n"))

    def test_no_target_and_interactive_shape(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertIsNone(payload["target"])
        self.assertEqual(payload["mode"], "interactive")
        self.assertEqual(
            list(payload["waist"]["configEcho"]),
            ["model_tier", "pr_comment_cap", "delivery_tier"],
        )
        self.assertTrue(proc.stderr.startswith("Resolved config:\n"))

    def test_invalid_pins_are_exact_and_stdout_is_empty(self):
        cases = [
            ("CODE_GAUNTLET_MODEL_TIER", "bad", "{optimized}"),
            ("CODE_GAUNTLET_DELIVERY", "bad", "{chat,pr_comments,markdown}"),
            ("CODE_GAUNTLET_POST_MODE", "later", "{dry-run,live}"),
            ("CODE_GAUNTLET_PR_COMMENT_CAP", "0", "{positive integer}"),
            ("CODE_GAUNTLET_DELIVERY_TIER", "branch_only", "{all,main_only}"),
            ("CODE_GAUNTLET_DRAFT_POLICY", "defer", "{review,skip}"),
            ("CODE_GAUNTLET_REVIEWED_POLICY", "partial", "{incremental,full,skip}"),
            ("CODE_GAUNTLET_PR_NOT_FOUND_POLICY", "ignore", "{local,error}"),
            ("CODE_GAUNTLET_TRIVIAL_SCOPE", "none", "{light,full}"),
        ]
        for variable, value, allowed in cases:
            proc = self.run_cli(
                "--target", "pr", CODE_GAUNTLET_HEADLESS="1", **{variable: value}
            )
            self.assertEqual(proc.returncode, 1, variable)
            self.assertEqual(proc.stdout, "", variable)
            self.assertEqual(
                proc.stderr,
                f"HEADLESS CONFIG ERROR: {variable}={value} not in {allowed}\n",
            )
        proc = self.run_cli(
            "--target",
            "pr",
            CODE_GAUNTLET_PR_COMMENT_CAP="not-a-cap",
        )
        self.assertEqual(
            proc.stderr,
            "HEADLESS CONFIG ERROR: CODE_GAUNTLET_PR_COMMENT_CAP=not-a-cap not in {digits or null}\n",
        )

    def test_interactive_and_numeric_pin_matrix(self):
        proc = self.run_cli(CODE_GAUNTLET_PR_COMMENT_CAP="null")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stdout
            and json.loads(proc.stdout)["waist"]["configEcho"]["pr_comment_cap"][
                "value"
            ],
            "null",
        )
        for value in (
            "00",
            "01",
            "06",
            "9007199254740992",
            "\N{ARABIC-INDIC DIGIT ONE}",
        ):
            proc = self.run_cli(CODE_GAUNTLET_PR_COMMENT_CAP=value)
            self.assertEqual(proc.returncode, 1, value)
            self.assertIn("{digits or null}", proc.stderr)

    def test_usage_and_setup_fail_without_process_stream_noise(self):
        proc = self.run_cli("--target", "wrong")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr.count("\n"), 1)
        self.assertTrue(proc.stderr.startswith("RESOLVER SETUP ERROR: "))
        code, stdout, stderr = resolver.run(["--target", "wrong"], {})
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr.count("\n"), 1)
        self.assertTrue(stderr.startswith("RESOLVER SETUP ERROR: "))
        with tempfile.TemporaryDirectory() as directory:
            code, stdout, stderr = resolver.run(["--cwd", directory], {})
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("RESOLVER SETUP ERROR:", stderr)

    def test_plugin_root_must_match_and_matching_symlink_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            code, stdout, stderr = resolver.run(["--plugin-root", directory], {})
            self.assertEqual(
                (code, stdout, stderr),
                (2, "", "RESOLVER SETUP ERROR: plugin root mismatch\n"),
            )
            link = Path(directory) / "plugin"
            link.symlink_to(REPO, target_is_directory=True)
            proc = self.run_cli("--plugin-root", str(link))
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(
                json.loads(proc.stdout)["identity"]["plugin_root"], str(REPO)
            )

            mismatch = subprocess.run(
                [sys.executable, str(SCRIPT), "--plugin-root", directory],
                cwd=REPO,
                env=clean_environment(),
                capture_output=True,
            )
            self.assertEqual(mismatch.returncode, 2)
            self.assertEqual(mismatch.stdout, b"")
            self.assertEqual(
                mismatch.stderr,
                b"RESOLVER SETUP ERROR: plugin root mismatch\n",
            )

    def test_argument_failures_are_one_line_and_stream_silent_before_run(self):
        cases = [
            ("--help",),
            ("--unknown",),
            ("--target", "wrong"),
            ("--target",),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, stdout, stderr = resolver.run(list(argv), {})
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("RESOLVER SETUP ERROR: "))
                self.assertEqual(stderr.count("\n"), 1)
                proc = subprocess.run(
                    [sys.executable, str(SCRIPT), *argv],
                    cwd=REPO,
                    env=clean_environment(),
                    capture_output=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, b"")
                self.assertEqual(proc.stderr.count(b"\n"), 1)

    def test_missing_bundle_version_is_setup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "workflows" / "pipeline.js"
            bundle.parent.mkdir()
            bundle.write_text("const not_version = 'x';", encoding="utf-8")
            with self.assertRaises(resolver.ResolverSetupError):
                resolver.read_pipeline_version(str(root))

    def test_control_values_are_json_encoded_on_the_error_line(self):
        for value in ("bad\nvalue", "bad\rvalue", "bad\x00value", "bad`value"):
            code, stdout, stderr = resolver.run(
                ["--target", "pr"],
                {"CODE_GAUNTLET_HEADLESS": "1", "CODE_GAUNTLET_DELIVERY": value},
            )
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            expected_value = (
                json.dumps(value, ensure_ascii=False)
                if "\n" in value or "\r" in value or "\x00" in value
                else value
            )
            self.assertEqual(
                stderr,
                f"HEADLESS CONFIG ERROR: CODE_GAUNTLET_DELIVERY={expected_value} not in {{chat,pr_comments,markdown}}\n",
            )


class TestGeneratedDataContracts(unittest.TestCase):
    def test_phase1_config_composite_is_r8_exact_and_has_no_review_root_section(self):
        skill = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
        start = skill.find('echo "=== config ==="')
        end = skill.find('echo "=== pr_view ==="', start)
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        expected = "\n".join(
            (
                'echo "=== config ==="',
                'if ! CONFIG_JSON=$(python3 "{plugin_root}/scripts/resolve_config.py" --target {target_type} --plugin-root "{plugin_root}"); then',
                '  echo "config: FAILED"',
                "  exit 1",
                "fi",
                'echo "$CONFIG_JSON"',
            )
        )
        self.assertEqual(skill[start:end].rstrip("\n"), expected)
        self.assertNotIn("review_md_root", skill)

    def test_pr_not_found_headless_gate_is_targetless_and_resolved(self):
        text = (REPO / "skills/code-gauntlet/references/phase1-preflight.md").read_text(
            encoding="utf-8"
        )
        line = next(
            line
            for line in text.splitlines()
            if line.startswith("> Headless exception")
        )
        self.assertEqual(
            line,
            "> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): call `scripts/resolve_config.py` with no `--target` on this path. The question is never presented in headless mode. Branch on `resolved.pr_not_found_policy`: `error` stops the run, and `local` proceeds with `pr_number` cleared. See `references/headless-mode.md`.",
        )

    def test_config_blocks_have_only_the_three_documented_producers(self):
        pattern = re.compile(r"(?m)^(?:Resolved|Headless) config:\n(?:^  .*\n?)+")
        actual = {}
        for path in sorted((REPO / "skills/code-gauntlet").rglob("*.md")):
            blocks = pattern.findall(path.read_text(encoding="utf-8"))
            if blocks:
                actual[str(path.relative_to(REPO))] = len(blocks)
        self.assertEqual(
            actual,
            {
                "skills/code-gauntlet/SKILL.md": 2,
                "skills/code-gauntlet/references/headless-mode.md": 1,
                "skills/code-gauntlet/references/report-format.md": 1,
            },
        )

    def test_headless_final_message_fallback_sentence_is_retained(self):
        text = (REPO / "skills/code-gauntlet/references/headless-mode.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "If no report materializes, repeat the block in the final message as the fallback receipt.",
            text,
        )

    def test_headless_gate_index_names_the_resolved_knob_for_each_gate(self):
        text = (REPO / "skills/code-gauntlet/references/headless-mode.md").read_text(
            encoding="utf-8"
        )
        rows = {}
        for line in text.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == 2:
                rows[cells[0]] = cells[1]
        required = {
            "PR-not-found (resolution failure)": ("resolved.pr_not_found_policy",),
            "Draft PR": ("resolved.draft_policy",),
            "Previously reviewed (Phase 2 2b-post step 3, after checkout)": (
                "resolved.reviewed_policy",
            ),
            "Trivial / light-scope (all low-risk, <50 lines)": (
                "CODE_GAUNTLET_TRIVIAL_SCOPE",
            ),
            "Phase 8 Stage 1 (delivery question)": (
                "resolved.delivery",
                "resolved.post_mode",
            ),
        }
        for gate, knobs in required.items():
            row = next((value for key, value in rows.items() if key == gate), None)
            self.assertIsNotNone(row, gate)
            for knob in knobs:
                self.assertIn(knob, row, gate)

    def test_skill_resolved_mentions_match_resolved_key_contract(self):
        modes_by_file = {
            "references/headless-mode.md": {"headless"},
        }
        resolved_keys = {
            "headless": {
                "model_tier",
                "delivery",
                "post_mode",
                "draft_policy",
                "reviewed_policy",
                "pr_not_found_policy",
            },
            "interactive": {"model_tier"},
        }
        false_rows = {
            row["key"]
            for row in generator.load_registry(str(REPO))["knobs"]
            if not row["resolvedKey"]
        }

        for mode, expected in resolved_keys.items():
            self.assertEqual(
                expected,
                set(resolver.resolve(mode, {}, None, "pr")["resolved"]),
                mode,
            )

        def assert_mentions_are_valid():
            for path in sorted(SKILL_ROOT.rglob("*.md")):
                relative = path.relative_to(SKILL_ROOT).as_posix()
                modes = modes_by_file.get(relative, {"headless", "interactive"})
                allowed = set().union(*(resolved_keys[mode] for mode in modes))
                text = path.read_text(encoding="utf-8")
                for match in re.finditer(
                    r"(?:configResult\.)?resolved\.([a-z_]+)", text
                ):
                    key = match.group(1)
                    self.assertIn(key, allowed, str(path))
                    self.assertNotIn(key, false_rows, str(path))

        assert_mentions_are_valid()

        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory) / "skills" / "code-gauntlet"
            shutil.copytree(SKILL_ROOT, temp_root)
            injected = temp_root / "references" / "phase2-triage.md"
            injected.write_text(
                injected.read_text(encoding="utf-8")
                + "\nThe receipt must not use `resolved.pr_comment_cap`.\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(sys.modules[__name__], "SKILL_ROOT", temp_root),
                self.assertRaisesRegex(AssertionError, "pr_comment_cap"),
            ):
                assert_mentions_are_valid()

    def test_derived_field_mentions_outside_fences_match_the_inventory(self):
        rows = generator.load_registry(str(REPO))["knobs"]
        tokens = [row["waistPath"] for row in rows if row["waistPath"] is not None] + [
            f"configEcho.{row['key']}" for row in rows if row["derivedFrom"] is not None
        ]
        patterns = {
            token: re.compile(rf"(?<![\w.]){re.escape(token)}(?!\w)")
            for token in tokens
        }
        expected = {
            ("skills/code-gauntlet/SKILL.md", "limits.deliveryCap"): 2,
            ("skills/code-gauntlet/SKILL.md", "scopeAnswer"): 7,
            (
                "skills/code-gauntlet/references/delivery-guide.md",
                "limits.deliveryCap",
            ): 1,
            (
                "skills/code-gauntlet/references/phase1-preflight.md",
                "scopeAnswer",
            ): 2,
            (
                "skills/code-gauntlet/references/phase2-triage.md",
                "scopeAnswer",
            ): 4,
            (
                "skills/code-gauntlet/references/phase3-dispatch.md",
                "limits.deliveryCap",
            ): 1,
            (
                "skills/code-gauntlet/references/phase3-dispatch.md",
                "scopeAnswer",
            ): 1,
            (
                "skills/code-gauntlet/references/phase8-delivery.md",
                "limits.deliveryCap",
            ): 2,
        }

        def assert_inventory():
            actual = {}
            for path in sorted(SKILL_ROOT.rglob("*.md")):
                rel_path = (
                    "skills/code-gauntlet/" + path.relative_to(SKILL_ROOT).as_posix()
                )
                inside_fence = False
                counts = {token: 0 for token in tokens}
                for line in path.read_text(encoding="utf-8").split("\n"):
                    marker = generator._IDENTITY_MARKER_RE.match(line)
                    if marker:
                        inside_fence = not bool(marker.group("close"))
                        continue
                    if not inside_fence:
                        for token, pattern in patterns.items():
                            counts[token] += bool(pattern.search(line))
                actual.update(
                    {
                        (rel_path, token): count
                        for token, count in counts.items()
                        if count
                    }
                )
            self.assertEqual(actual, expected)

        assert_inventory()

        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory) / "skills" / "code-gauntlet"
            shutil.copytree(SKILL_ROOT, temp_root)
            injected = temp_root / "references" / "phase2-triage.md"
            injected.write_text(
                injected.read_text(encoding="utf-8")
                + "\nThe workflow fills `limits.deliveryCap` from `configEcho.pr_comment_cap`.\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(sys.modules[__name__], "SKILL_ROOT", temp_root),
                self.assertRaises(AssertionError),
            ):
                assert_inventory()

    def test_generated_receipts_are_resolver_fixtures(self):
        registry = generator.load_registry(str(REPO))["knobs"]
        text = (REPO / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
        open_marker, close_marker = generator.identity_marker_lines(
            "config_receipt", "skills/code-gauntlet/SKILL.md"
        )
        body = text.split(open_marker, 1)[1].split(close_marker, 1)[0]
        for mode, label in (("interactive", "Interactive"), ("headless", "Headless")):
            match = re.search(
                rf"\*\*{label} receipt:\*\*\n\n```json\n(.*?)\n```",
                body,
                re.DOTALL,
            )
            self.assertIsNotNone(match)
            expected = resolver.resolve(mode, {}, None, "pr", registry=registry)[
                "configEcho"
            ]
            self.assertEqual(json.loads(match.group(1)), expected)

    def test_headless_table_is_registry_locked(self):
        registry = generator.load_registry(str(REPO))["knobs"]
        text = (REPO / "skills/code-gauntlet/references/headless-mode.md").read_text(
            encoding="utf-8"
        )
        rows = {
            match.group(1): (match.group(2), match.group(3))
            for match in re.finditer(
                r"\| `(CODE_GAUNTLET_[A-Z_]+)` \| `([^`]*)` \| `([^`]*)` \|", text
            )
        }
        expected = {
            row["env"]: (expected_rule_values(row), str(row["defaults"]["headless"][0]))
            for row in registry
            if row["env"]
        }
        self.assertEqual(rows, expected)

    def test_machine_parsed_producer_sets_are_exact(self):
        text = (REPO / "docs/machine-parsed-strings.md").read_text(encoding="utf-8")
        expected = {
            "Headless config:": {
                "workflows/src/renderReport.js",
                "skills/code-gauntlet/references/report-format.md",
                "skills/code-gauntlet/references/headless-mode.md",
                "skills/code-gauntlet/SKILL.md",
                "scripts/resolve_config.py",
            },
            "Resolved config:": {
                "workflows/src/renderReport.js",
                "skills/code-gauntlet/references/report-format.md",
                "skills/code-gauntlet/SKILL.md",
                "scripts/resolve_config.py",
            },
            "HEADLESS CONFIG ERROR:": {
                "skills/code-gauntlet/references/headless-mode.md",
                "scripts/resolve_config.py",
            },
        }
        for token, paths in expected.items():
            row = next(
                line for line in text.splitlines() if line.startswith(f"| `{token}`")
            )
            cells = [cell.strip() for cell in row.strip("|").split("|")]
            actual = {item.strip(" `") for item in cells[1].split(",")}
            self.assertEqual(actual, paths, token)
