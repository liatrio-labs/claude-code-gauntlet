# CHANGELOG

<!-- version list -->

## v3.5.2 (2026-08-07)

### Bug Fixes

- **args**: Absolute-harden provenance-only repoRoot
  ([#152](https://github.com/liatrio-labs/claude-code-gauntlet/pull/152),
  [`300ddef`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/300ddeff9932e583ff828cc79a17c04253057689))

- **args**: Absolute-harden provenance-only repoRoot (#81)
  ([#152](https://github.com/liatrio-labs/claude-code-gauntlet/pull/152),
  [`300ddef`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/300ddeff9932e583ff828cc79a17c04253057689))

### Refactoring

- **args**: Merge absolute path guards for outputDir and repoRoot
  ([#152](https://github.com/liatrio-labs/claude-code-gauntlet/pull/152),
  [`300ddef`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/300ddeff9932e583ff828cc79a17c04253057689))


## v3.5.1 (2026-08-07)

### Bug Fixes

- Give Cursor Cloud VMs a build-time toolchain fix instead of deleted prose (#98)
  ([#151](https://github.com/liatrio-labs/claude-code-gauntlet/pull/151),
  [`da495f6`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/da495f6a796a312fb6e530c0dee60d00b87905e5))


## v3.5.0 (2026-08-07)

### Features

- Persist outputDir-prefix fence for planned and writer paths (#148)
  ([#150](https://github.com/liatrio-labs/claude-code-gauntlet/pull/150),
  [`3608a60`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3608a60c9f27b39d98930db520e1e312ee35fd8e))

- **workflows**: Confine Persist paths under absolute outputDir
  ([#150](https://github.com/liatrio-labs/claude-code-gauntlet/pull/150),
  [`3608a60`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3608a60c9f27b39d98930db520e1e312ee35fd8e))

### Testing

- **workflows**: Cover derived Persist path fences
  ([#150](https://github.com/liatrio-labs/claude-code-gauntlet/pull/150),
  [`3608a60`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3608a60c9f27b39d98930db520e1e312ee35fd8e))


## v3.4.0 (2026-08-07)

### Bug Fixes

- Contain review artifacts in a single ignored output dir (#86)
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))

- **args**: Reject non-absolute outputDir
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))

- **scripts**: Address #86 PR review and lint failures
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))

- **skill**: Phase 1 ensure_output_dir; drop Composite A gitignore
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))

### Documentation

- Markdown delivery is path-surface only
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))

### Features

- **scripts**: Ensure_output_dir containment gate
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))

### Testing

