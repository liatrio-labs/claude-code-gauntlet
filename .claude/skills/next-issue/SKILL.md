---
name: next-issue
description: Work the next open item on the issue #101 roadmap from plan to a review-ready PR, then close it out after the merge.
argument-hint: "[issue number]"
disable-model-invocation: true
---

Plan, implement and validate the next open item on the roadmap in issue #101. That is the first unchecked bullet from the top, nested bullets included. Read #101 first to get up to speed. If the item is a checkpoint run rather than an issue, stop and ask me.

Issue to work instead, if given: $ARGUMENTS

Before planning, look for other open items in #101 that belong in the same session. Candidates are siblings nested under the same parent and items that touch the same files or mechanism or would share one design. Group them into one plan and one PR where a single change serves them better than several. Keep unrelated items apart, and keep a group small enough to review as one change. Say which items you grouped and why before you start.

You are the orchestrator and planner; your agents do the work. Follow your usual pattern. Run no paired mini or other paid bench run unless the plan calls for one, and never without my approval.

Every PR also pays down tech debt in and around the code it touches. Trim code comments and doc text to what a reader needs. Keep a comment that says why the code is the way it is, and cut ones that restate the code, narrate its history or repeat a docstring or AGENTS.md. Move duplicated or bespoke logic into shared functions and modules, and remove over-engineering the change exposes. Keep behaviour unchanged unless the issue changes it, and let the suites prove that. A bigger PR is fine where the cleanup is reasonable to review with the change. Put the cleanup in the build brief and the review scope.

Test bloat is debt too. When logic moves, cut its tests down to what they prove. Mapping each old test to a new one is a port, not a consolidation. Put a cut list in the plan. Judge the result by a kill matrix over arm-level mutants: keep the smallest set of tests that still kills every mutant, plus any test that pins output a user sees. Keep the matrix cheap. Generate mutants only for the functions and tables whose tests are cut or rewritten, not for every declaration in every touched module. Run the old tests and the cut tests over that one mutant set and compare by mutant; run no matrix for a stage that only moves code or comments, where the suites and a line-for-line comparison are the proof. Run one matrix at a time, and never alongside agents that run the suites themselves. An audit of the cut covers only what the matrix cannot model (regex alternatives, object keys and key order, literal strings); it does not build a second mutation sweep. Report tests cut, not tests mapped.

Size each test's net to what it protects. Give full proof where a miss reaches users, security or a published contract. Give developer tooling, and tests of test helpers, one accepting and one rejecting case per behaviour. The old suite is not the standard: drop a proof it held when the proof fails that bar. If the cut list misses the issue's line target, stop before the build and put the value call to me.

You judge every agent finding before it becomes work. A finding is a claim, not an instruction. Accept it only when you can name a realistic edit or input that causes the failure, and who it hurts. Reject a scenario that needs a change no maintainer would make, a mutant finer than the arm level, or a difference with no visible effect, and say why in one line. A reviewer's own mutants never widen the matrix. Brief reviewers with this bar, and tell them a finding without a realistic cause is not a finding.

Triage every out-of-scope finding:

- Fix it in this PR when it is in or next to code you touched, small, and needs no new design decision. This is the default.
- File an issue, nested in #101 under the item it came from, only when it needs its own design or decision, belongs to another subsystem, or is a real defect that would otherwise be lost. File it only if I would plausibly prioritise it on its own.
- Otherwise note it in the PR body and move on. Small debt in files a later #101 item will touch belongs to that item.

When the work is validated, open the PR and run the headless gauntlet on it. Address its findings and every other comment on the PR, and get CI fully green. Verify the PR body's claims against the code and the run records, and fold what that finds into the PR.

Then stop and ask me to approve the merge. That approval is the one human gate in this run. Once I approve, merge if I tell you to, then close out: check off every completed issue in #101 and post the close-out records.
