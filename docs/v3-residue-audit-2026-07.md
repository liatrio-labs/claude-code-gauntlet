# v2→v3 residue audit — 2026-07

**Date:** 2026-07-31. **HEAD:** `6ef3d9c`. **Tracking:** GitHub #37 (Wave 1 of #101).
**Scope:** `skills/`, `agents/`, `scripts/`, `workflows/src/`, and `.github/`.

**This is a point-in-time inventory.** Commands and results below were measured at the pinned HEAD.
The living machine-string contract is
[docs/machine-parsed-strings.md](machine-parsed-strings.md); re-run the commands for current state.

The adversarial pass (§3.8, `R-012`–`R-042`) ran at HEAD `a103945` on the same branch. It produced
dispositions only: mechanical corrections and new issue filings are deliberately left to the
following tasks, so a `fix-in-place` row here is a decision, not an applied edit.

## 1. Owned elsewhere

These known instances are recorded but not re-litigated or fixed under #37.

| ID | Instance | Owner |
| --- | --- | --- |
| R-001 | `report-format.md` is held by the orchestrator but never passed to `report-writer` | #36 |
| R-002 | `phase8-delivery.md` tells the orchestrator to render the full report | #36 |
| R-003 | `filterFindings.js` exports shipped-but-uncalled `parseReviewMd` / `loadExclusions` | #24 |
| R-004 | Limits defaults exist only in prompt-facing contracts | #24 |
| R-005 | Three implementations treat a missing `REVIEW.md` differently | #35 |
| R-006 | `phase2-triage.md` carries a stale prompts pointer | #35 |
| R-007 | `#17` roadmap pointers now belong to the V3.2 / fable work | #29 |

The closed #30 contribution-surface work and the #36 marker-payload skew are not open inventory
items. If rediscovered, they will be recorded as `resolved-on-main`.

## 2. Intentional retention

| ID | Surface | Reason |
| --- | --- | --- |
| R-008 | False-positive exclusions duplicated across seven discovery agents | Failure isolation: each agent retains the exclusions if a shared read fails |
| R-009 | Python transform twins and frozen parity fixtures | Python remains the CLI/shared-library surface; JS is required in the sandbox |
| R-010 | v2 persist alias fields | Compatibility at the artifact-writer persistence boundary |
| R-011 | `ndjson-emission-contract.md` and `validate_ndjson.py` | Retained v2-compatibility and benchmark surface, not a v3 discovery-agent contract |

All four rows are `intentional-and-documented`; this audit must not delete them.

## 3. Method

### 3.1 Measurement pin

```text
$ git rev-parse --short HEAD
6ef3d9c
```

The branch was `feat/issue-37-v3-residue-audit`, and `git status --short` produced no output before
the inventory file was created.

### 3.2 Orphaned-reference detector

```text
$ python -m pytest tests/test_references_reachability.py -q
..                                                                       [100%]
2 passed in 0.02s
```

Result: no orphan candidates.

### 3.3 Dead-export detector

```text
$ python -m pytest tests/test_workflows_dead_exports.py -q
..                                                                       [100%]
2 passed in 0.09s
```

`EXPORT_ALLOWLIST` contains two known candidates, both owned by #24:

```text
filterFindings.js:parseReviewMd -> owned-elsewhere:#24
filterFindings.js:loadExclusions -> owned-elsewhere:#24
```

Result: no new dead-export candidates.

### 3.4 Stale version and roadmap grep

```text
$ rg -n "Phase 3 — Review agents|Phase 4 — Classify|deep-review v2|TODO.*#17|issue #17|frontier.*tier|2\.3\.4" \
    skills/ agents/ scripts/ workflows/src/ .github/ \
    --glob '!**/pipeline.js'
workflows/src/registry.js:149:  // roadmap work (issue #17 V3.2) and land behind their own paired measurement.
skills/code-gauntlet/SKILL.md:28:code-gauntlet v3 requires Claude Code >= 2.1.154 with dynamic workflows. Install the pre-rename deep-review v2.x for older CLIs.
skills/code-gauntlet/SKILL.md:93:... Alternate model modes are roadmap work (issue #17). ...
skills/code-gauntlet/references/phase1-preflight.md:207:... legacy v2-era files in the wild say `frontier` ... roadmap work tracked in issue #17 ...
skills/code-gauntlet/references/phase2-triage.md:384:... Alternate model modes are roadmap work (issue #17).
skills/code-gauntlet/references/review-md-spec.md:63:     benchmarked policy. Alternate modes are roadmap work (issue #17). -->
skills/code-gauntlet/references/review-md-spec.md:159:... legacy v2-era files say `frontier` ... roadmap work (issue #17) ...
```

