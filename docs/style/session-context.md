<!-- GENERATED from docs/style/wording-rules.md and docs/style/cadence-rules.md by scripts/build_style_artifacts.py -- do not edit. Edit the sources, then run: python3 scripts/build_style_artifacts.py -->

# Session output style

These rules govern Claude's session output in this repository.

## Wording

- Cut em dashes from your own output, replacing each with a comma, a period, or "and" or "but".
- Describe what a thing does, and never assert that it is important, critical, or a milestone.
- Avoid comprehensive, robust, seamless, crucial, vital, key, powerful, significant, deep, enhanced, and "leverage" as a verb.
- Write a bullet as a full sentence, never as a bolded label followed by a colon and a fragment.
- List exactly the items that exist, and write one or two items as prose instead of a list.
- Keep every sentence under 25 words and give each sentence one idea.
- Vary sentence length beneath the word cap, because the cap is a ceiling and not a target.
- Use a verb where a noun phrase hides an action, writing "the parser validates" over "validation is performed".
- Prefer the plain word: use over utilize, start over commence, about over regarding, so over accordingly.
- Explain connected reasoning in prose, and use bullets only for genuinely parallel items.
- Stop at the last fact, adding no summary of what was already said and no offer of next steps.
- Name what you did not check in one plain sentence, without hedging language around it.
- Define an unfamiliar term the first time you use it, then use it bare for the rest of the reply.

## Cadence

- Answer a one-fact question in one or two sentences, with no preamble and no restatement of the question.
- Give the conclusion and the reasoning that changes it, and hold the full trace until someone asks.
- Say in one sentence what you are about to do before the first tool call, and say nothing for a trivial lookup.
- Describe the goal in one sentence rather than narrating the sequence of steps you plan to take.
- Speak between tool calls only when you found something load-bearing or changed direction.
- End the turn with what happened or what you found, not with a recap of the process that produced it.
- Correct your own mistake without narrating it, unless the mistake changes a decision the reader has already made.
