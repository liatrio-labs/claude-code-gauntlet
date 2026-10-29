"""Patch artifact bytes, receipt progress, and repeat-run isolation."""

import json

import pytest
from gauntlet import patches

from tests.support.delivery import patch_inputs
from tests.support.path_text import normalize_path_text

QUOTE = "original_source_value()"
CAPTURE = b"--- x.py\n+++ x.py\n@@ -1 +1 @@\n-old\n+original_source_value()\n"


def patch_finding(**values):
    return {
        "file": "x.py",
        "line_start": 1,
        "line_end": 1,
        "claimed_line_start": 80,
        "claimed_line_end": 80,
        "location_status": "relocated",
        "anchor": QUOTE,
        "line": 99,
        "end_line": 100,
        "suggested_fix_code": "changed",
        **values,
    }


def test_filtered_patches_are_counted_and_disclosed(tmp_path, invoke):
    patch_inputs(
        tmp_path,
        [{"suggested_fix_code_removed_by": "injection"}, {}],
        CAPTURE,
    )
    result = invoke(
        "report_patches",
        ["--output-dir", str(tmp_path), "--head-sha", "abc1234"],
        tmp_path,
    )
    receipt = json.loads(result.stdout)
    document = (tmp_path / "code-gauntlet-patches-abc1234.md").read_text(
        encoding="utf-8"
    )
    assert (result.returncode, receipt["filtered_earlier"], receipt["candidates"]) == (
        0,
        1,
        0,
    )
    assert (
        "1 patch(es) were removed earlier by the pipeline's content filter "
        "and are not candidates here."
    ) in document


def test_patch_artifact_requires_location_and_full_quote(tmp_path, invoke, monkeypatch):
    rows = [patch_finding(location_status="unconfirmed"), patch_finding()]
    patch_inputs(tmp_path, rows, CAPTURE)
    calls = []
    original = patches.gate.evaluate_fix

    def evaluate(finding, **kwargs):
        calls.append((finding, kwargs["apply_range"]))
        return original(finding, **kwargs)

    monkeypatch.setattr(patches.gate, "evaluate_fix", evaluate)
    result = invoke(
        "report_patches",
        ["--output-dir", str(tmp_path), "--head-sha", "abc1234"],
        tmp_path,
    )
    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    assert (receipt["candidates"], receipt["kept"], receipt["downgraded"]) == (2, 1, 1)
    assert receipt["reasons"] == {"location_unconfirmed": 1}
    assert len(calls) == 1 and calls[0][1] == (1, 1)
    document = (tmp_path / "code-gauntlet-patches-abc1234.md").read_text(
        encoding="utf-8"
    )
    assert "`x.py`:1-1" in document and document.count("\nchanged\n") == 1
    assert (
        json.loads((tmp_path / "code-gauntlet-findings-abc1234.json").read_bytes())
        == rows
    )


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
        if case in ("gate-exception", "sorted-partial", "surrogate-write"):
            rows = json.loads(findings)
            findings = [
                {
                    **row,
                    "line_start": row["line"],
                    "line_end": row["end_line"],
                    "claimed_line_start": row["line"],
                    "claimed_line_end": row["end_line"],
                    "location_status": "confirmed",
                    "anchor": QUOTE,
                }
                for row in rows
            ]
            if case == "surrogate-write":
                capture = capture.replace(b"+orig\n", b"+original_source_value()\n")
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


def test_repeat_runs_identical_output_and_leave_findings_untouched(tmp_path, invoke):
    primary = tmp_path / "code-gauntlet-findings-abc1234.json"
    documents = []
    for _ in range(2):
        patch_inputs(tmp_path, [patch_finding()], CAPTURE)
        before = (primary.read_bytes(), primary.stat().st_mtime_ns)
        result = invoke(
            "report_patches",
            ["--output-dir", str(tmp_path), "--head-sha", "abc1234"],
            tmp_path,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["kept"] == 1
        documents.append((tmp_path / "code-gauntlet-patches-abc1234.md").read_bytes())
        assert (primary.read_bytes(), primary.stat().st_mtime_ns) == before
    assert documents[0] == documents[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "code-gauntlet-diff-abc1234.patch",
        "code-gauntlet-findings-abc1234.json",
        "code-gauntlet-patches-abc1234.md",
    ]
