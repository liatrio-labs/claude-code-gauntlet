"""Bounded rule collection and the disclosure consumed by review callers."""

import builtins
import json
import os
from pathlib import Path

import pytest
from gauntlet.project_rules import _find_imports

from tests.support.phase2 import normalize_frame_ids

EMPTY_RULES_NOTICE = (
    "project rules: none collected (REVIEW.md, CLAUDE.md, AGENTS.md, QODO.md)\n"
)


@pytest.fixture
def rule_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return str(root)


@pytest.fixture
def rule_output(tmp_path):
    return str(tmp_path / "rules.md")


@pytest.fixture
def write_rule(rule_root):
    def write(relative, content, root=None):
        path = Path(root or rule_root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        return str(path)

    return write


@pytest.fixture
def opened_paths(monkeypatch):
    original = builtins.open
    opened = []

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    return opened


@pytest.fixture
def collect_rules(invoke, rule_root, rule_output, tmp_path):
    def collect(*extra, repo=None):
        result = invoke(
            "collect_project_rules",
            ["--repo-root", repo or rule_root, "--out", rule_output, *extra],
            tmp_path,
        )
        assert len(result.stdout.splitlines()) == 1
        receipt = json.loads(result.stdout)
        output = Path(rule_output)
        body = output.read_bytes().decode("utf-8") if output.exists() else ""
        return result.returncode, receipt, body

    return collect


def reasons(receipt):
    return sorted({entry["reason"] for entry in receipt["skipped"]})


def source_paths(receipt):
    return [source["path"] for source in receipt["sources"]]


def test_pointer_in_fenced_block_or_code_span_is_not_followed(
    collect_rules, write_rule
):
    # Convention filenames would mask a broken import parser via direct discovery.
    write_rule("CLAUDE.md", "Mention `@NOTES.md` inline.\n\n```\n@NOTES.md\n```\n")
    write_rule("NOTES.md", "RULE-DELTA: never reached.\n")
    _, receipt, body = collect_rules()
    assert "RULE-DELTA" not in body
    assert "NOTES.md" not in source_paths(receipt)


def test_missing_pointer_target_is_disclosed_and_the_run_still_succeeds(
    collect_rules, write_rule
):
    # Callers fold gaps into the report; a skip alone does not disclose missing rules.
    write_rule("CLAUDE.md", "@NOPE.md\n")
    code, receipt, _ = collect_rules()
    assert code == 0
    assert receipt["ok"]
    assert "missing" in reasons(receipt)
    assert any("NOPE.md" in g for g in receipt["gaps"])


@pytest.mark.parametrize(
    ("pointer", "path", "reason"),
    [
        ("~/secret.md", "~/secret.md", "absolute_path"),
        ("../outside.md", "outside.md", "outside_repo"),
        ("payload.txt", "payload.txt", "not_markdown"),
    ],
    ids=["absolute", "outside", "extension"],
)
def test_pointer_refusals(pointer, path, reason, collect_rules, write_rule, tmp_path):
    write_rule("outside.md", "OUTSIDE-CANARY\n", root=tmp_path)
    write_rule("payload.txt", "SECRET-CANARY\n")
    write_rule("CLAUDE.md", f"@{pointer}\n")
    code, receipt, body = collect_rules()
    assert code == 0
    assert receipt["skipped"] == [{"path": path, "reason": reason}]
    assert receipt["gaps"] == [
        f"project_rules_refused: {path} ({reason}) \u2014 "
        "pointer refused; it is not a markdown file inside the repository"
    ]
    assert "CANARY" not in body
    assert '<project-rules path="CLAUDE.md"' in body


@pytest.mark.usefixtures("symlink_or_skip")
def test_md_named_first_class_symlink_to_an_in_repo_secret_is_refused(
    collect_rules, rule_root, write_rule
):
    write_rule("payload.txt", "AWS_SECRET=SYMLINK-CANARY\n")
    os.symlink("payload.txt", os.path.join(rule_root, "CLAUDE.md"))
    _, receipt, body = collect_rules()
    assert "SYMLINK-CANARY" not in body
    assert "not_markdown" in reasons(receipt)
    assert any("project_rules_refused" in g for g in receipt["gaps"])


def test_a_free_duplicate_does_not_trip_the_total_cap(collect_rules, write_rule):
    # Charging identical content would falsely disclose missing rules.
    body = "r" * 400
    write_rule("CLAUDE.md", body)
    write_rule("AGENTS.md", body)
    _, receipt, _ = collect_rules("--max-total-bytes", "500")
    assert not receipt["truncated"]
    assert "total_cap_reached" not in reasons(receipt)
    assert "duplicate_of" in reasons(receipt)
    assert receipt["gaps"] == []
    assert len(receipt["sources"]) == 1


def test_import_depth_cap_matches_the_real_product_and_is_disclosed(
    collect_rules, write_rule
):
    write_rule("CLAUDE.md", "@d1.md\n")
    for i in range(1, 4 + 1):
        write_rule(f"d{i}.md", f"RULE-D{i}\n@d{i + 1}.md\n")
    write_rule(f"d{4 + 1}.md", "RULE-TOO-DEEP\n")
    _, receipt, body = collect_rules()
    assert f"RULE-D{4}" in body
    assert "RULE-TOO-DEEP" not in body
    assert "depth_exceeded" in reasons(receipt)


def test_import_cycle_terminates_and_is_disclosed(collect_rules, write_rule):
    write_rule("CLAUDE.md", "@a.md\n")
    write_rule("a.md", "RULE-A\n@b.md\n")
    write_rule("b.md", "RULE-B\n@a.md\n")
    code, receipt, body = collect_rules()
    assert code == 0
    assert "RULE-A" in body
    assert "RULE-B" in body
    assert {"cycle", "duplicate_of"} & set(reasons(receipt))
    assert any("cycle" in g for g in receipt["gaps"])


def test_all_declared_source_filenames_are_collected(collect_rules, write_rule):
    for name in ("CLAUDE.md", "AGENTS.md", "QODO.md"):
        write_rule(name, f"RULE-FROM-{name.replace('.md', '')}\n")
    _, _, body = collect_rules()
    for name in ("CLAUDE.md", "AGENTS.md", "QODO.md"):
        assert f"RULE-FROM-{name.replace('.md', '')}" in body


def test_windows_style_changed_entry_discovers_nested_rules(collect_rules, write_rule):
    write_rule("pkg/sub/AGENTS.md", "NESTED-RULE\n")
    changed = write_rule("../changed.json", json.dumps(["pkg\\sub\\x.py"]))
    _, receipt, body = collect_rules("--changed-files", changed)
    assert receipt["ok"]
    assert "NESTED-RULE" in body


def test_import_target_change_does_not_propagate_from_or_to_importer(
    collect_rules, write_rule
):
    write_rule("CLAUDE.md", "See @rules.md here.\n")
    write_rule("rules.md", "TARGET-RULE\n")
    target_changed = write_rule("../target-changed.json", json.dumps(["rules.md"]))
    _, target_receipt, target_body = collect_rules("--changed-files", target_changed)
    target_attributes = {
        source["path"]: source["modified_in_diff"]
        for source in target_receipt["sources"]
    }
    assert target_attributes == {"CLAUDE.md": False, "rules.md": True}
    assert (
        '<project-rules path="rules.md" modified-in-this-diff="true" id="ID">'
        in normalize_frame_ids(target_body)
    )
    importer_changed = write_rule("../importer-changed.json", json.dumps(["CLAUDE.md"]))
    _, importer_receipt, importer_body = collect_rules(
        "--changed-files", importer_changed
    )
    importer_attributes = {
        source["path"]: source["modified_in_diff"]
        for source in importer_receipt["sources"]
    }
    assert importer_attributes == {"CLAUDE.md": True, "rules.md": False}
    assert (
        '<project-rules path="rules.md" modified-in-this-diff="false" id="ID">'
        in normalize_frame_ids(importer_body)
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See @b.md, then @a.md! Again @b.md.", ["b.md", "a.md"]),
        ("Email user@example.com; @type @param @Override @media", []),
    ],
    ids=["imports", "prose"],
)
def test_find_imports_normal_prose(text, expected):
    assert _find_imports(text) == expected


