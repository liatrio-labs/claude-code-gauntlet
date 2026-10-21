# Model Policy

## Session model and stage defaults

The orchestrator uses the session's model; there is no skill-level knob for it. Stage defaults come from `resolvePolicy`: discovery uses Sonnet except security-reviewer uses Opus; validator, challenger, executor, and artifact-writer default to Sonnet. A non-null `policy.subagentModel` overrides every stage.

**Per-agent pins use full model IDs on first-party sessions**, except aliases listed in `policy.mappedAliases`. `resolvePolicy` maps unmapped aliases through `MODEL_IDS` (`sonnet` -> `claude-sonnet-5-5`, `opus` -> `claude-opus-5-5`, `haiku` -> `claude-haiku-4-5-20251001`) to prevent the session's model variant from cascading into agents. Mapped aliases dispatch bare so Claude Code resolves the user's `ANTHROPIC_DEFAULT_<ALIAS>_MODEL`, including its `[1m]` suffix, model overrides and allowlist. Only alias names enter the policy and methodology, never deployment IDs. Bedrock, Vertex and Foundry always receive bare aliases because their deployment mappings require them. Gateways retain the provider's naming rule; a gateway with nonstandard names can use a tier mapping or `CLAUDE_CODE_SUBAGENT_MODEL`. Schema handling is independent; see the [capture contract](phase2-triage.md#args-preparation).

`CLAUDE_CODE_SUBAGENT_MODEL` follows the same resolution: a bare override of a mapped alias stays bare; an unmapped first-party alias receives its full-ID pin. Explicit model or deployment IDs pass through unchanged. The override applies to every stage.
