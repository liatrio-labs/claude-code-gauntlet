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

Every PR also pays down tech debt in and around the code it touches. Trim code comments and doc text to what a reader needs. Keep a comment that says why the code is the way it is, and cut ones that restate the code, narrate its history or repeat a docstring or AGENTS.md. Move duplicated or bespoke logic into shared functions and modules, and remove over-engineering the change exposes. Test bloat is debt too: when logic moves, rewrite and consolidate its tests with it, keeping what they prove rather than their shape. Keep behaviour unchanged unless the issue changes it, and let the suites prove that. A bigger PR is fine where the cleanup is reasonable to review with the change. Put the cleanup in the build brief and the review scope.

Triage every out-of-scope finding:

- Fix it in this PR when it is in or next to code you touched, small, and needs no new design decision. This is the default.
- File an issue, nested in #101 under the item it came from, only when it needs its own design or decision, belongs to another subsystem, or is a real defect that would otherwise be lost. File it only if I would plausibly prioritise it on its own.
- Otherwise note it in the PR body and move on. Small debt in files a later #101 item will touch belongs to that item.

When the work is validated, open the PR and run the headless gauntlet on it. Address its findings and every other comment on the PR, and get CI fully green. Verify the PR body's claims against the code and the run records, and fold what that finds into the PR.

Then stop and ask me to approve the merge. That approval is the one human gate in this run. Once I approve, merge if I tell you to, then close out: check off every completed issue in #101 and post the close-out records.
