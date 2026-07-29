#!/usr/bin/env python3
"""
collect_project_rules.py — Assemble a reviewed repository's project-rule text.

Phase 2 hands the review agents a shared context file whose "project rules"
section used to be whatever the orchestrator's ``Read`` of ``CLAUDE.md``
returned. That is broken for the layout most repositories now use.

Why this script exists
----------------------
The ``Read`` tool does **not** expand Claude Code ``@path`` import directives —
import expansion belongs to the memory loader that assembles CLAUDE.md into a
session at launch, not to the generic file reader. Anthropic's own docs tell a
repository that keeps its rules in ``AGENTS.md`` to write a ``CLAUDE.md`` that
*imports* it. The result, measured at HEAD across the five benchmark mirrors:

    sentry     CLAUDE.md = "@AGENTS.md\\n"                       (11 bytes)
    grafana    CLAUDE.md = "@AGENTS.md\\n"   (identical blob)    (11 bytes)
    discourse  CLAUDE.md = "See @AI-AGENTS.md for all instructions.\\n"
    cal.com    CLAUDE.md -> AGENTS.md        (symlink; already worked)
    keycloak   neither file

So for three of five real repositories the entire project-rules section was a
pointer string, and nothing anywhere said so. A hardcoded filename list does not
fix that either: discourse points at ``AI-AGENTS.md``, a name no such list would
contain. The pointer is the mechanism, so the pointer is what gets resolved.

Doing this in a script rather than in Phase 2 prose is deliberate. The
resolution rules are arithmetic, not judgment (CLAUDE.md's issue #48 lesson),
and — decisively — the confinement rules below are a security boundary. This
script reads files from an attacker-influenceable repository and its output
flows into nine agent prompts and potentially into a posted PR comment;
SECURITY.md puts exactly that data path in scope. A boundary like that belongs
in code that a test can pin, not in an instruction a model is asked to follow.

Contract
--------
``--out`` receives the assembled markdown, written atomically. It is written on
every non-crashing path, **including when no convention files exist at all** —
an empty file means "collected, found nothing", a missing file means "this step
never ran". Phase 2 depends on that distinction: it reads ``--out``
unconditionally so a skipped collection fails the write loudly instead of
silently producing a rules-less context file.

stdout carries EXACTLY one line of JSON — the provenance receipt — on every
path. Diagnostics go to stderr. An empty stdout must stay distinguishable from
a dead process.

Skip reasons appearing in the receipt's ``skipped[]``:

    missing           named/pointed-at file does not exist
    not_regular       exists but is not a regular file
    absolute_path     pointer was absolute, home-relative, or Windows-style
    outside_repo      resolved (symlinks followed) outside the repository
    not_markdown      resolved inside the repo but is not a .md file
    too_large         exceeds the per-file byte cap
    total_cap_reached the total byte cap was already reached
    file_cap_reached  --max-files sources already collected (runaway guard)
    depth_exceeded    beyond MAX_IMPORT_DEPTH import hops
    cycle             already being visited on this import chain
    duplicate_of      same real path already contributed

Exit codes:
    0 — collection completed (including "no convention files found")
    1 — structural failure; a receipt with ok:false is still printed
    2 — usage error

Usage:
    python3 collect_project_rules.py --repo-root <path> --out <path>
                                     [--changed-files <path>]
                                     [--max-file-bytes N] [--max-total-bytes N]
                                     [--max-files N]
"""

import argparse
import json
import os
import re
import stat
import sys
import tempfile


# The one place a convention filename is added. Ordered: this order is also the
# tie-break precedence when two files at the same directory level state
# conflicting rules (see "Precedence" in references/phase2-triage.md).
#
# REVIEW.md is deliberately NOT here. It has its own structured parse path and
# its own precedence semantics (references/review-md-spec.md); collecting it as
# free rule text here would duplicate that and give one file two meanings.
PROJECT_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "QODO.md")

# Claude Code resolves at most four hops of recursive imports. Matching the real
# product's cap rather than inventing a different number keeps this script's
# view of a repository identical to the harness's.
MAX_IMPORT_DEPTH = 4

# Caps chosen from measurement, not from a round number: real AGENTS.md files in
# the benchmark mirrors run 2.7-20.1 KB, with one 71 KB monorepo outlier. These
# are tunable surface — raise them against evidence, not taste.
DEFAULT_MAX_FILE_BYTES = 65536
DEFAULT_MAX_TOTAL_BYTES = 131072

