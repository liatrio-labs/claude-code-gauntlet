# Headless Mode Reference

Code-gauntlet runs unattended when `CODE_GAUNTLET_HEADLESS=1`. In headless mode there is no user to answer an `AskUserQuestion`, so **every interactive gate is resolved deterministically from the environment** and the skill never prompts. A single `AskUserQuestion` call in a headless run deadlocks the process — the harness marks such runs invalid.

This file is the authority for the headless contract: the env variables, their precedence, the validation rule, the hard rules that always hold, the per-gate resolution, and the `Headless config:` echo block a runner parses.

---

## Env contract

Read once at Phase 1 entry. Every value is echoed in a `Headless config:` block (stdout) and recorded in the report methodology section. An invalid value fails loud naming the variable — never a silent fallback, never a question.

| Variable | Values (headless default) | Pins |
|---|---|---|
| `CODE_GAUNTLET_HEADLESS` | `1` | master switch |
| `CODE_GAUNTLET_MODEL_TIER` | `optimized` (`optimized`) — the single benchmarked policy; any other value fails loud | Phase 1 gate (a) |
| `CODE_GAUNTLET_DELIVERY` | subset of `chat,pr_comments,markdown` (`markdown`) | Phase 1 gate (b); `pr_comments` illegal for local targets |
| `CODE_GAUNTLET_POST_MODE` | `dry-run`\|`live` (`dry-run`) | whether post_review.py gets `--dry-run`; post_review.py also reads this var directly and self-enforces dry-run (belt-and-braces) |
| `CODE_GAUNTLET_PR_COMMENT_CAP` | int (`6`) | Phase 8 Stage 1 cap; threaded into `limits.deliveryCap` so the workflow's `selectDelivery` applies it; bench sets 25 (flood guard) |
| `CODE_GAUNTLET_DELIVERY_TIER` | `all`\|`main_only` (`all`) | which challenge-survivors post as PR comments; threaded into `args.delivery.tier` for `selectDelivery`; default `all` posts everything (bench leaves it unset → `all`) |
| `CODE_GAUNTLET_DRAFT_POLICY` | `review`\|`skip` (`review`) | draft-PR gate |
| `CODE_GAUNTLET_REVIEWED_POLICY` | `incremental`\|`full`\|`skip` (`full`) | previously-reviewed gate, every branch |
| `CODE_GAUNTLET_PR_NOT_FOUND_POLICY` | `local`\|`error` (`error`) | resolution-failure gate |
| `CODE_GAUNTLET_TRIVIAL_SCOPE` | `light`\|`full` (`full`) | trivial-PR scope gate — stamped verbatim into `args.scopeAnswer`; the workflow derives dimension flags from it plus `riskTable`/`changedLines` (`light` -> Discover runs bugs+security only, `full` -> all dimensions) |

---

## Precedence

For each knob, resolve in this order and stop at the first hit:

**explicit env pin > REVIEW.md explicit value > headless default**

REVIEW.md contributes only its two native keys — `model_tier` and `default_delivery` (→ `delivery`). For every other knob there is no REVIEW.md source, so resolution is env pin > headless default. This mirrors the interactive precedence (user answer > REVIEW.md > ask), with the env pin standing in for the user's answer and the headless default standing in for the question.

---

## Validation

Each knob's resolved value must be a member of its allowed set (see the table). On the first invalid value, print exactly:

```
HEADLESS CONFIG ERROR: <VAR>=<value> not in {<allowed>,<values>}
```

and stop the run with a non-zero outcome. **Never** fall back to a default and never ask. `<VAR>` is the full environment variable name (e.g. `CODE_GAUNTLET_MODEL_TIER`); `<value>` is the offending value; `{…}` lists the allowed values. `CODE_GAUNTLET_PR_COMMENT_CAP` must parse as a positive integer. `CODE_GAUNTLET_DELIVERY` is a comma-separated subset of `chat,pr_comments,markdown`; `pr_comments` is invalid when the review target is local (no PR/MR to post to). `CODE_GAUNTLET_DELIVERY_TIER` must be `all` or `main_only`.

---

## Orchestrator model & per-agent pins (V3.1)

