"""Registry interpretation and the configuration receipt consumed by callers."""

import json
import os
import re
from pathlib import Path

import pytest
from gauntlet import config as resolver

ROOT = Path(__file__).resolve().parents[1]

INTERACTIVE_ECHO = {
    "model_tier": {"value": "optimized", "source": "fixed"},
    "pr_comment_cap": {"value": "null", "source": "default"},
    "delivery_tier": {"value": "all", "source": "default"},
}
HEADLESS_ECHO = {
    "model_tier": {"value": "optimized", "source": "default"},
    "delivery": {"value": "markdown", "source": "default"},
    "post_mode": {"value": "dry-run", "source": "default"},
    "pr_comment_cap": {"value": "6", "source": "default"},
    "delivery_tier": {"value": "all", "source": "default"},
    "draft_policy": {"value": "review", "source": "default"},
    "reviewed_policy": {"value": "full", "source": "default"},
    "pr_not_found_policy": {"value": "error", "source": "default"},
    "trivial_scope": {"value": "full", "source": "default"},
}
HEADLESS_RESOLVED = {
    "model_tier": "optimized",
    "delivery": ["markdown"],
    "post_mode": "dry-run",
    "draft_policy": "review",
    "reviewed_policy": "full",
    "pr_not_found_policy": "error",
}


@pytest.fixture
def config_cli(invoke, monkeypatch):
    def call(arguments=(), cwd=ROOT, **values):
        for name in list(resolver.os.environ):
            if name.startswith("CODE_GAUNTLET_"):
                monkeypatch.delenv(name)
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return invoke("resolve_config", list(arguments), cwd)

    return call


ENUM_RULE = {"kind": "enum", "values": ["Chat"]}
CSV_RULE = {"kind": "csv_subset", "values": ["chat", "markdown"]}
POSITIVE_RULE = {"kind": "positive_digits"}
NULLABLE_RULE = {"kind": "digits_or_null"}
_RULE_ROWS = [
    ("enum-exact", ENUM_RULE, "Chat", "headless", True),
    ("enum-case", ENUM_RULE, "chat", "headless", False),
    ("csv-subset", CSV_RULE, "chat,markdown", "headless", True),
    ("csv-duplicate", CSV_RULE, "chat,chat", "headless", False),
    ("positive-one", POSITIVE_RULE, "1", "headless", True),
    ("positive-zero", POSITIVE_RULE, "0", "headless", False),
    ("digits-zero", NULLABLE_RULE, "0", "interactive", True),
    ("digits-null", NULLABLE_RULE, "null", "interactive", True),
    ("digits-unsafe", NULLABLE_RULE, "9007199254740992", "interactive", False),
]


@pytest.mark.parametrize(
    ("rule", "value", "mode", "expected"),
    [pytest.param(*row, id=name) for name, *row in _RULE_ROWS],
)
def test_rules_are_exact_and_fail_closed(rule, value, mode, expected):
    assert resolver.matches_rule(rule, value, mode) is expected


_DELIVERY_ROWS = [
    ("next-heading", "chat,pr_comments\n## Ignore\nmarkdown", "chat,pr_comments"),
    ("backtick-fence", "```yaml\nchat\n```", None),
    ("multiline-comment", "<!-- chat,\npr_comments -->\nmarkdown", "markdown"),
    ("prose", "prose about chat", None),
]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("## Default Delivery\n" + body, expected, id=name)
        for name, body, expected in _DELIVERY_ROWS
    ]
    + [
        pytest.param(
            "prefix\r\n## Default Delivery\r\n\r\n  chat  \r\n",
            "chat",
            id="crlf-preamble-indented-value",
        ),
    ],
)
def test_default_delivery_parser(source, expected):
    assert resolver.parse_default_delivery(source) == expected


@pytest.mark.parametrize(
    ("mode", "environ", "review", "key", "expected"),
    [
        pytest.param(
            "headless",
            {"CODE_GAUNTLET_DELIVERY": "chat"},
            "## Default Delivery\nchat,pr_comments\n",
            "delivery",
            {"value": "chat", "source": "env"},
            id="env-over-review",
        ),
        pytest.param(
            "interactive",
            {"CODE_GAUNTLET_MODEL_TIER": "optimized"},
            None,
            "model_tier",
            {"value": "optimized", "source": "fixed"},
            id="validated-fixed-model",
        ),
    ],
)
def test_valid_pins_keep_their_allowed_source(mode, environ, review, key, expected):
    assert resolver.resolve(mode, environ, review, "pr")["configEcho"][key] == expected


