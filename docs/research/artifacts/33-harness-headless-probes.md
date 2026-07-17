# Harness Headless Invocation Probes

**Date:** 2026-07-17

**Purpose:** Platform probes for the bench harness headless invocation contract; consumed by bench runner Tasks 9/11 and cited by future Phase 0.

## Summary

| # | Probe | Status | Observed | Consequence |
|---|-------|--------|----------|-------------|
| P1 | `AskUserQuestion` availability under `claude -p`, per permission mode | CONFIRMED for default and `acceptEdits`; INCONCLUSIVE for `--dangerously-skip-permissions` (blocked at the outer session's permission layer, not yet run) | `AskUserQuestion` is absent from the headless tool registry in both tested modes — `ToolSearch select:AskUserQuestion` returns no match, and a direct invocation errors with "No such tool available" | No hang path via this tool was observed under `-p`. Task 9's `invoke.py` still keeps a per-PR watchdog plus an `AskUserQuestion` tool-use scan of the result JSON as defense-in-depth |
| P2 | Plugin loading and isolation mechanism | CONFIRMED | `--bare` strips the ambient OAuth session (breaks login unless API-key auth is supplied) and does not isolate operator-installed plugins from the skill list. Isolated `HOME`/`CLAUDE_CONFIG_DIR` + `--plugin-dir`, without `--bare`, loads only the target plugin and rejects the workspace's own `.claude/settings.json` as untrusted | Pinned invocation context: isolated `HOME` + `CLAUDE_CONFIG_DIR` + `--plugin-dir`, no `--bare`. `invoke.py` must pre-seed the isolated config's `.claude.json` with `hasTrustDialogAccepted: true` for the worktree path before invoking |
| P3 | Cost/usage field paths in `-p --output-format json` | CONFIRMED | Exact field paths for `total_cost_usd`, `modelUsage`, `usage`, and supporting envelope fields — see detail section below | `costs.py` can read these paths directly from the single result envelope with no parsing of a per-message transcript |

## P1 — AskUserQuestion under `claude -p`, per permission mode

**Command template:**
```
timeout 180 claude -p "Use the AskUserQuestion tool to ask me a yes/no question. Report what happens." [mode-flag] --output-format json
```

**Default mode** (no flag): exit 0, no hang, `is_error:false`, `num_turns` 4, `total_cost_usd` ≈ 0.974. The model reported that `AskUserQuestion` is not in the headless tool registry at all: `ToolSearch select:AskUserQuestion` returned "No matching deferred tools found," and it is not among the preloaded tool schemas either. Raw: `/tmp/p1-default.json`.

**`acceptEdits` mode** (`--permission-mode acceptEdits`): identical behavior — exit 0, `num_turns` 4, cost ≈ 0.987, same "tool not in the deferred-tool registry" finding. This transcript additionally attempted a direct invocation, which returned the error text *"No such tool available: AskUserQuestion. AskUserQuestion exists but is not enabled in this context."* Raw: `/tmp/p1-acceptedits.json`.

**Note on the "direct call" evidence:** the default-mode transcript's assistant text frames the direct-call outcome as a prediction ("attempting to call an unloaded tool directly would just fail with an InputValidationError") rather than an executed attempt — no actual direct call appears in that transcript. The verbatim "No such tool available: AskUserQuestion" error was only actually observed in the `acceptEdits` transcript. Both modes agree on the underlying conclusion (tool absent from the registry); only the specific mechanism of the "direct call" observation differs by mode. See the appendix for both raw transcripts.

**`bypass` mode** (`--dangerously-skip-permissions`): Status INCONCLUSIVE by direct observation — the outer orchestration session's auto-mode permission classifier denied launching a nested `claude … --dangerously-skip-permissions`, so this variant could not run yet. Inference (recorded as inference, not observation): since the `-p` tool registry is assembled identically for default and `acceptEdits` modes, the tool is almost certainly absent under bypass too. Re-run is pending an owner permission rule (`Bash(claude *)` in `.claude/settings.local.json`).

**Consequence:** `AskUserQuestion` cannot fire under `claude -p` — no hang path via this tool was observed. Task 9's `invoke.py` keeps the per-PR watchdog plus an `AskUserQuestion` tool-use scan of the result JSON as defense-in-depth; a hit still marks the run `invalid`. The observed response shape to scan: `-p --output-format json` returns a single result envelope (`type:"result"`), not a per-message transcript — tool-use records do not appear in this format, so the scanner must scan the envelope's `result` text plus treat any `permission_denials` entries naming `AskUserQuestion` as a hit.

## P2 — Plugin loading / isolation

Three variants were run, all targeting `--plugin-dir <repo>` (the claude-deep-review repo root):

**Original** (`--bare --plugin-dir <repo>` with ambient OAuth HOME): FAILED before skills could even be listed — `is_error:true`, exit 1, `result` = "Not logged in · Please run /login", cost 0. `--bare` strips the OAuth session entirely. Raw: `/tmp/p2.json`.

**P2a** (`--bare --plugin-dir <repo>` + `ANTHROPIC_API_KEY` env): exit 0, cost ≈ 0.055. `deep-review` and `build-review-md` were listed, confirming `--bare --plugin-dir` does load the repo plugin. However, operator-installed plugins (`cw-*` claude-workflow skills, `create-skill`, `create-custom-agent`) also leaked into the skill list, so `--bare` is **not** a clean isolation boundary. Raw: `/tmp/p2a.json`.

**P2b** (isolated `HOME=/tmp/bench-claude-home` + `CLAUDE_CONFIG_DIR=/tmp/bench-claude-home/config` + `--plugin-dir <repo>`, no `--bare`, + `ANTHROPIC_API_KEY`): exit 0, `num_turns` 1, cost ≈ 0.063. `deep-review:deep-review` resolved correctly (skills appear namespace-qualified in this mode, unlike P2a's flat names). The CLI explicitly printed that it ignored the repo's `.claude/settings.json` permissions because the workspace is untrusted in the isolated config, instructing the operator to set `projects[…].hasTrustDialogAccepted: true` in `<config>/.claude.json`. Operator config did not leak in. Raw: `/tmp/p2b.json`.

**Status:** CONFIRMED — the plugin loads under both mechanisms tested (`--bare` and isolated-HOME). **Isolation decision:** the pinned invocation context is isolated `HOME` + `CLAUDE_CONFIG_DIR` + `--plugin-dir`, without `--bare` — this gives cleaner separation from operator-installed plugins and matches the plan's fallback branch.

**Consequence:** `invoke.py` must pre-seed the isolated config's `.claude.json` with `projects[<worktree-path>].hasTrustDialogAccepted: true` (and any needed allowlist) before invoking, since first-run trust dialogs cannot be answered headlessly.

## P3 — Cost/usage fields in `-p --output-format json`

**Status:** CONFIRMED. Exact field paths for `costs.py`, all observed across the P1/P2 raw outputs:

- `.total_cost_usd` — float, USD, total for the run
- `.modelUsage` — object keyed by model id (e.g. `"claude-fable-5"`) holding per-model usage: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`, `contextWindow`, `maxOutputTokens`
- `.usage` — aggregate usage: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, `server_tool_use.*` (`web_search_requests`, `web_fetch_requests`), plus `service_tier`, `cache_creation.*`, `inference_geo`, `iterations` (array of per-message breakdowns), `speed`
- Useful envelope fields: `.num_turns`, `.is_error`, `.permission_denials` (array), `.duration_ms`, `.duration_api_ms`, `.session_id`, `.result` (final text), `.type` == `"result"`, `.subtype` == `"success"`

**Additional observation (not in the original handoff, derived from the raw files):** `.modelUsage` can contain more than one keyed entry per run. P2a and P2b both show a secondary `"claude-haiku-4-5-20251001"` entry alongside the primary model — a background Haiku call (likely session/title bookkeeping) that costs.py should sum, not discard. P2b's primary model id also appeared as `"claude-opus-4-8[1m]"` rather than `"claude-fable-5"` — model ids in `modelUsage` are not always the plain model name and can carry a context-window-variant suffix like `[1m]`. `costs.py` should treat `modelUsage` keys as opaque and sum across all of them rather than assuming a single fixed key.

**Consequence:** `costs.py` can read all cost/usage figures directly from the single result envelope with no per-message transcript parsing, but must iterate `modelUsage` as a variable-length, variable-key map rather than indexing a known model id.

## Appendix — raw command transcripts

Exit codes recorded via a sibling `.exit` file per probe (`exit=N`).

### P1 default mode — exit 0

Command:
```
timeout 180 claude -p "Use the AskUserQuestion tool to ask me a yes/no question. Report what happens." --output-format json
```

```json
{
    "type": "result",
    "subtype": "success",
    "is_error": false,
    "api_error_status": null,
    "duration_ms": 22405,
    "duration_api_ms": 22146,
    "ttft_ms": 6889,
    "ttft_stream_ms": 5387,
    "time_to_request_ms": 126,
    "num_turns": 4,
    "result": "Here's what happened: **the AskUserQuestion tool isn't available in this session**, so I couldn't invoke it.\n\nDetails:\n\n1. A direct lookup (`select:AskUserQuestion`) against the deferred-tool registry returned \"No matching deferred tools found\" — twice, including after all MCP servers finished connecting.\n2. A keyword search for \"ask user question\" only surfaced unrelated matches (Slack/Miro user-profile tools, TaskCreate, etc.).\n3. It's also not in my preloaded tool schemas, and attempting to call an unloaded tool directly would just fail with an InputValidationError.\n\nInterestingly, AskUserQuestion *is* listed in some subagent tool grants (e.g. `claude-workflow:planner`), but it's not exposed to me in this main session — likely because the session is running in autonomous mode, where interactive question prompts are disabled.\n\nSo the fallback is plain text. Since you asked for a yes/no question: **would you like me to continue working on the `feat/bench-harness` branch next?**",
    "stop_reason": "end_turn",
    "session_id": "e0012987-1a3b-497c-ad3c-932eab602231",
    "total_cost_usd": 0.974331,
    "usage": {
        "input_tokens": 8,
        "cache_creation_input_tokens": 38526,
        "cache_read_input_tokens": 157031,
        "output_tokens": 934,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0
        },
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 38526,
            "ephemeral_5m_input_tokens": 0
        },
        "inference_geo": "not_available",
        "iterations": [
            {
                "input_tokens": 2,
                "output_tokens": 426,
                "cache_read_input_tokens": 53621,
                "cache_creation_input_tokens": 235,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 235
                },
                "type": "message"
            }
        ],
        "speed": "standard"
    },
    "modelUsage": {
        "claude-fable-5": {
            "inputTokens": 8,
            "outputTokens": 934,
            "cacheReadInputTokens": 157031,
            "cacheCreationInputTokens": 38526,
            "webSearchRequests": 0,
            "costUSD": 0.974331,
            "contextWindow": 1000000,
            "maxOutputTokens": 64000
        }
    },
    "permission_denials": [],
    "terminal_reason": "completed",
    "fast_mode_state": "off",
    "uuid": "c0822fd8-8bee-4a99-858a-2639a0c833a0"
}
```

### P1 acceptEdits mode — exit 0

Command:
```
timeout 180 claude -p "Use the AskUserQuestion tool to ask me a yes/no question. Report what happens." --permission-mode acceptEdits --output-format json
```

```json
{
    "type": "result",
    "subtype": "success",
    "is_error": false,
    "api_error_status": null,
    "duration_ms": 24338,
    "duration_api_ms": 24052,
    "ttft_ms": 6358,
    "ttft_stream_ms": 4971,
    "time_to_request_ms": 138,
    "num_turns": 4,
    "result": "Here's what happened: **AskUserQuestion is not available in this session**, so I couldn't ask you the question.\n\nThe sequence:\n\n1. `ToolSearch` for `select:AskUserQuestion` returned \"No matching deferred tools found\" — it isn't in this session's deferred tool list (which includes things like CronCreate, WebFetch, SendMessage, but not AskUserQuestion).\n2. A keyword search (\"ask user question yes no prompt\") only surfaced unrelated tools like PushNotification and Slack/Krisp user-profile tools.\n3. Attempting to call `AskUserQuestion` directly anyway returned the error: *\"No such tool available: AskUserQuestion. AskUserQuestion exists but is not enabled in this context.\"*\n\nSo the tool exists in the broader Claude Code toolset (subagents like `claude-workflow:planner` list it), but it's disabled for this main session — likely because the session is running in an autonomous mode where blocking on interactive user input isn't allowed. If you want the yes/no answered, just reply here in chat.",
    "stop_reason": "end_turn",
    "session_id": "0308763b-01f3-4941-bbcb-9db39a6916ba",
    "total_cost_usd": 0.98724,
    "usage": {
        "input_tokens": 8,
        "cache_creation_input_tokens": 38853,
        "cache_read_input_tokens": 157150,
        "output_tokens": 1059,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0
        },
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 38853,
            "ephemeral_5m_input_tokens": 0
        },
        "inference_geo": "not_available",
        "iterations": [
            {
                "input_tokens": 2,
                "output_tokens": 361,
                "cache_read_input_tokens": 53718,
                "cache_creation_input_tokens": 465,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 465
                },
                "type": "message"
            }
        ],
        "speed": "standard"
    },
    "modelUsage": {
        "claude-fable-5": {
            "inputTokens": 8,
            "outputTokens": 1059,
            "cacheReadInputTokens": 157150,
            "cacheCreationInputTokens": 38853,
            "webSearchRequests": 0,
            "costUSD": 0.98724,
            "contextWindow": 1000000,
            "maxOutputTokens": 64000
        }
    },
    "permission_denials": [],
    "terminal_reason": "completed",
    "fast_mode_state": "off",
    "uuid": "ea0143e5-8c89-4025-9524-4d9cb092e7eb"
}
```

### P2 original ( `--bare` + ambient OAuth HOME) — exit 1

Command (verbatim):
```
timeout 180 claude -p --bare --plugin-dir /Users/lee/personal/claude-deep-review "List the names of skills available to you, one per line." --output-format json
```

```json
{
    "type": "result",
    "subtype": "success",
    "is_error": true,
    "api_error_status": null,
    "duration_ms": 18,
    "duration_api_ms": 0,
    "num_turns": 1,
    "result": "Not logged in · Please run /login",
    "stop_reason": "stop_sequence",
    "session_id": "af281fd7-c91d-4ed5-9b18-694fb88ac37d",
    "total_cost_usd": 0,
    "usage": {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0
        },
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0
        },
        "inference_geo": "",
        "iterations": [],
        "speed": "standard"
    },
    "modelUsage": {},
    "permission_denials": [],
    "terminal_reason": "api_error",
    "fast_mode_state": "off",
    "uuid": "cddd1c6a-3ff6-4809-8f42-43b4153849f0"
}
```

### P2a ( `--bare` + `ANTHROPIC_API_KEY` ) — exit 0

Command (verbatim; same as P2 with the metered API key prepended via `env`):
```
timeout 180 env ANTHROPIC_API_KEY=<metered key> claude -p --bare --plugin-dir /Users/lee/personal/claude-deep-review "List the names of skills available to you, one per line." --output-format json
```

```json
{
    "type": "result",
    "subtype": "success",
    "is_error": false,
    "api_error_status": null,
    "duration_ms": 21577,
    "duration_api_ms": 18179,
    "ttft_ms": 5817,
    "ttft_stream_ms": 4790,
    "time_to_request_ms": 307,
    "num_turns": 3,
    "result": "Available skills:\n\n```\ncw-dispatch\ncw-dispatch-team\ncw-execute\ncw-gherkin\ncw-plan\ncw-research\ncw-review\ncw-review-team\ncw-spec\ncw-testing\ncw-validate\ncw-worktree\nbuild-review-md\ndeep-review\ncreate-custom-agent\ncreate-skill\n```",
    "stop_reason": "end_turn",
    "session_id": "1df814ad-38b1-4d31-b529-30599a42488f",
    "total_cost_usd": 0.054917499999999994,
    "usage": {
        "input_tokens": 6,
        "cache_creation_input_tokens": 2295,
        "cache_read_input_tokens": 3876,
        "output_tokens": 434,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0
        },
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 2295
        },
        "inference_geo": "global",
        "iterations": [
            {
                "input_tokens": 2,
                "output_tokens": 115,
                "cache_read_input_tokens": 2027,
                "cache_creation_input_tokens": 268,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 268,
                    "ephemeral_1h_input_tokens": 0
                },
                "type": "message"
            }
        ],
        "speed": "standard"
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 529,
            "outputTokens": 13,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "webSearchRequests": 0,
            "costUSD": 0.0005939999999999999,
            "contextWindow": 200000,
            "maxOutputTokens": 32000
        },
        "claude-fable-5": {
            "inputTokens": 6,
            "outputTokens": 434,
            "cacheReadInputTokens": 3876,
            "cacheCreationInputTokens": 2295,
            "webSearchRequests": 0,
            "costUSD": 0.0543235,
            "contextWindow": 1000000,
            "maxOutputTokens": 64000
        }
    },
    "permission_denials": [],
    "terminal_reason": "completed",
    "fast_mode_state": "off",
    "uuid": "00ae2bcf-fff5-47a9-ac2f-9928579aad11"
}
```

### P2b (isolated HOME + CLAUDE_CONFIG_DIR, no `--bare` ) — exit 0

Command (verbatim):
```
timeout 180 env HOME=/tmp/bench-claude-home CLAUDE_CONFIG_DIR=/tmp/bench-claude-home/config ANTHROPIC_API_KEY=<metered key> claude -p --plugin-dir /Users/lee/personal/claude-deep-review "List the names of skills available to you, one per line." --output-format json
```

Stdout preamble (printed before the JSON envelope, not part of the JSON itself):
```
Ignoring 1 permissions.allow entry from .claude/settings.json: this workspace has not been trusted. Run Claude Code interactively here once and accept the trust dialog, or set projects["/Users/lee/personal/claude-deep-review"].hasTrustDialogAccepted: true in /tmp/bench-claude-home/config/.claude.json.
```

```json
{
    "type": "result",
    "subtype": "success",
    "is_error": false,
    "api_error_status": null,
    "duration_ms": 3268,
    "duration_api_ms": 4931,
    "ttft_ms": 2747,
    "ttft_stream_ms": 1693,
    "time_to_request_ms": 22,
    "num_turns": 1,
    "result": "deep-research\ndeep-review:build-review-md\ndeep-review:deep-review\ndataviz\nupdate-config\nkeybindings-help\nverify\ncode-review\nsimplify\nfewer-permission-prompts\nloop\nclaude-api\nrun\ninit\nreview\nsecurity-review",
    "stop_reason": "end_turn",
    "session_id": "613eb14c-c51c-4fdb-855a-5742a17e9c7e",
    "total_cost_usd": 0.06296725,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 8275,
        "cache_read_input_tokens": 15449,
        "output_tokens": 117,
        "server_tool_use": {
            "web_search_requests": 0,
            "web_fetch_requests": 0
        },
        "service_tier": "standard",
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 8275
        },
        "inference_geo": "global",
        "iterations": [
            {
                "input_tokens": 2,
                "output_tokens": 117,
                "cache_read_input_tokens": 15449,
                "cache_creation_input_tokens": 8275,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 8275,
                    "ephemeral_1h_input_tokens": 0
                },
                "type": "message"
            }
        ],
        "speed": "standard"
    },
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 529,
            "outputTokens": 12,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "webSearchRequests": 0,
            "costUSD": 0.000589,
            "contextWindow": 200000,
            "maxOutputTokens": 32000
        },
        "claude-opus-4-8[1m]": {
            "inputTokens": 2,
            "outputTokens": 117,
            "cacheReadInputTokens": 15449,
            "cacheCreationInputTokens": 8275,
            "webSearchRequests": 0,
            "costUSD": 0.062378249999999996,
            "contextWindow": 1000000,
            "maxOutputTokens": 64000
        }
    },
    "permission_denials": [],
    "terminal_reason": "completed",
    "fast_mode_state": "off",
    "uuid": "c82fdae5-c769-4f58-bb09-ffc82d2c3bff"
}
```
