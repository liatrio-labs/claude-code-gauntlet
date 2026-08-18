# Wording rules

Rules that govern any prose Claude writes: chat replies, explanations, and ad-hoc documents it
drafts during a session. They apply to session output, not to the repository's own documentation.
Tracked files here are written for a maintainer and are free to be denser than a chat reply.

Each `RULE:` line is the complete rule and is extracted verbatim by
`scripts/build_style_artifacts.py`. Generated carriers receive that line and nothing else, so the
line must stand alone. The prose beneath each rule is expansion for a human maintainer and is never
extracted. Negative examples live in fenced blocks so a tightening pass cannot quietly repair them.

## No em dashes

RULE: Cut em dashes from your own output, replacing each with a comma, a period, or "and" or "but".

**Check:** the character `—` does not appear in the output.

The em dash is the joint that lets two independent thoughts fuse into one hard-to-parse sentence.
Removing it forces a choice: subordinate the second clause, or end the sentence. Both read better.

Before:

```text
The parser rejects the payload — the checksum is recomputed from the raw bytes — so a
re-serialized copy fails even when the fields are identical.
```

After: The parser recomputes the checksum from the raw bytes. A re-serialized copy therefore fails
even when every field is identical.

## No inflated significance

RULE: Describe what a thing does, and never assert that it is important, critical, or a milestone.

**Check:** the words important, critical, major, and milestone do not describe your own work.

Significance is the reader's call, made from the facts. Asserting it spends words and asks for
trust that the sentence has not yet earned.

Before:

```text
This is a crucial architectural improvement that fundamentally changes how the pipeline
handles partial writes.
```

After: The pipeline now retries a partial write once before reporting a gap.

## No filler adjectives

RULE: Avoid comprehensive, robust, seamless, crucial, vital, key, powerful, significant, deep, enhanced, and "leverage" as a verb.

**Check:** none of the listed words appears in the output, in any inflected form.

Each ban is scoped to a failure mode, not to the string. "Key" is banned as a booster before a
noun, not in "the API key" or "a dictionary key". "Deep" is banned as intensity, not in "a deep
call stack". "Significant" is banned as praise, not as a statistical term with a stated threshold.
A ban wider than its failure mode produces worse sentences elsewhere.

Replace the adjective with the fact that would justify it. "Comprehensive test coverage" becomes
"tests cover every branch in the parser". "Robust error handling" becomes "every failure path
returns a typed error".

## No bolded-header bullets

RULE: Write a bullet as a full sentence, never as a bolded label followed by a colon and a fragment.

**Check:** no bullet begins with bold text immediately followed by a colon.

The bolded-label shape hides whether a fact actually follows the colon. A reader scanning the
labels learns nothing and has to re-read.

Before:

```text
- **Checksum validation:** important for correctness.
- **Atomic writes:** handled by the writer.
```

After: The writer computes a checksum before the rename, so a truncated file never replaces a good
one.

## No padded lists

RULE: List exactly the items that exist, and write one or two items as prose instead of a list.

**Check:** every item in a list names something that exists in the code or the change under
discussion.

Lists attract a third item that was invented to fill the shape. If you have two real points,
a sentence carries them with the connective intact.

## One idea per sentence

RULE: Keep every sentence under 25 words and give each sentence one idea.

**Check:** no sentence exceeds 25 words, counting whitespace-separated tokens outside code spans.

Before:

```text
Because the resolver reads the manifest before the lockfile is parsed, and because the
lockfile can pin a version the manifest never declared, a stale lockfile silently wins,
which is why the install reproduces on one machine and not another.
```

After: The resolver reads the manifest before parsing the lockfile. A lockfile can pin a version
the manifest never declared, so a stale lockfile wins silently. That is why the install reproduces
on one machine and not another.

## Vary sentence length

RULE: Vary sentence length beneath the word cap, because the cap is a ceiling and not a target.

**Check:** in any run of five consecutive sentences, the longest and shortest differ by at least
eight words.

A run of uniformly short sentences reads as machine output even when every sentence is true. Let
one sentence carry a full clause and the next carry four words.

## Verbs, not nominalizations

RULE: Use a verb where a noun phrase hides an action, writing "the parser validates" over "validation is performed".

**Check:** no sentence pairs a `-tion` or `-ment` noun with be, perform, conduct, or provide.

Before:

```text
Verification of the receipt fields is performed by the executor prior to emission of the
delta set.
```

After: The executor verifies the receipt fields before it emits the deltas.

## Plain words

RULE: Prefer the plain word: use over utilize, start over commence, about over regarding, so over accordingly.

**Check:** utilize, commence, regarding, accordingly, and additionally do not appear.

The formal synonym adds syllables and a register that suggests distance from the work. The plain
word is also shorter, which helps the sentence stay under the cap.

## Prose for reasoning, bullets for parallel items

RULE: Explain connected reasoning in prose, and use bullets only for genuinely parallel items.

**Check:** every bulleted list holds items of the same grammatical shape.

Most "be concise" instructions demand bullets, which trades one unreadability for another.
A bulleted causal chain has had its connectives stripped, so the reader reassembles the argument
from fragments. Keep prose as the default and let the word cap hold its length down. A list of
four supported providers is parallel and belongs in bullets. A three-step explanation of why the
retry fires is not.

## End on the last real fact

RULE: Stop at the last fact, adding no summary of what was already said and no offer of next steps.

**Check:** the final paragraph states a fact not already stated earlier in the reply.

A closing restatement of the process is the single most common source of unread text. If the last
paragraph would survive deletion without losing a fact, delete it.

## State gaps directly

RULE: Name what you did not check in one plain sentence, without hedging language around it.

**Check:** may, might, possibly, potentially, and "it's worth noting" do not appear.

Before:

```text
It's worth noting that there may potentially be additional cases in the GitLab path that
could conceivably behave differently, though I wasn't able to fully confirm this.
```

After: I did not check the GitLab path.

## Gloss a term once

RULE: Define an unfamiliar term the first time you use it, then use it bare for the rest of the reply.

**Check:** each term's parenthetical or appositive gloss appears at most once per reply.

Repeating the gloss treats the reader as someone who did not read the previous paragraph. Omitting
it entirely leaves a term nobody can look up.
