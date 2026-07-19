"""Tests for bench/runner/invoke.py + bench/runner/costs.py.

No network: ``claude`` is replaced by bench/tests/fakes/fake_claude.py, copied onto a
temp bin dir and put on PATH. invoke_review reads its base env from ``os.environ``, so
the tests patch os.environ (restored via a contextmanager) to inject PATH + the
FAKE_CLAUDE_* selectors, then let the fake propagate through build_env.
"""

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Import via the intended package path regardless of how pytest is invoked.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.runner import invoke  # noqa: E402
from bench.runner.costs import parse_costs  # noqa: E402
from bench.runner.invoke import InvokeResult, build_env, invoke_review  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fakes" / "fake_claude.py"

PR = {
    "owner": "octo",
    "repo": "widget",
    "pr_number": 5,
    "url": "https://github.com/octo/widget/pull/5",
}


@contextlib.contextmanager
def patched_environ(**overrides):
    saved = dict(os.environ)
    os.environ.update({k: str(v) for k, v in overrides.items()})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class InvokeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench-invoke-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # run_dir shaped as <workspace>/runs/<run_id> so claude-home derives to
        # <workspace>/claude-home under tmp (never the real ~/.claude).
        self.run_dir = Path(self.tmp) / "workspace" / "runs" / "gate-test"
        self.run_dir.mkdir(parents=True)
        self.worktree = Path(self.tmp) / "wt"
        self.worktree.mkdir()

    def install_fake(self):
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir(exist_ok=True)
        dst = bindir / "claude"
        shutil.copy(str(FAKE), str(dst))
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return bindir

    def _run(self, mode, timeout_s=30, extra_env=None, tool="deep-review-v3"):
        bindir = self.install_fake()
        overrides = {
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_CLAUDE_MODE": mode,
        }
        if extra_env:
            overrides.update(extra_env)
        with patched_environ(**overrides):
            return invoke_review(
                self.worktree, PR, self.run_dir, timeout_s=timeout_s, tool=tool
            )


# ------------------------------------------------------------------------ pr_dir_name


class PrDirNameTest(unittest.TestCase):
    def test_owner_repo_number_key(self):
        pr = {"owner": "octo", "repo": "widget", "pr_number": 5,
              "url": "https://github.com/octo/widget/pull/5"}
        self.assertEqual(invoke.pr_dir_name(pr), "pr-octo-widget-5")

    def test_derives_from_url_when_owner_repo_absent(self):
        pr = {"url": "https://github.com/acme/thing/pull/9"}
        self.assertEqual(invoke.pr_dir_name(pr), "pr-acme-thing-9")

    def test_same_number_different_repo_yields_distinct_keys(self):
        # The collision FIX 1 targets: /pull/1 in two forks must not share a dir.
        a = {"url": "https://github.com/ai-code-review-evaluation/sentry-greptile/pull/1"}
        b = {"url": "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/1"}
        self.assertNotEqual(invoke.pr_dir_name(a), invoke.pr_dir_name(b))
        self.assertEqual(
            invoke.pr_dir_name(a), "pr-ai-code-review-evaluation-sentry-greptile-1"
        )

    def test_unsafe_chars_collapsed(self):
        pr = {"owner": "o/w", "repo": "a b", "pr_number": 3}
        self.assertEqual(invoke.pr_dir_name(pr), "pr-o-w-a-b-3")


# --------------------------------------------------------------------------- build_env


