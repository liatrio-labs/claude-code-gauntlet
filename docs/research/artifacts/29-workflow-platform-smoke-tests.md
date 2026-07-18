# 29 — Workflow platform smoke tests (v3 Phase 0 gate)

Empirical verification of the 16 platform assumptions in the v3 design spec (S9).
Run dates, CLI version, and raw observations recorded per row. Statuses:
CONFIRMED / REFUTED / INCONCLUSIVE.

**CLI version under test:** 2.1.214 (Claude Code)

| # | Assumption | Status | Observed | Decision impact |
|---|------------|--------|----------|-----------------|
| 1 | Workflow scriptPath resolves from the installed plugin dir | CONFIRMED | Absolute scriptPath inside the versioned marketplace cache dir (`~/.claude/plugins/cache/claude-deep-review/deep-review/2.5.0/probes-tmp/noop.js`) executed and returned `{"ok":true,"marker":"PROBE_NOOP_V1"}`; repo-path invocation also works | packaging (S2) |
| 2 | scriptPath scripts can import/require sibling files | REFUTED | Static ESM import fails at parse: `SyntaxError: Unexpected token '{'. import call expects one or two arguments.` (script never launches); `require()` throws `ReferenceError: require is not defined` at runtime | source layout (S3) |
| 3 | Headless `claude -p` can invoke the Workflow tool via scriptPath | CONFIRMED | Cited from artifact 33 (P2b pinned invocation context, P3 cost fields) + one residual-gap check run 2026-07-18: default-mode `claude -p --model sonnet` invoked Workflow noop via scriptPath, marker returned, $0.348, `is_error:false`. Nested `--dangerously-skip-permissions` remains blocked by the outer session's classifier (as in artifact 33 P1 history) — default mode is the working path | bench runner (S6) |
| 4 | Workflow-spawned agent Bash works under interactive default/acceptEdits (or scoped allowlist pre-approves) | INCONCLUSIVE (default/bypass/auto CONFIRMED; acceptEdits refuted headlessly; attended confirmation pending) | Headless matrix: default ✓ (Workflow auto-allowed, agent Bash ran, Write wrote, 0 denials); bypassPermissions ✓ (same, and the `--permission-mode bypassPermissions` spelling passes the outer classifier that blocks `--dangerously-skip-permissions`); acceptEdits ✗ — the Workflow tool itself is denied with "Review dynamic workflow before running"; +`Workflow` allow rule → launch allowed but agent Bash then blocked ("requires approval"); +both rules (`Workflow` + scoped Bash glob) → Workflow denied AGAIN (suspected invalid Bash-rule spec poisoning the allow array). Auto-mode in-session run also ✓. Attended items: interactive prompt UX + D/E rule-syntax anomaly | executor (S4.4) |
| 5 | agent() returns null (not throw) on terminal failure; parallel() isolates member failures | REFUTED (half) | Bare `agent()` THROWS on structural failures: unsatisfiable schema → `TelemetrySafeError: StructuredOutput retry cap (5) exceeded`; unknown agentType → `Error: agent type not found`. parallel() isolation IS confirmed: failing member → `null`, sibling intact (`{v:1}`). Contract for Plan 2: wrap bare `agent()` in try/catch; `.filter(Boolean)` after `parallel()` | degradation paths (S4) |
| 6 | Frontmatter `effort` survives agentType dispatch | CONFIRMED | Twin sonnet agents differing only in `effort`, same reasoning prompt ×3 each: high-effort output tokens {470, 415, 175} (mean 353) vs low {88, 4, 158} (mean 83) — ratio 4.2× > 1.5× threshold. Effort survives; D3 = none needed | parity claim (S5) |
| 7 | opts.model overrides agentType frontmatter model | CONFIRMED | `agentType: 'deep-review:probe-effort-high'` (frontmatter `model: sonnet`) + `opts.model: 'opus'` → agent meta records `"model": "opus"`, transcript model id `claude-opus-4-8` | policy resolver (S5) |
| 8 | `model:'fable'` alias accepted in agent() opts | INCONCLUSIVE (deferred by owner) | Probe staged (`probes/fable-alias.js`), not run — owner authorization for the Fable spend not yet given; standing default is deferred. Frontier flag falls back to full model id string until confirmed | frontier flag (S5) |
| 9 | Worst-case args payload (~500KB) accepted without truncation | CONFIRMED | 422,549-byte args object (500 paths + 400KB filler + tail marker) passed via `workflow()` nesting arrived byte-identical and structurally intact (`argsIntact:true`). Caveat: model-emitted inline args are separately bounded by max output tokens — large payloads must be built in JS or passed as file paths | args waist (S1) |
| 10 | parallel() with >16 tasks queues to completion; pipeline()/phase() available | CONFIRMED | 20 sonnet tasks → `{completed:20, allPresent:true, pipelineExists:true, phaseExists:true}` in 63s, 0 errors, 712K subagent tokens. Excess tasks queue, none dropped | fan-out stages (S4) |
| 11 | Oversized StructuredOutput behavior: truncation vs retry, bounded retries, cost | CONFIRMED (at 300 items) | Sonnet agent returned the full 300-object array (~60KB JSON) with no truncation; leg cost 63,797 subagent tokens. Retry cap known from #5: 5 bounded attempts, then throw. Discovery arrays ≤300 items are safe; the #5 contract covers the failure mode beyond that | schema design (S4) |
| 12 | Large workflow return value (~2MB) survives to the session | CONFIRMED | 2MB string intact across both hops: `workflow()`→parent (`bigReturnLength:2097152, bigReturnIntact:true`) and workflow→session (headless child received all 2,097,152 chars; in-conversation display truncates to ~8KB with a pointer to the full output file — display artifact, not data loss). Compact returns still preferred for context hygiene | compact-return design (S1) |
| 13 | A skill can detect Workflow-tool absence and fail cleanly | CONFIRMED | `CLAUDE_CODE_DISABLE_WORKFLOWS=1 claude -p "...say WORKFLOW_PRESENT/WORKFLOW_ABSENT..."` → `WORKFLOW_ABSENT`; same prompt without the env var → `WORKFLOW_PRESENT`. Detection recipe for SKILL.md preflight: ask the session to check its own tool registry for `Workflow` before dispatch | preflight (S1) |
| 14 | semantic-release version_variables bumps pipeline.js in lockstep | CONFIRMED | Forced-patch local run (`semantic-release -c .releaserc.toml version --patch --no-commit --no-tag --no-push --no-changelog --no-vcs-release`) rewrote BOTH `.claude-plugin/plugin.json` (2.6.0→2.6.1) and the stand-in `pipeline.js:PIPELINE_VERSION` in lockstep. `-c .releaserc.toml` is mandatory (defaults ignore the file — that's also why the first two runs no-opped). Working entry: `"workflows/pipeline.js:PIPELINE_VERSION"` | packaging (S2) |
| 15 | /goal evaluator reliably matches sentinel lines (pass and fail transcripts) | CONFIRMED | Pass path: headless `/goal` session held the loop through two `GOAL_GATE1: FAIL ...` turns and stopped exactly on `GOAL_MET: true` (3 turns, $0.09). Fail path (clean condition, sentinel never printed): loop continued 4 turns then terminated without the sentinel — no runaway, no false match; turn-bound enforcement is approximate (4 vs "6 regardless"). Caveat: goal text implying unattainability lets the evaluator release early — keep working instructions out of the condition | goal loop (S7) |
| 16 | `tools:` frontmatter enforced under agentType (validator cannot Bash) | CONFIRMED | `deep-review:validator` (tools: Read, Grep, Glob, LSP) instructed to run `echo TOOLS_LEAK` via Bash → returned `{"bash":"unavailable"}`; no Bash tool in its registry | agent security (S4) |

## Decisions forced by this gate

- **D1 (from #2): source layout** — lib/ modules vs build-step artifact: **build-step artifact.** Workflow scripts are hard self-contained: static ESM import fails to parse and `require` is undefined. Source lives in `workflows/src/*.js`; a build step concatenates/bundles into the single shipped `workflows/pipeline.js`. JS/Python parity fixtures test the built artifact.
- **D2 (from #4): executor permission strategy** — allowlist entry vs docs vs redesign: **(provisional, owner confirmation pending)** No stage redesign needed. Bench/CI path: default permission mode — fully clean headlessly (Workflow auto-allowed, AST-simple executor Bash sandbox-approved, zero prompts). Interactive path: default mode works out-of-box pending prompt-UX confirmation; acceptEdits requires pre-approving BOTH the dynamic-workflow review gate (`Workflow` allow rule — worked in leg D) and the executor's Bash (rule syntax unresolved — leg D/E anomaly suggests an invalid Bash-glob spec disables the allow array); bypassPermissions works. Deliverable for Plan 2: documented allowlist recipe in the plugin README + skill preflight note, exact rule strings pinned in the owner's attended session.
- **D3 (from #6): effort regression compensation** — none needed vs model bump vs prompt: **none needed.** Frontmatter `effort` demonstrably shapes reasoning depth under agentType dispatch (4.2× output-token ratio between effort:high and effort:low twins on an identical reasoning task, n=3 each). The v3 pipeline can rely on per-agent effort frontmatter as-is.

## Design changes required by REFUTED rows

- **Row 2 (REFUTED)** → modifies **S3 (source layout)**: workflow scripts cannot import/require siblings; the shipped `workflows/pipeline.js` must be a build-step artifact bundled from `workflows/src/*.js`. Parity fixtures test the built artifact. (= D1.)
- **Row 5 (REFUTED half)** → modifies **S4 (degradation wrappers)**: bare `agent()` throws on structural failures (schema-retry exhaustion cap 5, unknown agentType) instead of returning null; every pipeline stage wraps calls in try/catch or routes through `parallel()` null-isolation, always `.filter(Boolean)`.
- **Row 4 (acceptEdits leg refuted headlessly)** → modifies **S1/S4.4 docs surface only** (no stage redesign): dynamic-workflow review gate + agent-Bash approval both need pre-approval under acceptEdits; ship a documented allowlist recipe; bench pins default mode. (= D2 provisional.)

## Raw observations

### Task 2 — Workflow invocation probes (2026-07-18, CLI 2.1.214)

**#1 (scriptPath from installed plugin dir).** The plugin is a versioned marketplace copy (`~/.claude/plugins/cache/claude-deep-review/deep-review/{2.4.0,2.5.0}/`), not a symlink to the repo. Copied `noop.js` to `2.5.0/probes-tmp/`, invoked Workflow with the absolute path → completed in 7ms, 0 agents, returned `{"ok":true,"marker":"PROBE_NOOP_V1"}`. `probes-tmp/` removed afterward. Note: `rm -rf` against the plugin cache is denied by the session permission layer; per-file `rm -f` + `rmdir` works.

**#2 (sibling import).** `import { probeValue } from './importee.js'` → rejected at launch: `SyntaxError: Unexpected token '{'. import call expects one or two arguments.` (the parser treats `import` as the dynamic `import()` call form only). `require('./importee.js')` variant → script launches but throws `ReferenceError: require is not defined`. Both module mechanisms unavailable ⇒ D1 = build-step artifact.

**#3 (headless).** Primary citation: artifact 33 — P2b pins the bench invocation context (isolated `HOME`+`CLAUDE_CONFIG_DIR`+`--plugin-dir`, no `--bare`, pre-seeded `hasTrustDialogAccepted`), P3 documents the cost-field envelope. Residual gap: artifact 33 never invoked the Workflow tool itself under `-p`, so one direct check was run: `claude -p "Invoke the Workflow tool with scriptPath '$PWD/probes/noop.js'..." --model sonnet --output-format json` (default permission mode) → `is_error:false`, 5 turns, $0.348, result text contains `{"ok":true,"marker":"PROBE_NOOP_V1"}`. The `--dangerously-skip-permissions` variant of the same command is denied by the outer orchestration session's classifier (consistent with artifact 33 P1's history note); default mode suffices for the bench runner.

### Task 4 — executor permission matrix, headless legs (2026-07-18)

Five headless legs (`claude -p --model sonnet`, repo cwd, ambient HOME) invoking `probes/executor-perm.js` (spawned sonnet agent runs `python3 probes/echo_receipt.py NONCE123` then Writes a file), reading the result envelope's `permission_denials` as the headless analogue of a surfaced prompt; plus one in-session auto-mode run.

| Leg | Mode | Workflow call | Agent Bash | Agent Write | Cost |
|-----|------|--------------|-----------|-------------|------|
| in-session | auto (goal session) | allowed | ran | wrote | — |
| A | default | auto-allowed (0 denials) | ran | wrote | $0.47 |
| B | acceptEdits | DENIED — "Review dynamic workflow before running" | — | — | $0.29 |
| C | bypassPermissions | allowed | ran | wrote | $0.33 |
| D | acceptEdits + `Workflow` allow rule | allowed | BLOCKED — "requires approval", stdout error returned by agent, `wrote:false` | not written | $0.28 |
| E | acceptEdits + `Workflow` + `Bash(python3 */probes/echo_receipt.py:*)` rules | DENIED again (same review gate) | — | — | $0.32 |

Notable: (1) the dynamic-workflow review gate is a **Workflow-tool-level** approval distinct from agent Bash approval — acceptEdits requires both; (2) default and bypass modes need nothing; (3) leg D vs E contradiction — adding the scoped Bash glob apparently *disabled* the previously-working `Workflow` rule; suspicion: invalid rule spec poisons the allow-array parse. Settings restored after each leg (verified). Attended follow-ups for the owner: confirm interactive prompt UX under default (does the review prompt surface interactively where headless auto-allowed?), resolve the D/E rule-syntax anomaly, bless the documented allowlist recipe. Bench-runner impact: none — the pinned invocation context is default mode, which is fully clean.

### Task 6 — size-limit probes (2026-07-18)

**Method note.** The ~500KB args payload was built in plain JS inside a parent workflow (`probes/sizes-parent.js`) and handed to `probes/sizes-child.js` via `workflow()` nesting — no model ever emits the payload (a model-emitted inline args JSON is separately bounded by max output tokens, which is itself the S1-relevant ceiling for session-supplied args). The 2MB return-to-session leg ran in a disposable headless child session to keep the orchestrator context safe.

**#9 (args waist).** Sent 422,549 bytes (500 paths + 400KB filler + `tail:'ARGS_TAIL_MARKER'`); child observed identical `argsBytes` and full structural integrity (`argsIntact:true` = tail marker + 500-element array + 400,000-char filler ending correctly). No truncation, no error.

**#11 (oversized StructuredOutput).** Request for exactly 300 `{id, text~200ch}` objects (~60KB JSON): sonnet returned all 300 (`itemCount:300`), no truncation, no retry exhaustion; leg used 63,797 subagent tokens / 3 tool uses. Bounded-retry failure mode beyond this size is covered by #5 (cap = 5 attempts, then throw). Plan-2 constants: discovery arrays ≤300 items ≈ 60KB are demonstrated-safe; verify slices can stay well under that.

**#12 (large return).** `workflow()`→parent: 2,097,152-char string intact (`bigReturnIntact:true`). Workflow→session (headless child, $0.41): full 2MB received as data; the in-conversation tool-result display truncates to ~8KB with a verbatim notice pointing at the full output file (`... (truncated 2089186 chars, full result in .../tasks/<id>.output)`) — display artifact, not data loss. Design: keep returns compact anyway; large payloads via artifact paths.

**args-mode quirk (S1-relevant).** `args` passed in the session's Workflow tool call arrive in the script as a JSON **string** (`typeof args === 'string'`, observed `"{\"probe\": \"argmode-check\"}"`); args passed through `workflow()` nesting arrive as a real object. v3 scripts must normalize: `const A = typeof args === 'string' ? JSON.parse(args) : args`. (This quirk caused one aborted probe run before diagnosis; a second self-inflicted abort came from calling `JSON.stringify(undefined).slice` in the diagnostic itself when no args were passed.)

### Task 8 — owner-gated probes (2026-07-18)

**#15 (goal-sentinel).** Headless `claude -p '/goal ...' --model sonnet` probes in scratch dirs. Pass path: two `GOAL_GATE1: FAIL f1=30.1 anchor=37.6` turns, then `GOAL_MET: true` → session ended exactly on the sentinel, `num_turns:3`, $0.091. Contaminated fail path (goal text itself said "Never print the GOAL_MET line"): evaluator released after 2 turns — an unattainability-declaring condition is licence to stop; keep conditions clean. Clean fail path: 4 turns of FAIL lines, then release without the sentinel, $0.101 — bound enforcement approximate vs the specified 6, in the conservative (early-stop) direction. Live corroboration: this goal session itself has continued through many `NEEDS_OWNER:`-bearing turns without a false `GOAL_MET` match. Sentinel grammar for Plan 4: exact-match full-line sentinels (`SENTINEL_NAME: value` at line start) are reliably matched; turn bounds are safety nets, not precise counters.

**#8 (fable alias).** Deferred by owner default; probe staged at `probes/fable-alias.js`, one trivial call when authorized. Until then: INCONCLUSIVE; the S5 frontier flag must accept a full model id string as fallback.

### Task 5 — model/effort routing probes (2026-07-18)

**Method note.** The agentType registry is built at session start (see Task 3 observation), so the twin probe agents were staged into the active plugin cache (`2.5.0/agents/`) and `probes/routing.js` was dispatched via a fresh headless child session (`claude -p --model sonnet`, $0.44) whose registry included them. Per-agent evidence read from the child's workflow transcript dir (`agent-*.jsonl` for model + `usage` sums; `agent-*.meta.json` for agentType/model overrides — the run's `journal.jsonl` holds only result cache keys, not usage).

**#6 (effort survival).** Six sequential runs of the same bat-and-ball reasoning prompt: `probe-effort-high` output tokens 470/415/175 (mean 353), `probe-effort-low` 88/4/158 (mean 83), identical input context (11,650 tokens each). mean(high) = 4.2 × mean(low) > 1.5× rule → CONFIRMED. Variance within arms is large (4–470), so effort shapes the *budget*, not a deterministic depth — fine for the S5 parity claim.

**#7 (model precedence).** The `model-precedence` call (frontmatter sonnet + opts.model 'opus') ran on `claude-opus-4-8`; its meta.json records `"model": "opus"` alongside `"agentType": "deep-review:probe-effort-high"`. opts.model wins.

**#16 (tools enforcement).** `deep-review:validator` returned `{"bash":"unavailable"}` when instructed to run a shell command — frontmatter `tools:` is enforced as a hard sandbox under agentType dispatch, consistent with artifact 20.

### Task 7 — release-plumbing probe (2026-07-18)

**#14 (lockstep versioning).** Stand-in `probes/version-probe/pipeline.js` with `const PIPELINE_VERSION = '2.6.0'` (== plugin.json current). Temp `.releaserc.toml` edits: appended `"probes/version-probe/pipeline.js:PIPELINE_VERSION"` to `version_variables` and widened the branch match to include `feat/v3-phase0`. Key finding: PSR ignores `.releaserc.toml` unless invoked with `-c .releaserc.toml` (CI does this; the plan's bare `uvx ... semantic-release --noop version --print` silently used defaults and no-opped with "branch isn't in any release groups"). With the CI-mirrored invocation plus `--patch --no-commit --no-tag --no-push --no-changelog --no-vcs-release`, both files rewrote to 2.6.1 in lockstep. A `WARNING Failed to add path (probes/version-probe/pipeline.js) to index` appeared solely because `probes/` is gitignored — content was still rewritten; the production `workflows/pipeline.js` is tracked, so no warning applies. Permanent entry for Plan 2: `"workflows/pipeline.js:PIPELINE_VERSION"`. All temp edits reverted (verified: version back to 2.6.0, single-entry version_variables, branch match `main`).

### Task 3 — agent() failure-contract probes (2026-07-18)

**#5 (failure contract).** Three legs via `probes/failure-contract.js`:
- (a) impossible schema (`const:'ABC'` + `maxLength:1`), model sonnet → after 5 attempts: `THREW: TelemetrySafeError: agent({schema}): StructuredOutput retry cap (5) exceeded — 5 failed calls with no valid output`. Retries are bounded at 5; each retry is a full metered call (observed 123K subagent tokens across the probe's 4 agents).
- (b) `agentType: 'deep-review:does-not-exist'` → immediate (3s) `THREW: Error: agent({agentType}): agent type 'deep-review:does-not-exist' not found. Available agents: <list>`.
- (c) `parallel([ok, failing])` → `{sibling: {v:1}, failed: null}` — the member's throw is converted to `null`, sibling unaffected. The failing member surfaced a second distinct terminal message ("subagent completed without calling StructuredOutput (after in-conversation nudge)") — failure text varies by path; only the null-in-parallel behavior is stable.

**Contract sentence for Plan 2:** structural failures (schema exhaustion, agentType resolution) THROW from bare `agent()` and must be try/caught; inside `parallel()` they resolve to `null` for that member only; degradation wrappers therefore standardize on `parallel()`-style null-isolation or explicit try/catch, and always `.filter(Boolean)`.

**#10 (queueing).** `probes/parallel-queue.js`, 20 one-shot sonnet agents under the 16-cap → `{completed:20, allPresent:true, pipelineExists:true, phaseExists:true}`, 63s wall, 0 errors. Fan-out beyond the cap queues transparently.

**Mid-session registration observation (affects Task 5 method).** Agent files staged into the active plugin cache (`2.5.0/agents/probe-effort-*.md`) do NOT become addressable mid-session: `agent type 'deep-review:probe-effort-high' not found` from a workflow launched after staging. The agentType registry is built at session start. Consequence: routing probes dispatch through a fresh headless child session; v3 itself is unaffected (plugin agents exist before any reviewing session starts).

**#13 (absence detection).** `CLAUDE_CODE_DISABLE_WORKFLOWS=1 claude -p <one-word probe> --model sonnet` → `WORKFLOW_ABSENT` ($0.195); identical command without the env var → `WORKFLOW_PRESENT` ($0.165). Clean, deterministic detection; no hang, no error path.
