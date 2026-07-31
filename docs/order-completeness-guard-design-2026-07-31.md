# ORDER completeness guard (#74) — design

Date: 2026-07-31
Issues: [#74](https://github.com/liatrio-labs/claude-code-gauntlet/issues/74) (Wave 1)
Roadmap: [#101](https://github.com/liatrio-labs/claude-code-gauntlet/issues/101)
Tier: suites-only

## Goal

Fail loudly when `set(ORDER)` ≠ `set(*.js on disk in workflows/src/)`, at the
moment someone runs `node workflows/build.js`. A new file left out of the pinned
`ORDER` array is today silently excluded from the bundle (`present()` returns
the intersection); unit tests that import `../src/<file>.js` stay green while
the shipped bundle is missing the module.

The guard lives in `workflows/build.js`, which is **not** itself bundled.
`workflows/pipeline.js` is therefore untouched entirely — byte-identical by
construction, not merely "byte-identical today."

## Approach

Same shape as `detectTopLevelCollisions`:

1. Export a pure function `orderMismatches(order, onDisk)` that returns sorted
   mismatch arrays in both directions.
2. Call it from `present()` (the single seam where `ORDER` meets disk) and throw
   on any mismatch, with an actionable remedy in the message.
3. Unit-test the pure function in `workflows/test/build.test.js` with fabricated
   mismatches; prove the mechanism by mutation (gut to empty returns → red).

No second predicate in the Python suites. `tests/test_bundle_fresh.py` already
subprocesses `node workflows/build.js`, so a build-time throw reaches that CI
leg without a copied assertion.

## Scope

### In scope

| Track | Deliverable |
| --- | --- |
| `workflows/build.js` | Export `orderMismatches`; call from `present()`; throw with remedies |
| `workflows/test/build.test.js` | Fabricated mismatch tests both directions + equal/empty; extend import |
| Mutation proof | Gut `orderMismatches` to always-empty; fabricated tests red; restore |
| Process | Suites-only PR; close #74; tick #101 Wave 1 checkbox |

### Out of scope

- Any assertion in `tests/test_bundle_fresh.py` or other Python suites.
- Smoke / bench / measurement.
- Reordering `ORDER`, auto-discovering concat order, or removing the
  post-check intersection filter in `present()`.
- Docs beyond the PR body and this design/plan pair.

## Mechanism

### `orderMismatches(order, onDisk)`

Pure exported function. No filesystem access.

```js
export function orderMismatches(order, onDisk) {
  // returns { missingFromOrder: string[], missingFromDisk: string[] }
  // both arrays sorted for stable messages
}
```

- `missingFromOrder` — basenames present in `onDisk` but not in `order`.
- `missingFromDisk` — basenames present in `order` but not in `onDisk`.
- Sort only the **returned** arrays. Do not disturb `ORDER` (dependency order)
  or the disk listing order passed in (`readdirSync` is platform-dependent).

### Seam: `present()`

`present()` (`build.js` ~33–36) is the one place `ORDER` meets disk and where
the silent intersection lives today:

1. Compute `found` from `readdirSync(SRC)` (`.js` only), as today.
2. Call `orderMismatches(ORDER, [...found])`.
3. If either array is non-empty, throw. Message must include the remedy:
   - for `missingFromOrder`: add the file to `ORDER` in dependency order, or
     remove the stray file;
   - for `missingFromDisk`: remove the name from `ORDER`, or restore the file.
4. Return the existing `ORDER.filter((f) => found.has(f))` unchanged.

`build()` inherits the guard through its only caller of `present()`. Leaving
the intersection in place after the equality check is deliberate YAGNI: once
equality is proven it is dead-defensive but free; restructuring it is not
worth the diff.

### Incidental behavior (correct; name in the PR)

Any stray `*.js` under `workflows/src/` — including editor swap/backup files
named `*.js` — now fails the build. That is the point of the guard and the
only new way a previously-green local build can start throwing. There is no
legitimate third state for a `.js` file in that directory.

## Tests

Extend `workflows/test/build.test.js`:

```js
import { detectTopLevelCollisions, build, orderMismatches } from '../build.js';
```

Required cases:

1. File on disk not in `ORDER` → `missingFromOrder` populated (sorted).
2. Name in `ORDER` not on disk → `missingFromDisk` populated (sorted).
3. Both directions at once.
4. Equal inputs (and empty inputs) → both arrays empty.

**Mutation rule (must actually run, not reason about):** gut the whole
`orderMismatches` mechanism so it always returns empty arrays; watch the
fabricated tests go red; restore. Do not gut half the check — a neighboring
fallback must not pass the mutation misleadingly.

Optional real-disk assertion: skip. The existing
`the real bundle build() produces no top-level collisions` test already calls
`build()`, which now transits the guard, so the live path is exercised for free.

## Verification tier

Suites-only under the #101 per-PR rule. This changes `build()` failure mode on
inputs that are today impossible (all nine `src/*.js` files are listed in
`ORDER`), and `pipeline.js` is byte-identical by construction because
`build.js` is not bundled. No smoke.

## Success criteria

- `node workflows/build.js` succeeds on the current tree; committed
  `pipeline.js` unchanged.
- Fabricated mismatch tests cover both directions and go red under full
  mechanism gutting.
- No new Python predicate.
- #74 closable; #101 Wave 1 `#74` checkbox tickable after merge.
