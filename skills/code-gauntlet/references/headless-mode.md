# Headless Mode Reference

Code-gauntlet runs unattended when `CODE_GAUNTLET_HEADLESS=1`. The resolver supplies every configuration decision, so the skill never prompts for those decisions.

This file is the authority for the headless contract: the environment table, resolver precedence, validation, hard rules, gate outcomes, and the `Headless config:` block.

---

## Env contract

The resolver is the one reader of configuration pins and the one producer of the block. It emits the block into the Phase 1 Bash result, and the report records the same values.

<!-- generated-from-registry-identity:headless_env_table — do not edit; run scripts/generate_contract_requirements.py -->
| Variable | Values | Default |
| --- | --- | --- |
| `CODE_GAUNTLET_MODEL_TIER` | `optimized` | `optimized` |
| `CODE_GAUNTLET_DELIVERY` | `chat,pr_comments,markdown` | `markdown` |
| `CODE_GAUNTLET_POST_MODE` | `dry-run,live` | `dry-run` |
| `CODE_GAUNTLET_PR_COMMENT_CAP` | `positive integer` | `6` |
| `CODE_GAUNTLET_DELIVERY_TIER` | `all,main_only` | `all` |
| `CODE_GAUNTLET_DRAFT_POLICY` | `review,skip` | `review` |
| `CODE_GAUNTLET_REVIEWED_POLICY` | `incremental,full,skip` | `full` |
| `CODE_GAUNTLET_PR_NOT_FOUND_POLICY` | `local,error` | `error` |
| `CODE_GAUNTLET_TRIVIAL_SCOPE` | `light,full` | `full` |
<!-- /generated-from-registry-identity:headless_env_table -->

- The resolver validates the model tier and resolves delivery methods.
- The resolver resolves posting mode, comment cap, and delivery tier.
- The resolver resolves draft, previously-reviewed, PR-not-found, and trivial-scope policies.

---

## Precedence

For each knob, resolve in this order and stop at the first hit:

**explicit env pin > REVIEW.md explicit value > headless default**

Only `delivery` has a REVIEW.md source: `default_delivery`. Every other knob resolves from its environment pin or mode default. The model tier remains fixed in interactive mode, while delivery tier and cap pins govern both modes.

---

## Validation

`scripts/resolve_config.py` validates pins in both modes. On the first invalid value, it prints exactly:

```
HEADLESS CONFIG ERROR: <VAR>=<value> not in {<allowed>,<values>}
```

The resolver stops with a non-zero outcome. It never falls back or asks. The local target rule rejects delivery containing `pr_comments`.

---

## Orchestrator model & per-agent pins (V3.1)

