---
name: next-issue
description: Work the next open item on the issue #101 roadmap from plan to a review-ready PR, then close it out after the merge.
argument-hint: "[issue number]"
disable-model-invocation: true
---

Plan, implement and validate the next open item on the roadmap in issue #101. That is the first unchecked bullet from the top, nested bullets included. Read #101 first to get up to speed. If the item is a checkpoint run rather than an issue, stop and ask me.

Issue to work instead, if given: $ARGUMENTS

You are the orchestrator and planner; your agents do the work. Follow your usual pattern. No paired mini or other paid bench run.

When the work is validated, open the PR and run the headless gauntlet on it. Address its findings and every other comment on the PR, and get CI fully green. File each out-of-scope problem you find as an issue, nested in #101 under the item it came from.

Then stop. Merging is the one sign-off that is mine.

After I merge, close out: check off every completed issue in #101 and post the close-out records.
