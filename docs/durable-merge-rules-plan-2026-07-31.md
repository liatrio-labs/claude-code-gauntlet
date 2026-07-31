# Durable merge rules (#58 + #108) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make merge-to-`main` durable via required CI checks, tag protection, squash-only settings, and code guards that freeze check names and harden rules-file / release-token surfaces — closing #58 through the #108 vehicle.

**Architecture:** Settings first (live ruleset + tag ruleset + repo PR options), then one code PR on a branch from current `main`: frozen check-name test, expanded residue scrub, rules cross-ref check (after citation prose fixes), octo-sts `claim_pattern` pin, and a short CONTRIBUTING note. Evidence and issue closeout last.

**Tech Stack:** GitHub Rulesets API (`gh api`), repo settings PATCH, stdlib-only Python unittest (pytest in CI), octo-sts trust policy YAML, existing workflow YAML under `.github/workflows/`.

**Spec:** `docs/durable-merge-rules-design-2026-07-31.md`

## Global Constraints

- Stdlib-only Python in `tests/` and `scripts/` (no new pip deps).
- Do not invent new CI jobs; only require today's always-on PR check-run names.
- `required_approving_review_count` stays `0`; strict up-to-date stays off.
- Bypass remains Octo STS Integration `801323` only — do not add admin bypass for routine merges.
- Do not require CodeQL, Analyze (\*), Cursor Bugbot, or `labels-verify`.
- `docs/superpowers/` is gitignored — keep plans/specs under `docs/*.md`.
- Never put the literal skip-ci token in commit messages.
- Mutation proofs required for every new assertion (rename/mutate → red, restore → green).

---

## File map

| File | Responsibility |
| --- | --- |
| GitHub ruleset `16049246` | Required status checks + description of zero-approval bar |
| New tag ruleset | Protect `refs/tags/v*` from delete/update except Octo STS |
| Repo PR settings | Squash-only + delete head branches |
| `tests/test_contribution_surface.py` | Frozen nine check names + derive-from-workflows assertion |
| `tests/test_agent_contracts.py` | Repo-wide rules-file residue scrub (split Bash scope) |
| `tests/test_agent_instruction_layout.py` | Quoted rules-file cross-reference check |
| `skills/code-gauntlet/SKILL.md` | Fix bad CLAUDE.md citation |
| `skills/code-gauntlet/references/ndjson-emission-contract.md` | Fix invented heading citation |
| `.github/chainguard/main-semantic-release.sts.yaml` | Pin release workflow via `claim_pattern` |
| `CONTRIBUTING.md` | Contributor-facing CI-gate / bypass note |

---

### Task 1: Branch ruleset — required checks + description

**Files:**
- Modify: live GitHub ruleset `repos/liatrio-labs/claude-code-gauntlet/rulesets/16049246` (no git file)

**Interfaces:**
- Consumes: nine check contexts from the spec
- Produces: active `required_status_checks` rule readable by later evidence steps and Task 4's docstring

- [ ] **Step 1: Snapshot current ruleset**

```bash
gh api repos/liatrio-labs/claude-code-gauntlet/rulesets/16049246 > /tmp/ruleset-16049246-before.json
python3 -c "import json; d=json.load(open('/tmp/ruleset-16049246-before.json')); print([r['type'] for r in d['rules']]); print(d.get('bypass_actors')); print(d.get('description'))"
```

Expected: rules are `deletion`, `non_fast_forward`, `pull_request` only; bypass is Octo STS `801323`; description empty/absent.

- [ ] **Step 2: PUT updated ruleset (preserve PR params + bypass)**

Write the body with `python3 -c` (zsh corrupts `!` in heredocs) then PUT:

