"""The product identity surface: one declaration, generated mirrors (issue #36).

`workflows/src/registry.js` is the ONE hand-authored copy of the brand mark, the
display name and the severity emoji map. Every other copy — the Python constants in
`scripts/post_review.py`, the legends in `references/report-format.md` and
`references/delivery-guide.md`, the chat-identity sentence in `SKILL.md` — is emitted
from it by `scripts/generate_contract_requirements.py` into a hand-placed
`generated-from-registry-identity` marker fence.

Freshness is already gated (`tests/test_dimensions_registry.py`'s `--check` run). What
these tests add is the thing `--check` cannot see: that the mirrors carry the SAME
CHARACTERS the registry declares. `--check` compares a file against the generator's own
output, so it stays green for any mark; a wrong-but-consistent mark, or a
registry.js edit shipped without a regenerate, is what turns these red.

Every mark literal here is written as its codepoint escape, never as a pasted glyph —
a glyph survives a copy-paste through an editor that normalises or drops the U+FE0F
variation selector, and the test would then pin the wrong bytes without anyone seeing it.
"""

import os
import subprocess
import sys
import unittest
from typing import ClassVar

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import apply_challenges, post_review
from scripts import generate_contract_requirements as gen
from tests import test_machine_parsed_strings as registry_doc

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CROSSED SWORDS U+2694 + VARIATION SELECTOR-16 U+FE0F. Two codepoints, both BMP.
BRAND_MARK = "\u2694\ufe0f"
# WARNING SIGN U+26A0 + VS16 — the one other VS16-carrying mark this repo renders
# (post_review's invalid-position warning). Disjointness from it is what keeps the
# mark's machine-parsed row a real invariant instead of an ambiguous substring.
WARNING_SIGN = "\u26a0\ufe0f"


def _tracked_files():
    """Every git-tracked path — the same scope docs/test registries use."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _read_text(rel_path):
    """The file's text, or None when it is not UTF-8 text (binary assets)."""
    try:
        with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as handle:
            return handle.read()
    except (UnicodeDecodeError, OSError):
        return None


def _codepoints(text):
    """A tuple of codepoint ordinals — the comparison that survives any glyph rendering."""
    return tuple(ord(char) for char in text)


