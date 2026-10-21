# Delivery Rendering

## Contents

- [Outbound text preparation](#outbound-text-preparation)
- [Posted body budgets](#posted-body-budgets)
- [Report display folds](#report-display-folds)
- [Summary index and host rendering](#summary-index-and-host-rendering)

Code-owned escaping, display folds, and host budgets belong here for maintenance.
The session passes persisted artifacts to the delivery scripts without reproducing these rules.

## Outbound text preparation

Every posted text field is prepared before Markdown is assembled. Titles and corroborator
header values use the single-line class; body and suggestion use multiline prose; the cited
rule is normalized, redacted, and has backtick runs of three or more collapsed to two
before its source text is capped at 500 characters. Containment then escapes fence-shaped
lines and unsafe visible characters before blockquote markers are added. Escaping can make
the posted rule longer than 500 characters. Display
locations are quoted with a code-span delimiter longer than any path backtick run. The
caller-supplied `review_body` runs through the same prose guard before composition. A visible
ASCII `@` at text start or after a non-alphanumeric character becomes U+FF20 FULLWIDTH COMMERCIAL AT,
including inside inline code, quoted locations and a URL segment such as `/@name`.
`<` before an ASCII letter, `/`, `!` or `?` becomes `&lt;` in prose and U+FF1C
FULLWIDTH LESS-THAN SIGN inside a paired inline span or quoted location. Email address
syntax stays intact. Outside inline code, display locations and trusted fences, reference
syntax is broken: `#`, `!` and `&` before an ASCII digit become U+FF03, U+FF01 and U+FF06,
a word-initial `GH-` before a digit takes U+2011 as its hyphen, and `~` or `%` before an
ASCII letter, digit, underscore or `"` becomes U+FF5E or U+FF05. Tilde runs stay, and a
rewritten sigil loses one preceding backslash when its backslash run is odd.
A URL that starts at line start, after whitespace, or after `*`, `_`, `~`, `(`, `<`
or a link label's `[`, and every inline link destination, keeps its sigils.
A github.com or www.github.com URL shaped `/<owner>/<repo>/(issues|pull|discussions)/<digit>`,
or a URL on any host containing `/-/(issues|merge_requests|work_items|epics|milestones|snippets)/<digit>`,
becomes inline code, and as a link destination its `(` becomes U+FF08.
GitHub Enterprise issue URLs are not wrapped.
Numeric references and `&commat;`, `&excl;`, `&num;`, `&percnt;`, `&lbrack;`, `&lsqb;`,
`&rsqb;`, `&rbrack;`, `&Hat;`, `&colon;`, `&lpar;`, `&rpar;`, `&bsol;`, `&nbsp;` and
`&NonBreakingSpace;` decode as each replacement
is consumed before the next input character. Decimal and hexadecimal accumulation saturates
at 127; only ASCII values 32 through 126 are emitted. Other named references and query
ampersands stay literal. Invisible code points, including CR, are discarded before matching;
TAB and LF remain. Raw U+00A0 and its two named references become plain spaces everywhere,
including inside inline code and trusted fences, like other decoded references. Text loss
was measured on GitLab CE 19.4.1 through its Markdown API for a bracket-paren shape holding
U+00A0 next to `&`, `<` or `>`; the prepared form was measured to show all text.
The shared vectors pin the bytes. Numeric references to 160 are removed before Markdown
classification.
Comments are removed only when a `-->` begins after the end of a live `<!--` opener. An
overlapping closer in `<!-->tail` stays visible as `&lt;!-->tail`; a later non-overlapping
closer still removes the comment. Eager reduction can expose text that the previous wave order
removed: `<!--&#38;#45;&#38;#45;&#38;#62;z-->tail` becomes `z-->tail`,
`<!--x--` + U+200B + `>y-->z` becomes `y-->z`, and
`<!-<!--x-->- y --<!--z-->> w` becomes `> w`.
Secrets are redacted before containment. Inline spans pair on one line at the next exact
backtick-run length, retrying shorter opener suffixes when a complete run has no mate.
Only containment and the tilde guard share that span selection (`workflows/src/outboundText.js` for the pipeline Summary). The colon guard is
span-blind and rewrites inside inline code too.
A wrong pairing can show literal `&lt;` or backslashes, or show
fullwidth characters in prose, but leaves the text contained. Fullwidth `＜` and `＠`
inside code and display locations paste as different characters. Permalink URLs are
encoded separately and retain the original path.

Body text trusts column-zero backtick and tilde fences. Suggestion text and cited rules pass
through `prepared_prose`, which collapses backtick runs of three or more to two before fence
classification. Suggestion text therefore trusts only column-zero tilde fences; cited rules
trust no fences. Trusted body and suggestion payloads retain normalized and redacted bytes,
but accepted opener lines lose their entire info string. Finding-marker opening grammar is
broken across the whole fence, including across newlines. Exact CommonMark
closers end a trusted fence: up to three spaces of indent, the same fence character and at
least the opener length, then only spaces or tabs. Every unprotected prose line escapes each
tilde of a run of three or more outside complete paired inline-code spans. An even preceding
backslash run gains one slash; an odd run already escapes the first tilde, and the remaining
tildes each gain a slash. Fully escaped runs are fixed points. Ordinary mid-line `~~~`
therefore displays as punctuation. Other untrusted backtick fence-shaped lines have their
first fence character escaped, including lines after bullets or `[0-9]{1,9}[.)]` list
markers followed by a space or tab. Single-line fields do not track fences or escape tilde
runs. Summary titles and assembled bullets apply the prose tilde guard, preserving code-owned
location spans while escaping runs in link destinations. Cited rules trust no fences:
every backtick fence-shaped rule line has its first fence character escaped, every tilde
run outside paired inline code is escaped, and every line receives outside-fence
containment before blockquoting. An unclosed trusted body or suggestion fence gets a
synthetic closer. A terminated HTML comment inside code is removed before fence or inline
classification. Fields
containing only Unicode whitespace are absent; Markdown blank lines still use only
spaces and tabs.

GitLab (measured on CE 19.4.1) runs a quick action from a posted note when a raw line of a
top-level paragraph starts with `/` at column zero followed by a command name. The action runs
with the posting account's permissions, and GitLab deletes the command line from the stored
note even when it refuses the action. Indented lines, fences, blockquotes, lists, tables and
mid-line slashes do not run. A multiline quote opener (a line starting with `>>>`, including
alert openers such as `>>> [!note]`) can close a fenced block early and expose its lines,
committable suggestion lines included, as prose. So every prepared prose field, on both
platforms and outside trusted fences, puts a backslash before a line's leading `/` and before
a run of three or more `>` that starts a line's content after list, quote and indent prefixes.
The rules run after normalization and redaction. Suggested-fix code inside its suggestion fence
is unchanged. Single-line fields carry neither escape because they follow a code-owned prefix
or sit inside a code span. In ordinary prose, CommonMark consumes the escape backslash and
displays the punctuation as intended. An opener on an indented-code line shows its backslash.
GitLab CE 19.4.1 passes a backslash inside `$$` display math to its math renderer.
Nested-quote shorthand such as `>>> text` displays literally.

In prose, inline spans and quoted locations, the bracket after `!` or `[` (also
across a run of backslashes) becomes U+FF3B FULLWIDTH LEFT SQUARE BRACKET.
Both hosts render an image from `![`; GitLab also renders one from `[[`.
So does every `[` before a caret, also across a run of backslashes: GitHub hides text
between `[^` and a `]` on a later line of the paragraph. A regex such as `[^a-z]` shows
as `［^a-z]`.
GitLab parses text holding `](` a second time after backslashes are consumed,
so a backslash does not hold there. On an unprotected physical line holding `](` or `]\(`,
every `]` plus zero or more backslashes plus `:` everywhere on that triggered line changes
only its colon to U+FF1A FULLWIDTH COLON. Named HTML5 forms for the closing bracket, colon,
parentheses and backslash decode
before this check. The colon rule is span-blind and applies inside inline code and to location
paths because a platform can assign a backtick to another construct. A displayed path such
as `src/[x]:.py` shows U+FF1A as `src/[x]：.py`, while its permalink href is unchanged.
Without that trigger, a line-initial
reference-definition label
(`[label]:`) keeps the existing backslash-before-colon rule when only non-letter characters
precede `[` on its line. A preceding backslash or another `[` prevents the match if it remains
in the prefix when the guard runs. For `[[x]: u`, containment first turns the second bracket
fullwidth, leaving the first bracket as the label opener, so its colon is escaped. In multiline fields,
the old label may continue across lines, and it still applies inside code spans. This prevents
a definition from turning code-owned text such as `[CRITICAL]` into a link. A footnote
label has no rule of its own: its opener is already fullwidth.
Footnote definitions and reference-style links in finding text therefore show as literal text. The
new colon becomes fullwidth even when copied; the retained line-initial escape displays its
original colon.
None of these rules runs inside a trusted fenced payload. Summary titles and location paths
force the fullwidth colon rule to remain stable when the containing bullet has a link pair;
ordinary single-line fields keep
the trigger-based rule.

## Posted body budgets

- `review_body` — exactly the pipeline-rendered Summary section body: counts first, selected findings index,
  and any remainder with selection reasons. Whole index bullets fit within a 12,000-code-point
  limit; later units join the remainder. The change summary stays in the report's Change Context section. `post_review.py`
  bounds the summary comment, and every inline comment, discussion and note, with a per-platform
  UTF-8 byte budget. Each includes its trailer; GitLab discussions and notes also include a live
  marker when one is appended. Inline folds happen in place at a line boundary with a fold line
  and may keep or drop a whole committable suggestion; summary folds do not apply that suggestion
  rule. Every fold emits a stderr notice. The header and footer are reserved first; the summary
  folds only when it cannot fit beside the skipped-section frame and footer. Skipped groups appear whole or not at all in list order,
  followed by one closing count line when any group is omitted; the footer stays last.

## Report display folds

The display folds are per field, not a whole-report bound and not a return-budget mechanism. A
fold that cuts inside a code fence closes it with a fence of the same character and length before
the fold line. The display fold counts Unicode code points; the posted summary budget counts UTF-8
bytes.
Description, suggestion, cited text, cross-file references, extra fields, and corroboration
descriptions fold after 4,000 Unicode code points. The summary comment body is the report's
Summary section body: the code-owned counts sentence comes first, followed by an index of the
consolidation units selected for delivery and any remainder count with its selection reasons.
The change-summarizer prose appears only in the following Change Context section. A present summary
has a code-owned lead-in explaining its role as shared review-agent context, then folds at
`REPORT_FOLD_LIMITS.summaryChars` (`workflows/src/registry.js`). An absent or blank summary uses this exact sentence with no
lead-in:

```text
No change summary was produced for this run.
```

`post_review.py` bounds the summary comment, and every inline
comment, discussion and note, with a per-platform UTF-8 byte budget. Each includes its trailer;
GitLab discussions and notes also include a live marker when one is appended. Inline folds happen
in place at a line boundary with a fold line and may keep or drop a whole committable suggestion;
summary folds do not apply that suggestion rule. Every fold emits a stderr notice. The header and footer are reserved first; the
summary folds only when it cannot fit beside the skipped-section frame and footer. Skipped groups
appear whole or not at all in list order, followed by one closing count line when any group is
omitted; the footer stays last.
Evidence folds after 40 lines or 8,000 code points, whichever comes first. One-line display
values fold after 512 code points. Finding cardinality and whole severity buckets never fold.
Full confirmed finding values remain in `findings.json`; full unverified finding values remain
in the checkpoint's challenge entry. The report renders any severity value outside `critical`,
`high`, `medium`, or `low` as `low`, while the original value stays in `findings.json`. The
report is the sole persisted summary surface.

## Summary index and host rendering

The pipeline passes selected findings by object identity; structurally equal copies and IDs do not
establish membership. A consolidation key identifies grouped units, while each ungrouped unit uses
its original finding object. The index follows the full report's group order. Each selected
consolidation group uses its selected primary, or its first selected member in delivery order if the
primary was withheld. A partially selected group counts once in the index and zero times in the
remainder. The remainder noun follows the counts sentence: `finding(s)` when consolidation merged
nothing, and `reported issue(s)` when it did. Its line is `{K} more {noun} not listed here ({reasons}).`
The delivery selection appears only when a PR identity is present and comments can be posted; in
headless mode the `delivery` receipt must include `pr_comments`. Otherwise every reported unit is
eligible for the index. Whole bullets are admitted in index order while their code-point total,
including newlines between bullets, stays at or below 12,000. The first bullet that does not fit
and every later unit join the remainder with the reason `over the summary length limit`. An
applicable numeric cap reason reads `over the delivery cap of {cap} finding(s)`, with the noun
pluralized for the cap, because selection caps raw findings before grouping. Cap and tier reasons
consider only units omitted by delivery selection.
Unverified findings contribute to
the counts sentence but never to the index. Index units plus remainder equal the reported-unit count,
before the poster applies its byte budget. Each bullet places its location link or code span before
the optional improvement-suggestion label, then the colon and folded title, so title Markdown cannot
alter the location.
The Summary index prepares each title as a single line, folds it to 512 code points, then
contains raw HTML and visible mentions even in inline code. Its new definition-colon guard
is forced because the containing bullet has a linked location: a Summary title `[x]: u`
becomes `[x]` followed by U+FF1A FULLWIDTH COLON and `u`, while an ordinary single-line
field keeps the existing `[x]\: u` output. It counts the final bullet after preparation against
the index budget. Its display path and line sit inside one code span whose delimiter exceeds
every backtick run in the location. The colon rule is span-blind and rewrites `]` plus zero
or more backslashes plus `:` inside location paths too: `src/[x]:.py:3` displays as
`src/[x]：.py:3` with U+FF1A, so copying it no longer yields the original path.
The permalink href is unchanged. Edge spaces receive CommonMark padding. Display paths
use fullwidth `＜` and `＠` for dangerous characters, so copying them yields different
characters. The structured permalink destination percent-encodes the original valid path,
including any `<` or `@`. Normalization or redaction of the original path suppresses
the permalink.
The complete posted comment is assembled in this order: header, body, skipped section, footer.
`post_review.py` bounds the summary comment, and every inline comment, discussion and note, with a
per-platform UTF-8 byte budget. Each includes its trailer; GitLab discussions and notes also include
a live marker when one is appended. Inline folds happen in place at a line boundary with a fold
line and may keep or drop a whole committable suggestion; summary folds do not apply that suggestion
rule. Every fold emits a stderr notice. The header and footer are reserved first; the summary folds only when it cannot fit beside
the skipped-section frame and footer. Skipped groups appear whole or not at all in list order,
followed by one closing count line when any group is omitted; the footer stays last.