@pytest.mark.parametrize("source", ["env", "review_md"])
def test_local_delivery_rejects_pr_comments_from_env_or_review(source):
    environ = {"CODE_GAUNTLET_DELIVERY": "chat,pr_comments"} if source == "env" else {}
    review = "## Default Delivery\nchat,pr_comments" if source == "review_md" else None
    name = "CODE_GAUNTLET_DELIVERY" if source == "env" else "REVIEW.md default_delivery"
    with pytest.raises(resolver.ResolverError) as raised:
        resolver.resolve("headless", environ, review, "local")
    assert (
        str(raised.value)
        == f"HEADLESS CONFIG ERROR: {name}=chat,pr_comments not in {{chat,markdown}}"
    )


@pytest.mark.parametrize(
    ("target", "source"),
    [("pr", "env"), (None, "review_md")],
    ids=["pr-env", "targetless-review"],
)
def test_pr_and_unset_targets_accept_pr_comments(target, source):
    environ = {"CODE_GAUNTLET_DELIVERY": "chat,pr_comments"} if source == "env" else {}
    review = "## Default Delivery\nchat,pr_comments" if source == "review_md" else None
    result = resolver.resolve("headless", environ, review, target)
    assert result["configEcho"]["delivery"] == {
        "value": "chat,pr_comments",
        "source": source,
    }


_INVALID_PIN_ROWS = [
    ("model-enum", "headless", "MODEL_TIER", "bad", "{optimized}"),
    ("delivery-csv", "headless", "DELIVERY", "bad", "{chat,pr_comments,markdown}"),
    ("positive-cap", "headless", "PR_COMMENT_CAP", "0", "{positive integer}"),
    (
        "interactive-cap",
        "interactive",
        "PR_COMMENT_CAP",
        "not-a-cap",
        "{digits or null}",
    ),
    (
        "interactive-model-standard",
        "interactive",
        "MODEL_TIER",
        "standard",
        "{optimized}",
    ),
]


@pytest.mark.parametrize(
    ("mode", "variable", "value", "allowed"),
    [
        pytest.param(mode, "CODE_GAUNTLET_" + variable, value, allowed, id=name)
        for name, mode, variable, value, allowed in _INVALID_PIN_ROWS
    ],
)
def test_invalid_pins_are_exact_and_stdout_is_empty(
    mode, variable, value, allowed, config_cli
):
    values = {
        variable: value,
        "CODE_GAUNTLET_HEADLESS": "1" if mode == "headless" else "0",
    }
    result = config_cli(["--target", "pr"], **values)
    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr.decode()
        == f"HEADLESS CONFIG ERROR: {variable}={value} not in {allowed}\n"
    )