class TestIdentitySurface(unittest.TestCase):
    IDENTITY_OWNING_SECTIONS: ClassVar[dict[tuple[str, str], tuple[str, str]]] = {
        (
            "skills/code-gauntlet/references/report-format.md",
            "summary_header",
        ): ("## PR Comment Format (abbreviated)", "## Inline PR Comment Format"),
        (
            "skills/code-gauntlet/references/report-format.md",
            "inline_trailer",
        ): ("## Inline PR Comment Format", "**`suggested_fix_code` field:**"),
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "summary_header",
        ): ("**Script behavior:**", "### Findings metadata footer"),
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "inline_trailer",
        ): ("### Comment body format", "**Script behavior:**"),
        (
            "skills/code-gauntlet/references/delivery-guide.md",
            "delivery_identity",
        ): ("**Script behavior:**", "### Findings metadata footer"),
        ("skills/code-gauntlet/SKILL.md", "chat_identity"): (
            "### Deliver",
            "### Print methodology",
        ),
    }

    @classmethod
    def setUpClass(cls):
        cls.registry = gen.load_registry(REPO_ROOT)

    def test_python_mirror_decodes_to_the_registry_identity(self):
        """T-MIRROR: post_review's constants ARE registry.js's, character for character.

        registry.js declares the mark as an escape and the mirror carries the literal
        bytes, so the two files never share a byte sequence — decoded codepoints are the
        only honest comparison, and they are what a `--check`-clean tree can still get
        wrong (edit registry.js, forget to regenerate).
        """
        identity = self.registry
        self.assertEqual(
            _codepoints(post_review.BRAND_MARK),
            _codepoints(identity["brand"]["mark"]),
        )
        self.assertEqual(post_review.BRAND_NAME, identity["brand"]["name"])
        self.assertEqual(
            {k: _codepoints(v) for k, v in post_review.SEVERITY_EMOJI.items()},
            {k: _codepoints(v) for k, v in identity["severityEmoji"].items()},
        )
        self.assertEqual(
            _codepoints(post_review.SEVERITY_EMOJI_FALLBACK),
            _codepoints(identity["severityEmojiFallback"]),
        )

    def test_the_mark_is_the_pinned_codepoint_sequence(self):
        """T-GLYPH: the mark is U+2694 U+FE0F and collides with nothing else rendered."""
        self.assertEqual(_codepoints(post_review.BRAND_MARK), _codepoints(BRAND_MARK))
        for severity, emoji in post_review.SEVERITY_EMOJI.items():
            with self.subTest(severity=severity):
                self.assertNotEqual(
                    _codepoints(emoji), _codepoints(post_review.BRAND_MARK)
                )
        self.assertNotEqual(
            _codepoints(post_review.BRAND_MARK), _codepoints(WARNING_SIGN)
        )

    def test_the_declared_fences_are_exactly_the_fences_in_the_tree(self):
        """T-DISCOVERY: IDENTITY_FENCES equals what a whole-tree scan finds.

        The dict is the only thing that makes a file a generator target and the only
        thing T-DOCFENCE iterates, so dropping one entry un-generates that mirror and
        deletes its own coverage in the same edit — leaving marker debris whose "do
        not edit; run ..." hint has quietly become false, with nothing red. Rebuilding
        the mapping from the tree and comparing both directions is what makes a
        dropped declaration (and an undeclared fence) unrepresentable.
        """
        discovered = {}
        for rel_path in _tracked_files():
            text = _read_text(rel_path)
            if text is None:
                continue
            for line in text.split("\n"):
                match = gen._IDENTITY_MARKER_RE.match(line)
                if match:
                    discovered.setdefault(rel_path, set()).add(match.group("symbol"))
        self.assertEqual(
            {rel: set(symbols) for rel, symbols in discovered.items()},
            {rel: set(symbols) for rel, symbols in gen.IDENTITY_FENCES.items()},
        )

    def test_identity_fences_stay_under_their_owning_headings(self):
        """T-PLACEMENT: section-scoped identity prose cannot move to another section."""
        for (rel_path, symbol), (start, end) in self.IDENTITY_OWNING_SECTIONS.items():
            text = _read_text(rel_path)
            open_line, _ = gen.identity_marker_lines(symbol, rel_path)
            with self.subTest(path=rel_path, symbol=symbol):
                self.assertLess(
                    text.index(start),
                    text.index(open_line),
                    "identity fence moved above its owning section",
                )
                self.assertLess(
                    text.index(open_line),
                    text.index(end),
                    "identity fence moved below its owning section",
                )

    def test_the_severity_map_is_the_repo_severity_order(self):
        """Key order is the render order of every generated legend, and the key SET
        decides which severities render their own emoji instead of the fallback bulb.
        Both ride on an object literal's undeclared order unless they are pinned to
        the list that already owns severity ordering repo-wide
        (`scripts/apply_challenges.py`'s SEVERITY_ORDER, twin of
        `workflows/src/filterFindings.js`'s).
        """
        self.assertEqual(
            list(post_review.SEVERITY_EMOJI), apply_challenges.SEVERITY_ORDER
        )

    def test_the_declaring_sources_carry_no_literal_mark_bytes(self):
        """docs/machine-parsed-strings.md lists four producers of the mark and
        deliberately omits registry.js and the bundle, because both declare it as
        escapes and contain none of its literal bytes. A pasted glyph in either would
        turn that row into the phantom the registry forbids, so the absence is
        asserted rather than left to the row's own prose note.

        The needle is the registry's OWN mark, not a pinned codepoint: a hard-coded
        U+2694 would go silently vacuous the first time the mark changes, which is the
        one edit this whole mechanism exists to make cheap. T-GLYPH owns the codepoint.
        """
        mark = self.registry["brand"]["mark"]
        for rel_path in ("workflows/src/registry.js", "workflows/pipeline.js"):
            with self.subTest(path=rel_path):
                self.assertNotIn(mark[0], _read_text(rel_path))

    def test_the_mark_row_lists_exactly_the_generated_producers(self):
        """docs/machine-parsed-strings.md's producer list is a consequence of the dict.

        `IDENTITY_FENCES` is what mechanically decides which files the generator writes
        the mark into; the row's own notes state that relationship as prose. The
        presence check at tests/test_machine_parsed_strings.py only runs
        listed -> contains, so a fifth generated mirror leaves the row under-listing
        with every suite green.
        """
        rows = registry_doc.parse_registry(
            registry_doc.REGISTRY.read_text(encoding="utf-8")
        )
        marked = [
            r for r in rows if _codepoints(r["string"]) == _codepoints(BRAND_MARK)
        ]
        self.assertEqual(len(marked), 1, "expected exactly one brand-mark row")
        self.assertEqual(set(marked[0]["producers"]), set(gen.IDENTITY_FENCES))

    def test_the_composed_constants_are_the_generated_identity(self):
        """BRAND_TRAILER / BRAND_SUMMARY_HEADER are hand-composed below the fence.

        The header is a second spelling of the `summary_header` fence body, so it is
        asserted EQUAL to that body rather than to a copy of it — one invariant, not
        two literals that can drift the moment S2 posts them. The trailer has no
        generated twin, so its bytes are pinned outright.
        """
        self.assertEqual(
            post_review.BRAND_SUMMARY_HEADER,
            "\n".join(
                gen.identity_body(
                    "skills/code-gauntlet/references/delivery-guide.md",
                    "summary_header",
                    self.registry,
                )
            ),
        )
        self.assertEqual(
            _codepoints(post_review.BRAND_TRAILER),
            _codepoints("\u2694\ufe0f *Code Gauntlet*"),
        )

    def test_the_doc_legends_are_the_generated_bytes(self):
        """T-DOCFENCE: every markdown mirror carries the generator's exact fence.

        Markers included, so a hand-edit inside a fence — or a fence relocated without
        its markers — is a substring miss here, not just a `--check` diff.
        """
        for rel_path, symbols in gen.IDENTITY_FENCES.items():
            if not rel_path.endswith(".md"):
                continue
            with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as handle:
                text = handle.read()
            for symbol in symbols:
                with self.subTest(path=rel_path, symbol=symbol):
                    open_line, close_line = gen.identity_marker_lines(symbol, rel_path)
                    body = gen.identity_body(rel_path, symbol, self.registry)
                    self.assertIn("\n".join([open_line, *body, close_line]), text)


if __name__ == "__main__":
    unittest.main()
