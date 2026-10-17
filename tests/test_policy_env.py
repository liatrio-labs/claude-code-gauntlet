"""Environment capture preserves Claude Code's model resolution inputs."""

import pytest
from gauntlet.policy_env import capture_policy_env


@pytest.mark.parametrize(
    "env,expected",
    [
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
