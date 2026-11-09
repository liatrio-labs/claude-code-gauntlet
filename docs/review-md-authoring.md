# Writing REVIEW.md

See the [configuration contract](../skills/code-gauntlet/references/review-md-spec.md)
for settings, defaults, and inheritance. REVIEW.md is edited by hand; review runs leave it read-only.

## Rule-authoring policy

Keep each file short and focused. Write specific, verifiable rules about the code being reviewed.
Use requirements for security and correctness, and directional guidance for design tradeoffs.
Include rationale when it clarifies a rule's applicability. Omit rules enforced by linters, type
checkers, or other deterministic tools. Put repository-wide rules in the root file and
subtree-specific rules in that directory's REVIEW.md.

## Scaffolding templates

### Root REVIEW.md template

````markdown
# Review Configuration

## Rules

## Default Delivery

<!-- Headless runs only. Options: chat, pr_comments, markdown.
     Uncomment and adjust to set a default. -->
<!-- chat,pr_comments -->

```yaml
# code-gauntlet
# Omit keys to use built-in defaults; uncomment only settings you need.
# confidence_threshold: <0-100>
# security_min_confidence: <0-100>
# severity_threshold: <critical|high|medium|low>
# ignore:
#   - <literal substring of finding text>
```
````

Interactive runs ask about posting only for open PR/MR targets. Task creation is offered separately
after delivery only in interactive runs with a persisted delivery set (`artifactPaths.postReview`
is non-null). See [Phase 8 delivery](../skills/code-gauntlet/references/phase8-delivery.md).

### Subdirectory REVIEW.md template

````markdown
# Review Configuration

## Rules

```yaml
# code-gauntlet
# confidence_threshold: <0-100>
# security_min_confidence: <0-100>
# severity_threshold: <critical|high|medium|low>
# ignore:
#   - <literal substring of finding text>
```
````