_IMPORT_PROBE_ROWS = [
    ("tab-before-fence", "\t```\n@imp.md\n```\n", ["imp.md"]),
    ("space-tab-before-fence", " \t```\n@imp.md\n```\n", ["imp.md"]),
    ("prose-punctuation", "Specify the @type. Use @media.", []),
    ("c1-opener-line", "```\n```python\n@imp.md\n```\n", []),
    ("c2-closer-trailing-text", "```\n@a.md\n``` trailing\n@b.md\n```\n", []),
    ("c3-backtick-info", "```a`b\n@imp.md\n", ["imp.md"]),
    ("c4-equal-width-span", "``@imp.md``\n", []),
    ("c7-tilde-closer-text", "~~~\n~~~x\n@imp.md\n~~~\n", []),
    ("c8-four-space-indent", "    ```\n@imp.md\n", ["imp.md"]),
    ("c9-unclosed-fence", "```\n@imp.md\n", []),
    ("d3-list-fence", "- item\n  ```\n  @imp.md\n  ```\n", []),
    ("e1-span-leaves-import-boundary", "a`x`@imp.md", ["imp.md"]),
    ("e2-span-before-import", "`x`@imp.md", ["imp.md"]),
    ("e3-midword-import", "word@imp.md", []),
    ("f1-span-splits-import", "@imp`code`.md", []),
    ("f2-mask-hidden-import-only", "`see @hidden.md here` and @live.md", ["live.md"]),
    ("escaped-opener-is-not-a-span", "see \\` @imp.md ` here", ["imp.md"]),
    ("even-backslashes-open-a-span", "see \\\\` @imp.md ` here", []),
    ("nested-fence-longer-closer", "````\n@A.md\n```\n@B.md\n````\n@C.md\n", ["C.md"]),
    ("crlf-fence-lines", "```\r\n@hidden.md\r\n```\r\n@live.md", ["live.md"]),
    ("cr-only-fence-then-span", "```\rx\r```\rsee ` @skip.md ` here\r", []),
    ("d1-multiline-span", "`open\n@imp.md`\n", []),
    ("d1-cr-only-multiline-span", "see `a\r@imp.md` here", []),
    ("d2-blockquote-fence", "> ```\n> @imp.md\n> ```\n", []),
    ("d4-html-comment", "<!-- @imp.md -->\n", []),
    ("d4b-bang-close-keeps-comment-open", "<!--\nx --!>\n@imp.md\n", []),
    ("d5-indented-code", "paragraph\n\n    @imp.md\n", []),
    ("p4-lazy-indent", "paragraph\n    @imp.md\n", ["imp.md"]),
    ("p7-quote-indented", "> para\n>\n>     @imp.md\n", []),
    ("p8-span-across-blank", "`open\n\n@imp.md`\n", ["imp.md`"]),
    ("p11-quote-fence-lazy-exit", "> ```\n@imp.md\n", ["imp.md"]),
    ("q1-quote-without-space", ">@imp.md\n", ["imp.md"]),
    ("p16-indented-after-heading", "# Title\n    @imp.md\n", []),
    ("p18-html-block-blank-ends", "<div>\n\n@imp.md\n</div>\n", ["imp.md"]),
    ("s1-setext-then-indent", "Guide\n===\n    @imp.md\n", []),
    ("h1-atx-span", "# Guide: ` @imp.md `\n", []),
    ("r1-quote-then-ordered-two", "> note\n2. step\n\n    @imp.md\n", []),
    (
        "r2-empty-item-trailing-spaces",
        "Details:\n\n-    \n    @imp.md\n",
        ["imp.md"],
    ),
    ("i1-comment-then-span-live", "Text <!-- ` --> @imp.md `\n", ["imp.md"]),
    ("i2-span-then-comment-live", "Text `<!--` @imp.md `-->`\n", ["imp.md"]),
    ("t7-custom-tag", "<custom-tag>\n@imp.md\n", []),
    ("t7-interrupt-para", "para\n<custom-tag>\n@imp.md\n", ["imp.md"]),
]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(source, expected, id=name)
        for name, source, expected in _IMPORT_PROBE_ROWS
    ],
)
def test_find_imports_matches_measured_parser(source, expected):
    assert _find_imports(source) == expected