```bash
python3 -c "
import json
body = {
  'name': 'protect-default-branch',
  'target': 'branch',
  'enforcement': 'active',
  'description': (
    'Required CI must be green before merge. '
    'required_approving_review_count stays 0 on purpose: solo owner; '
    'GitHub blocks self-approval and a count of 1 would deadlock merges. '
    'Bypass is Octo STS (Integration 801323) for release automation only — '
    'not a routine admin fast-path.'
  ),
  'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
  'bypass_actors': [
    {'actor_id': 801323, 'actor_type': 'Integration', 'bypass_mode': 'always'}
  ],
  'rules': [
    {'type': 'deletion'},
    {'type': 'non_fast_forward'},
    {
      'type': 'pull_request',
      'parameters': {
        'required_approving_review_count': 0,
        'dismiss_stale_reviews_on_push': True,
        'require_code_owner_review': False,
        'require_last_push_approval': False,
        'required_review_thread_resolution': False,
        'allowed_merge_methods': ['squash', 'merge', 'rebase'],
      },
    },
    {
      'type': 'required_status_checks',
      'parameters': {
        'strict_required_status_checks_policy': False,
        'do_not_enforce_on_create': False,
        'required_status_checks': [
          {'context': c} for c in [
            'Run Linting',
            'Run Tests (3.10)',
            'Run Tests (3.11)',
            'Run Tests (3.12)',
            'Run Workflow JS Tests',
            'Run Bench Self-Tests (3.11)',
            'Run Bench Self-Tests (3.12)',
            'Validate plugin.json',
            'lint-pr-title',
          ]
        ],
      },
    },
  ],
}
open('/tmp/ruleset-16049246-put.json','w').write(json.dumps(body))
print('wrote', len(body['rules']), 'rules')
"
gh api --method PUT repos/liatrio-labs/claude-code-gauntlet/rulesets/16049246 \
  --input /tmp/ruleset-16049246-put.json \
  > /tmp/ruleset-16049246-after.json
```

If the API rejects `allowed_merge_methods` or `description`, drop the unsupported field and retry once, recording the deviation on #108.

- [ ] **Step 3: Verify**

```bash
python3 -c "
import json
d=json.load(open('/tmp/ruleset-16049246-after.json'))
types={r['type'] for r in d['rules']}
assert 'required_status_checks' in types, types
rsc=next(r for r in d['rules'] if r['type']=='required_status_checks')
ctx=sorted(c['context'] for c in rsc['parameters']['required_status_checks'])
want=sorted([
  'Run Linting','Run Tests (3.10)','Run Tests (3.11)','Run Tests (3.12)',
  'Run Workflow JS Tests','Run Bench Self-Tests (3.11)','Run Bench Self-Tests (3.12)',
  'Validate plugin.json','lint-pr-title'])
assert ctx==want, (ctx, want)
assert rsc['parameters'].get('strict_required_status_checks_policy') is False
pr=next(r for r in d['rules'] if r['type']=='pull_request')
assert pr['parameters']['required_approving_review_count']==0
assert d['bypass_actors']==[{'actor_id':801323,'actor_type':'Integration','bypass_mode':'always'}]
print('OK', len(ctx), 'checks; approvals=0; bypass=octo-sts')
"
```

- [ ] **Step 4: Comment evidence on #58 and #108**

Paste the dated `gh api .../rulesets/16049246` JSON (or a trimmed rules/bypass excerpt) onto both issues noting Task 1 done. Do not close yet.

---

### Task 2: Tag ruleset + squash-only repo settings

**Files:**
- Create: live tag ruleset (API)
- Modify: repo `allow_*_merge` / `delete_branch_on_merge` settings

**Interfaces:**
- Consumes: Octo STS actor id `801323` from Task 1
- Produces: tag ruleset id for evidence; squash-only repo config

- [ ] **Step 1: Create tag ruleset**

```bash
python3 -c "
import json
body = {
  'name': 'protect-release-tags',
  'target': 'tag',
  'enforcement': 'active',
  'description': (
    'v* release tags: restrict deletion and updates. Creations unrestricted '
    'so release.yml can mint tags; only Octo STS may move/delete.'
  ),
  'conditions': {'ref_name': {'include': ['refs/tags/v*'], 'exclude': []}},
  'bypass_actors': [
    {'actor_id': 801323, 'actor_type': 'Integration', 'bypass_mode': 'always'}
  ],
  'rules': [
    {'type': 'deletion'},
    {'type': 'non_fast_forward'},
  ],
}
open('/tmp/tag-ruleset-create.json','w').write(json.dumps(body))
"
gh api --method POST repos/liatrio-labs/claude-code-gauntlet/rulesets \
  --input /tmp/tag-ruleset-create.json > /tmp/tag-ruleset-after.json
python3 -c "import json; d=json.load(open('/tmp/tag-ruleset-after.json')); print(d['id'], d['name'], d['target'], [r['type'] for r in d['rules']])"
```

Expected: new id printed; `target` is `tag`; rules `deletion` + `non_fast_forward`.

- [ ] **Step 2: Squash-only + auto-delete head branches**

