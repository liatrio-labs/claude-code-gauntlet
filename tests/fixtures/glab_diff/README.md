# `glab mr diff` fixtures

The output shapes `scripts/gauntlet/delivery/post.py::parse_diff_text` must read on GitLab.
They are load-bearing: every inline MR comment's `position` is computed from them. Neither
shape is git's unified diff. `glab mr diff` does not shell out to git; it reads the merge
request's diff versions from the API and composes every header line itself.

## Shapes

**Git-style** is what glab 1.119.0 prints, per file:

```text
diff --git a/<old_path> b/<new_path>
<new file mode | deleted file mode | rename from / rename to, when they apply>
--- a/<old_path>          (or --- /dev/null for an added file)
+++ b/<new_path>          (or +++ /dev/null for a deleted file)
<the API's diff body, which begins at @@>
```

- **`a/` and `b/` are syntax**, so a file under a real top-level `b/` directory reads
  `+++ b/b/inner.py`.
- **`/dev/null` is the added-file and deleted-file signal.** `@@ -0,0` over a named old
  side is an edit of a file that was already empty.
- **Paths are raw.** glab does not C-quote a path and appends no TAB after one holding a
  space, so `diff --git a/old name.py b/new name.py` cannot be split into its two paths
  on its own. The parser never splits it: it rebuilds the line from the `---`/`+++` pair
  and strips the prefixes only when the two agree.

**Plain** is what older glab printed, and stays supported:

```text
--- <old_path>
+++ <new_path>
<the API's diff body, which begins at @@>
```

- **No git decoration and no prefixes.** A leading `a/` or `b/` is a real top-level
  directory, and stripping it addresses a path GitLab does not have.
- **No `/dev/null`.** An added file repeats its path on both sides, so `@@ -0,0 +N,M @@`
  is the only added-file signal. A deleted file repeats its path too.
- **A renamed file is the one case where the two headers differ**, and that `---` path is
  what `position.old_path` needs.

The plain shape is sourced from glab's printer at the time, `internal/commands/mr/diff/diff.go`
in `gitlab-org/cli` (it wrote `"--- " + OldPath`, `"+++ " + NewPath`, then the API's `Diff`
body), and from the merge-request-versions API
(`GET /projects/:id/merge_requests/:iid/versions`), which supplies `old_path`, `new_path`
and `diff`.

Neither shape puts a separator between two files, so one file's last line runs into the
next file's header unless the API's body is newline-terminated. Both captures show that it
is. The plain multi-file test constants are concatenations of single-file fixtures and
rely on the same property.

`glab mr diff --raw` streams git's own diff instead. The parser is not written against it.

## Provenance

`git_style.diff` and `git_style_spaces.diff` are byte-for-byte captures of
`glab mr diff <iid>`, taken 2026-09-30 with glab 1.119.0 against project `cdr-group/probe`
on a local GitLab CE 19.4.1. The merge requests were built through the commits API. The
first covers a real top-level `b/` directory, an added, an edited and a deleted file, and
a rename; the second covers paths holding spaces and a non-ASCII name.

The plain fixtures are constructed to the plain shape, not captured, except the three
header lines that open `added.diff`, which are quoted from a real run:

```text
--- src/app/clients/api/__init__.py
+++ src/app/clients/api/__init__.py
@@ -0,0 +1,16 @@
```

Constructed bytes are kept plainly synthetic so they cannot be mistaken for a capture:
synthetic paths (`src/edited.py`, `old_name.py`) and synthetic content (`added_01`,
`unchanged_ctx`).

## Why there is no binary fixture

`glab mr diff` has no binary branch: it prints the header lines and whatever body the API
hands it, and for a binary blob that body carries no hunk. With no `@@` the parser never
leaves its header zone and nothing can reach `valid_lines`, so a GitLab binary test would
assert the empty set with no mutation able to falsify it. The branch a binary file does
exercise is the between-hunk catch-all that keeps `Binary files … differ` out of
`valid_lines`; that prose is git's spelling, reaches the parser through `gh pr diff`, and
is owned by the github-platform test in `tests/test_post_review.py`.

## Byte-exactness

`tests/test_post_review.py::TestGlabFixtureBytes` asserts two properties the parser cannot
see: every blank line in a fixture is a lone space (a blank context line; at least one
fixture must carry one), and every fixture ends in exactly one newline.
`.pre-commit-config.yaml` excludes `*.diff` in this directory from `trailing-whitespace`
and `end-of-file-fixer` so the hooks do not rewrite those bytes; the assertions, not the
exclusion, are what report the loss.

## Files

| fixture | shape | case | consumed by |
| --- | --- | --- | --- |
| `git_style.diff` | git-style, captured | real `b/` directory, added, edited, deleted, renamed | `GL_DIFF_GIT_STYLE` |
| `git_style_spaces.diff` | git-style, captured | paths with spaces, non-ASCII name, rename | `GL_DIFF_GIT_STYLE_SPACES` |
| `modified.diff` | plain | modified file: context, removal, addition | `GL_DIFF_CONTRACT`, `GL_DIFF_DELETED_THEN_MODIFIED` |
| `added.diff` | plain | added file, `@@ -0,0` as the only signal | `GL_DIFF_CONTRACT` |
| `deleted.diff` | plain | deleted file, path repeated on both sides | `GL_DIFF_DELETED_THEN_MODIFIED` |
| `rename.diff` | plain | renamed file, differing `---`/`+++` paths | `GL_DIFF_RENAME` |