def test_nested_pointer_uses_its_containing_directory(collect_rules, write_rule):
    write_rule("pkg/AGENTS.md", "@nested/RULES.md\n")
    write_rule("pkg/nested/RULES.md", "RULE-NESTED\n")
    write_rule("nested/RULES.md", "RULE-ROOT\n")
    changed = write_rule("../changed.json", '["pkg/thing.txt"]')
    code, receipt, body = collect_rules("--changed-files", changed)
    assert code == 0
    assert source_paths(receipt) == ["pkg/AGENTS.md", "pkg/nested/RULES.md"]
    assert "RULE-NESTED" in body
    assert "RULE-ROOT" not in body


def test_rules_output_creates_missing_parent(invoke, rule_root, tmp_path):
    target = tmp_path / "missing" / "rules.md"
    result = invoke(
        "collect_project_rules",
        ["--repo-root", rule_root, "--out", str(target)],
        tmp_path,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["ok"] is True
    assert target.read_bytes() == (
        b"project rules: none collected (REVIEW.md, CLAUDE.md, AGENTS.md, QODO.md)\n"
    )


def test_review_prose_uses_ordinary_imports_and_reports_retired_blocks(
    collect_rules, write_rule
):
    write_rule(
        "REVIEW.md",
        "```yaml code-gauntlet\nseverity_threshold: critical\nignore:\n  - everything\n```\nKeep invariants clear.\n@docs/POLICY.md\n",
    )
    write_rule("docs/POLICY.md", "A rule reached through an import.\n")
    code, receipt, body = collect_rules()
    assert code == 0
    assert receipt["unsupported_review_config"] is True
    assert [s["path"] for s in receipt["sources"]] == ["REVIEW.md", "docs/POLICY.md"]
    assert (
        "Keep invariants clear." in body and "A rule reached through an import." in body
    )
    assert "<project-rules " in body and "<review-rules " not in body
    assert receipt["gaps"] == []


@pytest.mark.parametrize(
    "form",
    [
        "## Default Delivery\r\npr_comments\r\n",
        "confidence_threshold = 70\n",
        "security_min_confidence: 60\n",
        "severity_threshold: high\n",
        "ignore:\n  - generated\n",
        "<!-- deep-review-config\nignore:\n  - generated\n-->\n",
        "```yaml\n# deep-review\nignore:\n  - generated\n```\n",
    ],
)
def test_every_retired_configuration_form_is_disclosed_locally(
    collect_rules, write_rule, form
):
    write_rule("REVIEW.md", form + "\nA prose invariant.\n")
    code, receipt, body = collect_rules()
    assert code == 0
    assert receipt["unsupported_review_config"]
    assert "A prose invariant." in body
    assert receipt["gaps"] == []


@pytest.mark.parametrize(
    "prose",
    [
        "## Skip\nSkip expensive checks when appropriate.\n",
        "## Ignore\nExplain ignored input.\n",
        "ignore: ordinary prose without a list\n",
        "confidence_threshold: undecided\n",
        "## Default Delivery\nUse whatever fits the project.\n",
        "## Default Delivery\u0085pr_comments\n",
    ],
)
def test_ordinary_review_prose_is_not_retired_configuration(
    collect_rules, write_rule, prose
):
    write_rule("REVIEW.md", prose)
    code, receipt, body = collect_rules()
    assert code == 0
    assert not receipt["unsupported_review_config"]
    assert prose.strip() in body
