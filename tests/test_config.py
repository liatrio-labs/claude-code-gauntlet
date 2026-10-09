"""Registry interpretation and the configuration receipt consumed by callers."""

import json
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


@pytest.mark.parametrize(
    ("rule", "value", "mode", "expected"),
    [
        pytest.param(
            {"kind": "enum", "values": ["Chat"]},
            "Chat",
            "headless",
            True,
            id="enum-exact",
        ),
        pytest.param(
            {"kind": "enum", "values": ["Chat"]},
            "chat",
            "headless",
            False,
            id="enum-case",
        ),
        pytest.param(
            {"kind": "csv_subset", "values": ["chat", "markdown"]},
            "chat,markdown",
            "headless",
            True,
            id="csv-subset",
        ),
        pytest.param(
            {"kind": "csv_subset", "values": ["chat", "markdown"]},
            "",
            "headless",
            False,
            id="csv-empty",
        ),
        pytest.param(
            {"kind": "csv_subset", "values": ["chat", "markdown"]},
            "chat,,markdown",
            "headless",
            False,
            id="csv-empty-part",
        ),
        pytest.param(
            {"kind": "csv_subset", "values": ["chat", "markdown"]},
            "chat,chat",
            "headless",
            False,
            id="csv-duplicate",
        ),
        pytest.param(
            {"kind": "csv_subset", "values": ["chat", "markdown"]},
            "chat, markdown",
            "headless",
            False,
            id="csv-untrimmed",
        ),
        pytest.param(
            {"kind": "csv_subset", "values": ["chat", "markdown"]},
            "Chat",
            "headless",
            False,
            id="csv-case",
        ),
        pytest.param(
            {"kind": "positive_digits"}, "1", "headless", True, id="positive-one"
        ),
        pytest.param(
            {"kind": "positive_digits"}, "0", "headless", False, id="positive-zero"
        ),
        pytest.param(
            {"kind": "digits_or_null"}, "0", "interactive", True, id="digits-zero"
        ),
        pytest.param(
            {"kind": "digits_or_null"}, "null", "interactive", True, id="digits-null"
        ),
        pytest.param(
            {"kind": "digits_or_null"},
            "00",
            "interactive",
            False,
            id="digits-zero-padded",
        ),
        pytest.param(
            {"kind": "digits_or_null"}, "01", "interactive", False, id="digits-padded"
        ),
        pytest.param(
            {"kind": "digits_or_null"},
            "9007199254740992",
            "interactive",
            False,
            id="digits-unsafe",
        ),
        pytest.param(
            {"kind": "digits_or_null"},
            "\u0661",
            "interactive",
            False,
            id="digits-nonascii",
        ),
        pytest.param({"kind": "bogus"}, "x", "headless", False, id="unknown-kind"),
        pytest.param(
            {"headless": {"kind": "enum", "values": ["x"]}},
            "x",
            "interactive",
            False,
            id="missing-mode",
        ),
        pytest.param(
            {"kind": "enum", "values": [1]}, 1, "headless", False, id="nonstring-value"
        ),
    ],
)
def test_rules_are_exact_and_fail_closed(rule, value, mode, expected):
    assert resolver.matches_rule(rule, value, mode) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "## Default Delivery\nchat,pr_comments\n## Ignore\nmarkdown",
            "chat,pr_comments",
            id="next-heading",
        ),
        pytest.param(
            "## Default Delivery\nchat\n### Ignore\nmarkdown", "chat", id="subheading"
        ),
        pytest.param(
            "## Default Delivery\nchat\n```yaml\nmarkdown\n```:",
            "chat",
            id="backtick-fence",
        ),
        pytest.param(
            "## Default Delivery\nchat\n~~~\nmarkdown\n~~~", "chat", id="tilde-fence"
        ),
        pytest.param(
            "## Default Delivery\n<!-- chat,\npr_comments -->\nmarkdown",
            "markdown",
            id="multiline-comment",
        ),
        pytest.param(
            "## Default Delivery\n<!-- chat,pr_comments -->", None, id="comment-only"
        ),
        pytest.param("## Default Delivery\nprose about chat", None, id="prose"),
        pytest.param("## Default Delivery\nchat, pr_comments", None, id="spaced-list"),
        pytest.param("## Default Delivery\nChat", None, id="case"),
        pytest.param("## Default Delivery\nchat,,markdown", None, id="empty-item"),
        pytest.param("# Default Delivery\nchat", None, id="wrong-heading"),
        pytest.param(
            "## Default Delivery\n<!--\n# Ignore\n-->\nchat\n",
            None,
            id="root-heading-ends-section",
        ),
        pytest.param(
            "prefix\r\n## Default Delivery\r\n\r\n  chat  \r\n",
            "chat",
            id="crlf-preamble-indented-value",
        ),
        pytest.param("## Default Delivery\rchat\r", "chat", id="bare-cr"),
        pytest.param(
            "## Default Delivery\nchat\u2028markdown\n",
            None,
            id="unicode-line-separator",
        ),
        pytest.param(
            "## Default Delivery\n<!--\n```\n-->\nchat\n",
            None,
            id="comment-wrapped-fence",
        ),
    ],
)
def test_default_delivery_parser(source, expected):
    assert resolver.parse_default_delivery(source) == expected


