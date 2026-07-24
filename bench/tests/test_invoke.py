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

    def _run(self, mode, timeout_s=30, extra_env=None, tool="deep-review-v3",
             child_model="inherit", child_auth="api"):
        bindir = self.install_fake()
        overrides = {
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_CLAUDE_MODE": mode,
        }
        if extra_env:
            overrides.update(extra_env)
        with patched_environ(**overrides):
            return invoke_review(
                self.worktree, PR, self.run_dir, timeout_s=timeout_s, tool=tool,
                child_model=child_model, child_auth=child_auth,
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
        self.assertEqual(env["CODE_GAUNTLET_HEADLESS"], "1")
        self.assertEqual(env["CODE_GAUNTLET_MODEL_TIER"], "optimized")
        self.assertEqual(env["CODE_GAUNTLET_DELIVERY"], "pr_comments,markdown")
        self.assertEqual(env["CODE_GAUNTLET_POST_MODE"], "dry-run")
        self.assertEqual(env["CODE_GAUNTLET_PR_COMMENT_CAP"], "25")
        self.assertEqual(env["CODE_GAUNTLET_DRAFT_POLICY"], "review")
        self.assertEqual(env["CODE_GAUNTLET_REVIEWED_POLICY"], "full")
        self.assertEqual(env["CODE_GAUNTLET_PR_NOT_FOUND_POLICY"], "error")
        self.assertEqual(env["CODE_GAUNTLET_TRIVIAL_SCOPE"], "full")

    def test_output_dir_and_gh_repo(self):
        env = build_env(PR, self.run_dir, {})
        self.assertEqual(env["CODE_GAUNTLET_OUTPUT_DIR"], str(self.run_dir / "output"))
        self.assertEqual(env["GH_REPO"], "octo/widget")

    def test_gh_repo_from_url_fallback(self):
        pr = {"url": "https://github.com/acme/thing/pull/9", "pr_number": 9}
        env = build_env(pr, self.run_dir, {})
        self.assertEqual(env["GH_REPO"], "acme/thing")

    def test_uncaps_background_wait_ceiling(self):
        # The child must carry CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="0" so the CLI does not
        # terminate a still-running Phase 3 Workflow at its default 600s background-wait cap
        # (which sank smoke-20260719-190902-a14b4cc). The per-PR watchdog bounds total time.
        env = build_env(PR, self.run_dir, {})
        self.assertEqual(env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")

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


# ------------------------------------------------------------------ child auth mode


class BuildEnvChildAuthTest(InvokeTestBase):
    """build_env's ``child_auth`` mode is the single auth branch point for the child.

    ``api`` (the default) must stay behaviourally identical to the pre-mode harness --
    every recorded run and every paired leg of record depends on that. ``subscription``
    must actually reach the subscription: the documented precedence chain puts cloud
    providers, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY and an apiKeyHelper all ABOVE
    CLAUDE_CODE_OAUTH_TOKEN, and the child inherits the full parent env -- so any one of
    them left in place silently bills the API key while the operator believes the run is
    on subscription capacity. These tests pin the strip and the token precedence.
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

    def test_mode_constants(self):
        self.assertEqual(invoke.CHILD_AUTH_MODES, ("api", "subscription"))
        self.assertEqual(invoke.OAUTH_TOKEN_VAR, "CLAUDE_CODE_OAUTH_TOKEN")
        self.assertEqual(
            invoke._OUTRANKING_CREDENTIAL_VARS,
            (
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_API_KEY",
            ),
        )

    def test_api_is_the_default_mode(self):
        self._use_env_file("ANTHROPIC_API_KEY=sk-from-dotenv\n")
        base = {"PATH": "/x/y", "HOME": str(self.tmp)}
        self.assertEqual(
            build_env(PR, self.run_dir, base),
            build_env(PR, self.run_dir, base, child_auth="api"),
        )

    def test_api_mode_keeps_ambient_oauth_token_and_injects_dotenv_key(self):
        # api mode strips nothing: an operator with a subscription token exported for
        # their own shell must still get exactly today's child env.
        self._use_env_file("ANTHROPIC_API_KEY=sk-from-dotenv\n")
        env = build_env(
            PR, self.run_dir, {"CLAUDE_CODE_OAUTH_TOKEN": "oat-ambient"},
            child_auth="api",
        )
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oat-ambient")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-from-dotenv")

    def test_subscription_does_not_inject_dotenv_api_key(self):
        self._use_env_file(
            "ANTHROPIC_API_KEY=sk-from-dotenv\nCLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n"
        )
        env = build_env(PR, self.run_dir, {}, child_auth="subscription")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_subscription_strips_ambient_anthropic_api_key(self):
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        env = build_env(
            PR, self.run_dir, {"ANTHROPIC_API_KEY": "sk-ambient"},
            child_auth="subscription",
        )
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_subscription_strips_ambient_anthropic_auth_token(self):
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        env = build_env(
            PR, self.run_dir, {"ANTHROPIC_AUTH_TOKEN": "at-ambient"},
            child_auth="subscription",
        )
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_subscription_strips_bedrock_switch(self):
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        env = build_env(
            PR, self.run_dir, {"CLAUDE_CODE_USE_BEDROCK": "1"},
            child_auth="subscription",
        )
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)

    def test_subscription_strips_vertex_switch(self):
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        env = build_env(
            PR, self.run_dir, {"CLAUDE_CODE_USE_VERTEX": "1"},
            child_auth="subscription",
        )
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", env)

    def test_subscription_token_from_dotenv(self):
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        env = build_env(PR, self.run_dir, {}, child_auth="subscription")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oat-dotenv")

    def test_subscription_token_from_ambient_when_dotenv_has_none(self):
        self._use_missing_env()
        env = build_env(
            PR, self.run_dir, {"CLAUDE_CODE_OAUTH_TOKEN": "oat-ambient"},
            child_auth="subscription",
        )
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oat-ambient")

    def test_subscription_dotenv_token_wins_over_ambient(self):
        # One rule for bench/.env, mirroring ANTHROPIC_API_KEY: the file is authoritative.
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        env = build_env(
            PR, self.run_dir, {"CLAUDE_CODE_OAUTH_TOKEN": "oat-ambient"},
            child_auth="subscription",
        )
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oat-dotenv")

    def test_subscription_without_any_token_raises(self):
        self._use_missing_env()
        with self.assertRaises(RuntimeError) as ctx:
            build_env(PR, self.run_dir, {}, child_auth="subscription")
        message = str(ctx.exception)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", message)
        self.assertIn("claude setup-token", message)

    def test_error_message_never_carries_a_credential(self):
        # An unknown mode must not turn the base env into a leak channel.
        self._use_env_file("ANTHROPIC_API_KEY=sk-from-dotenv\n")
        with self.assertRaises(ValueError) as ctx:
            build_env(
                PR, self.run_dir, {"ANTHROPIC_API_KEY": "sk-ambient"},
                child_auth="oauth",
            )
        message = str(ctx.exception)
        self.assertIn("oauth", message)
        self.assertIn("subscription", message)
        self.assertNotIn("sk-ambient", message)
        self.assertNotIn("sk-from-dotenv", message)

    def test_unknown_mode_raises_value_error(self):
        with self.assertRaises(ValueError):
            build_env(PR, self.run_dir, {}, child_auth="")

    def test_subscription_keeps_the_isolated_home_and_gh_auth(self):
        # The auth mode changes credentials only -- the S7 isolation and the gh
        # re-pointing must be identical in both modes.
        self._use_env_file("CLAUDE_CODE_OAUTH_TOKEN=oat-dotenv\n")
        home = Path(self.tmp) / "realhome"
        (home / ".config" / "gh").mkdir(parents=True)
        env = build_env(
            PR, self.run_dir, {"HOME": str(home), "GH_TOKEN": "ght"},
            child_auth="subscription",
        )
        workspace = self.run_dir.resolve().parent.parent
        self.assertEqual(env["HOME"], str(workspace / "claude-home"))
        self.assertEqual(env["GH_CONFIG_DIR"], str(home / ".config" / "gh"))
        self.assertEqual(env["GH_TOKEN"], "ght")


# ------------------------------------------------------------------- claude home


class ResolveClaudeHomeTest(InvokeTestBase):
    """The isolated claude home has exactly one derivation.

    ``check_prereqs`` must inspect the very dir ``build_env`` will hand the child --
    an apiKeyHelper in a settings file the preflight never looked at would outrank the
    OAuth token at runtime. So the public helper and ``_claude_home`` share one rule,
    and ``_claude_home``'s existing behaviour is unchanged.
    """

    def test_workspace_relative_default(self):
        workspace = Path(self.tmp) / "workspace"
        self.assertEqual(
            invoke.resolve_claude_home(workspace, {}), workspace / "claude-home"
        )

    def test_bench_claude_home_override_wins(self):
        workspace = Path(self.tmp) / "workspace"
        override = Path(self.tmp) / "elsewhere"
        self.assertEqual(
            invoke.resolve_claude_home(workspace, {"BENCH_CLAUDE_HOME": str(override)}),
            override,
        )

    def test_claude_home_still_derives_from_run_dir(self):
        workspace = self.run_dir.resolve().parent.parent
        self.assertEqual(
            invoke._claude_home(self.run_dir, {}), workspace / "claude-home"
        )

    def test_claude_home_still_honors_the_override(self):
        override = Path(self.tmp) / "elsewhere"
        self.assertEqual(
            invoke._claude_home(self.run_dir, {"BENCH_CLAUDE_HOME": str(override)}),
            override,
        )


class ApiKeyHelperSourcesTest(InvokeTestBase):
    """An ``apiKeyHelper`` in the isolated config outranks CLAUDE_CODE_OAUTH_TOKEN.

    Unlike an env var it cannot be stripped from the child env, so preflight has to
    refuse the run instead. The inspection runs on every prereq check, including runs
    with no claude home at all, so a missing dir, an unreadable file or corrupt JSON
    must read as "no helper" rather than crashing the harness.
    """

    def _config_dir(self):
        path = Path(self.tmp) / "home" / "config"
        path.mkdir(parents=True)
        return path

    def test_missing_dir_is_empty(self):
        self.assertEqual(
            invoke.api_key_helper_sources(Path(self.tmp) / "no-such-home"), []
        )

    def test_settings_without_the_key_is_empty(self):
        cfg = self._config_dir()
        (cfg / "settings.json").write_text(json.dumps({"model": "opus"}))
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [])

    def test_empty_helper_value_is_empty(self):
        cfg = self._config_dir()
        (cfg / "settings.json").write_text(json.dumps({"apiKeyHelper": "   "}))
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [])

    def test_corrupt_json_is_empty(self):
        cfg = self._config_dir()
        (cfg / "settings.json").write_text("{not json")
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [])

    def test_non_object_json_is_empty(self):
        cfg = self._config_dir()
        (cfg / "settings.json").write_text(json.dumps(["apiKeyHelper"]))
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [])

    def test_unreadable_file_is_empty(self):
        cfg = self._config_dir()
        (cfg / "settings.json").mkdir()  # a dir where a file is expected: OSError on read
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [])

    def test_finds_helper_in_settings_json(self):
        cfg = self._config_dir()
        path = cfg / "settings.json"
        path.write_text(json.dumps({"apiKeyHelper": "/bin/echo sk-x"}))
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [str(path)])

    def test_finds_helper_in_settings_local_json(self):
        cfg = self._config_dir()
        path = cfg / "settings.local.json"
        path.write_text(json.dumps({"apiKeyHelper": "/bin/echo sk-x"}))
        self.assertEqual(invoke.api_key_helper_sources(cfg.parent), [str(path)])

    def test_both_files_reported_sorted(self):
        cfg = self._config_dir()
        (cfg / "settings.json").write_text(json.dumps({"apiKeyHelper": "a"}))
        (cfg / "settings.local.json").write_text(json.dumps({"apiKeyHelper": "b"}))
        self.assertEqual(
            invoke.api_key_helper_sources(cfg.parent),
            sorted([str(cfg / "settings.json"), str(cfg / "settings.local.json")]),
        )

    def test_dot_claude_under_the_home_is_also_inspected(self):
        # BENCH_CLAUDE_HOME can point at a home that is not bench-created, and the CLI
        # reads user settings from the HOME-relative .claude dir when CLAUDE_CONFIG_DIR
        # is not what it resolves. Scanning both is fail-closed: the only outcome is a
        # refusal, and the bench-created home has neither file.
        home = Path(self.tmp) / "home"
        dot_claude = home / ".claude"
        dot_claude.mkdir(parents=True)
        path = dot_claude / "settings.json"
        path.write_text(json.dumps({"apiKeyHelper": "/bin/echo sk-x"}))
        self.assertEqual(invoke.api_key_helper_sources(home), [str(path)])

    def test_helpers_from_both_dirs_are_reported_together(self):
        cfg = self._config_dir()
        home = cfg.parent
        (home / ".claude").mkdir()
        config_path = cfg / "settings.json"
        home_path = home / ".claude" / "settings.local.json"
        config_path.write_text(json.dumps({"apiKeyHelper": "a"}))
        home_path.write_text(json.dumps({"apiKeyHelper": "b"}))
        self.assertEqual(
            invoke.api_key_helper_sources(home), sorted([str(config_path), str(home_path)])
        )


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

    def test_invokes_namespace_qualified_skill_command(self):
        # Regression: the child must be invoked with the NAMESPACE-QUALIFIED slash command
        # ``/code-gauntlet:code-gauntlet <n>``, not the flat ``/code-gauntlet <n>``. In the pinned
        # isolated --plugin-dir context the flat alias is not reliably registered and
        # resolves to "Unknown command" (num_turns 0), which sank real smoke runs pre-rename
        # children. See invoke.SKILL_COMMAND and artifact 33 (P2b).
        argv_file = Path(self.tmp) / "argv.txt"
        res = self._run("ok", extra_env={"FAKE_CLAUDE_ARGV_FILE": str(argv_file)})
        self.assertEqual(res.status, "ok")
        self.assertTrue(argv_file.exists())
        argv = argv_file.read_text().splitlines()
        self.assertIn("-p", argv)
        prompt = argv[argv.index("-p") + 1]
        self.assertEqual(prompt, "/code-gauntlet:code-gauntlet {}".format(PR["pr_number"]))
        # Guard the exact regression: the bare/flat command must not reappear.
        self.assertNotEqual(prompt, "/code-gauntlet {}".format(PR["pr_number"]))

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

    def test_backgrounded_workflow_is_distinct_reason(self):
        # A child whose Phase 3 Workflow ran detached and got killed at the CLI's
        # background-wait ceiling (echo + payload absent for THAT reason) must be labeled
        # workflow_backgrounded, NOT conflated with a genuine config_echo_mismatch. The
        # "Background tasks still running after ...; terminating" notice is the signature.
        res = self._run("bg_killed")
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "workflow_backgrounded")
        self.assertNotEqual(res.reason, "config_echo_mismatch")

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


class PluginMutationGuardTest(InvokeTestBase):
    """invoke.py's plugin-repo integrity guard. A child that writes into REPO_ROOT
    mid-run (self-healing the plugin) contaminates the measurement: the PR is marked
    invalid ('plugin_mutated_by_child') and the repo is reset. Pre-existing local edits
    (captured as the baseline) and the controller-owned experiments.jsonl are exempt.
    Each test points invoke.REPO_ROOT at a throwaway git repo so the real plugin is
    never touched."""

    def _init_repo(self, files):
        import subprocess
        root = Path(self.tmp) / "plugin"
        root.mkdir()
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "baseline"]):
            subprocess.run(cmd, cwd=str(root), check=True, capture_output=True, env=env)
        return root

    def _porcelain(self, root):
        import subprocess
        return subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True
        ).stdout.strip()

    def _run_mutating(self, root, mutate_path):
        with patch.object(invoke, "REPO_ROOT", root):
            return self._run("mutate_repo", extra_env={"FAKE_CLAUDE_MUTATE_PATH": str(mutate_path)})

    def test_untracked_file_mutation_marks_invalid_and_removes_file(self):
        root = self._init_repo({"README.md": "base\n"})
        injected = root / "workflows" / "src" / "injected.js"
        res = self._run_mutating(root, injected)
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "plugin_mutated_by_child")
        self.assertFalse(injected.exists(), "untracked child file removed on reset")
        self.assertEqual(self._porcelain(root), "", "plugin repo clean after reset")

    def test_tracked_file_modification_marks_invalid_and_reverts(self):
        root = self._init_repo({"workflows/src/filterFindings.js": "ORIGINAL\n"})
        target = root / "workflows" / "src" / "filterFindings.js"
        res = self._run_mutating(root, target)
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "plugin_mutated_by_child")
        self.assertEqual(target.read_text(), "ORIGINAL\n", "tracked edit reverted on reset")
        self.assertEqual(self._porcelain(root), "")

    def test_controller_owned_experiments_jsonl_is_not_flagged(self):
        root = self._init_repo({"bench/experiments.jsonl": "{}\n"})
        target = root / "bench" / "experiments.jsonl"
        res = self._run_mutating(root, target)
        self.assertEqual(res.status, "ok", "a controller-owned change is not child contamination")
        self.assertNotEqual(self._porcelain(root), "", "experiments.jsonl left modified (controller owns it)")

    def test_controller_owned_report_html_is_not_flagged(self):
        # The live dashboard (regenerated from the ledger by bench/report.py, typically run
        # mid-run to watch progress) is a tracked file stamped with today's date + sha, so any
        # regeneration dirties it. It is operator-owned, NOT child contamination: a regeneration
        # while a review child is in-flight must not invalidate that PR or reset the dashboard.
        root = self._init_repo({"bench/report.html": "<html>old</html>\n"})
        target = root / "bench" / "report.html"
        res = self._run_mutating(root, target)
        self.assertEqual(res.status, "ok", "a controller-owned dashboard regeneration is not child contamination")
        self.assertNotEqual(self._porcelain(root), "", "report.html left modified (operator owns it)")

    def test_preexisting_local_edit_is_not_flagged_or_reset(self):
        # A dirty file that predates the child (the run's baseline) must survive untouched
        # — the guard flags DELTA, not absolute dirtiness.
        root = self._init_repo({"workflows/src/stages.js": "COMMITTED\n"})
        preexisting = root / "workflows" / "src" / "stages.js"
        preexisting.write_text("LOCAL WIP\n")  # dirty BEFORE the child runs
        injected = root / "agents" / "injected.md"
        res = self._run_mutating(root, injected)
        # The child's NEW file is flagged + removed; the pre-existing edit is preserved.
        self.assertEqual(res.status, "invalid")
        self.assertEqual(res.reason, "plugin_mutated_by_child")
        self.assertFalse(injected.exists())
        self.assertEqual(preexisting.read_text(), "LOCAL WIP\n", "pre-existing local edit untouched")

    def test_clean_run_is_not_flagged(self):
        root = self._init_repo({"README.md": "base\n"})
        with patch.object(invoke, "REPO_ROOT", root):
            res = self._run("ok")  # no mutation
        self.assertEqual(res.status, "ok")
        self.assertEqual(self._porcelain(root), "", "clean repo stays clean")


class ChildModelCommandTest(InvokeTestBase):
    """child_model appends ``--model <m>`` to the child command unless inheriting."""

    def _argv(self, res_env):
        argv_file = Path(self.tmp) / "argv.txt"
        # The fake records its argv (minus argv[0]) to FAKE_CLAUDE_ARGV_FILE on the main
        # invocation (not the --version probe), one arg per line.
        res_env["FAKE_CLAUDE_ARGV_FILE"] = str(argv_file)
        return argv_file

    def test_v3_command_carries_model_sonnet(self):
        env = {}
        argv_file = self._argv(env)
        res = self._run("ok", tool="deep-review-v3", child_model="sonnet", extra_env=env)
        self.assertEqual(res.status, "ok")
        argv = argv_file.read_text().splitlines()
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_opus_child_model_carried_verbatim(self):
        env = {}
        argv_file = self._argv(env)
        res = self._run("ok", child_model="opus", extra_env=env)
        self.assertEqual(res.status, "ok")
        argv = argv_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_inherit_omits_model_flag(self):
        env = {}
        argv_file = self._argv(env)
        res = self._run("ok", child_model="inherit", extra_env=env)
        self.assertEqual(res.status, "ok")
        self.assertNotIn("--model", argv_file.read_text().splitlines())


class InvokeReviewChildAuthTest(InvokeTestBase):
    """invoke_review is a pass-through for the auth mode.

    build_env owns the whole credential decision; invoke_review must not re-derive or
    default it, or a subscription run would silently fall back to the metered key.
    """

    @contextlib.contextmanager
    def _build_env_spy(self):
        seen = {}
        real = invoke.build_env

        def spy(pr, run_dir, base_env, child_auth="api"):
            seen["child_auth"] = child_auth
            # Run the real assembly in api mode: this test is about the plumbing, and
            # no live subscription token exists here.
            return real(pr, run_dir, base_env)

        with patch.object(invoke, "build_env", spy):
            yield seen

    def test_forwards_explicit_child_auth(self):
        with self._build_env_spy() as seen:
            res = self._run("ok", child_auth="subscription")
        self.assertEqual(res.status, "ok")
        self.assertEqual(seen["child_auth"], "subscription")

    def test_defaults_to_api(self):
        with self._build_env_spy() as seen:
            bindir = self.install_fake()
            overrides = {
                "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_CLAUDE_MODE": "ok",
            }
            with patched_environ(**overrides):
                res = invoke_review(self.worktree, PR, self.run_dir, timeout_s=30)
        self.assertEqual(res.status, "ok")
        self.assertEqual(seen["child_auth"], "api")


class ChildProcessCredentialEnvTest(InvokeTestBase):
    """What the CHILD process actually received, across the subprocess boundary.

    Every other auth test inspects ``build_env``'s return value, which proves the dict is
    right but not that the dict is what ``claude`` runs with. This mode's whole promise is
    that the metered key never reaches the child, so the fake records its own view of the
    credential vars (presence only -- never values) and the assertions read that.
    """

    POLLUTED = {
        "ANTHROPIC_API_KEY": "sk-ambient-should-not-arrive",
        "ANTHROPIC_AUTH_TOKEN": "at-ambient-should-not-arrive",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "oat-ambient",
    }

    def setUp(self):
        super().setUp()
        # No bench/.env: the ambient values above are then the only credential source, so
        # the assertions describe exactly what the mode did with them.
        saved = invoke.ENV_PATH
        invoke.ENV_PATH = Path(self.tmp) / "absent.env"
        self.addCleanup(setattr, invoke, "ENV_PATH", saved)
        self.env_file = Path(self.tmp) / "child-credentials.json"

    def _child_saw(self, child_auth):
        result = self._run(
            "ok",
            extra_env={**self.POLLUTED, "FAKE_CLAUDE_ENV_FILE": str(self.env_file)},
            child_auth=child_auth,
        )
        self.assertEqual(result.status, "ok", result.reason)
        return json.loads(self.env_file.read_text())

    # Spelled out rather than read from invoke._OUTRANKING_CREDENTIAL_VARS: a test that
    # iterates the list it is checking passes vacuously the moment that list is emptied,
    # which is exactly the regression it exists to catch.
    MUST_NOT_REACH_CHILD = (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    )

    def test_subscription_child_receives_only_the_oauth_token(self):
        saw = self._child_saw("subscription")
        self.assertTrue(saw["CLAUDE_CODE_OAUTH_TOKEN"])
        for name in self.MUST_NOT_REACH_CHILD:
            self.assertFalse(saw[name], "{} reached the child".format(name))

    def test_the_strip_list_covers_every_var_this_test_names(self):
        # Keeps the hardcoded list above honest in the other direction: if the chain grows
        # a source, the production constant and this test must both learn about it.
        self.assertEqual(
            sorted(invoke._OUTRANKING_CREDENTIAL_VARS), sorted(self.MUST_NOT_REACH_CHILD)
        )

    def test_api_child_receives_the_ambient_key(self):
        # The mirror image, from the same harness: api mode passes the key through, so a
        # false pass in the test above would have to survive this one too.
        saw = self._child_saw("api")
        self.assertTrue(saw["ANTHROPIC_API_KEY"])


if __name__ == "__main__":
    unittest.main()
