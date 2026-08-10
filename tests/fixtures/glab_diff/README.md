# `glab mr diff` fixtures

The output shape `scripts/post_review.py::parse_diff_lines` must read on GitLab. It is
load-bearing — every inline MR comment's `position` is computed from it (issues #127
and #130) — and it is **not** git's unified diff, which is how both defects got in.

## The shape

`glab mr diff` does not shell out to git. It reads the merge request's diff versions from
the API (`GET /projects/:id/merge_requests/:iid/versions`, then the single-version
endpoint) and prints, per file:

```text
--- <old_path>
+++ <new_path>
<the API's diff body, which begins at @@>
```

Everything the parser relies on follows from that:

- **No git decoration.** No `diff --git`, `index`, `similarity index`, `rename from` or
  `rename to` lines exist. Between two hunks there is only the next file's `---`/`+++`
  pair.
- **Paths verbatim.** No synthetic `a/` / `b/` prefix, so a leading `a/` is a real
  top-level directory and stripping it addresses a path GitLab does not have.
- **No `/dev/null`.** An ADDED file repeats its path on both sides (GitLab reports
  `old_path == new_path`), so `@@ -0,0 +N,M @@` is the only added-file signal. A DELETED
  file repeats its path too, which means `current_file` stays live through its body —
  unlike the GitHub shape, where `+++ /dev/null` blanks it.
- **A RENAMED file is the one case where the two headers differ**, and that `---` path is
  what `position.old_path` needs.

`glab mr diff --raw` streams git's own diff instead and has none of these properties. The
parser is written against the plain output, and `post_review.py` records that constraint
at the invocation.

## Provenance — what is captured and what is written

**Real capture: three lines, and only three.** `added.diff` opens with the added-file
block quoted verbatim in the issue #127 report:

```text
--- src/app/clients/api/__init__.py
+++ src/app/clients/api/__init__.py
@@ -0,0 +1,16 @@
```

Everything else here — those 16 body lines, and every byte of `modified.diff`,
`deleted.diff` and `rename.diff` — is **constructed** to the shape described above, not
captured from a run. The constructed files use plainly synthetic paths (`src/edited.py`,
`src/removed.py`, `old_name.py` / `new_name.py`) so no reader mistakes one for a capture.

The shape itself is sourced, not inferred:

- glab's printer, `internal/commands/mr/diff/diff.go` in `gitlab-org/cli`: it writes
  `"--- " + OldPath`, `"+++ " + NewPath`, then the API's `Diff` body, and takes a wholly
  separate code path under `--raw`;
- the GitLab merge-request-versions API, which supplies the `old_path`, `new_path` and
  `diff` fields glab prints.

**Upgrade path.** Anyone with a GitLab account can replace these files with a real
`glab mr diff` capture from a throwaway MR covering the same four cases; that is a
provenance upgrade, not a behaviour change. If the captured bytes disagree with what is
here, the parser's contract is what moved, and these tests should be the first thing to
say so.

## Why there is no binary fixture

`glab mr diff` has no binary branch: it prints the two header lines and whatever body the
API hands it, and for a binary blob that body carries no hunk. With no `@@` the parser
never leaves its header zone, no hunk budget moves, and nothing can reach `valid_lines` —
a GitLab binary test would assert the empty set with no mutation able to falsify it. The
branch a binary file really does exercise is the between-hunk catch-all that keeps
`Binary files … differ` prose out of `valid_lines`, and that prose is git's spelling,
reaching the parser through `gh pr diff`; it is already owned by the github-platform test
in `tests/test_post_review.py`. A `binary.diff` here would add a second assertion of the
same branch and no new failure mode.

## Byte-exactness

`.pre-commit-config.yaml` excludes `*.diff` in this directory from `trailing-whitespace`
and `end-of-file-fixer`. A unified diff renders a blank context line as a lone space
(`rename.diff` has one), and a capture's final newline is part of the record; both hooks
would rewrite the recorded bytes without anyone noticing.

## Files

| fixture | case | consumed by |
| --- | --- | --- |
| `modified.diff` | modified file: context, removal, addition | `GL_DIFF_CONTRACT`, `GL_DIFF_DELETED_THEN_MODIFIED` |
| `added.diff` | added file, `@@ -0,0` as the only signal | `GL_DIFF_CONTRACT` |
| `deleted.diff` | deleted file, path repeated on both sides | `GL_DIFF_DELETED_THEN_MODIFIED` |
| `rename.diff` | renamed file, differing `---`/`+++` paths | `GL_DIFF_RENAME` |

The multi-file constants concatenate fixtures in the order shown, which is what makes the
"does this file's hunk budget drain before the next file's headers arrive?" cases
assertable at all.