The **orchestrator** (the session running this skill and the workflow's own reasoning) is simply the session's model — there is no skill-level knob for it. In bench harness runs it is selected with `--child-model`; in real use it is whatever model the user's session runs.

**Per-agent pins are explicit full model IDs on first-party sessions** (`resolvePolicy` maps the policy aliases through a `MODEL_IDS` table: `sonnet` → `claude-sonnet-5`, `opus` → `claude-opus-4-8`, `haiku` → `claude-haiku-4-5-20251001`). Bare aliases resolve against the *session's* model variant at dispatch time — a session pinned to `sonnet[1m]` used to cascade the `[1m]` variant into every agent whose policy said `sonnet`. With full-ID pins, agent models are immune to the orchestrator's session variant. **The pin is conditional on `policy.provider`:** when the Phase 2 capture stamps `bedrock`/`vertex`/`foundry`, agents dispatch the bare aliases untouched — third-party providers use deployment-specific model IDs and would 400 the first-party names, so the deployment mapping (`ANTHROPIC_DEFAULT_*_MODEL`) is the resolution layer there, and variant-cascade immunity does not apply.

**Behavior change (intended):** the `CLAUDE_CODE_SUBAGENT_MODEL` override maps through the same first-party pin — a bare `sonnet` pins plain `claude-sonnet-5` instead of inheriting the session variant. Pass an explicit full/dated model ID if you need a specific variant. On a non-`firstParty` provider the override passes through verbatim (an explicit deployment ID like `us.anthropic.…` is exactly what the knob is for there).

---

## Hard rules (always true when headless — no env var toggles these)

- **PR-comment selection is deterministic.** The resolver's delivery list and the workflow's derived tier and cap select `artifactPaths.postReview`. The pipeline posts that payload verbatim.
- **Closed/merged PRs are reviewed, not skipped.** The headless branch runs against the pinned head. Posting follows the resolver's `post_mode` and delivery list, not PR state. **Markdown delivery in headless** means the report is already persisted at `artifactPaths.report`; no additional file is written.
- **`gh pr checkout` is never run.** Headless never checks out, fetches, or stashes to move the working tree — the harness pre-places a worktree pinned at the review head, and a checkout would abandon it for the live branch head. Instead verify the tree is already at the intended commit: compare `git rev-parse HEAD` against the PR's live head (`gh pr view <n> --json headRefOid`). If they match, review the current checkout as-is; if they differ, print `HEADLESS INPUT ERROR: working tree HEAD <sha> != PR head <sha>` and stop with a non-zero outcome — never silently review a different commit than the one pinned.
- **The Phase 3 wait is a held turn, never a yielded one.** Run `{plugin_root}/scripts/await_workflow.py` under an explicit Bash `timeout: 600000` and branch on its exit code; SKILL.md's "Wait protocol — MANDATORY" owns the full flow. Headless is where this matters most: a `-p` run blocks on background tasks still running at turn end only up to `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600000 ms) and then terminates them, so a child that ends its turn to wait for the completion notification loses any review that outlasts the ceiling. Bench children set that variable to `"0"` (wait unbounded) and so are already insulated; an ordinary headless run on the default is not, which is why the protocol may never be skipped here.
- **Task board = none.** The Phase 8 task-board offer is skipped; no tasks are created.
- **REVIEW.md setup and subdirectory notices = suppressed.** Neither is a question in either mode any more (issue #35); headless additionally suppresses the notice text, since no operator is reading it.
- **`build-review-md` is never invoked.** Headless runs never launch the REVIEW.md configuration wizard.
- **REVIEW.md is read-only.** Both remaining write paths (root scaffold, subdirectory scaffold) are disabled; the dismissed-findings append path no longer exists in either mode (issue #35). Reads run unchanged: the Phase 1 quick-check for `default_delivery`, the Phase 2d hierarchical parse, and the JS `filterStage`'s consumption of the parsed `reviewConfig`/`exclusionPatterns` (passed through the args waist).
- **The apply-checked patches render step is unconditional.** It runs on every Phase 8 pass whenever `artifactPaths.findings` is non-null — the same rule as interactive mode, no headless carve-out. It is read-only and posts nothing; its artifact path is named alongside the report path wherever the report path is disclosed.

---

## Per-gate resolution

Every interactive gate in the pipeline maps to a deterministic headless outcome. Each gate's own site carries a headless exception note; this table is the index.

| Gate (site) | Headless resolution |
|---|---|
| Phase 1 configuration resolution | Resolve every knob per precedence; print the `Headless config:` block. No question in either mode since issue #35 — interactive prints `Resolved config:` instead. |
| Phase 2 entry gate | Passes if the `Headless config:` block was printed in Phase 1 (interactive: `Resolved config:`). The gate reads resolved state, never whether a prompt occurred. |
| PR-not-found (resolution failure) | `resolved.pr_not_found_policy`: `error` stops the run; `local` proceeds as a local review. |
| Closed / merged PR (eligibility) | Proceed — do not stop. Review the pinned head as resolved; posting follows `resolved.post_mode` and delivery follows `resolved.delivery`. (Interactive mode stops here; headless does not.) |
| Draft PR | `resolved.draft_policy`: `review` proceeds; `skip` stops the run. |
| Previously reviewed (Phase 2 2b-post step 3, after checkout) | `resolved.reviewed_policy`: `incremental` scopes the diff to new commits only when `detect_prior_review.py`'s `incremental_safe` is true; unsafe history degrades to `full` and discloses the degradation. `skip` stops the run only when `sha_is_ancestor` is true. |
| Trivial / light-scope (all low-risk, <50 lines) | The workflow derives headless `scopeAnswer` from the resolver receipt and the risk table. |
| REVIEW.md detection (root setup + subdirectory offer) | Discovered configs apply as in interactive mode: root defaults plus matching subtree overrides; never invoke `build-review-md`. |
| Phase 8 Stage 1 (delivery question) | Not asked. Deliver per `resolved.delivery` and post `artifactPaths.postReview` verbatim. The workflow derives tier and cap from the resolver receipt. Posting follows `resolved.post_mode`. |
| Phase 8 Stage 2 (task board) | Skipped. |

---

## `Headless config:` echo block

Immediately after resolving all knobs in Phase 1, the resolver renders the block into the Phase 1 Bash result. It has one line per knob, `key=value (source)` where `source ∈ env|review_md|default`.

```
Headless config:
  model_tier=optimized (env)
  delivery=pr_comments,markdown (env)
  post_mode=dry-run (env)
  pr_comment_cap=25 (env)
  delivery_tier=all (default)
  draft_policy=review (env)
  reviewed_policy=full (env)
  pr_not_found_policy=error (env)
  trivial_scope=full (env)
  pipeline_version=3.1.3 (bundle)
  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)
```

The nine echoed knobs are followed by `pipeline_version` and `plugin_root` identity lines. The report renders the validated waist, and the Phase 1 Bash result carries the resolver block.

The example shows a bench-configured run (env overrides throughout) except `delivery_tier`, which bench leaves unset so it resolves to the `all` default — the benchmark posts every challenge-survivor, which is the intended default. A run relying on headless defaults would show e.g. `delivery=markdown (default)` and `pr_comment_cap=6 (default)`, and a REVIEW.md-sourced value would show e.g. `delivery=chat (review_md)`.

The pipeline renders the block in the report's last `Review Methodology` section. At delivery,
point the chat methodology to that section and include the materialization proof, patches path,
delivery outcome, post-report gaps, and duration. If no report materializes, repeat the block in
the final message as the fallback receipt; do not claim the three surfaces are byte-identical.

---

## Prerequisite

`gh` / `glab` authentication is **ambient** — headless mode assumes the CLI is already authenticated in the environment (there is no interactive login step). PR/MR resolution, diff fetch, and (in `live` post mode) comment posting all rely on that ambient auth.
