"""Environment capture preserves Claude Code's model resolution inputs."""

import json

import pytest
from gauntlet.policy_env import capture_policy_env


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, (None, "firstParty", False, [])),
        (
            {
                "CLAUDE_CODE_USE_BEDROCK": "  TrUe \t",
                "CLAUDE_CODE_USE_VERTEX": "1",
                "CLAUDE_CODE_USE_FOUNDRY": "on",
            },
            (None, "bedrock", False, []),
        ),
        (
            {
                "CLAUDE_CODE_USE_BEDROCK": "false",
                "CLAUDE_CODE_USE_VERTEX": "YES",
                "CLAUDE_CODE_USE_FOUNDRY": "1",
            },
            (None, "vertex", False, []),
        ),
        (
            {
                "CLAUDE_CODE_USE_BEDROCK": "0",
                "CLAUDE_CODE_USE_VERTEX": "",
                "CLAUDE_CODE_USE_FOUNDRY": " On ",
            },
            (None, "foundry", False, []),
        ),
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, (None, "bedrock", False, [])),
        ({"CLAUDE_CODE_USE_BEDROCK": "yes"}, (None, "bedrock", False, [])),
        ({"CLAUDE_CODE_USE_BEDROCK": "on"}, (None, "bedrock", False, [])),
        (
            {
                "CLAUDE_CODE_USE_BEDROCK": "enabled",
                "CLAUDE_CODE_USE_VERTEX": "no",
                "CLAUDE_CODE_USE_FOUNDRY": "off",
                "ANTHROPIC_BASE_URL": " \t",
                "CLAUDE_CODE_SUBAGENT_MODEL": "\n ",
            },
            (None, "firstParty", False, []),
        ),
        (
            {
                "ANTHROPIC_BASE_URL": " https://gateway.example ",
                "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
            },
            ("sonnet", "firstParty", True, []),
        ),
        ({"ANTHROPIC_BASE_URL": "false"}, (None, "firstParty", True, [])),
        (
            {
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku-deployment",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "deployment[1m]",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "arn:deployment",
            },
            (None, "firstParty", False, ["sonnet", "opus", "haiku"]),
        ),
        (
            {
                "CLAUDE_CODE_USE_BEDROCK": "true",
                "ANTHROPIC_BASE_URL": "gateway",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": " \t",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "id",
            },
            (None, "bedrock", True, ["opus"]),
        ),
    ],
)
def test_capture_policy_env(env, expected):
    subagent_model, provider, gateway, mapped_aliases = expected
    assert capture_policy_env(env) == {
        "subagentModel": subagent_model,
        "provider": provider,
        "gateway": gateway,
        "mappedAliases": mapped_aliases,
    }


def test_capture_policy_env_cli(tmp_path, invoke, monkeypatch):
    monkeypatch.setattr(
        "gauntlet.policy_env.os.environ",
        {
            "CLAUDE_CODE_USE_BEDROCK": " true ",
            "ANTHROPIC_BASE_URL": "https://gateway.example",
            "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deployment[1m]",
        },
    )
    result = invoke("capture_policy_env", [], tmp_path)
    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "subagentModel": "sonnet",
        "provider": "bedrock",
        "gateway": True,
        "mappedAliases": ["opus"],
    }