Result: seven hits. The six `#17` / `frontier` roadmap hits are pre-dispositioned as R-007. The
`deep-review v2.x` compatibility message is a triage candidate, not a finding: its current wording
may be intentional. No retired phase-name or `2.3.4` hit appeared.

### 3.5 Canonical-source drift

```text
$ rg -n "Canonical source:" skills/ agents/ scripts/ workflows/src/ .github/
# 35 lines:
#   10 complete-read agent-copy markers
#    9 investigation-methodology agent-copy markers
#    7 false-positive-exclusion agent-copy markers
#    9 source, rules, or generated-snippet markers
```

The one canonical source with explicit byte boundaries has a direct guard:

```text
$ python -m pytest \
    tests/test_agent_contracts.py::TestCompleteReadContract::test_every_file_reading_agent_carries_the_block_byte_identically \
    -q
.                                                                        [100%]
1 passed in 0.01s
```

A byte-containment probe over the other marked agent copies produced:

```text
$ python3 -c 'from pathlib import Path
root = Path(".")
checks = [
    ("false-positive-exclusions.md", [
        "bug-detector", "security-reviewer", "cross-file-impact", "test-analyzer",
        "conventions-and-intent", "type-design-analyzer", "code-simplifier",
    ], "---\n\n", False),
    ("investigation-methodology.md", [
        "bug-detector", "security-reviewer", "cross-file-impact", "test-analyzer",
        "conventions-and-intent", "type-design-analyzer", "code-simplifier",
        "validator", "challenger",
    ], "## LSP-first investigation\n", True),
]
for filename, agents, separator, restore_separator in checks:
    text = (root / "skills/code-gauntlet/references" / filename).read_text()
    body = text.split(separator, 1)[1]
    if restore_separator:
        body = separator + body
    missing = [
        name for name in agents
        if body not in (root / "agents" / f"{name}.md").read_text()
    ]
    print(
        f"{filename}: exact={len(agents) - len(missing)}/{len(agents)}; "
        f"non_exact={missing}"
    )'
false-positive-exclusions.md: exact=0/7; non_exact=['bug-detector', 'security-reviewer', 'cross-file-impact', 'test-analyzer', 'conventions-and-intent', 'type-design-analyzer', 'code-simplifier']
investigation-methodology.md: exact=0/9; non_exact=['bug-detector', 'security-reviewer', 'cross-file-impact', 'test-analyzer', 'conventions-and-intent', 'type-design-analyzer', 'code-simplifier', 'validator', 'challenger']
```

Those 16 results are candidates, not drift verdicts. The canonical investigation file explicitly
calls its copies “adapted,” while the false-positive source calls its copies adapted in practice but
has no machine-readable block boundaries. The adversarial pass must distinguish intentional
compression from semantic drift. No fix is made in this pass.

### 3.6 Duplication-register intersection

`docs/duplication-register.md` was opened and intersected with this scope. Its nine `accidental`
rows are owned by #110 and must not be re-filed here. Its intentional agent copies, Python/JS twins,
and parity-fixture rows support R-008 and R-009.

### 3.7 Machine-parsed string registry

```text
$ python -m pytest tests/test_machine_parsed_strings.py -q
..                                                                       [100%]
2 passed in 0.01s
```

The Task 1 guard is green. Seed-row compromise: the registry deliberately proves exact-string
presence in every producer and at least one parser, not semantic shape equality. Deeper semantic
contracts remain a skipped-with-reason decision for the adversarial pass.

### 3.8 Adversarial pass (auditor → blind challenge → adjudicate)

Run at HEAD `a103945`. Five auditors took one slice each — `skills/code-gauntlet/`, `agents/`,
`scripts/`, `workflows/src/`, `.github/` — and were handed the detector candidates plus the
`R-001`–`R-011` skip list and the #55/#110 clone-pair exclusion.

Every allegation was then challenged by a separate agent that received only the title, the location,
a self-contained factual description, and the raw excerpt at the alleged location. Challengers did
**not** receive the auditor's reasoning, the disagreeing-path quote, the suggested disposition, or
the auditor's confidence, and each was told on which grounds a claim should be rejected or narrowed.
Challenges were partitioned by subject rather than by auditor slice, so the three threshold
allegations (`R-019`, `R-021`, `R-022`) were settled against one independently established ground
truth.

