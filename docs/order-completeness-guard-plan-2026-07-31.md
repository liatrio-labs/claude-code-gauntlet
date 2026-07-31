# ORDER completeness guard (#74) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `workflows/build.js` fail loudly when `ORDER` and `workflows/src/*.js` disagree, so a new source module cannot be silently dropped from the shipped bundle.

**Architecture:** Export a pure `orderMismatches(order, onDisk)` in the same shape as `detectTopLevelCollisions`. Call it from `present()` (the seam where `ORDER` meets disk) and throw with actionable remedies. Prove the mechanism with fabricated mismatch tests in `workflows/test/build.test.js` and a whole-function mutation that must go red. No Python-suite predicate copy.

**Tech Stack:** Node 24 builtins + `node --test` / `node:assert/strict`; no npm. Stdlib-only Python suites already subprocess the real build.

**Spec:** `docs/order-completeness-guard-design-2026-07-31.md`

## Global Constraints

- Suites-only under #101 — no smoke, no bench.
- Do not modify `workflows/pipeline.js` by hand; it must remain byte-identical by construction (`build.js` is not bundled).
- Do not add any assertion to `tests/test_bundle_fresh.py` or other Python suites.
- Leave the `ORDER.filter((f) => found.has(f))` intersection in `present()` after the equality check (YAGNI).
- Sort only the returned mismatch arrays — never reorder `ORDER` or the disk listing passed in.
- Mutation proofs must actually run (gut whole mechanism → red → restore), not be reasoned about.
- Never put the literal skip-ci token in commit messages.
- `docs/superpowers/` is gitignored — keep plans/specs under `docs/*.md`.

---

## File map

| File | Responsibility |
| --- | --- |
| `workflows/build.js` | Export `orderMismatches`; call from `present()`; throw with remedies |
| `workflows/test/build.test.js` | Fabricated mismatch tests (both directions, both-at-once, equal/empty) |
| `workflows/pipeline.js` | Untouched (verify byte-identical after build) |

---

### Task 1: Pure `orderMismatches` + fabricated tests (TDD)

**Files:**

- Modify: `workflows/build.js` (add exported function near `detectTopLevelCollisions`)
- Modify: `workflows/test/build.test.js` (extend import; add four tests)

**Interfaces:**

- Consumes: nothing from later tasks
- Produces: `export function orderMismatches(order, onDisk) → { missingFromOrder: string[], missingFromDisk: string[] }` with both arrays sorted

- [ ] **Step 1: Extend the import and write the failing tests**

Replace the import line and append these tests to `workflows/test/build.test.js`:

```js
import { detectTopLevelCollisions, build, orderMismatches } from '../build.js';

test('orderMismatches flags a file on disk that is missing from ORDER', () => {
  const result = orderMismatches(['a.js', 'b.js'], ['b.js', 'a.js', 'stray.js']);
  assert.deepEqual(result.missingFromOrder, ['stray.js']);
  assert.deepEqual(result.missingFromDisk, []);
});

test('orderMismatches flags a name in ORDER that is missing from disk', () => {
  const result = orderMismatches(['a.js', 'gone.js', 'b.js'], ['b.js', 'a.js']);
  assert.deepEqual(result.missingFromOrder, []);
  assert.deepEqual(result.missingFromDisk, ['gone.js']);
});

test('orderMismatches reports both directions at once, sorted', () => {
  // Input order is deliberately unsorted so the test proves sorting of returns only.
  const result = orderMismatches(
    ['z.js', 'a.js', 'orphan-order.js'],
    ['stray-b.js', 'a.js', 'stray-a.js', 'z.js'],
  );
  assert.deepEqual(result.missingFromOrder, ['stray-a.js', 'stray-b.js']);
  assert.deepEqual(result.missingFromDisk, ['orphan-order.js']);
});

test('orderMismatches returns empty arrays when sets are equal (including both empty)', () => {
  assert.deepEqual(orderMismatches(['a.js', 'b.js'], ['b.js', 'a.js']), {
    missingFromOrder: [],
    missingFromDisk: [],
  });
  assert.deepEqual(orderMismatches([], []), {
    missingFromOrder: [],
    missingFromDisk: [],
  });
});
```

