"""Patch artifact bytes, receipt progress and isolation from delivery state."""

import pytest
from gauntlet import patches

from tests.support.delivery import patch_inputs
from tests.support.path_text import normalize_path_text

REPORT_CASES = {
    "multi-reason": (
        b'[{"file": "x.py", "line": 2, "end_line": 2, "title": "No-op X", "suggested_fix_code": "orig_x"}, {"file": "y.py", "line": 1, "end_line": 1, "title": "Secret Y", "suggested_fix_code": "token=ghp_'
        + b"A" * 24
        + b'"}, {"file": "z.py", "line": 1, "end_line": 1, "title": "Secret Z", "suggested_fix_code": "token=ghp_'
        + b"B" * 24
        + b'"}]',
        b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n line1\n+orig_x\ndiff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -1,1 +1,2 @@\n line1\n+orig_y\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 3, "kept": 0, "downgraded": 3, "reasons": {"no_op_replacement": 1, "redacted": 2}, "filtered_earlier": 0, "findings": 3, "warnings": ["report-patch downgraded: x.py:2 (no_op_replacement)", "report-patch downgraded: y.py:1 (redacted)", "report-patch downgraded: z.py:1 (redacted)"], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n0 of 3 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nDowngraded: 3 — reason tally: redacted (2), no_op_replacement (1)\n",
    ),
    "id-fallback": (
        b'[{"file": "a.py", "line": 2, "end_line": 2, "id": "F-42", "suggested_fix_code": "changed_a"}, {"file": "b.py", "line": 2, "end_line": 2, "suggested_fix_code": "changed_b"}]',
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n line1\n+orig_a\ndiff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,2 @@\n line1\n+orig_b\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 2, "kept": 2, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 2, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n2 of 2 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `a.py`:2-2 — F-42\n\n```py\nchanged_a\n```\n\n## `b.py`:2-2 — finding\n\n```py\nchanged_b\n```\n",
    ),
    "blank-title": (
        b'[{"file": "a.py", "line": 2, "end_line": 2, "title": "   \\n  ", "suggested_fix_code": "changed_a"}]',
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n line1\n+orig_a\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `a.py`:2-2 — finding\n\n```py\nchanged_a\n```\n",
    ),
    "surrogate-title": (
        b'[{"file": "s.py", "line": 2, "end_line": 2, "title": "oops_\\ud800_surrogate", "suggested_fix_code": "changed"}]',
        b"diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -1,1 +1,2 @@\n line1\n+orig\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `s.py`:2-2 — oops_?_surrogate\n\n```py\nchanged\n```\n",
    ),
    "bad-extension": (
        b'[{"file": "x.a```b", "line": 2, "end_line": 2, "title": "Backtick Ext", "suggested_fix_code": "changed"}]',
        b"diff --git a/x.a```b b/x.a```b\n--- a/x.a```b\n+++ b/x.a```b\n@@ -1,1 +1,2 @@\n line1\n+orig\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## ````x.a```b````:2-2 — Backtick Ext\n\n```\nchanged\n```\n",
    ),
    "comment-heading-raw-payload": (
        b'[{"file": "note.py", "line": 2, "end_line": 2, "title": "Bad <!-- title", "suggested_fix_code": "    replaced <!-- marker line"}]',
        b"diff --git a/note.py b/note.py\n--- a/note.py\n+++ b/note.py\n@@ -1,1 +1,2 @@\n line1\n+    original text\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `note.py`:2-2 — Bad &lt;!-- title\n\n```py\n    replaced <!-- marker line\n```\n",
    ),
    "filtered-earlier": (
        b'[{"file": "x.py", "line": 1, "end_line": 1, "title": "Filtered", "suggested_fix_code_removed_by": "injection_filter"}]',
        None,
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 1, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n0 of 0 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nThe pinned diff file was missing or empty, so every candidate patch failed closed (`no_diff_oracle`).\n\n1 patch(es) were removed earlier by the pipeline's content filter and are not candidates here.\n\nNo finding carried a patch this step could check.\n",
    ),
    "verbatim": (
        b'[{"file": "a/real/x.py", "line": 2, "end_line": 2, "title": "Verbatim Kept", "suggested_fix_code": "    changed"}, {"file": "real/x.py", "line": 2, "end_line": 2, "title": "Verbatim Stripped Not In Diff", "suggested_fix_code": "    other"}]',
        b"--- a/real/x.py\n+++ a/real/x.py\n@@ -1,1 +1,2 @@\n def f():\n+    original\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 2, "kept": 1, "downgraded": 1, "reasons": {"range_not_in_diff": 1}, "filtered_earlier": 0, "findings": 2, "warnings": ["report-patch downgraded: real/x.py:2 (range_not_in_diff)"], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 2 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nDowngraded: 1 — reason tally: range_not_in_diff (1)\n\n## `a/real/x.py`:2-2 — Verbatim Kept\n\n```py\n    changed\n```\n",
    ),
    "noprefix": (
        b'[{"file": "b/foo.py", "line": 2, "end_line": 2, "title": "Real Subdir No-Op", "suggested_fix_code": "CURRENT_TOP"}, {"file": "foo.py", "line": 2, "end_line": 2, "title": "Top File Kept", "suggested_fix_code": "CHANGED_TEXT"}]',
        b"diff --git b/foo.py b/foo.py\n--- b/foo.py\n+++ b/foo.py\n@@ -1,1 +1,2 @@\n line1\n+CURRENT_TOP\ndiff --git foo.py foo.py\n--- foo.py\n+++ foo.py\n@@ -1,1 +1,2 @@\n line1\n+DIFFERENT_TEXT\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 2, "kept": 1, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 2, "warnings": ["report-patch downgraded: b/foo.py:2 (no_diff_oracle)"], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 2 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nDowngraded: 1 — reason tally: no_diff_oracle (1)\n\n## `foo.py`:2-2 — Top File Kept\n\n```py\nCHANGED_TEXT\n```\n",
    ),
    "quoted-first": (
        b'[{"file": "caf\\u00e9.py", "line": 2, "end_line": 2, "title": "Quoted First File", "suggested_fix_code": "changed"}]',
        b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1,1 +1,2 @@\n line1\n+orig\n',
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `café.py`:2-2 — Quoted First File\n\n```py\nchanged\n```\n",
    ),
    "crlf-header": (
        b'[{"file": "crlf.py", "line": 2, "end_line": 2, "title": "CRLF", "suggested_fix_code": "changed content"}]',
        b"diff --git a/crlf.py b/crlf.py \xff\r\n--- a/crlf.py\r\n+++ b/crlf.py\r\n@@ -1,1 +1,2 @@\r\n line1\r\n+orig content\r\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `crlf.py`:2-2 — CRLF\n\n```py\nchanged content\n```\n",
    ),
    "empty": (
        b'[{"file": "a.py", "line": 1, "end_line": 1, "title": "A", "suggested_fix_code": "replacement"}]',
        b"",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 1, "kept": 0, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 1, "warnings": ["report-patch downgraded: a.py:1 (no_diff_oracle)"], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n0 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nDowngraded: 1 — reason tally: no_diff_oracle (1)\n\nThe pinned diff file was missing or empty, so every candidate patch failed closed (`no_diff_oracle`).\n",
    ),
    "whitespace": (
        b'[{"file": "a.py", "line": 1, "end_line": 1, "title": "A", "suggested_fix_code": "replacement"}]',
        b" \r\n\t",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 1, "kept": 0, "downgraded": 1, "reasons": {"no_diff_oracle": 1}, "filtered_earlier": 0, "findings": 1, "warnings": ["report-patch downgraded: a.py:1 (no_diff_oracle)"], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n0 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nDowngraded: 1 — reason tally: no_diff_oracle (1)\n\nThe pinned diff file was missing or empty, so every candidate patch failed closed (`no_diff_oracle`).\n",
    ),
    "replacement-findings": (
        b'[{"file":"a.py","line":1,"end_line":1,"title":"T\xffX"}]',
        None,
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n0 of 0 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nThe pinned diff file was missing or empty, so every candidate patch failed closed (`no_diff_oracle`).\n\nNo finding carried a patch this step could check.\n",
    ),
    "stamp-and-candidate": (
        b'[null, 17, {"file": "x.py", "line": 2, "end_line": 3, "title": "T", "suggested_fix_code": "    changed", "suggested_fix_code_removed_by": "filter"}]',
        b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,3 @@\n def f():\n+    line2\n+    line3\n",
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 1, "findings": 3, "warnings": [], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n1 patch(es) were removed earlier by the pipeline's content filter and are not candidates here.\n\n## `x.py`:2-3 — T\n\n```py\n    changed\n```\n",
    ),
    "tally-tie-by-name": (
        b'[{"file":"z.py","line":1,"end_line":1,"suggested_fix_code":"token=ghp_'
        + b"A" * 24
        + b'"},{"file":"a.py","line":1,"end_line":1,"suggested_fix_code":""}]',
        None,
        '{"ok": true, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 2, "kept": 0, "downgraded": 2, "reasons": {"empty": 1, "redacted": 1}, "filtered_earlier": 0, "findings": 2, "warnings": ["report-patch downgraded: z.py:1 (redacted)", "report-patch downgraded: a.py:1 (empty)"], "errors": []}\n',
        "# Apply-checked patches (against abc1234)\n\n0 of 2 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\nDowngraded: 2 — reason tally: empty (1), redacted (1)\n\nThe pinned diff file was missing or empty, so every candidate patch failed closed (`no_diff_oracle`).\n",
    ),
}