def test_control_values_are_json_encoded_on_the_error_line(config_cli):
    result = config_cli(
        ["--target", "pr"],
        CODE_GAUNTLET_HEADLESS="1",
        CODE_GAUNTLET_DELIVERY="bad\nvalue",
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.decode() == (
        'HEADLESS CONFIG ERROR: CODE_GAUNTLET_DELIVERY="bad\\nvalue" '
        "not in {chat,pr_comments,markdown}\n"
    )


@pytest.mark.parametrize("mode", ["interactive", "headless"])
def test_cli_shape_identity_and_stderr_block(config_cli, mode):
    arguments = ["--target", "pr"] if mode == "headless" else []
    result = config_cli(
        arguments, CODE_GAUNTLET_HEADLESS="1" if mode == "headless" else "0"
    )
    assert result.returncode == 0
    assert result.stdout.endswith(b"\n")
    payload = json.loads(result.stdout)
    manifest = json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    # The manifest is independent of the bundle the resolver reads.
    identity = {
        "pipeline_version": manifest["version"],
        "plugin_root": ROOT.as_posix(),
        "repo_root": ROOT.as_posix(),
        "host_platform": "windows" if os.name == "nt" else "posix",
    }
    echo = HEADLESS_ECHO if mode == "headless" else INTERACTIVE_ECHO
    resolved = HEADLESS_RESOLVED if mode == "headless" else {"model_tier": "optimized"}
    prefix = (
        "Headless config:\n"
        "  model_tier=optimized (default)\n"
        "  delivery=markdown (default)\n"
        "  post_mode=dry-run (default)\n"
        "  pr_comment_cap=6 (default)\n"
        "  delivery_tier=all (default)\n"
        "  draft_policy=review (default)\n"
        "  reviewed_policy=full (default)\n"
        "  pr_not_found_policy=error (default)\n"
        "  trivial_scope=full (default)\n"
        if mode == "headless"
        else "Resolved config:\n"
        "  model_tier=optimized (fixed)\n"
        "  pr_comment_cap=null (default)\n"
        "  delivery_tier=all (default)\n"
    )
    block = (
        prefix + f"  pipeline_version={manifest['version']} (bundle)\n"
        f"  plugin_root={ROOT.as_posix()} (resolved)"
    )
    expected = {
        "mode": mode,
        "target": "pr" if mode == "headless" else None,
        "block": block,
        "waist": {"configEcho": echo},
        "resolved": resolved,
        "identity": identity,
    }
    assert payload == expected
    assert (
        result.stdout.decode()
        == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
    )
    assert result.stderr.decode() == block + "\n"


@pytest.mark.parametrize("case", ["usage", "nonrepo"])
def test_usage_and_setup_fail_without_process_stream_noise(case, config_cli, tmp_path):
    result = config_cli(
        ["--target", "wrong"] if case == "usage" else ["--cwd", str(tmp_path)]
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.count(b"\n") == 1
    assert result.stderr.startswith(b"resolve_config: ")


def test_plugin_root_must_match(config_cli, tmp_path):
    result = config_cli(["--plugin-root", str(tmp_path)])
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        b"",
        b"resolve_config: plugin root mismatch\n",
    )


@pytest.mark.parametrize("name, expected", [("plugin$cache", 2), ("o'brien plugin", 0)])
def test_resolver_checks_resolved_plugin_root(
    name, expected, config_cli, tmp_path, monkeypatch, symlink_or_skip
):
    root = tmp_path / name
    bundle = root / "workflows/pipeline.js"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("const PIPELINE_VERSION = 'test';\n", encoding="utf-8")
    alias = tmp_path / "plugin"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(resolver, "PLUGIN_ROOT", str(alias))
    result = config_cli()
    assert result.returncode == expected, result.stderr
    if expected == 2:
        assert result.stdout == b""
        assert result.stderr == (
            b"resolve_config: resolved plugin root contains shell-active or control "
            b"characters; install the plugin at a shell-safe path\n"
        )
    else:
        assert json.loads(result.stdout)["identity"]["plugin_root"] == root.as_posix()


def test_missing_bundle_version_is_setup_failure(tmp_path):
    bundle = tmp_path / "workflows" / "pipeline.js"
    bundle.parent.mkdir()
    bundle.write_text("const not_version = 'x';", encoding="utf-8")
    with pytest.raises(
        resolver.ResolverSetupError,
        match="PIPELINE_VERSION not found in workflow bundle",
    ):
        resolver.read_pipeline_version(str(tmp_path))


def test_git_root_probe_oserror_is_setup_failure(config_cli, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("probe denied")

    monkeypatch.setattr(resolver.proc, "run", fail)
    result = config_cli()
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        b"",
        b"resolve_config: git repository probe failed: probe denied\n",
    )


@pytest.mark.parametrize(
    ("mode", "label", "echo"),
    [
        ("interactive", "Interactive", INTERACTIVE_ECHO),
        ("headless", "Headless", HEADLESS_ECHO),
    ],
)
def test_generated_receipts_are_resolver_fixtures(mode, label, echo):
    skill = (ROOT / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    match = re.search(
        rf"\*\*{label} receipt:\*\*\n\n```json\n(.*?)\n```", skill, re.DOTALL
    )
    assert match is not None
    assert json.loads(match[1]) == echo
    assert resolver.resolve(mode, {}, None, "pr")["configEcho"] == echo


def assert_resolved_mentions(root):
    allowed = {
        "model_tier",
        "delivery",
        "post_mode",
        "draft_policy",
        "reviewed_policy",
        "pr_not_found_policy",
    }
    for path in root.rglob("*.md"):
        for match in re.finditer(
            r"(?:configResult\.)?resolved\.([a-z_]+)", path.read_text(encoding="utf-8")
        ):
            assert match[1] in allowed, f"{path}: {match[1]}"


def test_caller_resolved_mentions(tmp_path):
    assert_resolved_mentions(ROOT / "skills/code-gauntlet")
    (tmp_path / "invalid.md").write_text(
        "Use `resolved.pr_comment_cap`.\n", encoding="utf-8"
    )
    with pytest.raises(AssertionError, match="pr_comment_cap"):
        assert_resolved_mentions(tmp_path)


def test_caller_review_scaffold():
    specification = (
        ROOT / "skills/code-gauntlet/references/review-md-spec.md"
    ).read_text(encoding="utf-8")
    match = re.search(
        r"### Root REVIEW\.md template\n\n(````markdown\n.*?\n````)",
        specification,
        re.DOTALL,
    )
    assert match is not None
    result = resolver.resolve("headless", {}, match[1], "pr")
    assert result["configEcho"]["delivery"] == {
        "value": "markdown",
        "source": "default",
    }