The **orchestrator** (the session running this skill and the workflow's own reasoning) is simply the session's model — there is no skill-level knob for it. In bench harness runs it is selected with `--child-model`; in real use it is whatever model the user's session runs.

**Per-agent pins are explicit full model IDs on first-party sessions** (`resolvePolicy` maps the policy aliases through a `MODEL_IDS` table: `sonnet` → `claude-sonnet-5`, `opus` → `claude-opus-4-8`, `haiku` → `claude-haiku-4-5-20251001`). Bare aliases resolve against the *session's* model variant at dispatch time — a session pinned to `sonnet[1m]` used to cascade the `[1m]` variant into every agent whose policy said `sonnet`. With full-ID pins, agent models are immune to the orchestrator's session variant. **The pin is conditional on `policy.provider`:** when the Phase 2 capture stamps `bedrock`/`vertex`/`foundry`, agents dispatch the bare aliases untouched — third-party providers use deployment-specific model IDs and would 400 the first-party names, so the deployment mapping (`ANTHROPIC_DEFAULT_*_MODEL`) is the resolution layer there, and variant-cascade immunity does not apply.

**Behavior change (intended):** the `CLAUDE_CODE_SUBAGENT_MODEL` override maps through the same first-party pin — a bare `sonnet` pins plain `claude-sonnet-5` instead of inheriting the session variant. Pass an explicit full/dated model ID if you need a specific variant. On a non-`firstParty` provider the override passes through verbatim (an explicit deployment ID like `us.anthropic.…` is exactly what the knob is for there).

---

## Hard rules (always true when headless — no env var toggles these)

- **PR-comment selection is deterministic.** The set posted is the pipeline's pre-selected `artifactPaths.postReview` payload — the challenge-survivors filtered by the delivery tier (`CODE_GAUNTLET_DELIVERY_TIER`: `all` by default → every survivor, main and suggestion tags alike; `main_only` → main-tagged only), then ranked and capped at `limits.deliveryCap`. Posted verbatim, never re-filtered or re-ranked. The default `all` is deliberate — headless posts everything that survives the blind challenge. The per-finding interactive walkthrough (the unbounded question loop) is structurally unreachable, which in turn makes the dismissed-findings gate unreachable.
- **Closed/merged PRs are reviewed, not skipped.** The interactive closed/merged stop does not apply — headless runs the full pipeline against the pinned head exactly as resolved. Benchmarking historical (already-merged) PRs is the primary headless use case; posting safety is governed by `CODE_GAUNTLET_POST_MODE` (`dry-run` writes a payload and posts nothing), not by PR state. Phase 8 delivery follows `CODE_GAUNTLET_DELIVERY` regardless of whether the PR is open, closed, or merged — the interactive chat/markdown-only restriction on closed/merged PRs does not apply headless. **Markdown delivery in headless** means the report is already persisted at the path in `artifactPaths.report`; no additional file is written (same as interactive Step C — never a root-level `code-gauntlet-{date}.md`).
- **`gh pr checkout` is never run.** Headless never checks out, fetches, or stashes to move the working tree — the harness pre-places a worktree pinned at the review head, and a checkout would abandon it for the live branch head. Instead verify the tree is already at the intended commit: compare `git rev-parse HEAD` against the PR's live head (`gh pr view <n> --json headRefOid`). If they match, review the current checkout as-is; if they differ, print `HEADLESS INPUT ERROR: working tree HEAD <sha> != PR head <sha>` and stop with a non-zero outcome — never silently review a different commit than the one pinned.
- **The Phase 3 wait is a held turn, never a yielded one.** Run `{plugin_root}/scripts/await_workflow.py` under an explicit Bash `timeout: 600000` and branch on its exit code; SKILL.md's "Wait protocol — MANDATORY" owns the full flow. Headless is where this matters most: a `-p` run blocks on background tasks still running at turn end only up to `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (default 600000 ms) and then terminates them, so a child that ends its turn to wait for the completion notification loses any review that outlasts the ceiling. Bench children set that variable to `"0"` (wait unbounded) and so are already insulated; an ordinary headless run on the default is not, which is why the protocol may never be skipped here.
- **Task board = none.** The Phase 8 task-board offer is skipped; no tasks are created.
- **REVIEW.md setup and subdirectory prompts = skip.** Neither the root-setup offer nor the subdirectory-REVIEW.md offer is presented.
- **`build-review-md` is never invoked.** Headless runs never launch the REVIEW.md configuration wizard.
- **REVIEW.md is read-only.** All three write paths (root scaffold, subdirectory scaffold, dismissed-findings `## Ignore` append) are disabled. Reads run unchanged: the Phase 1 quick-check for `model_tier`/`default_delivery`, the Phase 2d hierarchical parse, and the JS `filterStage`'s consumption of the parsed `reviewConfig`/`exclusionPatterns` (passed through the args waist).

---

## Per-gate resolution

Every interactive gate in the pipeline maps to a deterministic headless outcome. Each gate's own site carries a `> Headless exception (CODE_GAUNTLET_HEADLESS=1): …` note; this table is the index.

| Gate (site) | Headless resolution |
|---|---|
| Pre-flight configuration gate (Phase 1) | Resolve `model_tier` + `delivery` + `delivery_tier` per precedence; print the `Headless config:` block. No question. |
| Phase 2 entry check | Passes if the `Headless config:` block was printed in Phase 1; do not return to the gate. |
| PR-not-found (resolution failure) | `CODE_GAUNTLET_PR_NOT_FOUND_POLICY`: `error` stops the run; `local` proceeds as a local review. |
| Closed / merged PR (eligibility) | Proceed — do not stop. Review the pinned head as resolved; posting still obeys `CODE_GAUNTLET_POST_MODE` and delivery follows `CODE_GAUNTLET_DELIVERY`. (Interactive mode stops here; headless does not.) |
| Draft PR | `CODE_GAUNTLET_DRAFT_POLICY`: `review` proceeds; `skip` stops the run. |
| Previously reviewed (Phase 2 2b-post step 3, after checkout) | `CODE_GAUNTLET_REVIEWED_POLICY`: `incremental` scopes the diff to new commits only when `detect_prior_review.py`'s `incremental_safe` is true; when the head has not advanced, the recorded SHA is unresolvable, history was rewritten (rebase, squash, or a backward force-push — the reviewed commit is no longer an ancestor of the head), or detection errored, it degrades to `full` and the degradation is disclosed in the methodology. `full` always reviews from scratch. `skip` stops the run only when `previously_reviewed` is true AND `sha_is_ancestor` is true — on rewritten history the tree is effectively unreviewed, mirroring the interactive gate's neither-template branch, so `skip` never stops the run there; it proceeds as a full review with the degradation disclosed. Detection is a read-only script call that exits 0 for every outcome, so it is safe under `CODE_GAUNTLET_POST_MODE=dry-run` and can never fail the run. |
| Trivial / light-scope (all low-risk, <50 lines) | `CODE_GAUNTLET_TRIVIAL_SCOPE`, stamped verbatim into `args.scopeAnswer`: `light` -> Discover runs bugs+security only (2 agents); `full` -> all dimensions. The workflow derives the dimension flags itself (`deriveAgentFlags`) from `scopeAnswer` plus `riskTable`/`changedLines`. |
| REVIEW.md detection (root setup + subdirectory offer) | Skip; root config applies; never invoke `build-review-md`. |
| Phase 8 Stage 1 (PR comment selection) | Post `artifactPaths.postReview` verbatim — the workflow already applied the delivery tier (`CODE_GAUNTLET_DELIVERY_TIER`, default `all`) plus rank + cap `CODE_GAUNTLET_PR_COMMENT_CAP` (via `limits.deliveryCap`); the walkthrough is unavailable. Posting obeys `CODE_GAUNTLET_POST_MODE`. |
| Phase 8 Stage 2 (task board) | Skipped. |
| Phase 8 Stage 3 (dismissed findings) | Unreachable (no walkthrough ⇒ empty dismissed_set); never write REVIEW.md. |

---

## `Headless config:` echo block

Immediately after resolving all knobs in Phase 1, print the block below to stdout — one line per knob, `key=value (source)` where `source ∈ env|review_md|default`. The key names are exact and stable; a runner parses this block, so do not rename keys, reorder is tolerated but discouraged, and emit every knob every run.

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

The nine echoed knobs are every variable except the master switch `CODE_GAUNTLET_HEADLESS`. Two additional **identity receipt** lines follow: `pipeline_version` (the `PIPELINE_VERSION` constant from `{plugin_root}/workflows/pipeline.js`, source `(bundle)`) and `plugin_root` (absolute path resolved from this SKILL.md — two levels up from `skills/code-gauntlet/`, source `(resolved)`). Runners parse these to reject wrong-plugin children. Emit them every headless run; keep all three copies byte-identical.

The example shows a bench-configured run (env overrides throughout) except `delivery_tier`, which bench leaves unset so it resolves to the `all` default — the benchmark posts every challenge-survivor, which is the intended default. A run relying on headless defaults would show e.g. `delivery=markdown (default)` and `pr_comment_cap=6 (default)`, and a REVIEW.md-sourced value would show e.g. `pr_comment_cap=10 (review_md)`.

**Emit the block in three places, verbatim and identical:** (1) Phase 1 stdout (as above); (2) the markdown report's methodology section; and (3) the **final response message** of the run. The three copies must be byte-identical. The final-response copy is the machine-parsed receipt for `-p --output-format json` runs: intermediate-turn stdout is not captured in the result envelope, so only the last message survives in `.result`. A runner that cannot see Phase 1 stdout therefore recovers the receipt from the final message, or from the collected report markdown — all three carry the same block so the receipt is verifiable regardless of which output the runner can observe.

---

## Prerequisite

`gh` / `glab` authentication is **ambient** — headless mode assumes the CLI is already authenticated in the environment (there is no interactive login step). PR/MR resolution, diff fetch, and (in `live` post mode) comment posting all rely on that ambient auth.