```text
alleged                     29
confirmed as alleged        19
revised (narrowed)          10
rejected                     0
challenger verdicts overturned in adjudication   1
```

The overturned verdict is `R-022`. The challenger labelled it `REJECTED` while its own evidence
established the opposite — that `filterFindings.js` applies 70 to security and 55 to non-security
dimensions when `reviewConfig` omits a threshold, which is exactly the contradiction alleged. The
label was not supported by the body, so adjudication confirmed the allegation.

Nothing was rejected outright. The 29 allegation outcomes map to 27 inventory rows: the two
`filter_findings.py` comment allegations were merged as `R-023` because they repeat the same false
consensus claim about the same mechanism, and the separate `actions/checkout` and
`actions/setup-python` pin-split allegations were merged as `R-039` because both have the same
cross-workflow inconsistency and #106 disposition. `R-039` is therefore recorded as an
`owned-elsewhere` cross-link rather than #37 work.

The receipt-stream inconsistency in the #110 duplication register was excluded before inventory
assignment, as §3.6 requires; it is not an allegation-derived residue row. The three Appendix A
candidates were also outside the 29 allegations. Their dispositions add three rows (`R-040`–`R-042`)
to the 27 allegation-derived rows, yielding 30 adversarial-pass inventory rows in total.

Three candidates carried in from Appendix A were dispositioned rather than promoted as defects:
`C-001` → `R-040`, `C-002` → `R-041`, `C-003` → `R-042`. The `C-002` comparison did surface one real
semantic narrowing, carved out separately as `R-037`.

Two rows exist only because the #55 comment-accuracy pass corrected one copy of a false statement
and missed another: `R-023` (`filter_findings.py:41,704` versus the corrected docstring at `:584`)
and `R-024` (`apply_challenges.py:42-43` versus the corrected `filter_findings.py:946`). A
single-file comment sweep does not converge on duplicated prose.

## 4. Findings by class

Every row below is pre-loaded from the approved design or refreshed owned-elsewhere triage. Cheap
detector candidates are kept in Appendix A until challenged; they are not promoted to findings here.

### 4.1 Orphaned references and contracts

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-001 | `report-format.md` ↔ `report-writer` | orphaned reference / contract | Orchestrator holds the reference but does not pass it to the writer | owned-elsewhere | #36 |
| R-004 | Prompt-only limits defaults | dead / contract | Defaults are contract text without a live pipeline path | owned-elsewhere | #24 |

### 4.2 Stale instructions and roadmap pointers

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-002 | `phase8-delivery.md` render-full-report instruction | stale instruction | Rendering ownership moved out of the orchestrator | owned-elsewhere | #36 |
| R-006 | `phase2-triage.md` prompts pointer | stale pointer | Known three-way `REVIEW.md` follow-up | owned-elsewhere | #35 |
| R-007 | Five scoped files carrying `#17` roadmap pointers | stale roadmap | Cheap grep returned six lines across five files | owned-elsewhere | #29 |

### 4.3 Dead code paths

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-003 | `filterFindings.js` `parseReviewMd` / `loadExclusions` | dead code path | Dead-export guard passes only through its cited allowlist | owned-elsewhere | #24 |

### 4.4 Diverged and intentional duplicates

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-005 | Three-way missing-`REVIEW.md` behavior | diverged duplicates | Known behavior split from refreshed triage | owned-elsewhere | #35 |
| R-008 | False-positive exclusions ×7 agents | intentional duplication | Failure-isolation contract in `agents/AGENTS.md` and duplication register | intentional-and-documented | none |
| R-009 | Python twins plus parity fixtures | intentional retention | Cross-runtime parity is frozen by generated golden fixtures | intentional-and-documented | none |

### 4.5 Compatibility retention

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-010 | v2 persist alias fields | intentional retention | Artifact-writer compatibility boundary | intentional-and-documented | none |
| R-011 | `ndjson-emission-contract.md` + `validate_ndjson.py` | intentional retention (v2-compat) | Discovery-agent scrub excludes this retained compatibility surface | intentional-and-documented | none |

The remaining sections are the adversarial pass output (§3.8). Line numbers are as of `a103945`.