```bash
gh api --method PATCH repos/liatrio-labs/claude-code-gauntlet \
  -f allow_merge_commit=false \
  -f allow_rebase_merge=false \
  -f allow_squash_merge=true \
  -f delete_branch_on_merge=true \
  --jq '{allow_merge_commit, allow_rebase_merge, allow_squash_merge, delete_branch_on_merge}'
```

Expected: `false`, `false`, `true`, `true`.

- [ ] **Step 3: Align branch ruleset allowed_merge_methods with squash-only (optional consistency)**

If Task 1 left `allowed_merge_methods` as `["squash","merge","rebase"]`, PUT again with `["squash"]` only so the ruleset cannot advertise disabled methods. Re-verify required checks still present.

- [ ] **Step 4: Evidence comment on #108**

Include tag ruleset id + repo settings jq output.

---

### Task 3: Merge-block smoke evidence

**Files:** none (issue comments)

**Interfaces:**
- Consumes: required checks from Task 1
- Produces: #58/#108 verification evidence for requirement “failing required check cannot merge”

- [ ] **Step 1: Prefer a cheap UI/API proof on an open or draft PR**

If an open PR exists with any required check failed/pending:

```bash
gh pr list --state open --limit 5 --json number,mergeable,mergeStateStatus,statusCheckRollup
```

Record `mergeStateStatus` in `BLOCKED` / `UNSTABLE` while a required context is failed.

- [ ] **Step 2: If no suitable PR, document Settings proof**

From a PR merge box (or GitHub docs + API): note that with `required_status_checks` present, GitHub disables merge while a listed context is `failure` or `pending`. Quote the ruleset after dump showing the nine contexts. Explicitly state a throwaway red PR was skipped if so.

- [ ] **Step 3: Comment on #58 and #108; close #58**

Close #58 as completed via #108 settings work, linking the ruleset dump and smoke/docs proof. Leave #108 open until the code PR merges.

---

### Task 4: Frozen required-check names (TDD)

**Files:**
- Modify: `tests/test_contribution_surface.py` (helpers + new test class at end, before `if __name__`)
- Test: same file

**Interfaces:**
- Consumes: workflow files `ci.yml`, `validate.yml`, `pr-title-lint.yml`
- Produces: `REQUIRED_PR_CHECK_CONTEXTS` tuple and `_derive_required_pr_check_contexts()` used only by this test module

- [ ] **Step 1: Ensure branch is based on current `main`**

```bash
git fetch origin main
git checkout -B feat/durable-merge-rules origin/main
```

Cherry-pick or re-apply `docs/durable-merge-rules-design-2026-07-31.md` and this plan onto the branch if they are not already on `main`.

- [ ] **Step 2: Write the failing test first (helpers referenced but incomplete → fail)**

Append to `tests/test_contribution_surface.py`:

```python
# Required PR check-run names for ruleset 16049246 (protect-default-branch).
# Any new always-on PR gate must update THIS tuple and the live ruleset in the
# same change (see #108 / #102 / #105). GitHub matches check-run names, not
# workflow `name:` titles alone.
REQUIRED_PR_CHECK_CONTEXTS = (
    "Run Linting",
    "Run Tests (3.10)",
    "Run Tests (3.11)",
    "Run Tests (3.12)",
    "Run Workflow JS Tests",
    "Run Bench Self-Tests (3.11)",
    "Run Bench Self-Tests (3.12)",
    "Validate plugin.json",
    "lint-pr-title",
)

REQUIRED_PR_CHECK_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/validate.yml",
    ".github/workflows/pr-title-lint.yml",
)


def _derive_required_pr_check_contexts():
    """Expand job name: + matrix python-version into GitHub check-run names.

    Job-level name: wins; else the job id (pr-title-lint.yml → lint-pr-title).
    """
    raise NotImplementedError


class TestRequiredPrCheckContexts(unittest.TestCase):
    def test_frozen_tuple_matches_workflow_derived_names(self):
        derived = tuple(sorted(_derive_required_pr_check_contexts()))
        frozen = tuple(sorted(REQUIRED_PR_CHECK_CONTEXTS))
        self.assertEqual(
            derived,
            frozen,
            "workflow job names drifted from REQUIRED_PR_CHECK_CONTEXTS — "
            "update the tuple AND ruleset 16049246 together",
        )

    def test_docstring_names_the_ruleset(self):
        text = (REPO / "tests" / "test_contribution_surface.py").read_text(encoding="utf-8")
        self.assertIn("16049246", text)
        self.assertIn("protect-default-branch", text)
```

