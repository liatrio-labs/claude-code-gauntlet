"""Capture policy before dispatch because Workflows cannot read the environment.
Provider switches select deployment naming; model-policy.md owns alias pinning.
Gateways disable conditional schemas because they may forward to another backend.
"""

import argparse
import os
from collections.abc import Mapping
from typing import TypedDict

from gauntlet.cli import Command, Parser
from gauntlet.registry import (
    MODEL_ALIASES,
    POLICY_PROVIDERS,
    ModelAlias,
    PolicyProvider,
)


class PolicyEnv(TypedDict):
    subagentModel: str | None
    provider: PolicyProvider
    gateway: bool
    mappedAliases: list[ModelAlias]


def capture_policy_env(env: Mapping[str, str]) -> PolicyEnv:
    subagent_model = env.get("CLAUDE_CODE_SUBAGENT_MODEL", "")
    provider: PolicyProvider = next(
        (
            provider
            for provider in POLICY_PROVIDERS
            if provider != "firstParty"
            and env.get(f"CLAUDE_CODE_USE_{provider.upper()}", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
        "firstParty",
    )
    return {
        "subagentModel": subagent_model if subagent_model.strip() else None,
        "provider": provider,
        "gateway": bool(env.get("ANTHROPIC_BASE_URL", "").strip()),
        "mappedAliases": [
            alias
            for alias in MODEL_ALIASES
            if env.get(f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL", "").strip()
        ],
    }


def main(args: argparse.Namespace) -> tuple[PolicyEnv, int]:
    return capture_policy_env(os.environ), 0


CLI = Command(
    parser=Parser(prog="capture_policy_env", description=__doc__),
    main=main,
)
