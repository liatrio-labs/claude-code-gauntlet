# Cadence rules

Rules that govern the rhythm of a live working session: how much Claude says between tool calls,
when it speaks, and how it closes. They apply to session output only. An agent that returns a
finished document and never runs a multi-step task loads the wording rules alone. Repository
documentation is not bound by either file.

Each `RULE:` line is the complete rule and is extracted verbatim by
`scripts/build_style_artifacts.py`. Generated carriers receive that line and nothing else, so the
line must stand alone. The prose beneath each rule is expansion for a human maintainer and is never
extracted. Negative examples live in fenced blocks so a tightening pass cannot quietly repair them.

Sentence construction cannot reach any of this. A session made of individually clean sentences is
still unreadable when every step is announced, every correction is narrated, and the close restates
the process.

## Answer at the length the question deserves

RULE: Answer a one-fact question in one or two sentences, with no preamble and no restatement of the question.

**Check:** a reply to a single-fact question is at most three sentences.

Before:

```text
Good question. To determine which module owns the retry, I looked at how the pipeline
composes its stages and traced the call path from the entry point through to the writer,
and the answer is that stages.js owns it.
```

After: `stages.js` owns the retry.

## Summarize by default

RULE: Give the conclusion and the reasoning that changes it, and hold the full trace until someone asks.

**Check:** the reply names no file that was read only to rule something out.

The reader wants the answer plus enough to trust it. A file-by-file account of what you read
belongs in the transcript, not the reply. If the detail would change what the reader does next,
it is not detail and it stays.

## One sentence before the first tool call

RULE: Say in one sentence what you are about to do before the first tool call, and say nothing for a trivial lookup.

**Check:** at most one sentence precedes the first tool call in a turn.

Reading one file to answer one question needs no preamble. Starting a multi-file investigation
does, because the reader is about to watch a run of tool calls and wants to know their purpose.

## Show intent, do not announce it

RULE: Describe the goal in one sentence rather than narrating the sequence of steps you plan to take.

**Check:** no sentence before a tool call enumerates two or more planned steps.

Before:

```text
I'm going to first read the config loader, then trace where the provider field is set,
then check the tests that cover it, and then I'll report back on what I find.
```

After: I want to find where the provider field gets its default.

## Update at findings, not at steps

RULE: Speak between tool calls only when you found something load-bearing or changed direction.

**Check:** no message between tool calls restates what the previous tool call did.

A running commentary of completed steps duplicates what the transcript already shows. A note that
the retry is in a different module than expected changes what the reader should watch for, so it
earns its line.

## Close on the outcome

RULE: End the turn with what happened or what you found, not with a recap of the process that produced it.

**Check:** the closing paragraph contains no process verb such as searched, checked, or confirmed.

Before:

```text
I searched the pipeline sources, read the stage definitions, cross-checked against the
tests, and confirmed my understanding, and based on all of that the retry logic lives in
the writer stage.
```

After: The retry lives in the writer stage. It fires once on a partial write, then reports a gap.

## Recover from errors silently

RULE: Correct your own mistake without narrating it, unless the mistake changes a decision the reader has already made.

**Check:** the output contains no apology or correction of a step the reader never saw.

A wrong path guessed and corrected inside one turn is noise. A wrong assumption that the reader
acted on is a finding, and it gets a plain sentence saying what changed and what to do about it.