- [ ] **Step 3: Run test — expect fail**

```bash
python -m pytest tests/test_contribution_surface.py::TestRequiredPrCheckContexts -q
```

Expected: FAIL (`NotImplementedError` or unequal).

- [ ] **Step 4: Implement `_derive_required_pr_check_contexts`**

```python
def _derive_required_pr_check_contexts():
    contexts = []
    job_header = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
    job_name = re.compile(r'^    name:\s*(.+?)\s*$')
    matrix_versions = re.compile(
        r'^        python-version:\s*\[(.*)\]\s*$'
    )
    for rel in REQUIRED_PR_CHECK_WORKFLOWS:
        text = _read(rel)
        lines = text.splitlines()
        i = 0
        in_jobs = False
        while i < len(lines):
            line = lines[i]
            if line.startswith("jobs:"):
                in_jobs = True
                i += 1
                continue
            if not in_jobs:
                i += 1
                continue
            if line and not line.startswith(" ") and not line.startswith("#"):
                break
            m = job_header.match(line)
            if not m:
                i += 1
                continue
            job_id = m.group(1)
            display = None
            versions = None
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if job_header.match(nxt) or (nxt and not nxt.startswith(" ") and not nxt.startswith("#")):
                    break
                nm = job_name.match(nxt)
                if nm:
                    display = _unquote(nm.group(1))
                vm = matrix_versions.match(nxt)
                if vm:
                    versions = [_unquote(p.strip()) for p in vm.group(1).split(",") if p.strip()]
                i += 1
            label = display if display is not None else job_id
            if versions:
                contexts.extend(f"{label} ({v})" for v in versions)
            else:
                contexts.append(label)
    return contexts
```

- [ ] **Step 5: Run test — expect pass**

```bash
python -m pytest tests/test_contribution_surface.py::TestRequiredPrCheckContexts -q
```

Expected: PASS.

- [ ] **Step 6: Mutation proof**

```bash
# temporarily rename Run Linting → Run LintingX in ci.yml, run test (expect FAIL), restore
```

Restore `ci.yml` before committing.

- [ ] **Step 7: Commit**

```bash
git add tests/test_contribution_surface.py
git commit -m "$(cat <<'EOF'
test(ci): freeze required PR check-run names for ruleset 16049246

Pin the nine always-on contexts so a job rename without a ruleset update
fails the contribution-surface suite (#108).
EOF
)"
```

---

### Task 5: Expand residue scrub to all tracked rules files

**Files:**
- Modify: `tests/test_agent_contracts.py` (`DIRECTORY_RULES` discovery + scrub tests)
- Test: same file

**Interfaces:**
- Consumes: `git ls-files` rules paths
- Produces: `tracked_rules_files()` helper; split residue patterns

- [ ] **Step 1: Write failing expectations**

Replace the agents-only directory-rules scrub with:

```python
EMISSION_RESIDUE = re.compile(r"printf|ndjson|validate_ndjson", re.IGNORECASE)
BASH_RESIDUE = re.compile(r"Bash")


def tracked_rules_files():
    """Root and one-level AGENTS.md / CLAUDE.md, same shape as sync_agent_rules."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "AGENTS.md", "CLAUDE.md", "*/AGENTS.md", "*/CLAUDE.md"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout
    return [Path(p) for p in out.decode().split("\0") if p]


class TestDiscoveryAgentEmissionScrub(unittest.TestCase):
    # keep existing discovery-agent and by-value tests unchanged

    def test_no_emission_residue_in_tracked_rules_files(self):
        offenders = {}
        for rel in tracked_rules_files():
            text = (REPO / rel).read_text(encoding="utf-8")
            hits = sorted(set(EMISSION_RESIDUE.findall(text)))
            if hits:
                offenders[str(rel)] = hits
        self.assertEqual(offenders, {},
                         f"v2 emission residue in rules files: {offenders}")

    def test_no_bash_residue_in_agents_directory_rules(self):
        offenders = {}
        for name in ("AGENTS.md", "CLAUDE.md"):
            rel = Path("agents") / name
            text = (REPO / rel).read_text(encoding="utf-8")
            hits = sorted(set(BASH_RESIDUE.findall(text)))
            if hits:
                offenders[str(rel)] = hits
        self.assertEqual(offenders, {},
                         f"Bash residue in agents/ rules: {offenders}")
```