### 4.6 Adversarial pass — orphaned references and contracts

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-012 | `review-md-spec.md:20-94` | orphaned contract | The spec states a REVIEW.md `## Focus` section restricts which dimensions run ("ONLY the listed dimensions run"). `parseReviewMd` reads only `confidence_threshold`, `security_min_confidence`, `severity_threshold`, and `ignore`; dimension gating is `agentFlags` alone, which `SKILL.md` stamps only from the trivial-scope gate. Nothing maps a focus list to flags | file-as-issue | #113 |
| R-013 | `report-format.md:25` | orphaned reference | `See SKILL.md Phase 2a for VCS detection` — `SKILL.md` has no section labelled Phase 2a. VCS detection is `phase2-triage.md:18` `## 2a. Detect VCS Platform`. Distinct from R-001, which owns this file's delivery to `report-writer` | fix-in-place | fixed in Task 6 |

### 4.7 Adversarial pass — dead code paths

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-014 | `delivery-guide.md:13` | dead instruction | Tells the orchestrator to handle a `Default — top 6 by severity` choice and rank findings for inline comments. Phase 8 offers `Default — the pipeline's selected set` and forbids re-ranking (`phase8-delivery.md:69`); `selectDelivery` now ranks and caps inside the pipeline | fix-in-place | fixed in Task 6 |
| R-015 | `ndjson-emission-contract.md:88-90` | dead instruction | Claims Phase 2 writes `validate_ndjson.py` into the shared context file under a `Validator` section. `phase2-triage.md:334` states the opposite: "The NDJSON ## Validator section is likewise dropped". The file's *retention* stays intentional per R-011; only this claim is stale | fix-in-place | fixed in Task 6 |
| R-016 | `validator.md:47-54` | dead contract | "What you receive" promises blame tags, code pre-wrapped in `<untrusted-code-content>`, and "Blame classification from Phase 4a". `validatePrompt` (`stages.js:1352-1364`) sends a context read line plus id/dimension/severity/file/range/description/evidence only. `Phase 4a` appears nowhere else in the repo | fix-in-place | fixed in Task 6 |
| R-017 | `registry.js:140-151` | dead field | `resolvePolicy` returns `note`, non-empty when `CLAUDE_CODE_SUBAGENT_MODEL` bypasses model policy. `modelFor` (`stages.js:33-34`) is the only live consumer and reads `.model`; the run envelope discloses overrides via `resolvedPolicy.subagentModel` instead. Only `workflows/test/registry.test.js` reads `note`. Not visible to the dead-export guard — it is a return field, not an export | file-as-issue | #114 |
| R-018 | `publish-marketplace.yml:35` | dead branch | `github.event.release.tag_name` is unreachable because only `workflow_dispatch` is enabled. The file documents the switch at `:14-16` ("Re-enable automatic publishing by restoring:"), so this is a reversible hold, not conversion residue | intentional-and-documented | none |