# Bounds the WALK, not just the bytes it collects. The byte caps alone do not:
# an empty file contributes zero bytes, so a repo full of empty .md files (or an
# import graph that fans out across them) would be walked without limit while
# `total_bytes` never moves. Precedent: contextReadPlan refuses above its chunk
# ceiling *before* the first allocation, because an unbounded value once
# OOM-killed the node process — bound the plan, not only the input.
#
# This is a RUNAWAY GUARD, not a policy cap, and the number is chosen to make
# that unambiguous: real repos measured at HEAD carry 8 (sentry) and 10
# (grafana) rule files, so 512 is ~50x the observed need. A cap low enough to
# bind in a legitimate monorepo would silently drop real rules — the failure
# this script exists to end — so if this ever fires on a real repository, raise
# it rather than accept the truncation. Tunable via --max-files, for symmetry
# with the two byte caps.
DEFAULT_MAX_FILES = 512

# A ``@`` that begins a path token: at the start of a line or after whitespace,
# so an email address or a decorator mid-word is not mistaken for an import.
_IMPORT_RE = re.compile(r"(?:^|(?<=\s))@(\S+)")

_FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

# Trailing sentence punctuation is not part of the path. Stripped because the
# docs' own example is an inline mid-sentence import, e.g.
# "See @AI-AGENTS.md for all instructions." — ".md" survives this, "md." does not.
_TRAILING_PUNCT = ".,;:!?)]}>\"'"

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _strip_code(text):
    """Return *text* with fenced blocks and inline code spans blanked out.

    Claude Code skips imports inside code spans and fenced blocks — wrapping a
    path in backticks is the documented way to mention it without importing it.
    Blanking rather than deleting keeps this cheap and order-independent; only
    import scanning consumes the result.
    """
    out = []
    fence = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if match:
            marker = match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            out.append("")
            continue
        out.append("" if fence is not None else _INLINE_CODE_RE.sub("", line))
    return "\n".join(out)


def _find_imports(text):
    """Return the ``@path`` tokens in *text* that look like a file reference.

    A rules file is prose, and prose is full of at-signs that are not
    imports — discourse's real AI-AGENTS.md says "Specify the ``@type``.",
    and JSDoc/Java/CSS conventions add ``@param``, ``@Override``, ``@media``.
    Those are bare words with no ``.`` in them at all, so they are filtered
    here: treating every one as a refused pointer is safe but not harmless,
    since each refusal becomes a line in the triage announcement, and a
    disclosure channel that cries wolf on every run is one people learn to
    ignore.

    A token that DOES contain a ``.`` is always passed through, even when its
    extension is not ``.md``. `@.env` and `@secrets.yml` must still reach
    `_resolve_pointer` and be refused — and DISCLOSED — as `not_markdown`;
    filtering them out HERE instead would drop them silently, with no skip
    entry and no gap, which is exactly the silent degradation this script's
    disclosure contract exists to prevent. The `.md` extension is enforced by
    `_resolve_pointer`, never by this prefilter.
    """
    seen = set()
    found = []
    for raw in _IMPORT_RE.findall(_strip_code(text)):
        token = raw.rstrip(_TRAILING_PUNCT)
        if "." not in token:
            continue
        if token not in seen:
            seen.add(token)
            found.append(token)
    return found


def _within(path, root):
    """True when *path* is *root* or lives beneath it.

    Both arguments must already be realpath-resolved. The trailing-separator
    check is the point: a bare ``startswith`` would accept ``/tmp/repo-evil``
    for a root of ``/tmp/repo``.
    """
    return path == root or path.startswith(root + os.sep)