Also update the file header comment so it mentions ORDER completeness alongside collisions.

- [ ] **Step 2: Run tests to verify they fail**

```bash
node --test workflows/test/build.test.js
```

Expected: FAIL — `orderMismatches` is not exported / not a function.

- [ ] **Step 3: Implement `orderMismatches` in `workflows/build.js`**

Place this export next to `detectTopLevelCollisions` (after the `TOP_LEVEL_DECL` constant block is fine; before `export function detectTopLevelCollisions` or immediately after that function — either is fine as long as it is exported and pure). Do **not** wire `present()` yet.

```js
// ORDER must name every workflows/src/*.js file exactly once. present() used to
// silently intersect ORDER with disk, so a new module left out of ORDER shipped
// as an incomplete bundle while unit tests importing ../src/<file>.js stayed green.
export function orderMismatches(order, onDisk) {
  const inOrder = new Set(order);
  const onDiskSet = new Set(onDisk);
  const missingFromOrder = [...onDiskSet].filter((f) => !inOrder.has(f)).sort();
  const missingFromDisk = [...inOrder].filter((f) => !onDiskSet.has(f)).sort();
  return { missingFromOrder, missingFromDisk };
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
node --test workflows/test/build.test.js
```

Expected: all tests PASS (existing collision tests + four new ones).

- [ ] **Step 5: Mutation proof (whole mechanism)**

Temporarily replace the body of `orderMismatches` with:

```js
export function orderMismatches(order, onDisk) {
  return { missingFromOrder: [], missingFromDisk: [] };
}
```

Run:

```bash
node --test workflows/test/build.test.js
```

Expected: the three mismatch-direction tests FAIL; equal/empty may still PASS. Restore the real implementation and re-run — all PASS. Do not leave the gutted body committed.

- [ ] **Step 6: Commit**

```bash
git add workflows/build.js workflows/test/build.test.js
git commit -m "$(cat <<'EOF'
test(workflows): prove orderMismatches catches ORDER↔disk drift (#74)

Pure set-difference helper in the detectTopLevelCollisions shape, with
fabricated mismatch tests in both directions and a whole-mechanism mutation.
EOF
)"
```

---

### Task 2: Wire the guard into `present()` and verify the live path

**Files:**

- Modify: `workflows/build.js` (`present()` only)
- Test: existing `the real bundle build() produces no top-level collisions` (already calls `build()`)
- Verify: `workflows/pipeline.js` byte-identical; `tests/test_bundle_fresh.py` green

**Interfaces:**

- Consumes: `orderMismatches(order, onDisk)` from Task 1
- Produces: `present()` throws before returning when either mismatch array is non-empty

- [ ] **Step 1: Replace `present()` with the guarded version**

Replace the current `present()` body (keep the intersection return):