Add `import subprocess` at top if missing. Remove or rewrite `test_no_ndjson_emission_residue_in_the_agents_directory_rules` so it does not use the old combined `RESIDUE` on agents-only paths. Keep `RESIDUE` for discovery-agent body tests **or** switch those tests to `EMISSION_RESIDUE|Bash` equivalently — discovery agents must still forbid Bash.

Update `test_the_block_trips_no_existing_discovery_agent_guard` if it still references `RESIDUE` — keep forbidding all four tokens on the complete-read block.

- [ ] **Step 2: Run scrub tests**

```bash
python -m pytest tests/test_agent_contracts.py::TestDiscoveryAgentEmissionScrub -q
```

Expected: PASS on current tree (no residue). If anything fails on root/scripts/workflows pairs, fix content only if it is true v2 emission residue; do not gut legitimate docs.

- [ ] **Step 3: Mutation proof**

Insert `printf` into root `AGENTS.md` temporarily → test red; restore. Insert `Bash` into `scripts/AGENTS.md` → scrub must stay green; insert `Bash` into `agents/AGENTS.md` → red; restore.

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_contracts.py
git commit -m "$(cat <<'EOF'
test(rules): scrub emission residue across every tracked AGENTS/CLAUDE pair

Close the agents/-only gap called out in #108 without false-positive on
scripts/ shell discussion.
EOF
)"
```

---

### Task 6: Citation prose + rules cross-reference check

**Files:**
- Modify: `skills/code-gauntlet/SKILL.md` (shell hygiene paragraph ~line 37)
- Modify: `skills/code-gauntlet/references/ndjson-emission-contract.md` (lines 3–8)
- Modify: `tests/test_agent_instruction_layout.py` (new test class)
- Test: `tests/test_agent_instruction_layout.py`

**Interfaces:**
- Consumes: tracked rules file texts; quote patterns attributing CLAUDE.md / AGENTS.md
- Produces: `test_rules_file_quotations_resolve` green on fixed prose

- [ ] **Step 1: Mutation proof on current (broken) citations — expect red once test exists**

First add the test (Step 2), run it against current SKILL.md / ndjson contract → FAIL. Then apply prose fixes (Step 3) → PASS. Do not leave the suite red on the branch tip.

- [ ] **Step 2: Add cross-ref test**

```python
class TestRulesFileQuotations(unittest.TestCase):
    """Quoted spans attributed to AGENTS.md/CLAUDE.md must exist in a rules file."""

    ATTR = re.compile(
        r"""(?:CLAUDE\.md|AGENTS\.md)\s+"""
        r"""(?:says\s+|already applies[^.]*\(|"""
        r"""["']([^"']{12,})["']|"""
        r""""([^"]{12,})")""",
        re.IGNORECASE,
    )
    # Prefer a small explicit scanner: find CLAUDE.md / AGENTS.md near a "…"/'…' span.
    QUOTE_NEAR_RULES = re.compile(
        r'(?:CLAUDE\.md|AGENTS\.md)[^\n]{0,120}["\u201c]([^"\u201d]{12,})["\u201d]',
        re.IGNORECASE,
    )
    HEADING_ATTR = re.compile(
        r'(?:see\s+)?CLAUDE\.md\s+"([^"]+)"',
        re.IGNORECASE,
    )

    def _rules_corpus(self):
        files = subprocess.run(
            ["git", "ls-files", "-z", "--", "AGENTS.md", "CLAUDE.md", "*/AGENTS.md", "*/CLAUDE.md"],
            cwd=REPO, capture_output=True, check=True,
        ).stdout.decode().split("\0")
        return "\n".join((REPO / p).read_text(encoding="utf-8") for p in files if p)

    def _docs(self):
        paths = subprocess.run(
            ["git", "ls-files", "-z", "--", "skills/**/*.md", "agents/*.md"],
            cwd=REPO, capture_output=True, check=True,
        ).stdout.decode().split("\0")
        # git pathspecs with ** may need: skills/ agents/
        if not any(paths):
            paths = [
                p for p in subprocess.run(
                    ["git", "ls-files", "-z", "--", "skills", "agents"],
                    cwd=REPO, capture_output=True, check=True,
                ).stdout.decode().split("\0")
                if p.endswith(".md") and (
                    p.startswith("skills/") or (p.startswith("agents/") and p.count("/") == 1)
                )
            ]
        return [p for p in paths if p]

    def test_rules_file_quotations_resolve(self):
        corpus = self._rules_corpus()
        missing = []
        for doc in self._docs():
            text = (REPO / doc).read_text(encoding="utf-8")
            for rx in (self.QUOTE_NEAR_RULES, self.HEADING_ATTR):
                for quote in rx.findall(text):
                    if quote not in corpus:
                        missing.append((doc, quote[:80]))
        self.assertEqual(missing, [], f"rules quotations not found in any AGENTS/CLAUDE: {missing}")