class BuildEnvTest(InvokeTestBase):
    def test_sets_nine_bench_values(self):
        env = build_env(PR, self.run_dir, {})
        self.assertEqual(env["DEEP_REVIEW_HEADLESS"], "1")
        self.assertEqual(env["DEEP_REVIEW_MODEL_TIER"], "optimized")
        self.assertEqual(env["DEEP_REVIEW_DELIVERY"], "pr_comments,markdown")
        self.assertEqual(env["DEEP_REVIEW_POST_MODE"], "dry-run")
        self.assertEqual(env["DEEP_REVIEW_PR_COMMENT_CAP"], "25")
        self.assertEqual(env["DEEP_REVIEW_DRAFT_POLICY"], "review")
        self.assertEqual(env["DEEP_REVIEW_REVIEWED_POLICY"], "full")
        self.assertEqual(env["DEEP_REVIEW_PR_NOT_FOUND_POLICY"], "error")
        self.assertEqual(env["DEEP_REVIEW_TRIVIAL_SCOPE"], "full")

    def test_output_dir_and_gh_repo(self):
        env = build_env(PR, self.run_dir, {})
        self.assertEqual(env["DEEP_REVIEW_OUTPUT_DIR"], str(self.run_dir / "output"))
        self.assertEqual(env["GH_REPO"], "octo/widget")

    def test_gh_repo_from_url_fallback(self):
        pr = {"url": "https://github.com/acme/thing/pull/9", "pr_number": 9}
        env = build_env(pr, self.run_dir, {})
        self.assertEqual(env["GH_REPO"], "acme/thing")

    def test_home_and_config_dir_isolated(self):
        env = build_env(PR, self.run_dir, {})
        workspace = self.run_dir.resolve().parent.parent
        self.assertEqual(env["HOME"], str(workspace / "claude-home"))
        self.assertEqual(
            env["CLAUDE_CONFIG_DIR"], str(workspace / "claude-home" / "config")
        )

    def test_base_env_preserved(self):
        env = build_env(PR, self.run_dir, {"PATH": "/x/y", "FOO": "bar"})
        self.assertEqual(env["PATH"], "/x/y")
        self.assertEqual(env["FOO"], "bar")

    def test_seeds_claude_json_trust(self):
        env = build_env(PR, self.run_dir, {})
        cfg = Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        self.assertTrue(cfg.exists())
        data = json.loads(cfg.read_text())
        wt = str((self.run_dir / invoke.pr_dir_name(PR) / "worktree").resolve())
        self.assertIn(wt, data["projects"])
        self.assertTrue(data["projects"][wt]["hasTrustDialogAccepted"])

    def test_seeds_claude_json_merges_existing(self):
        # Pre-seed a config with an unrelated project and a top-level key.
        workspace = self.run_dir.resolve().parent.parent
        cfg_dir = workspace / "claude-home" / "config"
        cfg_dir.mkdir(parents=True)
        existing = {
            "numStartups": 4,
            "projects": {"/some/other/repo": {"hasTrustDialogAccepted": True}},
        }
        (cfg_dir / ".claude.json").write_text(json.dumps(existing))

        env = build_env(PR, self.run_dir, {})
        data = json.loads((cfg_dir / ".claude.json").read_text())
        # existing content preserved
        self.assertEqual(data["numStartups"], 4)
        self.assertIn("/some/other/repo", data["projects"])
        # new worktree merged in
        wt = str((self.run_dir / invoke.pr_dir_name(PR) / "worktree").resolve())
        self.assertTrue(data["projects"][wt]["hasTrustDialogAccepted"])


# --------------------------------------------------------------- build_env gh auth