### 4.8 Adversarial pass — doc/code contract skew

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-019 | `review-md-spec.md:345` | contract skew | Root scaffolding template: "Security findings always use a minimum of 60 regardless of this setting." No 60 floor exists in either runtime; `DEFAULT_SECURITY_MIN_CONFIDENCE = 70`. The same file documents 55/70 correctly at `:129-131` | fix-in-place | fixed in Task 6 |
| R-020 | `phase2-triage.md:395` | contract skew | Documents `persist` as `{ assembleScriptPath }` with absence falling back to the legacy by-value writer, and never mentions `returnPrimaries`. `SKILL.md:360-364` stamps `{ assembleScriptPath, returnPrimaries: true }` as the default RETURN channel and `args.js:571-586` validates it | fix-in-place | fixed in Task 6 |
| R-021 | `validation-pipeline.md:72` | contract skew | "cap confidence at 65 — below the non-security threshold of 70". The live non-security bar with no REVIEW.md override is 55, so 65 is *above* it and the rubric's intent fails. The same file states 55/70 correctly at `:82`. Filed rather than fixed: correcting the prose keeps a cap that no longer suppresses hypotheticals, and lowering the cap changes validator output | file-as-issue | #115 |
| R-022 | `security-reviewer.md:16-18` | contract skew | "Security findings use the same post-validation threshold as other findings (70) because V5-09 unified the thresholds." `filterFindings.js:151-159` decouples them — security `min(70, 70)`, non-security 55 — and its own comment names the "iter 5" decoupling. `V5-09` in code marks validator contestation, not threshold unification. The adjacent instruction ("report >= 60") stays correct and untouched | fix-in-place | fixed in Task 6 |
| R-023 | `filter_findings.py:41,704` | contract skew | Module output-schema comment and an inline comment in `detect_disagreement` call the +10 boost "multi-agent consensus"; the code boosts whenever `count > 1` in a `(file, 10-line bucket)` group with no distinct-agent check. #55 corrected the sibling docstring at `:584-588`; these two sites survived. The JS twin carries no such comment | fix-in-place | fixed in Task 6 |
| R-024 | `apply_challenges.py:42-43` | contract skew | Docstring: dedup is re-run "using the shared `group_by_proximity` utility from `filter_findings.py`". The script imports and calls `dedup_cross_agent` (`:75`, `:455`); `group_by_proximity` is reached only transitively. #55 corrected the same false claim in `filter_findings.py:946` and missed this copy | fix-in-place | fixed in Task 6 |
| R-025 | `stages.js:10-13` | contract skew | Header: "parallel() results are always .filter(Boolean)ed and a null member is recorded as a gap." Only `summarize` filters (`:207`); discover, validate, and challenge attribute by index and push named per-member gaps — `validateStage` says so at `:1274`. `summarize` silently drops null bucket members and emits one generic gap if no partial survives, the merge or single-call result is null, or any summarize dispatch throws | fix-in-place | fixed in Task 6; follow-up corrected the complete failure contract |
| R-026 | `stages.js:2307` | contract skew | `PAYLOAD_JSON:` is a cross-component wire marker — emitted by three persist prompt builders, parsed by the agent per `artifact-writer.md:19` — but was absent from `docs/machine-parsed-strings.md`, whose own rule is "Add a row before introducing a new machine-parsed token" | fix-in-place | Registry row added in this task |
| R-027 | `build.js:2-3` | contract skew | Header claims it "Concatenates workflows/src/\*.js"; `present()` (`:33-36`) emits only files named in the hard-coded `ORDER`, so a new `src/*.js` omitted from `ORDER` is dropped with no failure. Narrowed on challenge: `ORDER` pinning is deliberate (dependency order), so only the comment is wrong — the absent completeness guard is recorded in §6 | fix-in-place | fixed in Task 6 (+ §6 guard) |
| R-028 | `validate.yml:4-7,21-22` | contract skew | `claude plugin validate .` runs on every pull request, but neither `CONTRIBUTING.md:140-146` nor `.github/pull_request_template.md:26-31` lists it, while `CONTRIBUTING.md:205-207` claims the template "enumerates every CI-enforced gate". Filed rather than fixed: the gate needs a global npm install, so whether to list it or soften the claim is a maintainer decision, and `tests/test_contribution_surface.py:623-632` pins the command list | file-as-issue | #116 |
| R-043 | `review-md-spec.md:151` and matching template comment | contract skew | Dimensions omitted from a per-dimension block are documented to use the plain-number default, or 70 when no default is set. The live `DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD` is 55. Choosing whether the prose or constant is authoritative changes the REVIEW.md contract or live filtering behavior | file-as-issue | #118 |

### 4.9 Adversarial pass — stale version and roadmap pointers

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-029 | `review-md-spec.md:250`, `build-review-md/SKILL.md:4` | stale pointer | Both say REVIEW.md detection happens in "Phase 2c". `phase2-triage.md:7-8` assigns 2c to the review target and diff capture; hierarchical discovery and the setup prompts are 2d (`:125-186`), which `SKILL.md:254` confirms | fix-in-place | fixed in Task 6 |
| R-030 | `change-summarizer.md:63` | stale pointer | Heading `## Per-file summaries (Phase 2j, PRs > 500 lines)`. `phase2-triage.md:275-277` states "There is no separate 2j step". Narrowed on challenge: the trigger is also incomplete — `stages.js:195` requires `changedLines > 500` **and** `changedFiles.length > bucketSize` (default 20), so line count alone does not fan out; each dispatch receives up to `bucketSize` files, not one file | fix-in-place | fixed in Task 6; follow-up corrected the bucket-level instruction |
| R-031 | `validate_ndjson.py:14-27` | stale rationale | Present-tense claim that "Phase 3 review agents emit findings via `printf`" and run this script as their final action. `SKILL.md:294` records NDJSON emission as removed from discovery agents, and `tests/test_agent_contracts.py:42-66` scrubs the token from all seven contracts. Retention stays intentional per R-011 | fix-in-place | fixed in Task 6 |
| R-032 | `filterFindings.js:2-4,397,746` | stale pointer | Comments name a "Part 1 / Part 2" split plus "Task 5" and "Task 7". Narrowed on challenge: Part 1/Part 2 still mark real in-file boundaries and should stay; the `Task N` port-era labels resolve to nothing a reader can look up, and collide with unrelated "Task 5/7" uses in `bench/vendor/VENDORED.md` | fix-in-place | fixed in Task 6 |
| R-033 | `publish-marketplace.yml:20` | stale version | `description: "Release tag to publish (e.g. v2.5.0)"` — a pre-rename v2 tag. The release line is 3.x (`plugin.json` `3.3.4`, `tag_format = "v{version}"`) | fix-in-place | fixed in Task 6 |
| R-034 | `publish-marketplace.yml:11-12` | stale version | "automatic publish-on-release is disabled until v3.1.0 — v3.0.0 must NOT deploy". v3.1.0 shipped 2026-07-23 and the plugin is at 3.3.4. Narrowed on challenge: the hold itself is still in force for a different, documented reason (the reusable-workflow pin at `:3-7`), so only the stated rationale is stale | fix-in-place | fixed in Task 6 |

