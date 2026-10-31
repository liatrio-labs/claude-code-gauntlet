# Scope exclusion contract

Generated preparatory data; active discovery exclusions remain in force.

## `pre_existing`

Consumers: early; predicate: `blame_pre_existing`; proof: `verified_fact`; outcome: `report_only`.

Actual git_blame pre-existence with no affected cross-file references.

Origin surfaced, off-diff placement and descriptions do not prove pre-existence.

Exceptions: cross_file_effect_created_by_pr.

Facts: blame_class, blame_source, cross_file_refs.

## `untouched_line`

Consumers: early; predicate: `off_diff`; proof: `verified_fact`; outcome: `body`.

Verified in_diff=false; unknown is not false.

Placement only after admission and vetting; still competes for challenge capacity.

Exceptions: none.

Facts: in_diff.

## `generated_vendored`

Consumers: early; predicate: `generated_or_vendored`; proof: `verified_fact`; outcome: `eliminate`.

git check-attr at captured head or generated markers in the first source lines.

A conventions word is not classification; the security exception requires no confirmed vulnerable version.

Exceptions: security_dependency_vulnerability_claim.

Facts: linguist-generated, linguist-vendored, generated_marker.

## `docs_only`

Consumers: early; predicate: `documentation_only_code_claim`; proof: `verified_fact`; outcome: `eliminate`.

Finding file has a source role and every changed file is documentation, verified at captured head by git check-attr.

Extensions and mixed PRs prove neither whole-PR documentation scope nor irrelevance; unknown stays unknown.

Exceptions: documentation_or_comment_claim, test_applicable_documented_convention.

Facts: linguist-documentation, all_changed_files_documentation, path_role.

## `lockfile`

Consumers: early; predicate: `mechanical_lockfile`; proof: `verified_fact`; outcome: `eliminate`.

Closed basename list and verified mechanical scope.

The security exception is claim-based and needs no prior vulnerable-version confirmation.

Exceptions: security_dependency_vulnerability_claim.

Facts: lockfile_basename, mechanical_change.

Basenames: package-lock.json, npm-shrinkwrap.json, yarn.lock, pnpm-lock.yaml, Cargo.lock, Gemfile.lock, poetry.lock, Pipfile.lock, uv.lock, composer.lock, packages.lock.json.

## `general_quality`

Consumers: admission; predicate: `canonical_admission`; proof: `admission`; outcome: `admission`.

Consume canonical quality admission and its present_defect promotion.

A convention finding grounded in an in-repo precedent names the files that establish the pattern; a preference with no named precedent stays excluded.

Exceptions: named_in_repo_convention, confirmed_present_defect.

Facts: admission.

## `latent`

Consumers: post_validation; predicate: `future_change_only`; proof: `verified_fact`; outcome: `body`.

Existing future-change-only reachability assessment appropriate to the actual claim.

Placement after admission and vetting; no invented runtime trigger or early challenge exclusion.

Exceptions: present_static_defect.

Facts: reachability.

## `test_only_pattern`

Consumers: validator, challenger; predicate: `harmless_test_pattern`; proof: `citation`; outcome: `eliminate`.

Cited harmless test-role pattern and its applicability; test path alone is insufficient.

Wrong assertions, mocks and waits remain defects.

Exceptions: test_coverage_origin, test_applicable_convention, genuine_test_defect.

Facts: path_role.

## `lint_catchable`

Consumers: validator, challenger; predicate: `tool_covers_claim`; proof: `citation`; outcome: `eliminate`.

Cite applicable repository tool, rule or configuration and coverage of the actual claim.

Familiar syntax and assumed CI tools are insufficient.

Exceptions: none.

Facts: none.

## `suppressed`

Consumers: validator, challenger; predicate: `suppression_covers_claim`; proof: `citation`; outcome: `eliminate`.

Cited suppression location, range and rule cover this issue.

A nearby suppression does not establish applicability.

Exceptions: none.

Facts: none.

## `opted_out`

Consumers: validator, challenger; predicate: `opt_out_covers_claim`; proof: `citation`; outcome: `eliminate`.

Cite repository opt-out mechanism and applicable file or rule opt-out.

An author assertion is not an opt-out mechanism.

Exceptions: none.

Facts: none.

## `intentional`

Consumers: validator, challenger; predicate: `confirmed_intent`; proof: `citation`; outcome: `eliminate`.

Structured conventions side-record identity in the same file and 10-line bucket, plus independent rule/comment citation.

The side record is only a gating fact, never a finding or proof; description and intentional wording are insufficient.

Exceptions: proven_incorrect_or_dangerous_behavior_including_security.

Facts: conventions_confirmation.