class BuildEnvGhAuthTest(InvokeTestBase):
    """build_env must keep child ``gh`` authenticated across the HOME override.

    The claude-config isolation overrides HOME/CLAUDE_CONFIG_DIR (the S7 boundary is
    the claude binary's settings/plugins). The skill's children still shell out to
    ``gh pr view/diff``, whose auth lives under the REAL home -- so build_env points
    GH_CONFIG_DIR back at the real gh config dir and passes GH_TOKEN/GITHUB_TOKEN
    through. This does NOT weaken the isolation: gh auth is an ambient prerequisite,
    not part of the claude-settings boundary.
    """

    def test_gh_config_dir_derived_from_base_home_when_dir_exists(self):
        home = Path(self.tmp) / "realhome"
        gh_dir = home / ".config" / "gh"
        gh_dir.mkdir(parents=True)
        env = build_env(PR, self.run_dir, {"HOME": str(home)})
        self.assertEqual(env["GH_CONFIG_DIR"], str(gh_dir))
        # HOME itself is still overridden to the isolated claude-home.
        self.assertNotEqual(env["HOME"], str(home))

    def test_gh_config_dir_honors_xdg_config_home(self):
        xdg = Path(self.tmp) / "xdg"
        gh_dir = xdg / "gh"
        gh_dir.mkdir(parents=True)
        home = Path(self.tmp) / "realhome"
        (home / ".config" / "gh").mkdir(parents=True)  # must lose to XDG_CONFIG_HOME
        env = build_env(
            PR, self.run_dir, {"HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
        )
        self.assertEqual(env["GH_CONFIG_DIR"], str(gh_dir))

    def test_gh_config_dir_not_set_when_dir_absent(self):
        home = Path(self.tmp) / "realhome-empty"
        home.mkdir()  # no .config/gh underneath
        env = build_env(PR, self.run_dir, {"HOME": str(home)})
        self.assertNotIn("GH_CONFIG_DIR", env)

    def test_explicit_base_gh_config_dir_preserved(self):
        # An operator-set GH_CONFIG_DIR wins and passes through unchanged, even when it
        # does not exist on disk (the caller vouches for it -- no derivation, no check).
        env = build_env(
            PR, self.run_dir, {"HOME": str(self.tmp), "GH_CONFIG_DIR": "/custom/gh"}
        )
        self.assertEqual(env["GH_CONFIG_DIR"], "/custom/gh")

    def test_gh_and_github_tokens_passed_through(self):
        env = build_env(PR, self.run_dir, {"GH_TOKEN": "ght", "GITHUB_TOKEN": "ghb"})
        self.assertEqual(env["GH_TOKEN"], "ght")
        self.assertEqual(env["GITHUB_TOKEN"], "ghb")


# ------------------------------------------------------------------ build_env .env


class BuildEnvDotenvTest(InvokeTestBase):
    """build_env must load the metered ANTHROPIC_API_KEY from bench/.env.

    The isolated HOME/CLAUDE_CONFIG_DIR carries no credentials and the ambient
    env generally has no key, so every invocation would be unauthenticated
    without this. The bench/.env value is authoritative: it wins over any
    ambient ANTHROPIC_API_KEY so all bench spend lands on the single metered key.
    """

    def _use_env_file(self, text):
        path = Path(self.tmp) / "bench.env"
        path.write_text(text)
        saved = invoke.ENV_PATH
        invoke.ENV_PATH = path
        self.addCleanup(setattr, invoke, "ENV_PATH", saved)
        return path

    def _use_missing_env(self):
        saved = invoke.ENV_PATH
        invoke.ENV_PATH = Path(self.tmp) / "does-not-exist.env"
        self.addCleanup(setattr, invoke, "ENV_PATH", saved)

    def test_loads_api_key_from_dotenv(self):
        self._use_env_file("# metered key\nANTHROPIC_API_KEY=sk-from-dotenv\n\n")
        env = build_env(PR, self.run_dir, {})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-from-dotenv")

    def test_dotenv_wins_over_ambient(self):
        self._use_env_file("ANTHROPIC_API_KEY=sk-from-dotenv\n")
        env = build_env(PR, self.run_dir, {"ANTHROPIC_API_KEY": "sk-ambient"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-from-dotenv")

    def test_absent_dotenv_leaves_ambient_untouched(self):
        self._use_missing_env()
        env = build_env(PR, self.run_dir, {"ANTHROPIC_API_KEY": "sk-ambient"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ambient")

    def test_empty_key_in_dotenv_leaves_ambient_untouched(self):
        self._use_env_file("ANTHROPIC_API_KEY=\n")
        env = build_env(PR, self.run_dir, {"ANTHROPIC_API_KEY": "sk-ambient"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ambient")


# --------------------------------------------------------------------------- costs


class ParseCostsTest(unittest.TestCase):
    def test_fixture_envelope(self):
        envelope = {
            "total_cost_usd": 2.5,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
                "service_tier": "standard",  # non-token, ignored
            },
            "modelUsage": {
                "claude-opus-4-8": {
                    "inputTokens": 80,
                    "outputTokens": 40,
                    "cacheReadInputTokens": 10,
                    "cacheCreationInputTokens": 5,
                    "costUSD": 2.0,
                },
                "claude-haiku-4-5": {
                    "inputTokens": 20,
                    "outputTokens": 10,
                    "costUSD": 0.5,
                },
            },
        }
        out = parse_costs(envelope)
        self.assertEqual(out["cost_usd"], 2.5)
        self.assertEqual(out["tokens_total"], 165)  # 100+50+10+5
        self.assertEqual(out["per_model"]["claude-opus-4-8"]["tokens"], 135)
        self.assertEqual(out["per_model"]["claude-opus-4-8"]["cost_usd"], 2.0)
        self.assertEqual(out["per_model"]["claude-haiku-4-5"]["tokens"], 30)
        self.assertEqual(out["per_model"]["claude-haiku-4-5"]["cost_usd"], 0.5)

    def test_capacity_fields_are_not_usage(self):
        # maxOutputTokens (and any max/limit-prefixed field) is a model property
        # documented beside the real counters in modelUsage; it must not inflate
        # the summed totals.
        envelope = {
            "total_cost_usd": 1.0,
            "usage": {},
            "modelUsage": {
                "claude-opus-4-8[1m]": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "maxOutputTokens": 32000,
                    "tokenLimit": 200000,
                    "costUSD": 1.0,
                }
            },
        }
        out = parse_costs(envelope)
        self.assertEqual(out["tokens_total"], 150)
        self.assertEqual(out["per_model"]["claude-opus-4-8[1m]"]["tokens"], 150)

    def test_multi_model_sums_all_with_variant_suffix(self):
        # A run can report several models (a primary plus a background bookkeeping
        # call); ids are opaque and may carry variant suffixes. Every key is summed
        # and passed through -- no id is assumed or pattern-matched.
        envelope = {
            "total_cost_usd": 3.0,
            "usage": {
                "input_tokens": 300,
                "output_tokens": 120,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 10,
            },
            "modelUsage": {
                "claude-opus-4-8[1m]": {
                    "inputTokens": 280,
                    "outputTokens": 110,
                    "cacheReadInputTokens": 20,
                    "cacheCreationInputTokens": 10,
                    "costUSD": 2.9,
                },
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 20,
                    "outputTokens": 10,
                    "costUSD": 0.1,
                },
            },
        }
        out = parse_costs(envelope)
        self.assertEqual(out["cost_usd"], 3.0)
        self.assertEqual(out["tokens_total"], 450)  # 300+120+20+10
        self.assertEqual(
            set(out["per_model"]),
            {"claude-opus-4-8[1m]", "claude-haiku-4-5-20251001"},
        )
        self.assertEqual(out["per_model"]["claude-opus-4-8[1m]"]["tokens"], 420)
        self.assertEqual(out["per_model"]["claude-opus-4-8[1m]"]["cost_usd"], 2.9)
        self.assertEqual(out["per_model"]["claude-haiku-4-5-20251001"]["tokens"], 30)
        self.assertEqual(out["per_model"]["claude-haiku-4-5-20251001"]["cost_usd"], 0.1)

    def test_tokens_total_falls_back_to_modelusage_sum(self):
        # No aggregate .usage present -> sum tokens across ALL modelUsage keys.
        envelope = {
            "modelUsage": {
                "m-primary[1m]": {"inputTokens": 10, "outputTokens": 5},
                "m-background": {"inputTokens": 3, "outputTokens": 2},
            }
        }
        out = parse_costs(envelope)
        self.assertEqual(out["tokens_total"], 20)  # (10+5) + (3+2)

    def test_empty_envelope(self):
        out = parse_costs({})
        self.assertEqual(out["cost_usd"], 0.0)
        self.assertEqual(out["tokens_total"], 0)
        self.assertEqual(out["per_model"], {})

    def test_nested_cache_not_double_counted(self):
        # A nested cache_creation dict must not have its components summed on top of
        # the flat cache_creation_input_tokens aggregate.
        usage = {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_creation_input_tokens": 6,
            "cache_creation": {"ephemeral_5m_input_tokens": 6, "ephemeral_1h_input_tokens": 0},
        }
        out = parse_costs({"usage": usage})
        self.assertEqual(out["tokens_total"], 20)  # 10+4+6, nested dict skipped


# ------------------------------------------------------------------------- invoke


class InvokeReviewTest(InvokeTestBase):
    def test_ok(self):
        res = self._run("ok")
        self.assertIsInstance(res, InvokeResult)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.cost_usd, 1.23)
        self.assertTrue(res.echo_ok)
        self.assertIsNotNone(res.payload_path)
        self.assertTrue(Path(res.payload_path).exists())
        self.assertTrue(Path(res.raw_json_path).exists())
        self.assertIn("claude-opus-4-8", res.per_model)

    def test_hang_times_out_and_kills_group(self):
        pidfile = Path(self.tmp) / "pgid.txt"
        res = self._run("hang", timeout_s=2, extra_env={"FAKE_CLAUDE_PIDFILE": str(pidfile)})
        self.assertEqual(res.status, "timeout")
        self.assertEqual(res.reason, "watchdog_timeout")
        # The fake recorded its process-group id at startup; after invoke returns the
        # group must be gone (no orphaned child).
        self.assertTrue(pidfile.exists())
        pgid = int(pidfile.read_text().strip())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_asks_is_invalid(self):
        res = self._run("asks")
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "askuserquestion_detected")

    def test_badecho_is_invalid(self):
        res = self._run("badecho")
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "config_echo_mismatch")

    def test_claude_not_found(self):
        # PATH without the fake -> claude cannot be resolved -> failed, not a crash.
        empty_bin = Path(self.tmp) / "emptybin"
        empty_bin.mkdir()
        with patched_environ(PATH=str(empty_bin), FAKE_CLAUDE_MODE="ok"):
            res = invoke_review(self.worktree, PR, self.run_dir, timeout_s=5)
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.reason, "claude_not_found")