### 4.10 Adversarial pass — diverged duplicates

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-035 | `phase1-preflight.md:17` | diverged duplicate | Quotes the Workflow-absent stop message as "Install code-gauntlet v2.x for older CLIs." The registered verbatim message says "Install the pre-rename deep-review v2.x for older CLIs." (`SKILL.md:28`, registry row pinned by `tests/test_machine_parsed_strings.py`). The reference file names a product that never shipped | fix-in-place | fixed in Task 6 |
| R-036 | `phase2-triage.md:90` | diverged duplicate | Defers truncation on `previously_reviewed: true` and `head_advanced: false`. `SKILL.md:158-172` gates on `previously_reviewed and sha_resolvable and last_reviewed_sha == head_sha`, and `:183-188` explicitly rejects `head_advanced` because it also reads false for unresolvable SHAs and rewritten history — cases that must truncate immediately | fix-in-place | fixed in Task 6 |
| R-037 | `false-positive-exclusions.md:82` ↔ 7 discovery agents | diverged duplicate | Canonical rule 6 is two-part: the underlying issue must not be flagged, **and** "The suppression itself may be worth discussing." No agent copy contains that permission (zero hits under `agents/`). Narrowed on challenge: the omission is uniform across all seven, but only four keep an explicit "do not flag the underlying issue"; `test-analyzer`, `code-simplifier`, and `type-design-analyzer` use domain-rewritten rule 6. Filed, not fixed: adding a clause to seven prompts changes what agents may report, and it lands on the R-008 intentional-duplication surface | file-as-issue | #117 |
| R-039 | `ci.yml`, `validate.yml`, `release.yml`, `bench-smoke.yml`, `labels-verify.yml` | diverged duplicate | `actions/checkout` is split `@v4`/`@v6` and `actions/setup-python` `@v5`/`@v6`. Narrowed on challenge: this is a supply-chain pin inconsistency, not v2→v3 residue, and `docs/engineering-audit-2026-07.md:318-323` already names the checkout split with remediation scoped to #106 (SHA-pin every `uses:`) | owned-elsewhere | #106 |

### 4.11 Adversarial pass — candidate verdicts (intentional retention)

| ID | Location | Class | Evidence | Disposition | Fix or issue |
| --- | --- | --- | --- | --- | --- |
| R-040 | `SKILL.md:28` (candidate C-001) | compatibility wording | The `deep-review v2.x` phrase is correct, not rename residue: v2 shipped under the `deep-review` name, `plugin.json` records "Formerly deep-review", and the version gate matches `V3_MIN_CLAUDE_VERSION = (2, 1, 154)` in `bench/runner/invoke.py`. The whole message is a registered machine-parsed string, so it must not be reworded casually. The stale sibling is R-035, not this line | intentional-and-documented | none |
| R-041 | 7 false-positive-exclusion copies (candidate C-002) | intentional duplication | All 13 numbered rules survive in every copy; no rule is missing, inverted, or re-thresholded. Byte differences are dropped examples and rationale plus deliberate domain tailoring in `type-design-analyzer` and `code-simplifier`. Compression is intentional; the one genuine narrowing is carved out as R-037 | intentional-and-documented | none |
| R-042 | 9 investigation-methodology copies (candidate C-003) | intentional adaptation | The canonical file calls its copies "adapted" and every copy preserves the shared contract: LSP-first, `goToDefinition`/`findReferences` primary, `Grep`/`Glob`/`Read` fallback. Domain steps extend rather than negate it; the omissions found (`hover` in `test-analyzer`, `hover`/`Glob` in `challenger`) are non-mandatory. No drift verdict | intentional-and-documented | none |

