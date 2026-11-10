# Headless Mode Reference

## Contents

- [Env contract](#env-contract)
- [Precedence](#precedence)
- [Validation](#validation)
- [Model policy](#model-policy)
- [Hard rules (always true when headless — no env var toggles these)](#hard-rules-always-true-when-headless--no-env-var-toggles-these)
- [Prior-review policy](#prior-review-policy)
- [Per-gate resolution](#per-gate-resolution)
- [`Headless config:` echo block](#headless-config-echo-block)
- [Prerequisite](#prerequisite)

Code-gauntlet runs unattended when `CODE_GAUNTLET_HEADLESS=1`. In headless mode, no `AskUserQuestion` is ever presented: every interactive gate is resolved deterministically from the resolver result. A single `AskUserQuestion` call in a headless run deadlocks the process — the harness marks such runs invalid.

---

## Env contract

The resolver is the one reader of configuration pins and the one producer of the block. It emits the block into the Phase 1 Bash result, and the report records the same values.

<!-- generated-from-registry-identity:headless_env_table — do not edit; run scripts/generate_contract_requirements.py -->
| Variable | Values | Default |
| --- | --- | --- |
| `CODE_GAUNTLET_MODEL_TIER` | `optimized` | `optimized` |
| `CODE_GAUNTLET_DELIVERY` | `chat,pr_comments,markdown` | `markdown` |
| `CODE_GAUNTLET_POST_MODE` | `dry-run,live` | `dry-run` |
| `CODE_GAUNTLET_PR_COMMENT_CAP` | `positive integer` | `25` |
| `CODE_GAUNTLET_DELIVERY_TIER` | `all,main_only` | `all` |
| `CODE_GAUNTLET_DRAFT_POLICY` | `review,skip` | `review` |
| `CODE_GAUNTLET_REVIEWED_POLICY` | `incremental,full,skip` | `full` |
| `CODE_GAUNTLET_PR_NOT_FOUND_POLICY` | `local,error` | `error` |
| `CODE_GAUNTLET_TRIVIAL_SCOPE` | `light,full` | `full` |
<!-- /generated-from-registry-identity:headless_env_table -->

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

## Model policy

See [model-policy.md](model-policy.md) for stage defaults, overrides, and provider naming.

---

## Hard rules (always true when headless — no env var toggles these)

- **PR-comment selection is deterministic.** Post the pipeline's pre-selected `artifactPaths.postReview` payload verbatim, never re-filtered or re-ranked; the workflow already applied the receipt-backed tier (`all` or `main_only`) and cap. Neither mode asks per-finding or dismissed-findings questions.
- **Closed/merged PR state does not stop a review.** Posting follows the resolver's `post_mode` and delivery list regardless of PR state. **Markdown delivery in headless** means the report is already persisted at `artifactPaths.report`; no additional file is written.
- **`gh pr checkout` is never run.** Never check out, fetch, or stash to move the working tree: the harness pre-places a worktree pinned at the review head. Composite A compares `git rev-parse HEAD` with the PR head from its status receipt; on a mismatch it prints `HEADLESS INPUT ERROR: working tree HEAD <sha> != PR head <sha>` and stops with a non-zero outcome — never review a different commit than the one pinned.
- **Hold the turn during the Phase 3 wait.** Follow [phase3-dispatch.md](phase3-dispatch.md#wait-protocol) in both modes, including terminal failure salvage.
- **Task board = none.** The Phase 8 task-board offer is skipped; no tasks are created.
- **The apply-checked patches render step is unconditional.** It runs on every Phase 8 pass whenever `artifactPaths.findings` is non-null — the same rule as interactive mode, no headless carve-out. It is read-only and posts nothing; its artifact path is named alongside the report path wherever the report path is disclosed.

---

## Prior-review policy

Run read-only prior-review detection even headless, after the head check. Use
`configResult.resolved.reviewed_policy` instead of asking:

- `skip` stops only when `previously_reviewed` and `sha_is_ancestor` are both true.
- `incremental` uses the detector's `incremental_safe`; otherwise use full review and disclose why.
- Rewritten history (`sha_is_ancestor: false`) proceeds as full review, including for `skip`.
- Resolve `DEFERRED` truncation with the same decision. Run
  `{python} "{plugin_root}/scripts/stale_truncate.py" --output-dir "{output_dir}" --head-sha {head_sha_short} --unconditional`
  for every outcome except a `skip` that stops. An exit 2 truncated nothing; resolve it the same way.

Retain detector facts in `reviewScope`; omit receipt-derived `requested` headless.
Detection failures and degradations are disclosed even when detection also found a prior review.

## Per-gate resolution

Every interactive gate maps to a deterministic headless outcome. Phase 1 eligibility is owned by `check_eligibility.py`; this table indexes gate policies.

| Gate (site) | Headless resolution |
|---|---|
| Phase 1 configuration resolution | Resolve every knob per precedence; print the `Headless config:` block. Neither mode asks a question — interactive prints `Resolved config:` instead. |
| Phase 2 entry gate | Passes if the `Headless config:` block was printed in Phase 1 (interactive: `Resolved config:`). The gate reads resolved state, never whether a prompt occurred. |
| PR-not-found (resolution failure) | `resolved.pr_not_found_policy`: `error` stops the run; `local` proceeds as a local review. |
| Closed/merged PR | `check_eligibility.py` bypasses the interactive closed stop, then evaluates the remaining eligibility rules. |
| Empty / lockfile-only PR | `check_eligibility.py` returns `stop` before draft handling. |
| Draft PR | `check_eligibility.py` applies `resolved.draft_policy`: `review` proceeds; `skip` stops the run. |
| Previously reviewed (Phase 2 2b-post step 3, after checkout) | `resolved.reviewed_policy`, per [Prior-review policy](#prior-review-policy): `skip` stops only when `previously_reviewed` and `sha_is_ancestor` are both true; `incremental` falls back to a disclosed full review unless `incremental_safe`. |
| Trivial / light-scope (all low-risk, <50 lines) | `CODE_GAUNTLET_TRIVIAL_SCOPE` selects the scope: `light` dispatches only the two core agents (`bug-detector`, `security-reviewer`); `full` runs all seven discovery agents covering nine dimensions. The resolver emits no resolved key for `trivial_scope`. |
| REVIEW.md detection | Discovered configs apply as in interactive mode: root defaults plus matching subtree overrides. |
| Phase 8 Stage 1 (delivery question) | Not asked. Deliver per `resolved.delivery` and post `artifactPaths.postReview` verbatim, including its renderer-owned outcome-first Summary body. Posting follows `resolved.post_mode`; `dry-run` captures the payload without posting. |
| Checkpoint recovery | Follow [crash recovery](crash-recovery.md#3-checkpoint-resume-fallback-when-resumefromrunid-is-unavailable): one dispatched review retry (a checksum re-copy dispatches nothing and does not count); after a second checksum refusal deliver partial report plus gaps and stop, with no fresh paid review. |
| Phase 8 Stage 2 (task board) | Skipped; no tasks are created. |

---

Prior-review detection and GitLab deduplication read only entries the posting account
wrote. In CI, set `CODE_GAUNTLET_POSTER` to that account (for example
`github-actions[bot]` under Actions' `GITHUB_TOKEN`); without it, a token that cannot
read `/user` gets a full review every run. The trust rules are in
`references/phase1-preflight.md` under the detector's receipt.

## `Headless config:` echo block

Immediately after resolving all knobs in Phase 1, the resolver renders the block into the Phase 1 Bash result. It has one line per knob, `key=value (source)` where `source ∈ env|review_md|default`.

```
Headless config:
  model_tier=optimized (env)
  delivery=pr_comments,markdown (env)
  post_mode=dry-run (env)
  pr_comment_cap=25 (env)
  delivery_tier=all (env)
  draft_policy=review (env)
  reviewed_policy=full (env)
  pr_not_found_policy=error (env)
  trivial_scope=full (env)
  pipeline_version=3.1.3 (bundle)
  plugin_root=/absolute/path/to/claude-code-gauntlet (resolved)
```

The pipeline renders the block in the report's last `Review Methodology` section. At delivery, point the chat methodology to that section and include the materialization proof, patches path, delivery outcome, post-report gaps, and duration.
If no report materializes, repeat the block in the final message as the fallback receipt.
Do not claim the three surfaces are byte-identical.

---

## Prerequisite

`gh` / `glab` authentication is **ambient** — headless mode assumes the CLI is already authenticated in the environment (there is no interactive login step). PR/MR resolution, diff fetch, and (in `live` post mode) comment posting all rely on that ambient auth.