@pytest.mark.parametrize(
    ("mode", "echo", "resolved"),
    [
        ("interactive", INTERACTIVE_ECHO, {"model_tier": "optimized"}),
        ("headless", HEADLESS_ECHO, HEADLESS_RESOLVED),
    ],
)
def test_empty_environment_defaults_have_one_wire_copy(mode, echo, resolved):
    result = resolver.resolve(mode, {}, None, "pr")
    assert result == {
        "configEcho": echo,
        "waist": {"configEcho": echo},
        "resolved": resolved,
    }
    assert list(result["configEcho"]) == list(echo)


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


@pytest.mark.parametrize("target", ["pr", "mr", None], ids=["pr", "mr", "unset"])
@pytest.mark.parametrize("source", ["env", "review_md"])
def test_pr_mr_and_unset_targets_accept_pr_comments(target, source):
    environ = {"CODE_GAUNTLET_DELIVERY": "chat,pr_comments"} if source == "env" else {}
    review = "## Default Delivery\nchat,pr_comments" if source == "review_md" else None
    result = resolver.resolve("headless", environ, review, target)
    assert result["configEcho"]["delivery"] == {
        "value": "chat,pr_comments",
        "source": source,
    }


@pytest.mark.parametrize(
    ("mode", "variable", "value", "allowed"),
    [
        pytest.param(
            "headless",
            "CODE_GAUNTLET_MODEL_TIER",
            "bad",
            "{optimized}",
            id="model-enum",
        ),
        pytest.param(
            "headless",
            "CODE_GAUNTLET_DELIVERY",
            "bad",
            "{chat,pr_comments,markdown}",
            id="delivery-csv",
        ),
        pytest.param(
            "headless",
            "CODE_GAUNTLET_PR_COMMENT_CAP",
            "0",
            "{positive integer}",
            id="positive-cap",
        ),
        pytest.param(
            "interactive",
            "CODE_GAUNTLET_PR_COMMENT_CAP",
            "not-a-cap",
            "{digits or null}",
            id="interactive-cap",
        ),
        pytest.param(
            "interactive",
            "CODE_GAUNTLET_MODEL_TIER",
            "standard",
            "{optimized}",
            id="interactive-model-standard",
        ),
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


@pytest.mark.parametrize(
    ("value", "spelling"),
    [
        pytest.param("bad\nvalue", '"bad\\nvalue"', id="lf"),
        pytest.param("bad\rvalue", '"bad\\rvalue"', id="cr"),
        pytest.param("bad`value", "bad`value", id="backtick"),
    ],
)
def test_control_values_are_json_encoded_on_the_error_line(value, spelling, config_cli):
    result = config_cli(
        ["--target", "pr"],
        CODE_GAUNTLET_HEADLESS="1",
        CODE_GAUNTLET_DELIVERY=value,
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr.decode()
        == f"HEADLESS CONFIG ERROR: CODE_GAUNTLET_DELIVERY={spelling} not in {{chat,pr_comments,markdown}}\n"
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
    identity = {"pipeline_version": manifest["version"], "plugin_root": str(ROOT)}
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
        prefix
        + f"  pipeline_version={manifest['version']} (bundle)\n  plugin_root={ROOT} (resolved)"
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


@pytest.mark.usefixtures("symlink_or_skip")
def test_plugin_root_must_match_and_matching_symlink_is_allowed(config_cli, tmp_path):
    result = config_cli(["--plugin-root", str(tmp_path)])
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        b"",
        b"resolve_config: plugin root mismatch\n",
    )
    link = tmp_path / "plugin"
    link.symlink_to(ROOT, target_is_directory=True)
    result = config_cli(["--plugin-root", str(link)])
    assert result.returncode == 0
    assert Path(json.loads(result.stdout)["identity"]["plugin_root"]) == ROOT


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


def test_caller_preflight_commands_and_targetless_gate():
    skill = (ROOT / "skills/code-gauntlet/SKILL.md").read_text(encoding="utf-8")
    assert (
        'echo "=== output_dir ==="\n'
        'if ! OUTPUT_DIR=$(python3 "{plugin_root}/scripts/ensure_output_dir.py"); then\n'
        '  echo "output_dir: FAILED"\n  exit 1\nfi\necho "$OUTPUT_DIR"'
    ) in skill
    composite = skill.split('echo "=== config ==="\n', 1)[1].split(
        'echo "=== pr_view ==="', 1
    )[0]
    assert composite == (
        'if ! CONFIG_JSON=$(python3 "{plugin_root}/scripts/resolve_config.py" --target {target_type} --plugin-root "{plugin_root}"); then\n'
        '  echo "config: FAILED"\n  exit 1\nfi\necho "$CONFIG_JSON"\n\n'
    )
    assert "review_md_root" not in skill
    reference = (
        ROOT / "skills/code-gauntlet/references/phase1-preflight.md"
    ).read_text(encoding="utf-8")
    line = next(
        line
        for line in reference.splitlines()
        if line.startswith("> Headless exception")
    )
    assert line == (
        "> Headless exception (`CODE_GAUNTLET_HEADLESS=1`): call `scripts/resolve_config.py` "
        "with no `--target` on this path. The question is never presented in headless mode. "
        "Branch on `resolved.pr_not_found_policy`: `error` stops the run, and `local` proceeds "
        "with `pr_number` cleared. See `references/headless-mode.md`."
    )


def test_caller_headless_fallback():
    reference = (ROOT / "skills/code-gauntlet/references/headless-mode.md").read_text(
        encoding="utf-8"
    )
    assert (
        "If no report materializes, repeat the block in the final message as the fallback receipt."
        in reference
    )


@pytest.mark.parametrize(
    ("gate", "knobs"),
    [
        ("PR-not-found (resolution failure)", ("resolved.pr_not_found_policy",)),
        ("Draft PR", ("resolved.draft_policy",)),
        (
            "Previously reviewed (Phase 2 2b-post step 3, after checkout)",
            ("resolved.reviewed_policy",),
        ),
        (
            "Trivial / light-scope (all low-risk, <50 lines)",
            ("CODE_GAUNTLET_TRIVIAL_SCOPE",),
        ),
        (
            "Phase 8 Stage 1 (delivery question)",
            ("resolved.delivery", "resolved.post_mode"),
        ),
    ],
    ids=["not-found", "draft", "reviewed", "trivial", "delivery"],
)
def test_caller_gate_fields(gate, knobs):
    reference = (ROOT / "skills/code-gauntlet/references/headless-mode.md").read_text(
        encoding="utf-8"
    )
    row = next(
        line for line in reference.splitlines() if line.startswith(f"| {gate} |")
    )
    for knob in knobs:
        assert knob in row


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
    # A negative fixture proves the inventory check rejects an unowned gate input.
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


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (
            "Headless config:",
            {
                "workflows/src/renderReport.js",
                "skills/code-gauntlet/references/report-format.md",
                "skills/code-gauntlet/references/headless-mode.md",
                "skills/code-gauntlet/SKILL.md",
                "scripts/gauntlet/config.py",
            },
        ),
        (
            "Resolved config:",
            {
                "workflows/src/renderReport.js",
                "skills/code-gauntlet/references/report-format.md",
                "skills/code-gauntlet/SKILL.md",
                "scripts/gauntlet/config.py",
            },
        ),
        (
            "HEADLESS CONFIG ERROR:",
            {
                "skills/code-gauntlet/references/headless-mode.md",
                "scripts/gauntlet/config.py",
            },
        ),
    ],
    ids=["headless", "interactive", "error"],
)
def test_caller_config_producers(token, expected):
    registry = (ROOT / "docs/machine-parsed-strings.md").read_text(encoding="utf-8")
    row = next(
        line for line in registry.splitlines() if line.startswith(f"| `{token}`")
    )
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert {item.strip(" `") for item in cells[1].split(",")} == expected


def test_caller_config_block_producers():
    pattern = re.compile(r"(?m)^(?:Resolved|Headless) config:\n(?:^  .*\n?)+")
    actual = {}
    for path in (ROOT / "skills/code-gauntlet").rglob("*.md"):
        blocks = pattern.findall(path.read_text(encoding="utf-8"))
        if blocks:
            actual[path.relative_to(ROOT).as_posix()] = len(blocks)
    assert actual == {
        "skills/code-gauntlet/SKILL.md": 2,
        "skills/code-gauntlet/references/headless-mode.md": 1,
        "skills/code-gauntlet/references/report-format.md": 1,
    }
