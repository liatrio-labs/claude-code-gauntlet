# Skill trigger cases

These cases check whether a request activates `code-gauntlet`, independently of review completion.
They use the CLI's `prompt.md` plus `graders/*.md` layout with mechanical `tool_used: Skill` graders,
so a pre-flight stop after activation still passes; an LLM judge read that stop as a miss. Negative cases cover the description's exclusions
and REVIEW.md setup. The suite contains no review fixtures or expected findings.

Run against the baseline and the edited plugin with the same CLI and model:

```bash
claude plugin eval . --case 'trigger-*' --ablation none --no-publish --trust-plugin
```

Results belong outside version control. Evaluation requires the operator's configured model access.