class _Collector(object):
    def __init__(self, repo_root, max_file_bytes, max_total_bytes, max_files):
        self.repo_root = os.path.realpath(repo_root)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self.sources = []
        self.skipped = []
        self.total_bytes = 0
        self.truncated = False
        self.included = set()

    def _skip(self, path, reason, detail=None):
        entry = {"path": self._display(path), "reason": reason}
        if detail:
            entry["detail"] = detail
        self.skipped.append(entry)

    def _display(self, path):
        """Repo-relative when possible; never leak an absolute host path."""
        try:
            real = os.path.realpath(path)
            if _within(real, self.repo_root):
                return os.path.relpath(real, self.repo_root)
        except OSError:
            pass
        return os.path.basename(str(path))

    def _resolve_pointer(self, raw, containing_dir):
        """Resolve one ``@path`` token. Returns (realpath, None) or (None, reason).

        Order matters. Absolute and home-relative tokens are rejected *before*
        any join: ``os.path.join(base, "/etc/passwd")`` silently discards the
        base and returns ``/etc/passwd``, a failure with no exception to catch.
        Native Claude Code asks a human to approve an import that resolves
        outside the working directory; this script cannot prompt, so it refuses.
        """
        if raw.startswith("~") or raw.startswith("\\") or "\\" in raw:
            return None, "absolute_path"
        if os.path.isabs(raw) or _WINDOWS_DRIVE_RE.match(raw):
            return None, "absolute_path"

        # Relative paths resolve against the directory of the file containing
        # the import — never cwd, never the repo root.
        real = os.path.realpath(os.path.join(containing_dir, raw))

        if not _within(real, self.repo_root):
            return None, "outside_repo"
        # Confinement proves the target is inside the repo, not that it is a
        # rules file. Reading a named CLAUDE.md can only ever open one known
        # filename per directory; pointer indirection is what first makes an
        # *attacker-chosen* filename reachable, so `@.env` and `@secrets.yml`
        # need their own control. Extension-based keeps this general — it
        # accepts discourse's AI-AGENTS.md without guessing filenames.
        if not real.lower().endswith(".md"):
            return None, "not_markdown"
        return real, None

    def _read(self, real):
        """Read *real* if every bound allows it. Returns (text, None) or (None, reason).

        Every bound is checked against ``stat`` *before* ``open``: reading a
        file and then discarding it for being too big still pays the read. The
        file-count bound comes first, before even the ``stat``.
        """
        if len(self.sources) >= self.max_files:
            self.truncated = True
            return None, "file_cap_reached"
        try:
            st = os.stat(real)
        except OSError:
            return None, "missing"
        if not stat.S_ISREG(st.st_mode):
            return None, "not_regular"
        if st.st_size > self.max_file_bytes:
            return None, "too_large"
        if self.total_bytes + st.st_size > self.max_total_bytes:
            self.truncated = True
            return None, "total_cap_reached"
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return None, "missing"
        return text, None

    def visit(self, real, via, depth, chain):
        """Include *real*, then follow its imports depth-first."""
        if real in chain:
            self._skip(real, "cycle")
            return
        if real in self.included:
            self._skip(real, "duplicate_of")
            return

        text, reason = self._read(real)
        if text is None:
            self._skip(real, reason or "missing")
            return

        size = len(text.encode("utf-8"))
        self.included.add(real)
        self.total_bytes += size
        self.sources.append(
            {"path": self._display(real), "bytes": size, "via": via, "text": text}
        )

        containing_dir = os.path.dirname(real)
        for raw in _find_imports(text):
            if depth + 1 > MAX_IMPORT_DEPTH:
                self._skip(os.path.join(containing_dir, raw), "depth_exceeded")
                continue
            target, reason = self._resolve_pointer(raw, containing_dir)
            if reason is not None:
                self._skip(os.path.join(containing_dir, raw), reason)
                continue
            self.visit(target, "import:" + self._display(real), depth + 1,
                       chain + (real,))


