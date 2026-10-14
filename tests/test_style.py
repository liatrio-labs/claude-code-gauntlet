"""Style source validation and the carrier command's visible outcomes."""

from pathlib import Path

import pytest

EXPECTED = (
    "<!-- GENERATED from docs/style/wording-rules.md "
    "and docs/style/cadence-rules.md by "
    "scripts/build_style_artifacts.py -- do not edit. Edit the sources, then run: "
    "python3 scripts/build_style_artifacts.py -->\n\n# Session output style\n\n"
    "These rules govern Claude's session output in this repository.\n\n"
    "## Wording\n\n- Use plain words.\n\n## Cadence\n\n- Keep sentences connected.\n"
)


@pytest.fixture
def style_tree(tmp_path):
    directory = tmp_path / "docs/style"
    directory.mkdir(parents=True)
    (directory / "wording-rules.md").write_text(
        "# Wording\n\n## Plain\n\nRULE: Use plain words.\n", encoding="utf-8"
    )
    (directory / "cadence-rules.md").write_text(
        "# Cadence\n\n## Connected\n\nRULE: Keep sentences connected.\n",
        encoding="utf-8",
    )
    (directory / "session-context.md").write_text(EXPECTED, encoding="utf-8")
    return tmp_path


def test_fenced_example(style_tree, invoke):
    wording = style_tree / "docs/style/wording-rules.md"
    wording.write_text(
        "# Preamble\nRULE: Intro rule.\n## Plain\nRULE: Use plain words.\n"
        "  ````text\n## Hidden\nRULE: Poison.\n```suffix\n",
        encoding="utf-8",
    )
    result = invoke(
        "build_style_artifacts", ["--repo-root", str(style_tree)], style_tree
    )
    assert result.returncode == 0
    assert (style_tree / "docs/style/session-context.md").read_text(
        encoding="utf-8"
    ) == EXPECTED.replace("- Use plain words.", "- Intro rule.\n- Use plain words.")


def test_source_diagnostic(style_tree, invoke):
    (style_tree / "docs/style/wording-rules.md").write_text(
        "## Empty\n\n## Next\nRULE: Live.\n", encoding="utf-8"
    )
    result = invoke(
        "build_style_artifacts", ["--repo-root", str(style_tree)], style_tree
    )
    assert result.returncode == 1
    assert result.stdout == b""
    source = Path("docs/style/wording-rules.md")
    assert result.stderr.decode() == (
        f"build_style_artifacts: {source} section '## Empty' has 0 RULE: lines; "
        "all sections must carry exactly one\n"
    )


def test_carrier_drift(style_tree, invoke):
    carrier = style_tree / "docs/style/session-context.md"
    carrier.write_text("stale\n", encoding="utf-8")
    args = ["--repo-root", str(style_tree)]
    stale = invoke("build_style_artifacts", [*args, "--check"], style_tree)
    assert stale.returncode == 1
    assert stale.stdout == b""
    relative = Path("docs/style/session-context.md")
    assert stale.stderr.decode() == (
        f"build_style_artifacts: stale generated carrier: {relative}; "
        "run: python3 scripts/build_style_artifacts.py\n"
    )
    assert carrier.read_text(encoding="utf-8") == "stale\n"
    repaired = invoke("build_style_artifacts", args, style_tree)
    assert repaired.returncode == 0
    assert (
        repaired.stdout.decode()
        == f"regenerated: {Path('docs/style/session-context.md')}\n"
    )
    assert repaired.stderr == b""
    assert carrier.read_text(encoding="utf-8") == EXPECTED
    before = carrier.stat().st_mtime_ns
    current = invoke("build_style_artifacts", args, style_tree)
    assert current.returncode == 0
    assert current.stdout == b"style session-context carrier is current\n"
    assert current.stderr == b""
    assert carrier.stat().st_mtime_ns == before
