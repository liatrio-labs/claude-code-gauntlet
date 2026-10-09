"""Style source validation and the carrier command's visible outcomes."""

import re
from pathlib import Path

import pytest

EXPECTED = (
    "<!-- GENERATED from docs/style/wording-rules.md and docs/style/cadence-rules.md by "
    "scripts/build_style_artifacts.py -- do not edit. Edit the sources, then run: "
    "python3 scripts/build_style_artifacts.py -->\n\n"
    "# Session output style\n\n"
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


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param(
            "## Hidden\n```text\nRULE: Poison.\n```\n",
            "section '## Hidden' has 0 RULE: lines; all sections must carry exactly one",
            id="fenced-only-rule",
        ),
        pytest.param(
            "## Plain\nRULE: Use plain words.\n```\n",
            "unbalanced code fence in",
            id="unbalanced",
        ),
        pytest.param(
            "# no rules\njust prose\n", "yields zero RULE: lines", id="zero-rules"
        ),
        pytest.param(
            "## Empty\n\n## Next\nRULE: Live.\n",
            "section '## Empty' has 0 RULE: lines; all sections must carry exactly one",
            id="missing-section-rule",
        ),
        pytest.param(
            "## Twice\nRULE: First.\nRULE: Second.\n",
            "section '## Twice' has 2 RULE: lines; all sections must carry exactly one",
            id="duplicate-section-rule",
        ),
    ],
)
def test_source_diagnostic(style_tree, invoke, source, message):
    (style_tree / "docs/style/wording-rules.md").write_text(source, encoding="utf-8")
    result = invoke(
        "build_style_artifacts", ["--repo-root", str(style_tree)], style_tree
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert message in result.stderr.decode()
    assert re.fullmatch(r"build_style_artifacts: [^\n]+\n", result.stderr.decode())
    assert str(Path("docs/style/wording-rules.md")) in result.stderr.decode()


def test_missing_source(style_tree, invoke):
    (style_tree / "docs/style/cadence-rules.md").unlink()
    result = invoke(
        "build_style_artifacts", ["--repo-root", str(style_tree)], style_tree
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert (
        result.stderr.decode()
        == f"build_style_artifacts: missing style rule source: {Path('docs/style/cadence-rules.md')}\n"
    )


def test_carrier_drift(style_tree, invoke):
    carrier = style_tree / "docs/style/session-context.md"
    carrier.write_text("stale\n", encoding="utf-8")
    args = ["--repo-root", str(style_tree)]
    stale = invoke("build_style_artifacts", [*args, "--check"], style_tree)
    assert stale.returncode == 1
    assert stale.stdout == b""
    assert stale.stderr.decode() == (
        f"build_style_artifacts: stale generated carrier: {Path('docs/style/session-context.md')}; run: python3 scripts/build_style_artifacts.py\n"
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


def test_current_carrier(style_tree, invoke):
    carrier = style_tree / "docs/style/session-context.md"
    before = carrier.stat().st_mtime_ns
    result = invoke(
        "build_style_artifacts", ["--repo-root", str(style_tree)], style_tree
    )
    assert result.returncode == 0
    assert result.stdout == b"style session-context carrier is current\n"
    assert result.stderr == b""
    assert carrier.stat().st_mtime_ns == before


def test_missing_carrier(style_tree, invoke):
    carrier = style_tree / "docs/style/session-context.md"
    carrier.unlink()
    args = ["--repo-root", str(style_tree)]
    assert (
        invoke("build_style_artifacts", [*args, "--check"], style_tree).returncode == 1
    )
    assert not carrier.exists()
    assert invoke("build_style_artifacts", args, style_tree).returncode == 0
    assert carrier.read_text(encoding="utf-8") == EXPECTED


def test_usage(invoke, tmp_path):
    help_result = invoke("build_style_artifacts", ["--help"], tmp_path)
    assert help_result.returncode == 0
    assert (
        " ".join(help_result.stdout.decode().split("\n\n", 1)[0].split())
        == "usage: build_style_artifacts [-h] [--repo-root REPO_ROOT] [--check]"
    )
    usage = invoke("build_style_artifacts", ["--unknown"], tmp_path)
    assert usage.returncode == 2
    assert usage.stdout == b""
    assert usage.stderr == b"build_style_artifacts: unrecognized arguments: --unknown\n"


def test_unreadable_carrier(style_tree, invoke):
    carrier = style_tree / "docs/style/session-context.md"
    carrier.unlink()
    carrier.mkdir()
    result = invoke(
        "build_style_artifacts", ["--repo-root", str(style_tree), "--check"], style_tree
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert re.fullmatch(r"build_style_artifacts: [^\n]+\n", result.stderr.decode())
    assert "stale generated carrier" not in result.stderr.decode()
    assert carrier.is_dir()