```

Tune the regexes if they over-match; keep them narrow. Goal: catch the two known #108 citations and similar shapes, not every English quote in the tree.

- [ ] **Step 3: Fix prose**

In `SKILL.md`, replace the parenthetical that quotes a non-existent CLAUDE.md sentence with a pointer at the real doctrine, e.g. reference the `agents/AGENTS.md` bullet that false-positive / complete-read contracts are intentionally duplicated (do not invent a heading title).

In `ndjson-emission-contract.md`, replace:

```markdown
(intentional — see CLAUDE.md "False-positive exclusion list is
intentionally duplicated")
```

with a factual pointer to `agents/AGENTS.md` duplication doctrine (no fake heading).

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_agent_instruction_layout.py::TestRulesFileQuotations -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/code-gauntlet/SKILL.md \
  skills/code-gauntlet/references/ndjson-emission-contract.md \
  tests/test_agent_instruction_layout.py
git commit -m "$(cat <<'EOF'
fix(docs): retarget rules citations and assert quotations resolve

Stop quoting headings/sentences that are not in any AGENTS.md/CLAUDE.md,
and pin a scanner so the drift cannot return (#108).
EOF
)"
```

---

### Task 7: Octo-sts `claim_pattern` pin for release.yml

**Files:**
- Modify: `.github/chainguard/main-semantic-release.sts.yaml`

**Interfaces:**
- Consumes: OIDC claim name + value for release workflow identity
- Produces: fail-closed minting of `contents: write` only for release.yml on main

- [ ] **Step 1: Determine the claim to pin**

GitHub documents `workflow_ref` for the workflow file path on ordinary jobs, and `job_workflow_ref` primarily for reusable/called workflows. Verify which claim a `release.yml` job actually presents:

Prefer reading from a recent successful release run’s federation debug, or mint a one-off diagnostic. If neither is available in-session, start with:

```text
liatrio-labs/claude-code-gauntlet/.github/workflows/release.yml@refs/heads/main
```

Try `claim_pattern.workflow_ref` first (always present for Actions). If #108’s preferred `job_workflow_ref` is confirmed present on this repo’s release tokens with the same value, pin that instead and note which claim was used on the issue.

- [ ] **Step 2: Update the trust policy**

```yaml
issuer: https://token.actions.githubusercontent.com

# Immutable OIDC subject format: the 2026-07 rename (claude-deep-review ->
# claude-code-gauntlet) permanently moved this repo onto GitHub's owner-ID/
# repo-ID-qualified sub format (rollout for repos created/renamed after
# 2026-07-15). Value copied verbatim from the token presented in run
# 29962444808; the IDs are immutable, but a future rename would change the
# slug portions — update them here if that ever happens.
subject: repo:liatrio-labs@223510100/claude-code-gauntlet@1190928743:ref:refs/heads/main

# Only release.yml may mint this identity. Claim name+value verified against
# a release OIDC token (see #108 evidence). Regex anchors are intentional.
claim_pattern:
  workflow_ref: "^liatrio-labs/claude-code-gauntlet/\\.github/workflows/release\\.yml@refs/heads/main$"

permissions:
  contents: write
```

If the verified claim is `job_workflow_ref`, use that key instead of `workflow_ref` and keep the same regex.

- [ ] **Step 3: Sanity-check YAML still loads**

```bash
python3 -c "import pathlib; print(pathlib.Path('.github/chainguard/main-semantic-release.sts.yaml').read_text())"
```

- [ ] **Step 4: Commit**

```bash
git add .github/chainguard/main-semantic-release.sts.yaml
git commit -m "$(cat <<'EOF'
fix(release): pin main-semantic-release STS to release.yml via OIDC claim

Stop any other workflow on main from minting contents:write under this
identity (#108).
EOF
)"
```

---

### Task 8: CONTRIBUTING note + follow-up ratchet comments

**Files:**
- Modify: `CONTRIBUTING.md` (Pull Requests section ~line 205)

**Interfaces:**
- Consumes: nine check names / bypass story from Tasks 1–4
- Produces: contributor-visible merge policy

- [ ] **Step 1: Add a short paragraph under `## Pull Requests`**

After the “Ensure all checks pass…” bullets, add:

```markdown
- **Merges to `main` require the always-on CI check-runs to be green** (lint, pytest
  matrix, workflow JS tests, bench self-tests, plugin validate, PR title lint). Those
  contexts are required on the `protect-default-branch` ruleset; renaming a job without
  updating the ruleset and `REQUIRED_PR_CHECK_CONTEXTS` in
  `tests/test_contribution_surface.py` will fail CI. Approving reviews stay at zero by
  design (solo maintainer). The only ruleset bypass is the Octo STS app used by
  `release.yml` — not a routine admin merge path.
```

- [ ] **Step 2: Run cspell/contribution tests if needed**

```bash
python -m pytest tests/test_contribution_surface.py -q
```

- [ ] **Step 3: Comment ratchet on follow-ups**

On #102, #105 (and any other #55 follow-up that adds an always-on PR gate): comment that landing the gate must update ruleset `16049246` and `REQUIRED_PR_CHECK_CONTEXTS` in the same PR.

- [ ] **Step 4: Commit**

```bash
git add CONTRIBUTING.md
git commit -m "$(cat <<'EOF'
docs: note that merges to main are CI-gated by the branch ruleset

Point contributors at the frozen check list and the deliberate zero-approval
/ Octo-STS-only bypass policy (#58, #108).
EOF
)"
```

---

### Task 9: Full suite gate, PR, close #108

**Files:** none new

- [ ] **Step 1: Run full verification**

```bash
pre-commit run --all-files
python -m pytest tests/ -q
python -m pytest bench/tests/ -q
node --test workflows/test/*.test.js
node workflows/build.js && git diff --exit-code workflows/pipeline.js
```

Expected: all green; bundle unchanged.

- [ ] **Step 2: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "fix(ci): durable merge rules — required checks, freeze list, release STS pin" --body "$(cat <<'EOF'
## Summary
- Closes the #108 hardening that subsumes #58: required PR checks already live on ruleset 16049246; this PR freezes those check names, expands rules-file residue scrubbing, asserts rules quotations resolve, pins octo-sts to `release.yml`, and documents the merge policy.
- Tag ruleset + squash-only settings are owner-side (done in the issue thread).

## Test plan
- [x] Mutation: rename a required job in `ci.yml` → `TestRequiredPrCheckContexts` red
- [x] Mutation: `printf` in a non-agents rules file → residue test red; `Bash` in `scripts/AGENTS.md` stays green
- [x] Mutation: plant a fake CLAUDE.md quote → cross-ref test red
- [ ] CI green on the PR
- [ ] First post-merge release still cuts a `v*` tag under the new tag ruleset + STS pin

Closes #108
EOF
)"
```

- [ ] **Step 3: After merge + green release**

Confirm the release run created a `v*` tag. Comment final evidence on #108 and close it. Update #101 wave notes (#58 done; #108 done).

---

## Spec coverage checklist

| Spec item | Task |
| --- | --- |
| Required nine status checks on 16049246 | 1 |
| Strict up-to-date off; approvals 0 + description; Octo STS bypass | 1 |
| Tag ruleset v* delete/update | 2 |
| Squash-only + delete head branches | 2 |
| Smoke / evidence merge block; close #58 | 3 |
| Frozen check tuple + derive-from-workflows + ruleset docstring | 4 |
| Residue scrub all tracked rules; Bash scoped to agents/ | 5 |
| Citation fixes + cross-ref check | 6 |
| Octo-sts claim pin | 7 |
| CONTRIBUTING + follow-up ratchet comments | 8 |
| Full suites + PR + close #108 + post-merge tag | 9 |

## Plan self-review

- Placeholders: claim name (`workflow_ref` vs `job_workflow_ref`) is deliberately verified in Task 7 Step 1 — not left as TBD in the shipped file.
- Cross-ref regex may need tightening during Task 6 if false positives appear; the known #108 citations are the acceptance bar.
- Settings Tasks 1–3 require repo admin and must run before relying on merge protection in production; the code PR can still land if settings already applied.