```js
function present() {
  const found = new Set(readdirSync(SRC).filter((f) => f.endsWith('.js')));
  const { missingFromOrder, missingFromDisk } = orderMismatches(ORDER, [...found]);
  if (missingFromOrder.length || missingFromDisk.length) {
    const lines = [];
    if (missingFromOrder.length) {
      lines.push(
        `on disk but not in ORDER: ${missingFromOrder.join(', ')} `
          + `(add each file to ORDER in dependency order, or remove the stray file)`,
      );
    }
    if (missingFromDisk.length) {
      lines.push(
        `in ORDER but not on disk: ${missingFromDisk.join(', ')} `
          + `(remove the name from ORDER, or restore the file)`,
      );
    }
    throw new Error(
      `build.js: ORDER does not match workflows/src/*.js — every .js file in `
        + `src/ must appear in ORDER exactly once (and vice versa):\n`
        + lines.map((l) => `  ${l}`).join('\n'),
    );
  }
  return ORDER.filter((f) => found.has(f));
}
```

Note: `orderMismatches` is used here before its export site in the file. In ESM that is fine for function declarations/exports (live bindings). If the local runtime errors on TDZ because a `const` form is used, move the `orderMismatches` function above `present()` — prefer defining `orderMismatches` **above** `present()` to keep the read order obvious.

Recommended file order after this task:

1. `ORDER` constant
2. hoist helpers
3. `export function orderMismatches(...)`
4. `function present()` (calls it)
5. `strip` / collisions / `build` / main-guard

- [ ] **Step 2: Run JS build tests**

```bash
node --test workflows/test/build.test.js
```

Expected: all PASS. The existing `build()` call now transits the guard against real disk.

- [ ] **Step 3: Confirm the real build succeeds and `pipeline.js` is unchanged**

```bash
cp workflows/pipeline.js /tmp/pipeline.js.before
node workflows/build.js
cmp /tmp/pipeline.js.before workflows/pipeline.js
python3 -m pytest tests/test_bundle_fresh.py -q
```

Expected: `built .../pipeline.js`; `cmp` silent (identical); 7 passed.

- [ ] **Step 4: Optional local smoke of the throw path (do not commit the stray file)**

```bash
touch workflows/src/_stray_order_guard.js
node workflows/build.js; echo exit:$?
rm workflows/src/_stray_order_guard.js
```

Expected: non-zero exit; stderr names `_stray_order_guard.js` and includes the "add each file to ORDER… or remove the stray file" remedy. Confirm the stray file is gone before committing.

- [ ] **Step 5: Commit**

```bash
git add workflows/build.js
git commit -m "$(cat <<'EOF'
fix(workflows): fail the build when ORDER omits or invents a src module (#74)

present() is the seam where ORDER meets disk; throw there with remedies so a
new workflows/src/*.js file cannot silently drop out of the shipped bundle.
EOF
)"
```

---

### Task 3: Suites sweep + PR

**Files:**

- No further code unless suites fail
- Open PR against `main` closing #74

- [ ] **Step 1: Run the always-on suites that exercise this surface**

```bash
node --test workflows/test/build.test.js
python3 -m pytest tests/test_bundle_fresh.py -q
git diff --exit-code workflows/pipeline.js
```

Expected: green; no `pipeline.js` diff.

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin HEAD
gh pr create --title "fix(workflows): fail build when ORDER drifts from src/*.js" --body "$(cat <<'EOF'
## Summary
- Export pure `orderMismatches` and call it from `present()` so `ORDER` must equal `workflows/src/*.js` (both directions), throwing with actionable remedies.
- Unit-test fabricated mismatches in `workflows/test/build.test.js`; mutation-gutted the whole helper and watched the tests go red.
- `pipeline.js` is byte-identical by construction — `build.js` is not bundled. Suites-only (#101); no smoke.

## Incidental (intentional)
Any stray `*.js` under `workflows/src/` (including editor swap/backup files named `*.js`) now fails the build. That is the point of the guard.

## Test plan
- [x] `node --test workflows/test/build.test.js`
- [x] Whole-mechanism mutation of `orderMismatches` → fabricated tests red → restored
- [x] `node workflows/build.js` + `cmp` against pre-build `pipeline.js`
- [x] `python3 -m pytest tests/test_bundle_fresh.py -q`
- [x] Local stray-file throw (not committed)

Closes #74
EOF
)"
```

- [ ] **Step 3: After merge — tick #101**

Comment on #101 that Wave 1 `#74` is done (or edit the checkbox when closing), and close #74 via the PR.

---

## Spec coverage self-review

| Spec requirement | Task |
| --- | --- |
| Pure `orderMismatches` both directions, sorted returns | Task 1 |
| Fabricated tests: disk-only, ORDER-only, both, equal/empty | Task 1 |
| Whole-mechanism mutation proof | Task 1 Step 5 |
| Guard inside `present()`; keep intersection | Task 2 |
| Throw message includes remedies both ways | Task 2 |
| No Python predicate copy | Global + Task 3 (bundle-fresh only runs real build) |
| `pipeline.js` untouched / byte-identical by construction | Task 2 Step 3, Task 3 |
| Suites-only, no smoke | Global + Task 3 |
| Name stray-`*.js` incidental in PR | Task 3 PR body |
| Close #74 / tick #101 | Task 3 |

No placeholders found. Types consistent: `{ missingFromOrder, missingFromDisk }` throughout.
