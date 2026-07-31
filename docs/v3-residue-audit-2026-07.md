# v2→v3 residue audit — 2026-07

**Date:** 2026-07-31. **HEAD:** `6ef3d9c`. **Tracking:** GitHub #37 (Wave 1 of #101).
**Scope:** `skills/`, `agents/`, `scripts/`, `workflows/src/`, and `.github/`.

**This is a point-in-time inventory.** Commands and results below were measured at the pinned HEAD.
The living machine-string contract is
[docs/machine-parsed-strings.md](machine-parsed-strings.md); re-run the commands for current state.
This baseline contains only the cheap detector pass. The adversarial audit, final dispositions, and
mechanical fixes are deliberately deferred to the next tasks.

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

## 5. Filed issues summary

No new issue is filed by this detector-only baseline. The adversarial pass will add issue links for
confirmed non-mechanical findings. Existing ownership is #24, #29, #35, and #36; accidental
executable-code duplicate remediation remains #110.

## 6. Broader guards assessment

Tasks 1–3 shipped the three approved cheap guards:

- machine-parsed string producer/parser presence and byte checks;
- reference reachability from the live instruction graph;
- workflow dead-export detection with a cited allowlist.

They are green at the pinned HEAD. Deeper semantic contract guards, including semantic equality of
adapted canonical-source copies and JSON-shape equality beyond registered strings, are not part of
these tests. Whether each is feasible and valuable must be assessed during the adversarial pass and
recorded as implemented, filed, or deliberately skipped with a concrete reason.

## Appendix A. Candidates for adversarial triage

| Candidate | Detector output | Required next judgment |
| --- | --- | --- |
| C-001 | `SKILL.md:28` contains the compatibility phrase `deep-review v2.x` | Confirm intentional compatibility wording versus stale rename residue |
| C-002 | Seven false-positive-exclusion copies are not byte-identical to the long-form canonical file | Compare all 13 rules semantically and distinguish deliberate compression from drift |
| C-003 | Nine investigation-methodology copies are not byte-identical to the canonical file | Confirm each domain adaptation preserves the shared contract |

This appendix is an input to Task 5, not a dispositioned finding set.