- Pin output-dir containment and no-write markdown delivery
  ([#149](https://github.com/liatrio-labs/claude-code-gauntlet/pull/149),
  [`ccd8dcc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ccd8dccff161806b9d38f247a026e0e7367a6a71))


## v3.3.14 (2026-08-06)

### Bug Fixes

- **bench**: G3 degrade regex + retry archive for --check (#57, #85)
  ([#147](https://github.com/liatrio-labs/claude-code-gauntlet/pull/147),
  [`140bee2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/140bee2ed06e084ca95846715216d77ae52ebcf0))

- **bench**: Narrow G3 degrade regex and archive superseded wf records on retry
  ([#147](https://github.com/liatrio-labs/claude-code-gauntlet/pull/147),
  [`140bee2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/140bee2ed06e084ca95846715216d77ae52ebcf0))

### Chores

- Re-trigger CI after Actions recovery
  ([#147](https://github.com/liatrio-labs/claude-code-gauntlet/pull/147),
  [`140bee2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/140bee2ed06e084ca95846715216d77ae52ebcf0))

### Documentation

- Pin delivery vs report finding vocabularies (#64)
  ([#144](https://github.com/liatrio-labs/claude-code-gauntlet/pull/144),
  [`09970a2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/09970a29b6dcadd33037041d274747d523baacd6))

- **agents**: Write apostrophes literally in contract examples
  ([#145](https://github.com/liatrio-labs/claude-code-gauntlet/pull/145),
  [`d8b938f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/d8b938fc4d2a54a6ef3073f6575afc48a579b032))

- **agents**: Write apostrophes literally in contract examples (#68)
  ([#145](https://github.com/liatrio-labs/claude-code-gauntlet/pull/145),
  [`d8b938f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/d8b938fc4d2a54a6ef3073f6575afc48a579b032))

- **skills**: Name delivery vs report finding vocabularies
  ([#144](https://github.com/liatrio-labs/claude-code-gauntlet/pull/144),
  [`09970a2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/09970a29b6dcadd33037041d274747d523baacd6))

### Testing

- Assert delivery-guide JSON example has non-empty findings
  ([#144](https://github.com/liatrio-labs/claude-code-gauntlet/pull/144),
  [`09970a2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/09970a29b6dcadd33037041d274747d523baacd6))

- Make delivery guide JSON assertion type-safe
  ([#144](https://github.com/liatrio-labs/claude-code-gauntlet/pull/144),
  [`09970a2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/09970a29b6dcadd33037041d274747d523baacd6))

- Pin delivery vs report vocabulary to _FIELD_RENAMES
  ([#144](https://github.com/liatrio-labs/claude-code-gauntlet/pull/144),
  [`09970a2`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/09970a29b6dcadd33037041d274747d523baacd6))

- Sharpen the unicode-escape guard's comment and advice
  ([#145](https://github.com/liatrio-labs/claude-code-gauntlet/pull/145),
  [`d8b938f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/d8b938fc4d2a54a6ef3073f6575afc48a579b032))


## v3.3.13 (2026-08-05)

### Bug Fixes

- Restore await_workflow AGENTS rationale and raise byte ratchet
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

### Chores

- Allow Octo in README cspell override
  ([#140](https://github.com/liatrio-labs/claude-code-gauntlet/pull/140),
  [`b884c43`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/b884c430dbbcebb75aa021cc52550bb9129373b9))

- **deps**: Bump the actions group with 8 updates
  ([#139](https://github.com/liatrio-labs/claude-code-gauntlet/pull/139),
  [`a451c58`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a451c5817ecad1bb024fee0d6d51bc69e85637bb))

### Code Style

- **bench**: Ruff-format dark CSS string concat in report.py
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **tests**: Satisfy full verification gates
  ([#142](https://github.com/liatrio-labs/claude-code-gauntlet/pull/142),
  [`3a56dfc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3a56dfc8453fefcc13a21e54cfdbeed991615dd6))

### Continuous Integration

- Add Dependabot for Actions and gate workflows with zizmor
  ([#138](https://github.com/liatrio-labs/claude-code-gauntlet/pull/138),
  [`0d64292`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/0d64292b2366593c0853c9b91c801befc299e150))

- Add OpenSSF Scorecard workflow with SARIF publish
  ([#140](https://github.com/liatrio-labs/claude-code-gauntlet/pull/140),
  [`b884c43`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/b884c430dbbcebb75aa021cc52550bb9129373b9))

- Clarify upload-sarif pin comment as v3.37.6
  ([#140](https://github.com/liatrio-labs/claude-code-gauntlet/pull/140),
  [`b884c43`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/b884c430dbbcebb75aa021cc52550bb9129373b9))

- Exact-pin pip in coverage gate legs
  ([#138](https://github.com/liatrio-labs/claude-code-gauntlet/pull/138),
  [`0d64292`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/0d64292b2366593c0853c9b91c801befc299e150))

- Harden Actions supply chain (SHA pins, Dependabot, zizmor)
  ([#138](https://github.com/liatrio-labs/claude-code-gauntlet/pull/138),
  [`0d64292`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/0d64292b2366593c0853c9b91c801befc299e150))

- Pin in-workflow package installs and smoke CLI via npm
  ([#138](https://github.com/liatrio-labs/claude-code-gauntlet/pull/138),
  [`0d64292`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/0d64292b2366593c0853c9b91c801befc299e150))

- Publish OpenSSF Scorecard after supply-chain hardening
  ([#140](https://github.com/liatrio-labs/claude-code-gauntlet/pull/140),
  [`b884c43`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/b884c430dbbcebb75aa021cc52550bb9129373b9))

- SHA-pin Actions uses, persist-credentials, top-level permissions
  ([#138](https://github.com/liatrio-labs/claude-code-gauntlet/pull/138),
  [`0d64292`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/0d64292b2366593c0853c9b91c801befc299e150))

### Documentation

- Add OpenSSF Scorecard badge with solo-maintainer honesty note
  ([#140](https://github.com/liatrio-labs/claude-code-gauntlet/pull/140),
  [`b884c43`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/b884c430dbbcebb75aa021cc52550bb9129373b9))

- Record #110 consolidations and stages preamble decline
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **bench**: Record smoke CLI npm pin and checkpoint bump policy
  ([#138](https://github.com/liatrio-labs/claude-code-gauntlet/pull/138),
  [`0d64292`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/0d64292b2366593c0853c9b91c801befc299e150))

- **tests**: Justify #110 AGENTS.md byte ratchet raise
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

### Refactoring

- Consolidate accidental duplications (#110)
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- Share CLI result writes and pin stderr summaries
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- Unify merge-findings brace scanners in both runtimes
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **bench**: Emit dark-mode CSS variables from one map
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **bench**: Share transcript content-block walk in profile_run
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **bench**: Share wf_*.json baseline filter between copier and parser
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **tests**: Share assemble_artifacts hard-failure assertion helper
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

### Testing

- Strengthen overstated claim tests (#109)
  ([#142](https://github.com/liatrio-labs/claude-code-gauntlet/pull/142),
  [`3a56dfc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3a56dfc8453fefcc13a21e54cfdbeed991615dd6))

- Use mkdtemp for the new --output stdout pins
  ([#143](https://github.com/liatrio-labs/claude-code-gauntlet/pull/143),
  [`e7b42b4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e7b42b47bf2d6b135d88cb229a91252836aa24f7))

- **materialize**: Pin main() unexpected-exception receipt; reword happy-path
  ([#142](https://github.com/liatrio-labs/claude-code-gauntlet/pull/142),
  [`3a56dfc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3a56dfc8453fefcc13a21e54cfdbeed991615dd6))

- **post_review**: Pin valid_lines=None skip-diag branch; rename empty-map case
  ([#142](https://github.com/liatrio-labs/claude-code-gauntlet/pull/142),
  [`3a56dfc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3a56dfc8453fefcc13a21e54cfdbeed991615dd6))

- **verify_findings**: Assert grep fast path skips git grep
  ([#142](https://github.com/liatrio-labs/claude-code-gauntlet/pull/142),
  [`3a56dfc`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/3a56dfc8453fefcc13a21e54cfdbeed991615dd6))


## v3.3.12 (2026-08-04)

### Bug Fixes

- **workflows**: Drop unused bindings so Biome noUnusedVariables is green
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))

### Continuous Integration

- Add checksum-pinned Biome js-lint job and required-check freeze
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))

- Gate workflows JS on checksum-pinned Biome lint (#105)
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))

### Documentation

- Correct shipped-runtime tooling boundary (no Node builtins)
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))

- **tooling**: Adopt Biome config and restate no-npm CI-binary boundary
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))

### Testing

- Document AGENTS budget ratchet for #105 Biome docs
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))

- Ratchet AGENTS budget and exempt Biome rule id
  ([#136](https://github.com/liatrio-labs/claude-code-gauntlet/pull/136),
  [`68e1f3d`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/68e1f3d038b541373770aafe2d59f47f0525bbc3))


## v3.3.11 (2026-08-04)

### Bug Fixes

- **scripts**: Anchor renamed-file positions to the pre-rename old_path
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

- **scripts**: GitLab inline delivery — send old_line, real glab added-file detection,
  fault-tolerant posting ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

- **scripts**: Keep real a/ and b/ directories in GitLab paths
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

- **scripts**: Repair GitLab inline MR discussion delivery (#127, #130)
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

- **scripts**: Split diffs on newlines only; pin budget edge cases
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

- **scripts**: Stop is_new_file's stripped fallback from misreporting real a/-rooted files as new
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

- **scripts**: Suspend diff header matching inside hunk bodies; normalize finding paths
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))

### Chores

- **ci**: Raise scripts coverage floor to 91.7 from PR #135 CI headroom
  ([#135](https://github.com/liatrio-labs/claude-code-gauntlet/pull/135),
  [`02ac1c5`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/02ac1c51c1545c516b336000c84048f01884dfde))


## v3.3.10 (2026-08-04)

### Bug Fixes

- Address PR #129 review comments
  ([#129](https://github.com/liatrio-labs/claude-code-gauntlet/pull/129),
  [`93a7d6c`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/93a7d6c7af4a571cfd20c7a511e918ca5f1d15c4))

### Chores

- **tooling**: Gate ruff and mypy via pre-commit (#104 part B)
  ([#129](https://github.com/liatrio-labs/claude-code-gauntlet/pull/129),
  [`93a7d6c`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/93a7d6c7af4a571cfd20c7a511e918ca5f1d15c4))


## v3.3.9 (2026-08-03)

### Bug Fixes

- **bench**: Address PR #128 review nits
  ([#128](https://github.com/liatrio-labs/claude-code-gauntlet/pull/128),
  [`1864baf`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1864baf336b57d4c27e5c7ab981c0832b66a93c9))

### Chores

- **tooling**: Add ruff and mypy config for #104
  ([#128](https://github.com/liatrio-labs/claude-code-gauntlet/pull/128),
  [`1864baf`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1864baf336b57d4c27e5c7ab981c0832b66a93c9))

- **tooling**: Apply safe ruff --fix under locked select
  ([#128](https://github.com/liatrio-labs/claude-code-gauntlet/pull/128),
  [`1864baf`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1864baf336b57d4c27e5c7ab981c0832b66a93c9))

- **tooling**: Ruff config, format, and safe autofix (#104 part A)
  ([#128](https://github.com/liatrio-labs/claude-code-gauntlet/pull/128),
  [`1864baf`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1864baf336b57d4c27e5c7ab981c0832b66a93c9))

### Code Style

- Apply ruff format across Python tree
  ([#128](https://github.com/liatrio-labs/claude-code-gauntlet/pull/128),
  [`1864baf`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1864baf336b57d4c27e5c7ab981c0832b66a93c9))


## v3.3.8 (2026-08-03)

### Bug Fixes

- **ci**: Address #126 review — pin sync, exempt tripwire, floors wording
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- **test**: Correct AGENTS byte ratchet mangled by underscore replace
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

### Build System

- Add [project]-less pyproject for coverage config and CI pin group
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

- Measure subprocesses via coverage patch=subprocess
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

### Chores

- Gitignore coverage data files
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

### Continuous Integration

- Gate JS coverage with floors, allowlist, and presence (#103)
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- Gate JS coverage with floors, allowlist, and presence check
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- Gate Python coverage on 3.12 legs for scripts/ and bench/ (#102)
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

- Gate python coverage on the 3.12 legs of test and bench-tests
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

- Re-pin JS branch coverage floor to 82.3 from first green CI
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- Re-pin scripts coverage floor to 91.3 from first green 3.12 run
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

### Documentation

- Cross-reference the pytest-stack pin surface in pre-commit config
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

- Point CONTRIBUTING at AGENTS coverage gates; pin Node 24.18.0
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- Record coverage gate, thresholds, and CI-tooling stdlib carve-out
  ([#125](https://github.com/liatrio-labs/claude-code-gauntlet/pull/125),
  [`e943478`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/e9434782bd176efba8e31d95a535ce1270fd71f4))

- Unify coverage gates block and document JS floors
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- **security**: Record trust-boundaries posture for repo-supplied rule text (#82)
  ([#121](https://github.com/liatrio-labs/claude-code-gauntlet/pull/121),
  [`f888108`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/f8881082dac500054b397802c406933d4a51a036))

### Testing

- **ci**: Pin coverage gate command identity AGENTS↔ci.yml
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- **coverage**: Reject empty JS coverage scope
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- **parity**: Cover type_design dimension routing both ways
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))

- **workflows**: Add coverage presence check against scope allowlist
  ([#126](https://github.com/liatrio-labs/claude-code-gauntlet/pull/126),
  [`9a66910`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9a66910e62a93d2f10935a6d6cbd0c42f4ecfa83))


## v3.3.7 (2026-08-01)

### Bug Fixes

- **workflows**: Fail build when ORDER drifts from src/*.js
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))

- **workflows**: Fail the build when ORDER omits or invents a src module
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))

- **workflows**: Reject a module listed twice in ORDER
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))

### Chores

- **docs**: Evict session scratch from docs/ and guard the tracked doc surface
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))

### Documentation

- Design the build.js ORDER completeness guard
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))

- Plan the build.js ORDER completeness guard
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))

### Testing

- **workflows**: Prove orderMismatches catches ORDER↔disk drift
  ([#120](https://github.com/liatrio-labs/claude-code-gauntlet/pull/120),
  [`cbbfc72`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cbbfc72c18e8a3b8dfda1bd02ca99340db06a760))


## v3.3.6 (2026-07-31)

### Bug Fixes

- **docs**: Address PR #119 review comments on guards and delivery
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **docs**: Apply mechanical v2→v3 residue corrections
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **docs**: Correct Task 6 summarize contracts
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **tests**: Guard verify-receipt seed row in registry presence test
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **tests**: Make the three #37 guards able to fail
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

### Chores

- Ignore .worktrees for isolated issue execution
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **workflows**: Refresh pipeline.js after src comment edits
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

### Documentation

- **audit**: Adjudicate #37 adversarial residue findings
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Close §6 broader-guards Task 7 follow-up
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Finalize #37 residue inventory and guards assessment
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Link deferred #37 residue to filed issues
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Reconcile adversarial residue accounting
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Record canonical containment probe
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Record the guard repairs and the two exports they surfaced
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: Skeleton v3 residue inventory and detector baseline
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **audit**: V2→v3 residue inventory, string registry, and CI guards
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

### Testing

- **docs**: Add machine-parsed string registry and presence CI
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **skills**: Guard references/ against silent orphans
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))

- **workflows**: Guard export functions against silent dead paths
  ([#119](https://github.com/liatrio-labs/claude-code-gauntlet/pull/119),
  [`5d0a797`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5d0a7979ebb93452f746165a8255fe8e6e595a0c))


## v3.3.5 (2026-07-31)

### Bug Fixes

- **ci**: Durable merge rules — required checks, freeze list, release STS pin
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **docs**: Retarget rules citations and assert quotations resolve
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **release**: Pin main-semantic-release STS to release.yml via OIDC claim
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

### Documentation

- Normalize durable merge planning markdown
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- Note that merges to main are CI-gated by the branch ruleset
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **audit**: Land the #55 engineering audit — inventory, register, and 59 verified comment
  corrections ([#111](https://github.com/liatrio-labs/claude-code-gauntlet/pull/111),
  [`1ce6e1b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1ce6e1b883f432539b8b06cd6db261eb3ae4cec7))

- **plan**: Implement durable merge rules for #58 and #108
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **spec**: Design durable merge rules for #58 and #108
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

### Testing

- **ci**: Freeze required PR check-run names for ruleset 16049246
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **rules**: Assert ruleset naming in function docstring
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **rules**: Harden final merge-rule guards
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))

- **rules**: Scrub emission residue across every tracked AGENTS/CLAUDE pair
  ([#112](https://github.com/liatrio-labs/claude-code-gauntlet/pull/112),
  [`7889940`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/7889940fac67639bf1daf9fafcbff98977283a88))


## v3.3.4 (2026-07-30)

### Bug Fixes

- **persist**: Carry the artifacts home in the return, not through a model
  ([#100](https://github.com/liatrio-labs/claude-code-gauntlet/pull/100),
  [`93ed5bb`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/93ed5bbe3a6e98819ec095b95d3474079a3546a0))

- **persist**: Give both channels one serializer, and honour returnPrimaries on its own
  ([#100](https://github.com/liatrio-labs/claude-code-gauntlet/pull/100),
  [`93ed5bb`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/93ed5bbe3a6e98819ec095b95d3474079a3546a0))

### Testing

- **rules**: Pin the instruction budgets as ratchets, and drop one unjustified rule
  ([#100](https://github.com/liatrio-labs/claude-code-gauntlet/pull/100),
  [`93ed5bb`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/93ed5bbe3a6e98819ec095b95d3474079a3546a0))


## v3.3.3 (2026-07-30)

### Bug Fixes

- **agents**: Drop the v2 emission contract the directory rules re-taught
  ([#99](https://github.com/liatrio-labs/claude-code-gauntlet/pull/99),
  [`fe52e75`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fe52e758c6854e2b80289b32b18602ef59626231))

- **rules**: Apply the total-byte budget after dedup, not before it
  ([#99](https://github.com/liatrio-labs/claude-code-gauntlet/pull/99),
  [`fe52e75`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fe52e758c6854e2b80289b32b18602ef59626231))

- **rules**: Deliver directory rules as generated twins, not pointers
  ([#99](https://github.com/liatrio-labs/claude-code-gauntlet/pull/99),
  [`fe52e75`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fe52e758c6854e2b80289b32b18602ef59626231))

- **rules**: Remove claims about code that is not on main, and guard the class
  ([#99](https://github.com/liatrio-labs/claude-code-gauntlet/pull/99),
  [`fe52e75`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fe52e758c6854e2b80289b32b18602ef59626231))

### Documentation

- Make AGENTS.md canonical and give every tool one source
  ([#99](https://github.com/liatrio-labs/claude-code-gauntlet/pull/99),
  [`fe52e75`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fe52e758c6854e2b80289b32b18602ef59626231))


## v3.3.2 (2026-07-30)

### Bug Fixes

- **persist**: Stop the wire from carrying backslash runs the writer cannot transcribe
  ([#92](https://github.com/liatrio-labs/claude-code-gauntlet/pull/92),
  [`1bd722b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/1bd722b2c853037b910e9f99a2fef78ed6569761))


## v3.3.1 (2026-07-29)

### Bug Fixes

- **verify**: Address PR #83 review — exact id keying, line pin, NaN cleanup
  ([#83](https://github.com/liatrio-labs/claude-code-gauntlet/pull/83),
  [`2c219f4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/2c219f44e5f094bd5fbd9a5e53c6c04d47d5fd89))

- **verify**: Echo a per-id delta with a content proof, not the findings (#25 PR2)
  ([#83](https://github.com/liatrio-labs/claude-code-gauntlet/pull/83),
  [`2c219f4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/2c219f44e5f094bd5fbd9a5e53c6c04d47d5fd89))

- **verify**: Make a malformed slice input diagnosable, and normalise fractional numerics at the
  input boundary ([#83](https://github.com/liatrio-labs/claude-code-gauntlet/pull/83),
  [`2c219f4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/2c219f44e5f094bd5fbd9a5e53c6c04d47d5fd89))

- **verify**: Match finding ids exactly, not trimmed
  ([#83](https://github.com/liatrio-labs/claude-code-gauntlet/pull/83),
  [`2c219f4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/2c219f44e5f094bd5fbd9a5e53c6c04d47d5fd89))

### Documentation

- **executor**: The executor may have read the arrays, it just must not return them
  ([#83](https://github.com/liatrio-labs/claude-code-gauntlet/pull/83),
  [`2c219f4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/2c219f44e5f094bd5fbd9a5e53c6c04d47d5fd89))

- **verify**: Name joinVerifyDeltas' precondition and its only failure mode
  ([#83](https://github.com/liatrio-labs/claude-code-gauntlet/pull/83),
  [`2c219f4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/2c219f44e5f094bd5fbd9a5e53c6c04d47d5fd89))


## v3.3.0 (2026-07-29)

### Bug Fixes

- **collect_project_rules**: Address PR79 parser/security issues
  ([#79](https://github.com/liatrio-labs/claude-code-gauntlet/pull/79),
  [`5ce6bfa`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5ce6bfa2c4991ef0fd82880a492c79ee594207a6))

- **context**: Make the walk bound a tunable runaway guard, not a policy cap
  ([#79](https://github.com/liatrio-labs/claude-code-gauntlet/pull/79),
  [`5ce6bfa`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5ce6bfa2c4991ef0fd82880a492c79ee594207a6))

- **context**: Restore the walk bound and disclose non-markdown pointers
  ([#79](https://github.com/liatrio-labs/claude-code-gauntlet/pull/79),
  [`5ce6bfa`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5ce6bfa2c4991ef0fd82880a492c79ee594207a6))

### Documentation

- **context**: Fix duplication and agent policy
  ([#79](https://github.com/liatrio-labs/claude-code-gauntlet/pull/79),
  [`5ce6bfa`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5ce6bfa2c4991ef0fd82880a492c79ee594207a6))

### Features

- **context**: Resolve @import pointers so project rules actually reach agents
  ([#79](https://github.com/liatrio-labs/claude-code-gauntlet/pull/79),
  [`5ce6bfa`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5ce6bfa2c4991ef0fd82880a492c79ee594207a6))

- **context**: Resolve @import pointers so project rules actually reach agents (#49)
  ([#79](https://github.com/liatrio-labs/claude-code-gauntlet/pull/79),
  [`5ce6bfa`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5ce6bfa2c4991ef0fd82880a492c79ee594207a6))


## v3.2.7 (2026-07-29)

### Bug Fixes

- **wait**: Address PR #77 review comments on awaiter error paths
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Close the /dev/null descriptor on the broken-pipe fallback
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Disclose every scan bound, and degrade a broken pipe
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Harden the awaiter against torn reads, BOMs and early abandonment
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Replace the Phase 3 sleep/Read poll loop with a blocking awaiter
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Replace the Phase 3 sleep/Read poll loop with a blocking awaiter (#26)
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Report the search paths even when no candidate root exists
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Require column-zero document starts in the awaiter scan
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))

- **wait**: Stop the scan bounds from dropping real results
  ([#77](https://github.com/liatrio-labs/claude-code-gauntlet/pull/77),
  [`15364f7`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/15364f77798f0e04508ee1ee9b799a867b68ea39))


## v3.2.6 (2026-07-28)

### Bug Fixes

- **args**: Address PR #76 review; make CLAUDE.md budget hook CI-safe
  ([#76](https://github.com/liatrio-labs/claude-code-gauntlet/pull/76),
  [`a03a5db`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a03a5dbe06bcb2930db8ba6450a16123788ae982))

- **args**: Apply NONCE_RE charset to headShaShort argv safety
  ([#76](https://github.com/liatrio-labs/claude-code-gauntlet/pull/76),
  [`a03a5db`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a03a5dbe06bcb2930db8ba6450a16123788ae982))

- **args**: Make entry-guard recovery copy-paste, not inference
  ([#76](https://github.com/liatrio-labs/claude-code-gauntlet/pull/76),
  [`a03a5db`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a03a5dbe06bcb2930db8ba6450a16123788ae982))

- **args**: Make entry-guard recovery copy-paste, not inference (#27)
  ([#76](https://github.com/liatrio-labs/claude-code-gauntlet/pull/76),
  [`a03a5db`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a03a5dbe06bcb2930db8ba6450a16123788ae982))

### Documentation

- **args**: Name entryArgs in the two seam comments, not the removed helper
  ([#76](https://github.com/liatrio-labs/claude-code-gauntlet/pull/76),
  [`a03a5db`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a03a5dbe06bcb2930db8ba6450a16123788ae982))

### Testing

- **docs**: Pin a byte budget on CLAUDE.md so growth cannot be silent
  ([#76](https://github.com/liatrio-labs/claude-code-gauntlet/pull/76),
  [`a03a5db`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/a03a5dbe06bcb2930db8ba6450a16123788ae982))


## v3.2.5 (2026-07-28)

### Bug Fixes

- **verify**: Address PR #71 review comments
  ([#71](https://github.com/liatrio-labs/claude-code-gauntlet/pull/71),
  [`fa5d2a4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fa5d2a40f7b102a309924b4e76b63caea25f2c2b))

- **verify**: Degrade only the failed slice, and retry it once first (#54, #25 PR1)
  ([#71](https://github.com/liatrio-labs/claude-code-gauntlet/pull/71),
  [`fa5d2a4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fa5d2a40f7b102a309924b4e76b63caea25f2c2b))

### Documentation

- Drop the CLAUDE.md section this change added
  ([#71](https://github.com/liatrio-labs/claude-code-gauntlet/pull/71),
  [`fa5d2a4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fa5d2a40f7b102a309924b4e76b63caea25f2c2b))

- **verify**: Correct the stale per-slice dispatch wording the review pass found
  ([#71](https://github.com/liatrio-labs/claude-code-gauntlet/pull/71),
  [`fa5d2a4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fa5d2a40f7b102a309924b4e76b63caea25f2c2b))

- **verify**: Name the right detector in the never-drop qualifier
  ([#71](https://github.com/liatrio-labs/claude-code-gauntlet/pull/71),
  [`fa5d2a4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fa5d2a40f7b102a309924b4e76b63caea25f2c2b))

- **verify**: Qualify the never-drop claim — trustSlice binds shape, not content
  ([#71](https://github.com/liatrio-labs/claude-code-gauntlet/pull/71),
  [`fa5d2a4`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/fa5d2a40f7b102a309924b4e76b63caea25f2c2b))


## v3.2.4 (2026-07-28)

### Bug Fixes

- **schema**: Address PR #61 review comments and markdownlint
  ([#61](https://github.com/liatrio-labs/claude-code-gauntlet/pull/61),
  [`40a1992`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/40a1992b91a632c2cd35232e8e72630f50db5c0e))

- **schema**: Declare the finding fields the agent contracts already instruct
  ([#61](https://github.com/liatrio-labs/claude-code-gauntlet/pull/61),
  [`40a1992`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/40a1992b91a632c2cd35232e8e72630f50db5c0e))

- **schema**: Declare the finding fields the agent contracts already instruct (#47)
  ([#61](https://github.com/liatrio-labs/claude-code-gauntlet/pull/61),
  [`40a1992`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/40a1992b91a632c2cd35232e8e72630f50db5c0e))

### Chores

- **lint**: Clear CHANGELOG consecutive blank line from 3.2.3 release
  ([#61](https://github.com/liatrio-labs/claude-code-gauntlet/pull/61),
  [`40a1992`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/40a1992b91a632c2cd35232e8e72630f50db5c0e))

- **lint**: Exclude generated CHANGELOG.md from markdownlint
  ([#61](https://github.com/liatrio-labs/claude-code-gauntlet/pull/61),
  [`40a1992`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/40a1992b91a632c2cd35232e8e72630f50db5c0e))

### Documentation

- **bench**: Record the functional-smoke non-change failure floor
  ([#61](https://github.com/liatrio-labs/claude-code-gauntlet/pull/61),
  [`40a1992`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/40a1992b91a632c2cd35232e8e72630f50db5c0e))


## v3.2.3 (2026-07-28)

### Bug Fixes

- **context**: Compute the shared-context read plan instead of asking agents to paginate
  ([#59](https://github.com/liatrio-labs/claude-code-gauntlet/pull/59),
  [`5106d06`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5106d06aecbb6e684f0e5bcccb324fc5460d311f))

- **context**: Compute the shared-context read plan instead of asking agents to paginate (#48)
  ([#59](https://github.com/liatrio-labs/claude-code-gauntlet/pull/59),
  [`5106d06`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5106d06aecbb6e684f0e5bcccb324fc5460d311f))

- **context**: Disclose unplannable read-plan degradation
  ([#59](https://github.com/liatrio-labs/claude-code-gauntlet/pull/59),
  [`5106d06`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5106d06aecbb6e684f0e5bcccb324fc5460d311f))

### Chores

- Drop a stray blank line semantic-release left in CHANGELOG.md
  ([#59](https://github.com/liatrio-labs/claude-code-gauntlet/pull/59),
  [`5106d06`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5106d06aecbb6e684f0e5bcccb324fc5460d311f))

### Refactoring

- **context**: Remove the hand-roll capability instead of guarding it
  ([#59](https://github.com/liatrio-labs/claude-code-gauntlet/pull/59),
  [`5106d06`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/5106d06aecbb6e684f0e5bcccb324fc5460d311f))

## v3.2.2 (2026-07-28)

### Bug Fixes

- **bench**: Address PR #56 review nits on G3 degrade scan
  ([#56](https://github.com/liatrio-labs/claude-code-gauntlet/pull/56),
  [`263614e`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/263614eaea39ff720717de1344034b9df1e9478a))

- **bench**: Decide G3 writer degrades from parsed gaps, not raw bytes
  ([#56](https://github.com/liatrio-labs/claude-code-gauntlet/pull/56),
  [`263614e`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/263614eaea39ff720717de1344034b9df1e9478a))

- **lint**: Address code-quality bot findings on PR #51
  ([#51](https://github.com/liatrio-labs/claude-code-gauntlet/pull/51),
  [`348901a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/348901a1e59d72dcb1ee79cb0c3ac8c7c6bca9b6))

- **lint**: Remove double-blank-line markdownlint violation in CHANGELOG.md
  ([#51](https://github.com/liatrio-labs/claude-code-gauntlet/pull/51),
  [`348901a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/348901a1e59d72dcb1ee79cb0c3ac8c7c6bca9b6))

- **lint**: Resolve markdownlint MD038 trailing-space-in-code-span failure, address remaining review
  nits ([#51](https://github.com/liatrio-labs/claude-code-gauntlet/pull/51),
  [`348901a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/348901a1e59d72dcb1ee79cb0c3ac8c7c6bca9b6))

- **persist**: Unwrap JSON-wrapped reports and retry a refused derived persist once
  ([#51](https://github.com/liatrio-labs/claude-code-gauntlet/pull/51),
  [`348901a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/348901a1e59d72dcb1ee79cb0c3ac8c7c6bca9b6))

### Performance Improvements

- Cut review wall-clock without changing review output
  ([#51](https://github.com/liatrio-labs/claude-code-gauntlet/pull/51),
  [`348901a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/348901a1e59d72dcb1ee79cb0c3ac8c7c6bca9b6))

- Cut review wall-clock without changing review output (#38)
  ([#51](https://github.com/liatrio-labs/claude-code-gauntlet/pull/51),
  [`348901a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/348901a1e59d72dcb1ee79cb0c3ac8c7c6bca9b6))

## v3.2.1 (2026-07-27)

### Bug Fixes

- **detection**: Address adversarial audit findings
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Address CodeQL findings on PR #45
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Address round-2 audit findings
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Address round-3 audit findings
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Address round-4 audit findings and pin every fix
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Address round-5 review findings on PR #45
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Make prior-review detection read the signal the pipeline actually writes
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **detection**: Reapply defect fixes and regenerate bench fixtures
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

- **skill**: Surface the previously-reviewed gate in SKILL.md Phase 2
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

### Chores

- Apply markdownlint fix to CHANGELOG.md
  ([#45](https://github.com/liatrio-labs/claude-code-gauntlet/pull/45),
  [`00b8164`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/00b8164bd6a6ef6e987fdc7db96d7cd3a008d8d3))

## v3.2.0 (2026-07-27)

### Bug Fixes

- **bench**: Add child-auth {api,subscription} for review children
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Address PR #44 review comments on identity G4
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- **bench**: Branch child credentialing on an explicit auth mode
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Drop a non-billable cost from the release-card leg too
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Harden identity parse and relative scriptPath match
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- **bench**: Keep credential-named identifiers out of printed messages
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Name the helper lookup for the file paths it returns
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Never mix credentials across one run dir on resume
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Persist the auth mode a resume of an orphan run dir spends
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Resolve the resume auth mode from one read, not the caller's copy
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Widen the apiKeyHelper preflight to the home-relative .claude dir
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

### Chores

- Apply markdownlint fix to CHANGELOG.md
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- Keep agent SDD scratch out of the tree
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- Re-apply the markdownlint fix to CHANGELOG.md
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

### Code Style

- **bench**: Lead the subscription prereq comment with its functional reason
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

### Documentation

- Warn against the skip-ci token in commit messages
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Document the subscription child-auth mode
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Log the deferred usage-limit classification in the watch ledger
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Qualify quoted costs and the subscription smoke recommendation
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Record why the per-run knobs are absent from --check's guard
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **skill**: Add pipeline_version and plugin_root to headless echo
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

### Features

- **bench**: Add --child-auth {api,subscription} to the runner
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Environment purity receipts for wrong-plugin rejection
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- **bench**: Invalidate child runs on plugin identity mismatch
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- **bench**: Label ledger rows with auth_mode and gate billable cost on it
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Parse pipeline_version and plugin_root identity receipts
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- **bench**: Prefer echo identity receipts in smoke checker G4
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

### Refactoring

- **bench**: Give the child-auth vocabulary and manifest chain one home
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

### Testing

- **bench**: Assert the CHILD process never receives the metered key
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Cover G4 defense-in-depth echo plus stale scriptPath
  ([#44](https://github.com/liatrio-labs/claude-code-gauntlet/pull/44),
  [`8b23eab`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8b23eabf468a2aee1c4d9401cf58d9032c357910))

- **bench**: Drop an unused module import from test_ledger
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

- **bench**: Exercise report.py's sys.path shim through a real script run
  ([#40](https://github.com/liatrio-labs/claude-code-gauntlet/pull/40),
  [`8c23613`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8c23613107d2e6cf3c5dbf193ee81b74df2585cf))

## v3.1.3 (2026-07-24)

### Bug Fixes

- Repair the tier-slug contradiction and the workflow's dead remediation step
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- **ci**: Make the label helper refuse unusable input and close test gaps
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

### Chores

- Apply markdownlint autofix to CHANGELOG.md
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

### Continuous Integration

- Verify the label manifest against the repository's labels
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

### Documentation

- Add SECURITY.md and put it in the cspell scope
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Align CONTRIBUTING and the PR checklist with the CI-enforced gates
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Check in the label taxonomy and expand the work-queue standard
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Cut unauthorized policy from SECURITY.md and fix stale claims
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Drop the invented tier slug and finish the conduct-channel split
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Record two verification gotchas in AGENTS.md
  ([`4db589a`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/4db589ab9f1561c52dfd8b470577c23a54aca871))

- Refresh the public contribution surface for v3
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Resolve the label taxonomy and tier vocabulary against their sources
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Sync issue forms with the shipped v3 architecture
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

### Testing

- Address code-gauntlet review findings on PR #41
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Harden the contribution-surface contract after adversarial review
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

- Pin the public contribution surface (Issue #30)
  ([`ad15f2f`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/ad15f2fc13225b6addbf287d4267dd117a1e6d44))

## v3.1.2 (2026-07-24)

### Bug Fixes

- **bench**: Catch writer degrade on compact-return carriers
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Codify measurement policy with mini subset and smoke checker
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Cover GitLab/union-schema paths and align mini dashboard bits
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Cover wf collect wiring and grade --tier mini in report
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Ignore nested verify scriptPath in smoke G4
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Point smoke checker at real workflow records
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

- **bench**: Stop incomplete mirror caches from poisoning CI
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

### Chores

- Apply markdownlint-cli2 autofix to CHANGELOG.md
  ([`66f6648`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/66f6648d83ed9056f0f98cb0f66b0cd444570441))

## v3.1.1 (2026-07-23)

### Bug Fixes

- Apply markdownlint fix to CHANGELOG blank lines
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

### Documentation

- Add AGENTS.md with Cursor Cloud dev environment setup
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

- Add AGENTS.md with Cursor Cloud setup instructions
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

- Clarify markdownlint-fix CHANGELOG gotcha in AGENTS.md
  ([`bcd543b`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/bcd543b4b131b0b4ada93260b2c43aa995b767f9))

## v3.1.0 (2026-07-23)

### Bug Fixes

- **agents**: Schema-declared extras are omit-not-null; changelog markdownlint fix (Bugbot PR-20
  wave 1)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **args,docs**: Shape-guard exclusionPatterns like reviewConfig.ignore; correct stale post-c8 doc
  claims (adversarial review)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **guard**: Mirror stage defaults for absent size limits — agent-count guard can never go
  NaN-silent (#17 item 8)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **skill**: AgentFlags is a derivation rule at assembly time + pre-dispatch light-scope check (M4
  live miss)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **skill**: Ground the light-scope answer in a fresh env re-read at assembly (M4 second miss)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **summarize**: Pin changedLines in the bucketed-path merge prompt too (Bugbot PR-20 wave 2)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

### Chores

- **bench**: Ledger + report for M3 union smoke (smoke-20260723-033811-6ea1737)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **bench**: Ledger + report for M3-of-record smoke-20260723-051739-1c6a310
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **bench**: Ledger + report for mini-subset A (custom-20260723-070640-c1dd46f, FAILED paired bar ->
  item-4 surgical revert)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **bench**: Ledger + report for mini-subset A re-run (custom-20260723-102149-381e9ff) — V3.1
  comparison row
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

### Documentation

- Qualify anchor noise-rate comparability in benchmark results
  ([#18](https://github.com/liatrio-labs/claude-code-gauntlet/pull/18),
  [`9090838`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/9090838301f974bbeadb8fdd4b62b0fdb6db29ab))

- **bench**: Reframe the report as a release-progression artifact; README results as v2 -> v3.0 ->
  v3.1
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **delivery**: Let-me-pick deselections apply to the prIdentity wrapper path too (Bugbot PR-20)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **readme**: Add the v3.1 paired mini-subset row to the benchmark table
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

### Features

- Code-gauntlet v3.1 benchmark-gated hardening + orchestrator-model pinning
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **agents**: Remove v2 NDJSON emission contract from discovery agents — v3 by-value output is the
  sole path (live-run L10)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **args**: Validation mirrors consumption — require changedFiles/changedLines, demote
  changedFilesPath to optional provenance (#17 item 6)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **challenge**: Structured blind location (file/line) replaces dead code field; prompt return-shape
  matches schema (#17 item 1 + wave-3 rider)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **hardening**: Waist validation for reviewConfig, entry-args guard, PR-identity delivery wrapper,
  legacy REVIEW.md self-heal, info/exclude, failingPhase (live-run L1-L9)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **policy**: Pin explicit full model IDs through resolvePolicy — agent pins immune to orchestrator
  session variant (#17 V3.1)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **scope**: Wire light scope — deep flag gates the seven non-core dimensions, full scope
  byte-identical (#17 item 7)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

- **verify**: Echo content-fidelity gate, numeric confidence end-to-end, deterministic agent/extras
  echo, writer write-proof (#17 items 2,3,4,5)
  ([`08da653`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/08da6532142a624197cd4c3e4612d2b140eb5140))

## v3.0.0 (2026-07-23)

### Bug Fixes

- **adjudicator**: Recover verdict replies with unescaped inner quotes [owner-approved]
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Child-model default back to inherit — both sonnet variants measured worse
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Classify killed-background workflow distinctly (not config_echo_mismatch)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Invoke namespace-qualified /deep-review:deep-review
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Uncap CLI background-wait ceiling so long Workflows aren't killed
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **build**: Strip duplicate trailing newline in generated bundle
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: CRLF bug in loadExclusions bullet-list fallback + review nits
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: PyIntStrict accepts booleans, matching Python int(bool)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Apply the agent-count guard — wire coarsenLimits into runWith (Bugbot)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Guard counts absent challengeCap as challenge-all, matching the stage (Bugbot)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Land Bugbot #1 (validate content) and #3 (agent short-name)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **pipeline**: Land final-review findings — pin numerics on UNVERIFIED path, extract modelFor
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **release**: Bump PIPELINE_VERSION at its source, not just the bundle
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **release**: Remove unused meta.version instead of patching its drift
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Preserve finding description through verify; exempt dashboard from mutation guard
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Repair five smoke-revealed runtime defects + runner integrity guard
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Align every dispatch to the platform agent()/parallel() contract
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Args-waist rename contract comments + 3 tests
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Correct entry contract per live Workflow-tool probe
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Make discover degradation reachable (null agent -> dims degraded)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Materialize verify slice inputs; resume state on failure paths
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Normalize agent field names in validate/challenge stages
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Persist v2 aliases, checkpoint round-trip, report segmentation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Chores

- Fix pre-commit formatting (markdownlint, end-of-file-fixer)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: BUILD_GATE — fresh bundle, all suites green
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Continuous Integration

- Fix semantic-release OIDC subject after repo rename
  ([#19](https://github.com/liatrio-labs/claude-code-gauntlet/pull/19),
  [`8893dcd`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/8893dcd8b65839f1a0dd11062a51c31b2a1e4d7c))

### Documentation

- Add Phase 3 workflow wait protocol to SKILL (poll to terminal before Phase 8)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Fix markdownlint failures blocking merge
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Full README pass for the v3 launch + CI job for the JS suite
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Reconcile SKILL v3 prose to workflow behavior (triage descriptive, resume state, phase refs)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- SKILL.md v3 workflow invocation + CLAUDE.md JS rules
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **artifact-writer**: Document postReview in the final-artifacts payload shape
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Addendum — workflow entry contract pinned during build
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Draft provisional D2 pending owner confirmation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Finalize Phase 0 gate — decisions D1-D3 recorded
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Online validation addendum for build/bench discoveries
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record agent() failure-contract and queueing probes (tests 5, 10)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record headless executor permission matrix (test 4, five legs)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record model/effort routing probes and D3 (tests 6, 7, 16)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record semantic-release lockstep probe (test 14)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record size-limit and owner-gated probe results (tests 8, 9, 11, 12, 15)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record unattended executor-permission data point (test 4 partial)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Record workflow invocation probe results (tests 1-3, 13)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **research**: Scaffold v3 Phase 0 smoke-test findings artifact
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **skill**: Include top-level checkpoints in the compact-return shape
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **skill**: Mark light/trivial scope as unwired in v3.0 (Bugbot)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Features

- Code-gauntlet v3 (formerly deep-review) — workflow-native pipeline
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- Rename plugin to code-gauntlet (formerly deep-review)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Add --prs explicit golden-PR list override
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Add v2-vs-v3 verdict panel + normalized efficiency
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Allow 1m-context child-model variants
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Bring every dashboard section current with the full ledger
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Expose holdout tier in run.py CLI
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Make the report dashboard legible to share
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Pin child-session model per tool (Gate-2 config)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **bench**: Wire deep-review-v3 into the frozen bench harness
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Calibrate discoverPrompt against agent self-censoring (hill-climb iter 2)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Deliver all challenge-survivors, deterministic Phase 8 selection (hill-climb iter 4)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Delivery tier as a user choice, default all (hill-climb iter 4, refined)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Enrich discovery dispatch prompt to v2-grade elicitation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Iter-5 quality — non-security threshold 55, challenge teeth, discovery sweeps
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: Scope calibration paragraph to bug-detector only (hill-climb iter 3)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **v3**: ValidateArgs accepts optional delivery selector (iter 4 delta)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **verify**: Receipt + executor agent + UNVERIFIED degradation path
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Args waist + argsVersion validation
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Bundle build system, entry contract, and version_variables
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: DIMENSIONS registry + S5 policy resolver
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Report/writer/checkpoints, top-level orchestration, boundary parity
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Stages 1-3 (summarize/discover/merge) with degradation + agent-count coarsening
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **workflows**: Validate/filter/challenge stages with degradation semantics
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Performance Improvements

- **pipeline**: Gate-2 overhead-only token cuts (validate batch, slim checkpoint, writer dedup)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Refactoring

- **policy**: Remove the frontier mode — ship the single benchmarked policy
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

### Testing

- **bench**: Resume honors manifest tool over CLI default
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: ApplyChallenges twin (comparator/deep-clone/dedup reuse)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: ApplyValidations twin (int-strictness trap)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: Dual-runtime parity harness + findingDedup twin
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: FilterFindings twin part 1 (thresholds/injection/exclusions)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: FilterFindings twin part 2 (disagreement/dedup_cross_agent, pyRound)
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: MergeFindings twin
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

- **parity**: Scanner edge fixtures + divergence notes
  ([#16](https://github.com/liatrio-labs/claude-code-gauntlet/pull/16),
  [`cdf19f8`](https://github.com/liatrio-labs/claude-code-gauntlet/commit/cdf19f81f6ca04c6f1bedf2d85deff0a8f4a02c9))

## v2.6.0 (2026-07-18)

### Bug Fixes

- **bench**: Actionable score_run failure surface, naive-anchor costs, nested payload probe
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Adjudicator retries non-object JSON; string line normalized before hunk slicing
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Collision-proof per-PR dirs, judge-pin fallback, naive reason and shape validation
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Contain OSError and cleanup failures per-PR; smoke workflow gets actions:write for
  artifact upload ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Exclude capacity fields from token usage sums
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Failed-run reason names is_error, not the envelope subtype
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Fetcher pins merge-base as base_sha; anchor adjudicator iterates candidate records
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Forward gh auth into the isolated context; single canonical envelope parser
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Harden run loop against missing-SHA sentinels and clone failures; nonzero exit on
  failed runs ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Load metered key from bench/.env into the claude invocation env
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Naive-anchor structured output contract wired through the adapter
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Per-PR containment of unexpected errors; per-candidate adjudication identity
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Pre-clean stale scorer stage outputs; calibrate watchdog to 45m
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Re-pin 4 base SHAs to mirror merge-base (API base.sha was branch tip, not branch point)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Self-heal stale worktrees on resume; naive invocation labeling; single dotenv parser
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Single canonical dotenv reader for prereqs and build_env
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Wire --score-only to score_run
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Config-echo receipt must reach the final message and report; invoke accepts all
  three sources ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Dry-run stdout reports capture, not posting
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Env-enforced dry-run in post_review; robust naive fence parse; shared scorer stage
  runner ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Review closed/merged PRs, never checkout live heads; fix stale payload_path
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Code Style

- Pre-commit fixes; exclude bench/golden fixtures from markdownlint; JSON writers emit trailing
  newline ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Continuous Integration

- Bench self-tests on PR; live smoke via workflow_dispatch
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- Pin publish-to-marketplace reusable workflow to @main
  ([#13](https://github.com/liatrio-labs/claude-deep-review/pull/13),
  [`53cd5be`](https://github.com/liatrio-labs/claude-deep-review/commit/53cd5be1ff4a0465c13029fd92f6800ad6efb591))

- Restore last-known-good publish-to-marketplace pin for diagnosis
  ([#14](https://github.com/liatrio-labs/claude-deep-review/pull/14),
  [`8ecd975`](https://github.com/liatrio-labs/claude-deep-review/commit/8ecd975dfa964082d6c3e2a3a20f9de6ef0ea24c))

- Trigger marketplace publish on release and workflow_dispatch
  ([#12](https://github.com/liatrio-labs/claude-deep-review/pull/12),
  [`c2ee576`](https://github.com/liatrio-labs/claude-deep-review/commit/c2ee576cf4de887747ccb78b939f2d771fa7bd17))

### Documentation

- **bench**: Stranger quickstart README
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **research**: P1 bypass probe completed — all three modes confirmed
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **research**: Record harness headless probes (artifact 33)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Features

- Bench harness + headless review mode (v3 pre-work)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Anchors re-judged under pinned judge
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Committed performance dashboard (report.py generates report.html from the ledger)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Fetch-and-pin per-PR head/base SHAs
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Freeze baselines and protected-path SHAs
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Headless invoker with watchdog, invalid-run detection, cost capture
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Mirror+worktree lifecycle with SHA drift guard
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: One-command runner ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Payload-to-candidates adapter
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Pin judge to claude-opus-4-8 alias (no dated 4.8 exists; discrepancy logged)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: PR-granular checkpointing and append-only ledger
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Re-pin judge to claude-opus-4-5-20251101 (4.8 rejects temperature; spec H5 jointly
  unsatisfiable) ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Record k=5 judge determinism (judge_sd = 0)
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Record smoke, naive-anchor, and v2 baseline ledger rows
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Scaffold bench/ and env template
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Scoring orchestration, frozen adjudicator, judge pin
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Sign delta_noise=0.24; final protected-path freeze
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Vendor anchor candidates + judge plumbing spot-check
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Vendor golden data and pinned subsets
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Vendor pinned scorer (dedup+judge) with attribution
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: Add --dry-run payload capture to post_review.py
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **headless**: DEEP_REVIEW_HEADLESS env contract + gate carve-outs
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

### Testing

- **bench**: Fix module reference in capacity-field test
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

- **bench**: Local headless E2E fixture
  ([#15](https://github.com/liatrio-labs/claude-deep-review/pull/15),
  [`fa8aa35`](https://github.com/liatrio-labs/claude-deep-review/commit/fa8aa353d1df40ce027158ee705d6357d8c22e7d))

## v2.5.0 (2026-07-16)

### Bug Fixes

- Address CodeRabbit review nitpicks
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- Address PR #5 review loose ends (dropped_no_id, dedup test, docs)
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

- Address review feedback on finding dedup module
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- Give change-summarizer a non-empty tools list for Claude Code 2.1.211+
  ([#10](https://github.com/liatrio-labs/claude-deep-review/pull/10),
  [`4976fa0`](https://github.com/liatrio-labs/claude-deep-review/commit/4976fa04aa1cdd67459e4129268ea4b2c8f7f607))

- Resolve 3 blocking issues from leehopper review
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- **merge_findings**: Count dropped_no_id from pre-validation findings
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

- **merge_findings**: Ensure scripts/ on sys.path for direct invocation
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Chores

- Strip trailing whitespace in finding_dedup.py
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Continuous Integration

- Bump publish-to-marketplace workflow SHA to 933e23c
  ([`bbeb92b`](https://github.com/liatrio-labs/claude-deep-review/commit/bbeb92bf54bdd1e237c56b61059cfb88afa5a1fc))

- Publish to marketplace after semantic release
  ([`4761e94`](https://github.com/liatrio-labs/claude-deep-review/commit/4761e94e50c425a03f28b4e9cf226be5eb1d3440))

- Unblock semantic-release by decoupling marketplace publish
  ([#11](https://github.com/liatrio-labs/claude-deep-review/pull/11),
  [`5f8aa87`](https://github.com/liatrio-labs/claude-deep-review/commit/5f8aa873a4ec6758dd61f1c2407792e149b1a185))

### Documentation

- Add privacy policy
  ([`3b821f5`](https://github.com/liatrio-labs/claude-deep-review/commit/3b821f5f1c8cb714c4864ed5d72a1376094597c4))

- **claude-md**: Add finding_dedup.py to pipeline-script list
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

- **finding_dedup**: Show standalone and pytest import forms
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))

### Features

- Add standalone finding deduplication module with cross-session persistence
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Refactoring

- Extract dedup_by_id from merge_findings into standalone module
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- Scope PR to dedup_by_id extraction only (route A)
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

### Testing

- Avoid hardcoded path in merge_findings import regression
  ([#5](https://github.com/liatrio-labs/claude-deep-review/pull/5),
  [`9c2ca28`](https://github.com/liatrio-labs/claude-deep-review/commit/9c2ca2831b5217be247adc48f487eb6e9758266b))

- **finding_dedup**: Pin first-wins equal-priority collision invariant
  ([#8](https://github.com/liatrio-labs/claude-deep-review/pull/8),
  [`8efe604`](https://github.com/liatrio-labs/claude-deep-review/commit/8efe60432bea276c26f2a0f11e00c274667ca897))