## 5. Filed issues summary

Disposition totals across the 30 adversarial-pass rows plus the Task 6 design call recorded as
`R-043`:

| Disposition | Count | IDs |
| --- | --- | --- |
| `fix-in-place` | 20 | R-013 R-014 R-015 R-016 R-019 R-020 R-022 R-023 R-024 R-025 R-026 R-027 R-029 R-030 R-031 R-032 R-033 R-034 R-035 R-036 |
| `file-as-issue` | 6 | R-012 (#113) R-017 (#114) R-021 (#115) R-028 (#116) R-037 (#117) R-043 (#118) |
| `intentional-and-documented` | 4 | R-018 R-040 R-041 R-042 |
| `owned-elsewhere` | 1 | R-039 (#106) |

The six `file-as-issue` rows share a shape: each has two defensible remedies and picking one is a
design decision or changes live behavior. `R-012` (implement `## Focus` or delete the spec),
`R-017` (surface `resolvePolicy().note` in the run envelope or drop the field), `R-021` (accept that
a 65 cap no longer suppresses hypotheticals, or lower the cap and move validator output), `R-028`
(list the plugin-validate gate or soften the completeness claim), `R-037` (restore the suppression
clause across seven agent prompts, which changes what agents may report), and `R-043` (use the
documented 70 fallback or the implemented 55 default). Filed as #113–#118 in Task 7.

The 20 `fix-in-place` rows are prose, comment, and pointer corrections with no behavioral component;
`R-026` is already discharged by the registry row added in this task. Application is Task 6.

### Task 6 application notes

All 20 rows are applied. Four carried edits beyond the location the row names, because a false
statement duplicated across siblings is the failure mode `R-023`/`R-024` exist to record:

- `R-029` — the `Phase 2c` → `2d` correction landed at three sites in `review-md-spec.md`
  (`:250`, the `#### Detection flow` heading at `:252`, and the scaffolding-templates lead-in at
  `:330`), not only the `:250` the row cites, plus `build-review-md/SKILL.md:4`.
- `R-032` — the port-era `Task N` labels also appear at `applyChallenges.js:208` ("Task 5's
  dedupCrossAgent"), outside the `filterFindings.js` sites the row lists. Retired there too; the
  `Part 1` / `Part 2` in-file boundaries stay.
- `R-019` — the same false 60 floor is asserted twice in the root scaffolding template: the
  named `:345` line and "Security cannot be set below 60" at the end of the same comment block.
  Both corrected to the live `min(confidence_threshold, security_min_confidence)` rule.
- `R-035` — after the correction, `skills/code-gauntlet/references/phase1-preflight.md` was added
  to the producer list of the Workflow-absent row in
  [docs/machine-parsed-strings.md](machine-parsed-strings.md), per the sequencing note below.
  Mutation-tested: reverting the reference file's copy turns
  `tests/test_machine_parsed_strings.py` red.

Follow-up review corrected two incomplete Task 6 descriptions: `R-025` now covers filtered null
bucket members, null merge/single-call results, and caught dispatch throws; `R-030` now instructs
the summarizer to describe every file in its bucket (up to `limits.summarizeBucketSize`).

One adjacent skew was found and **deliberately not fixed**: `review-md-spec.md:151` and the
matching template comment both say dimensions omitted from a per-dimension block "use the plain
number default (or 70 if no default is set)", while `DEFAULT_NONSECURITY_CONFIDENCE_THRESHOLD` is
55. It is the same prose-versus-constant class as `R-019`/`R-021`/`R-022` but was not adjudicated
by the adversarial pass, and resolving it means deciding whether the documented 70 or the
implemented 55 is correct — a design call. Recorded as `R-043` and filed as #118 in Task 7.

Existing ownership is unchanged: #24, #29, #35, #36 for `R-001`–`R-007`, #106 for the Actions pin
split, and #110 for accidental executable-code duplicates.

**Sequencing note for Task 6 — done.** After `R-035` lands, add `skills/code-gauntlet/references/phase1-preflight.md`
to the producer list of the Workflow-absent message row in
[docs/machine-parsed-strings.md](machine-parsed-strings.md). It is deliberately absent today: the
file currently holds the wrong string, so registering it now would fail the presence guard rather
than catch the drift. Registering it after the fix converts a one-off correction into a standing
guard against the same divergence.

## 6. Broader guards assessment

Tasks 1–3 shipped the three approved cheap guards:

- machine-parsed string producer/parser presence and byte checks;
- reference reachability from the live instruction graph;
- workflow dead-export detection with a cited allowlist.

They are green at the pinned HEAD, and the adversarial pass extended two of them: the registry
gained the `PAYLOAD_JSON:` wire row (`R-026`) with `PAYLOAD_JSON:` added to the presence test's
required seed set, and the dead-export allowlist citations now carry the inventory ID `R-003`
alongside `owned-elsewhere:#24`, satisfying the design's requirement that every allowlist entry cite
an inventory ID.

The new registry row was mutation-tested in both directions: removing the marker from
`workflows/src/stages.js` (producer) and from `agents/artifact-writer.md` (parser) each turn the
presence test red. The first attempt listed `stages.js` as both producer and parser, and a
parser-side mutation passed — `parseWriterPayload` is exercised only from tests, so it is not a live
parser and naming it let a one-sided mutation fall through to a neighbouring fallback. The row names
the agent as the sole parser for that reason.

Assessment of the deeper guards the design deferred to this pass:

| Candidate guard | Verdict | Reason |
| --- | --- | --- |
| Semantic equality of adapted canonical-source copies | **deliberately skipped** | `R-041` / `R-042` establish that the copies are *intended* to differ — compressed and domain-tailored, with the canonical file calling them "adapted". A byte or structural equality guard would fail by design, and encoding "same 13 rules, any wording" needs a rule-boundary marker the sources do not carry. Making it mechanical means restructuring the canonical files into addressable rule blocks first, which is a design change, not a guard. `R-037` is the residue such a guard would have caught, and it is filed instead. |
| JSON/prompt shape equality beyond registered strings | **deliberately skipped** | `R-016` (validator input contract) is the residue this would catch, but the producer is a template string assembled in `stages.js` and the consumer is prose in an agent contract. Asserting equality means parsing English, so the honest guard is the registry's presence check on discrete tokens, already shipped. |
| `build.js` `ORDER` completeness versus `readdirSync(src)` | **feasible, cheap, filed** | `R-027`'s header comment was corrected in Task 6; the completeness guard is #74 (Wave 1). Latent today because all nine `src/*.js` files are listed, and no test compares `ORDER` to the directory. Mechanical — a set difference in either direction, in the same shape as the existing `detectTopLevelCollisions` guard. |
| Numeric threshold agreement between prose and `filterFindings.js` | **feasible, filed with reservations — Task 7 complete** | `R-019` and `R-022` were corrected in Task 6; the remaining prose-versus-constant skew is `R-043` (#118). A guard could assert that any prose line naming a confidence default matches the constants. The reservation is precision — prose legitimately names configured examples (`security_min_confidence: 60`) and agent-facing report floors (`>= 60`) that are not the filter default, so a naive grep would be noisy. Scoping deferred to #118's resolution. |

This assessment is complete. Tasks 1–3 shipped the reference-reachability, dead-export
allowlist, and machine-parsed-string presence guards. The remaining mechanical candidates are
tracked by #74 (`ORDER` completeness) and #118 (threshold agreement). Full semantic equality
between prose contracts and JSON/prompt shapes is deliberately skipped: #24, #35, and #36 own the
related pipeline contracts, and implementing it here would expand #37 into pipeline work. Reassess
that guard after those issues close.

## Appendix A. Candidates for adversarial triage

| Candidate | Detector output | Required next judgment |
| --- | --- | --- |
| C-001 | `SKILL.md:28` contains the compatibility phrase `deep-review v2.x` | Confirm intentional compatibility wording versus stale rename residue |
| C-002 | Seven false-positive-exclusion copies are not byte-identical to the long-form canonical file | Compare all 13 rules semantically and distinguish deliberate compression from drift |
| C-003 | Nine investigation-methodology copies are not byte-identical to the canonical file | Confirm each domain adaptation preserves the shared contract |

**Closed.** All three were carried through the adversarial pass and dispositioned: `C-001` → `R-040`,
`C-002` → `R-041` (plus the carve-out `R-037`), `C-003` → `R-042`. None became a defect requiring a
fix under #37. The appendix is retained as the provenance record for those three rows.
