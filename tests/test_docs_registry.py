"""Guard: the tracked documentation surface is an allowlist, not a landing zone.

Session scratch — design memos, implementation plans, handoff notes — kept landing
as tracked ``docs/*.md`` files (PR #120 removed four across two PRs). The gitignore
already quarantines the *known* scratch homes (``docs/superpowers/``,
``deep-review-*.md``, ``.superpowers/``), but an ignore list only blocks names it
anticipated; scratch written one directory up, under a fresh name, lands silently.
The durable homes for that material are the PR description and the issue thread.
This guard closes the gap from the other side: every tracked doc is a deliberate,
reviewable act — add the path to the allowlist below in the same commit, with a
reason a reviewer can weigh.

Scope: git-tracked files only (``git ls-files``), so gitignored local artifacts
never fail the suite.
"""

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Spelled-out number words for the duplication register's row-count sentence
# ("Forty rows: ..."). The table has never held fewer than thirty rows or as
# many as fifty; extend the range here if it ever does.
_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TENS = {30: "thirty", 40: "forty", 50: "fifty"}
_NUMBER_WORDS = {50: _TENS[50]}
for _tens in (30, 40):
    _NUMBER_WORDS[_tens] = _TENS[_tens]
    for _i in range(1, 10):
        _NUMBER_WORDS[_tens + _i] = f"{_TENS[_tens]}-{_ONES[_i]}"
_WORD_TO_NUMBER = {word: number for number, word in _NUMBER_WORDS.items()}

# Root-level markdown: community-health and contract files only. CHANGELOG.md is
# semantic-release-generated. Review outputs (deep-review-*.md and kin) are
# gitignored run artifacts and must never be tracked.
ROOT_MD_ALLOW = {
    "AGENTS.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "PRIVACY.md",
    "README.md",
    "REVIEW.md",
    "SECURITY.md",
}

# Files directly under docs/: living registries, required audit artifacts, and the
# maintainer standard. Entries may predate their file landing (an open PR may add
# one); the guard is subset-only in that direction on purpose.
DOCS_ALLOW = {
    "docs/duplication-register.md",  # living register, required by #55
    "docs/engineering-audit-2026-07.md",  # point-in-time audit artifact, required by #55
    "docs/machine-parsed-strings.md",  # living registry, required by #37 (PR #119)
    "docs/maintainer-issues.md",  # maintainer work-queue standard
    "docs/v3-residue-audit-2026-07.md",  # point-in-time audit artifact, required by #37 (PR #119)
    "docs/style/wording-rules.md",  # canonical output-style rule source
    "docs/style/cadence-rules.md",  # canonical output-style rule source
    "docs/style/session-context.md",  # generated session-output carrier
}

# Subtrees under docs/ with their own curated index; markdown only inside.
DOCS_ALLOWED_SUBTREES = ("docs/research/",)

POLICY = (
    "Session design/plan/handoff scratch never lands in the tree — it belongs in the "
    "PR description or issue thread. A genuinely durable doc is added to the allowlist "
    "in tests/test_docs_registry.py in the same commit, with a reason."
)


def tracked(pathspec):
    out = subprocess.run(
        ["git", "ls-files", "--", pathspec],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _individually_classified_rows_section():
    """Return ``(sentence, table_rows)`` for the duplication register's
    "## Individually classified rows" section.

    ``sentence`` is the prose line immediately preceding the table (skipping blank
    lines). ``table_rows`` is one list of stripped cells per data row — the header
    row (``| Pair | ... |``) and the separator row (``| --- | ... |``) are excluded.
    The section is bounded below by the next ``## `` heading, so a later table
    (e.g. "## Grouped patterns") is never mixed in.
    """
    text = (REPO / "docs" / "duplication-register.md").read_text()
    lines = text.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == "## Individually classified rows"
    )
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )

    sentence = None
    in_table = False
    table_rows = []
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("| Pair"):
            in_table = True
            continue
        if in_table and stripped.startswith("| ---"):
            continue
        if in_table and stripped.startswith("|"):
            table_rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
            continue
        if sentence is None and not in_table:
            sentence = stripped
    return sentence, table_rows


class TestDocsRegistry(unittest.TestCase):
    def test_every_tracked_docs_file_is_allowlisted(self):
        offenders = []
        for path in tracked("docs"):
            if path.startswith(DOCS_ALLOWED_SUBTREES):
                if not path.endswith(".md"):
                    offenders.append(f"{path} (non-markdown inside a docs subtree)")
            elif path not in DOCS_ALLOW:
                offenders.append(path)
        self.assertEqual(
            offenders,
            [],
            f"tracked under docs/ but not in the registry allowlist: {offenders}. {POLICY}",
        )

    def test_root_markdown_is_allowlisted(self):
        offenders = [
            path
            for path in tracked("*.md")
            if "/" not in path and path not in ROOT_MD_ALLOW
        ]
        self.assertEqual(
            offenders,
            [],
            f"tracked root-level markdown outside the allowlist: {offenders}. "
            f"Review-run outputs stay untracked (gitignored). {POLICY}",
        )

    def test_duplication_register_row_count_sentence_matches_table(self):
        """The sentence above the "Individually classified rows" table (e.g.
        "Forty rows: 30 `intentional-and-documented`, 10
        `intentional-but-undocumented`.") must state the table's real total and
        the real count for every classification the table actually contains — a
        row added without updating the sentence is exactly the kind of drift this
        guards against.

        Tolerant of the classification set: only the counts the sentence names are
        checked against the table, and the table's classifications must each be
        named — an unnamed classification, not a fixed vocabulary, is the failure.
        """
        sentence, table_rows = _individually_classified_rows_section()
        self.assertIsNotNone(sentence, "no row-count sentence found above the table")
        self.assertTrue(table_rows, "no data rows found in the table")

        table_counts = {}
        for cells in table_rows:
            classification = cells[1]
            table_counts[classification] = table_counts.get(classification, 0) + 1
        table_total = len(table_rows)

        word_match = re.match(r"^([A-Za-z-]+) rows:", sentence)
        self.assertIsNotNone(
            word_match, f"sentence does not start with '<Word> rows:': {sentence!r}"
        )
        word = word_match.group(1).lower()
        self.assertIn(
            word,
            _WORD_TO_NUMBER,
            f"{word!r} is not a recognized spelled-out number 30-50: {sentence!r}",
        )
        sentence_total = _WORD_TO_NUMBER[word]

        sentence_counts = {
            classification: int(count)
            for count, classification in re.findall(r"(\d+) `([\w-]+)`", sentence)
        }

        self.assertEqual(
            sentence_total,
            table_total,
            f"sentence claims {sentence_total} rows ({word!r}) but the table has "
            f"{table_total}: {sentence!r}",
        )
        self.assertEqual(
            set(table_counts),
            set(sentence_counts) & set(table_counts),
            "sentence is missing a classification present in the table: "
            f"table has {sorted(table_counts)}, sentence names {sorted(sentence_counts)}",
        )
        for classification, count in table_counts.items():
            self.assertEqual(
                sentence_counts.get(classification),
                count,
                f"sentence says {sentence_counts.get(classification)!r} "
                f"`{classification}` rows but the table has {count}",
            )


if __name__ == "__main__":
    unittest.main()