@pytest.mark.parametrize("case", REPORT_CASES)
def test_report(case, tmp_path, invoke):
    findings, capture, receipt, document = REPORT_CASES[case]
    patch_inputs(tmp_path, findings, capture)
    result = invoke(
        "report_patches",
        ["--output-dir", str(tmp_path), "--head-sha", "abc1234"],
        tmp_path,
    )
    assert result.returncode == 0
    assert normalize_path_text(result.stdout.decode(), tmp_path, "<OUT>") == receipt
    assert (
        tmp_path / "code-gauntlet-patches-abc1234.md"
    ).read_bytes() == document.encode()


RENDER_CASES = {
    "path-breakout": (
        {
            "file": "plain.py\ninjected/path.py",
            "line": 2,
            "end_line": 2,
            "title": "Multiline File",
            "suggested_fix_code": "changed",
        },
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `plain.py injected/path.py`:2-2 — Multiline File\n\n```py\nchanged\n```\n",
    ),
    "defensive-redaction": (
        {
            "file": "leak.py",
            "line": 1,
            "end_line": 1,
            "title": "Leak",
            "suggested_fix_code": "token=ghp_AAAAAAAAAAAAAAAAAAAAAAAA",
        },
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `leak.py`:1-1 — Leak\n\n```py\ntoken=[REDACTED]\n```\n",
    ),
    "trailing-blank-patch": (
        {
            "file": "blank.py",
            "line": 2,
            "end_line": 2,
            "title": "Trailing blank",
            "suggested_fix_code": "changed\n\n",
        },
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `blank.py`:2-2 — Trailing blank\n\n```py\nchanged\n\n```\n",
    ),
    "nested-ticks": (
        {
            "file": "fence.py",
            "line": 2,
            "end_line": 4,
            "title": "Fence",
            "suggested_fix_code": "```\nreplacement",
        },
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `fence.py`:2-4 — Fence\n\n````py\n```\nreplacement\n````\n",
    ),
    "x-leading-extension": (
        {
            "file": "prog.Xtend",
            "line": 2,
            "end_line": 2,
            "title": "X Ext",
            "suggested_fix_code": "changed",
        },
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `prog.Xtend`:2-2 — X Ext\n\n```Xtend\nchanged\n```\n",
    ),
    "missing-file": (
        {"line": 2, "end_line": 2, "title": "No File", "suggested_fix_code": "changed"},
        "# Apply-checked patches (against abc1234)\n\n1 of 1 suggested patch(es) passed the read-only apply-check against the pinned review diff (`code-gauntlet-diff-abc1234.patch`, captured at Phase 2, not the current working tree or branch). Platform render-site constraints are not applied here, nor is delivery's set-level overlap withholding (a fence overlapping an already-kept fence in the same file, reason `overlaps_kept_fence`), so a patch kept here may still be downgraded or withheld at delivery. This covers high-confidence findings only; unverified findings carry no patch here.\n\n## `?`:2-2 — No File\n\n```\nchanged\n```\n",
    ),
}


@pytest.mark.parametrize("case", RENDER_CASES)
def test_render(case):
    finding, document = RENDER_CASES[case]
    assert patches._render([finding], 1, 0, "ok", "abc1234", reasons={}) == document


FAILURE_CASES = {
    "missing-findings": (
        None,
        None,
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["could not read findings file <OUT>/code-gauntlet-findings-abc1234.json: [Errno 2] No such file or directory: \'<OUT>/code-gauntlet-findings-abc1234.json\'"]}\n',
    ),
    "object": (
        b'{"not":"an array"}',
        None,
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["findings file <OUT>/code-gauntlet-findings-abc1234.json must be a JSON array of findings, got dict"]}\n',
    ),
    "invalid-json": (
        b"{not valid json",
        None,
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["invalid JSON in findings file <OUT>/code-gauntlet-findings-abc1234.json: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"]}\n',
    ),
    "gate-exception": (
        b'[{"file":"x.py","line":2,"end_line":2,"suggested_fix_code":""},{"file":"x.py","line":2,"end_line":2,"suggested_fix_code":"changed"}]',
        b"--- x.py\n+++ x.py\n@@ -1 +1 @@\n-old\n+orig\n",
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 2, "kept": 0, "downgraded": 1, "reasons": {"empty": 1}, "filtered_earlier": 0, "findings": 2, "warnings": ["report-patch downgraded: x.py:2 (empty)"], "errors": ["RuntimeError: injected\\r\\ngate"]}\n',
    ),
    "sorted-partial": (
        b'[{"file":"z.py","line":1,"end_line":1,"suggested_fix_code":"token=ghp_AAAAAAAAAAAAAAAAAAAAAAAA"},{"file":"a.py","line":1,"end_line":1,"suggested_fix_code":""}]',
        None,
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "missing", "candidates": 2, "kept": 0, "downgraded": 2, "reasons": {"empty": 1, "redacted": 1}, "filtered_earlier": 0, "findings": 2, "warnings": ["report-patch downgraded: z.py:1 (redacted)", "report-patch downgraded: a.py:1 (empty)"], "errors": ["PermissionError: [Errno 13] Permission denied"]}\n',
    ),
    "surrogate-write": (
        b'[{"file":"s.py","line":2,"end_line":2,"title":"Lone Surrogate","suggested_fix_code":"text_\\ud800_here"}]',
        b"diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -1,1 +1,2 @@\n line1\n+orig\n",
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "ok", "candidates": 1, "kept": 1, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 1, "warnings": [], "errors": ["UnicodeEncodeError: \'utf-8\' codec can\'t encode character \'\\\\ud800\' in position 626: surrogates not allowed"]}\n',
    ),
}


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_failure_receipt(case, tmp_path, invoke, monkeypatch):
    findings, capture, receipt = FAILURE_CASES[case]
    if findings is not None:
        patch_inputs(tmp_path, findings, capture)

    def fail(*args, **kwargs):
        if case == "sorted-partial":
            raise PermissionError(13, "Permission denied")
        raise RuntimeError("injected\r\ngate")

    if case == "gate-exception":
        original = patches.gate.evaluate_fix

        def evaluate(finding, **kwargs):
            if finding["suggested_fix_code"]:
                return fail()
            return original(finding, **kwargs)

        monkeypatch.setattr(patches.gate, "evaluate_fix", evaluate)
    elif case == "sorted-partial":
        monkeypatch.setattr(patches, "write_atomic", fail)
    result = invoke(
        "report_patches",
        ["--output-dir", str(tmp_path), "--head-sha", "abc1234"],
        tmp_path,
    )
    assert result.returncode == 1
    assert normalize_path_text(result.stdout.decode(), tmp_path, "<OUT>") == receipt
    assert not (tmp_path / "code-gauntlet-patches-abc1234.md").exists()


CONFINEMENT_CASES = {
    "findings": (
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["findings path escapes --output-dir: <OUT>/code-gauntlet-findings-abc1234.json"]}\n',
    ),
    "diff": (
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["diff path escapes --output-dir: <OUT>/code-gauntlet-diff-abc1234.patch"]}\n',
    ),
    "out": (
        '{"ok": false, "path": "<OUT>/code-gauntlet-patches-abc1234.md", "oracle": "unattempted", "candidates": 0, "kept": 0, "downgraded": 0, "reasons": {}, "filtered_earlier": 0, "findings": 0, "warnings": [], "errors": ["out path escapes --output-dir: <OUT>/code-gauntlet-patches-abc1234.md"]}\n',
    ),
}


@pytest.mark.usefixtures("symlink_or_skip")
@pytest.mark.parametrize("case", CONFINEMENT_CASES)
def test_confinement(case, tmp_path, invoke):
    label = case
    (receipt,) = CONFINEMENT_CASES[case]
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "victim"
    outside.write_bytes(b"PRE-EXISTING CONTENT\n")
    patch_inputs(root, [])
    name = {
        "findings": "findings-abc1234.json",
        "diff": "diff-abc1234.patch",
        "out": "patches-abc1234.md",
    }[label]
    target = root / ("code-gauntlet-" + name)
    if target.exists():
        target.unlink()
    target.symlink_to(outside)
    result = invoke(
        "report_patches", ["--output-dir", str(root), "--head-sha", "abc1234"], tmp_path
    )
    assert result.returncode == 1
    assert normalize_path_text(result.stdout.decode(), root, "<OUT>") == receipt
    assert outside.read_bytes() == b"PRE-EXISTING CONTENT\n"


@pytest.mark.usefixtures("poster_state")
def test_run_isolation(tmp_path, invoke):
    primary = tmp_path / "code-gauntlet-findings-abc1234.json"
    documents = []
    for _ in range(2):
        findings, capture, expected, _ = REPORT_CASES["verbatim"]
        patch_inputs(tmp_path, findings, capture)
        before = (primary.read_bytes(), primary.stat().st_mtime_ns)
        result = invoke(
            "report_patches",
            ["--output-dir", str(tmp_path), "--head-sha", "abc1234"],
            tmp_path,
        )
        assert result.returncode == 0
        assert (
            normalize_path_text(result.stdout.decode(), tmp_path, "<OUT>") == expected
        )
        documents.append((tmp_path / "code-gauntlet-patches-abc1234.md").read_bytes())
        assert (primary.read_bytes(), primary.stat().st_mtime_ns) == before
    assert documents[0] == documents[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "code-gauntlet-diff-abc1234.patch",
        "code-gauntlet-findings-abc1234.json",
        "code-gauntlet-patches-abc1234.md",
    ]