# ------------------------------------------------------------------- echo receipt source


class EchoReceiptSourceTest(InvokeTestBase):
    """The config-echo receipt is accepted from any of stdout, .result, or a report .md.

    A ``-p --output-format json`` run hides intermediate-turn stdout, so the receipt is
    only recoverable from the envelope's ``.result`` or the collected report markdown.
    """

    def test_receipt_only_in_result_is_ok(self):
        res = self._run("echo_in_result")
        # Block absent from raw stdout, present only in the envelope .result.
        self.assertEqual(res.status, "ok")
        self.assertTrue(res.echo_ok)

    def test_receipt_only_in_report_md_is_ok(self):
        res = self._run("echo_in_report")
        # Block absent from stdout and .result, present only in a report .md.
        self.assertEqual(res.status, "ok")
        self.assertTrue(res.echo_ok)

    def test_partial_block_everywhere_is_invalid(self):
        # badecho emits a partial block in BOTH stdout and .result (no report .md).
        res = self._run("badecho")
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "config_echo_mismatch")
        self.assertFalse(res.echo_ok)


class V3PreflightTest(InvokeTestBase):
    """A deep-review-v3 run preflights the child CLI's Workflow-tool support.

    v3 dispatches through the Workflow tool (Claude Code >= 2.1.154). An older or
    unreadable CLI is marked ``invalid`` (never scored) with a clear reason, before the
    review is even launched; v2 skips the gate. The version probe is mocked so the check
    is exercised without depending on the fake binary's --version output.
    """

    def test_old_cli_marks_invalid(self):
        with patch.object(invoke, "_claude_version", lambda _bin: (2, 1, 100)):
            res = self._run("ok")  # tool defaults to deep-review-v3
        self.assertEqual(res.status, "invalid")
        self.assertIn("v3_workflow_unsupported", res.reason)
        self.assertIn("2.1.154", res.reason)
        self.assertIn("2.1.100", res.reason)

    def test_unreadable_version_marks_invalid(self):
        with patch.object(invoke, "_claude_version", lambda _bin: None):
            res = self._run("ok")
        self.assertEqual(res.status, "invalid")
        self.assertIn("v3_workflow_unsupported", res.reason)

    def test_new_enough_cli_passes_preflight(self):
        with patch.object(invoke, "_claude_version", lambda _bin: (2, 1, 154)):
            res = self._run("ok")
        self.assertEqual(res.status, "ok")

    def test_v2_tool_skips_preflight_even_on_old_cli(self):
        # A v2-labelled run needs no Workflow tool, so an old CLI still runs to completion.
        with patch.object(invoke, "_claude_version", lambda _bin: (2, 1, 100)):
            res = self._run("ok", tool="deep-review-v2")
        self.assertEqual(res.status, "ok")

    def test_preflight_reads_version_via_fake_binary(self):
        # End-to-end through the real _claude_version + fake claude --version (no mock):
        # FAKE_CLAUDE_VERSION drives an old CLI, proving the probe actually shells out.
        res = self._run("ok", extra_env={"FAKE_CLAUDE_VERSION": "2.1.100"})
        self.assertEqual(res.status, "invalid")
        self.assertIn("2.1.100", res.reason)


class ClaudeVersionParseTest(InvokeTestBase):
    """_claude_version parses the CLI's --version output via the fake binary."""

    def _bin(self, version=None):
        bindir = self.install_fake()
        if version is not None:
            self.addCleanup(os.environ.pop, "FAKE_CLAUDE_VERSION", None)
            os.environ["FAKE_CLAUDE_VERSION"] = version
        return str(bindir / "claude")

    def test_parses_semver_tuple(self):
        self.assertEqual(invoke._claude_version(self._bin("2.1.154")), (2, 1, 154))

    def test_unparseable_returns_none(self):
        self.assertIsNone(invoke._claude_version(self._bin("no-version-here")))

    def test_missing_binary_returns_none(self):
        self.assertIsNone(invoke._claude_version(str(Path(self.tmp) / "nope" / "claude")))


if __name__ == "__main__":
    unittest.main()