def _search_dirs(repo_root, changed_files):
    """Repo root, plus every directory holding a changed file, plus ancestors.

    Deterministic: root first, then the rest sorted. Ancestors are included
    because a rules file at ``pkg/`` governs ``pkg/storage/unified/`` too.
    """
    root = os.path.realpath(repo_root)
    dirs = [root]
    seen = {root}
    extra = set()
    for rel in changed_files or []:
        current = os.path.realpath(os.path.join(root, os.path.dirname(rel)))
        while _within(current, root) and current not in seen:
            extra.add(current)
            seen.add(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    dirs.extend(sorted(extra))
    return dirs


def _load_changed_files(path):
    """Read the changed-file list. Accepts strings or objects with a 'path'."""
    if not path:
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            value = item.get("path") or item.get("file")
            if isinstance(value, str):
                out.append(value)
    return out


def render(sources):
    """Assemble the markdown block. ``###`` nests under the context file's heading."""
    if not sources:
        return ""
    parts = []
    for entry in sources:
        parts.append("### %s\n" % entry["path"])
        text = entry["text"]
        parts.append(text if text.endswith("\n") else text + "\n")
        parts.append("\n")
    return "".join(parts).rstrip("\n") + "\n"


def write_text_atomic(path, text):
    """Write via a temp file in the same directory, then rename (mirrors
    assemble_artifacts.py's write_text_atomic). Opening the destination
    directly would truncate it before the encode, leaving a zero-byte file at
    a planned path on any failure; os.replace() is atomic within a
    filesystem, so the destination is always either its old content or the
    complete new content, never a prefix."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".rules-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            out.write(text)
        # mkstemp creates the temp file 0600; os.replace would carry that mode
        # onto the artifact, which a later step running as another user could
        # no longer read. Restore the mode a plain open() would have produced.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _gaps(collector):
    """Human-readable disclosure lines. Silence about a refusal is the failure
    mode this codebase's degrade-and-disclose contract exists to prevent —
    every skip reason gets a gap line here except ``duplicate_of``, which is
    correct dedup (cal.com's symlinked CLAUDE.md reaching the same real file
    twice), not a degradation."""
    gaps = []
    security = [s for s in collector.skipped
                if s["reason"] in ("outside_repo", "absolute_path", "not_markdown")]
    for entry in security:
        gaps.append(
            "project_rules_refused: %s (%s) — pointer refused; it is not a "
            "markdown file inside the repository" % (entry["path"], entry["reason"])
        )
    for entry in collector.skipped:
        if entry["reason"] in ("too_large", "total_cap_reached",
                               "file_cap_reached", "depth_exceeded"):
            gaps.append("project_rules_truncated: %s (%s) — its rules are NOT in "
                        "the review context" % (entry["path"], entry["reason"]))
    for entry in collector.skipped:
        if entry["reason"] in ("missing", "cycle", "not_regular"):
            gaps.append(
                "project_rules_unresolved: %s (%s) — this pointer did not resolve "
                "to rule content" % (entry["path"], entry["reason"])
            )
    if not collector.sources:
        gaps.append("project_rules_absent: no CLAUDE.md/AGENTS.md/QODO.md found; "
                    "agents receive no project rules for this repository")
    return gaps


def _emit(receipt):
    """EXACTLY one line of JSON on stdout, on every path."""
    try:
        line = json.dumps(receipt)
    except (TypeError, ValueError):
        line = json.dumps({"ok": False, "sources": [], "skipped": [],
                           "total_bytes": 0, "truncated": False,
                           "out": receipt.get("out") if isinstance(receipt, dict) else None,
                           "gaps": ["project_rules_receipt_unserializable"]})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Collect a repository's project-rule text, resolving @imports.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--changed-files")
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    args = parser.parse_args(argv)

    collector = None
    try:
        if not os.path.isdir(args.repo_root):
            _emit({"ok": False, "sources": [], "skipped": [], "total_bytes": 0,
                   "truncated": False, "out": args.out,
                   "gaps": ["project_rules_failed: --repo-root is not a directory"]})
            return 1

        collector = _Collector(args.repo_root, args.max_file_bytes,
                               args.max_total_bytes, args.max_files)
        changed = _load_changed_files(args.changed_files)

        for directory in _search_dirs(args.repo_root, changed):
            for name in PROJECT_RULE_FILENAMES:
                candidate = os.path.join(directory, name)
                if not os.path.lexists(candidate):
                    continue
                # Confinement applies to first-class sources too, not just to
                # pointers: cal.com's CLAUDE.md is itself a symlink, so the
                # mechanism an attacker would use is live in real repositories.
                real = os.path.realpath(candidate)
                if not _within(real, collector.repo_root):
                    collector._skip(candidate, "outside_repo")
                    continue
                collector.visit(real, "direct", 0, ())

        # Written even when empty. A missing file means the step never ran.
        write_text_atomic(args.out, render(collector.sources))

        _emit({
            "ok": True,
            "sources": [{k: v for k, v in s.items() if k != "text"}
                        for s in collector.sources],
            "skipped": collector.skipped,
            "total_bytes": collector.total_bytes,
            "truncated": collector.truncated,
            "out": args.out,
            "gaps": _gaps(collector),
        })
        return 0
    except Exception as exc:  # noqa: BLE001 — a receipt on every path
        sys.stderr.write("collect_project_rules: %s\n" % exc)
        try:
            write_text_atomic(args.out, render(collector.sources) if collector else "")
        except Exception:  # noqa: BLE001
            pass
        _emit({"ok": False,
               "sources": [], "skipped": collector.skipped if collector else [],
               "total_bytes": 0, "truncated": False, "out": args.out,
               "gaps": ["project_rules_failed: %s" % exc]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
