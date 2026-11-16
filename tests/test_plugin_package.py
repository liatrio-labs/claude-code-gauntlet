"""Boundary tests for the directory package builder, checker and publisher."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.support.git_diff import git, init_git_repo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "plugin_package", ROOT / ".github" / "plugin_package.py"
)
assert SPEC is not None and SPEC.loader is not None
plugin_package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin_package
SPEC.loader.exec_module(plugin_package)
MODULE = plugin_package

BLOB = "https://github.com/o/r/blob/main/"
COMPONENT_LOCATIONS = (
    *("agents", "skills", "commands", "workflows", "hooks", "output-styles"),
    *("themes", "monitors", "bin", ".mcp.json", ".lsp.json"),
)
AGENT = "---\nname: a\ndescription: Reads code\ntools: Read\n---\n\nRead.\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def _manifest(version: str = "1.0.0") -> str:
    return json.dumps(
        {
            "name": "demo",
            "version": version,
            "repository": "https://github.com/o/r",
            "agents": ["./agents/a.md"],
        }
    )


def _package_files() -> dict[str, str]:
    return {
        ".claude-plugin/plugin.json": _manifest(),
        "agents/a.md": AGENT,
        "skills/s/SKILL.md": "---\nname: s\ndescription: Does s\n---\n\nDo s.\n",
        "scripts/entry.py": "from gauntlet.core import value\n",
        "scripts/gauntlet/__init__.py": "",
        "scripts/gauntlet/core.py": "from . import paths\nvalue = 1\n",
        "scripts/gauntlet/paths.py": "",
        "workflows/pipeline.js": "export const meta = {};\n",
    }


def _source_files(version: str = "1.0.0") -> dict[str, str]:
    return {
        **_package_files(),
        ".claude-plugin/plugin.json": _manifest(version),
        ".claude-plugin/marketplace.json": "{}\n",
        "agents/AGENTS.md": "# agents\n",
        "scripts/gauntlet/measurement.py": "",
        "workflows/build.js": "",
        "README.md": "# Demo\n\n<picture>\n"
        '  <img alt="Overview" src="docs/assets/pipeline-overview-light.svg">\n'
        "</picture>\n\nSee [the tests](tests/t.py) and [the license](LICENSE).\n",
        "LICENSE": "license\n",
        "PRIVACY.md": "privacy\n",
        "SECURITY.md": "security\n",
        "docs/assets/pipeline-overview-light.svg": "<svg/>\n",
        "tests/t.py": "",
    }


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    init_git_repo(root, monkeypatch)
    _write(root, _source_files())
    git(root, "add", "--all")
    git(root, "commit", "-q", "-m", "plugin")
    return root


def _files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def test_real_tree_ships_runtime_files_with_resolvable_links(tmp_path, capsys):
    out = tmp_path / "package"
    assert MODULE.main(["build", "--out", str(out)]) == 0
    assert capsys.readouterr().out.startswith("package checks passed")
    shipped = set(_files(out))
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_bytes())
    assert {agent[2:] for agent in manifest["agents"]} <= shipped
    assert not shipped & MODULE.DEV_ONLY
    assert not {"agents/AGENTS.md", ".claude-plugin/marketplace.json"} & shipped
    assert not any(
        path.startswith("workflows/") for path in shipped - {"workflows/pipeline.js"}
    )
    # Claude Code loads these locations without a manifest entry, so each must ship.
    for location in COMPONENT_LOCATIONS:
        if (ROOT / location).exists():
            assert any(path.split("/")[0] == location for path in shipped), location
    # Nothing outside the package counts as tracked, so a link that leaves it would raise.
    for relative in shipped:
        if relative.endswith(".md"):
            MODULE.rewrite_markdown(
                (out / relative).read_bytes().decode("utf-8"),
                relative,
                MODULE._with_parents(shipped),
                frozenset(),
                BLOB,
            )


SHIPPED = MODULE._with_parents(
    {"README.md", "LICENSE", "docs/assets/a.svg", "skills/s/SKILL.md", "skills/s/r.md"}
)
TRACKED = SHIPPED | MODULE._with_parents({"bench/README.md", "docs/guide.md"})


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        (
            "README.md",
            "[l](LICENSE) [g](docs/guide.md#part) [w](https://x.test/a) [t](#top)\n",
            f"[l](LICENSE) [g]({BLOB}docs/guide.md#part) [w](https://x.test/a) [t](#top)\n",
        ),
        (
            "skills/s/r.md",
            "[d](../../docs/guide.md#a) and\n[r](SKILL.md)\n",
            f"[d]({BLOB}docs/guide.md#a) and\n[r](SKILL.md)\n",
        ),
        (
            "README.md",
            '<picture>\n  <source srcset="docs/assets/dark.svg">\n'
            '  <img alt="A [b] &amp; c" src="docs/assets/a.svg">\n</picture>\n',
            "![A \\[b\\] & c](docs/assets/a.svg)\n",
        ),
        (
            "README.md",
            "`[x](gone.md)`\n\n````md\n[y](gone.md)\n```\n````\n",
            "`[x](gone.md)`\n\n````md\n[y](gone.md)\n```\n````\n",
        ),
        ("README.md", "[g]: bench/README.md\n", f"[g]: {BLOB}bench/README.md\n"),
        (
            "README.md",
            "[![b](https://img.test/b.svg)](docs/guide.md)\n",
            f"[![b](https://img.test/b.svg)]({BLOB}docs/guide.md)\n",
        ),
    ],
    ids=["readme", "nested", "picture", "code", "reference", "badge"],
)
def test_markdown_links_resolve_in_package_or_on_github(path, text, expected):
    assert MODULE.rewrite_markdown(text, path, SHIPPED, TRACKED, BLOB) == expected


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[x](gone.md)\n", "README.md: link gone.md points at no tracked file"),
        ("![x](bench/README.md)\n", "image bench/README.md is not in the package"),
        ("[x](../outside.md)\n", "link ../outside.md leaves the repository"),
        ('<img src="docs/assets/a.svg">\n', "HTML image tag"),
        ("<picture><source></picture>\n", "<picture> has no <img src>"),
    ],
    ids=["dangling", "image", "escape", "html", "picture"],
)
def test_markdown_rewrite_fails_on_links_that_would_break(text, problem):
    with pytest.raises(MODULE.PackageError) as raised:
        MODULE.rewrite_markdown(text, "README.md", SHIPPED, TRACKED, BLOB)
    assert any(problem in line for line in raised.value.problems)


def _symlink(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")


def _track_symlink(root: Path, out: Path) -> None:
    _symlink(root / "LICENSE", root / "skills/s/link.md")
    git(root, "add", "--all")


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda root: None, None),
        (
            lambda root: _write(
                root, {"workflows/pipeline.js": "x" * (256 * 1024 + 1)}
            ),
            None,
        ),
        (
            lambda root: _write(root, {"scripts/big.py": "x" * (256 * 1024 + 1)}),
            "scripts/big.py: 262145 bytes exceeds 262144",
        ),
        (
            lambda root: _write(root, {f"skills/s/r{n}.md": "" for n in range(505)}),
            "513 files; the directory accepts at most 512",
        ),
        (
            lambda root: _write(root, {"docs/research/a.md": ""}),
            "docs/research/a.md: development path in the package",
        ),
        (
            lambda root: _symlink(root / "LICENSE", root / "skills/s/link.md"),
            "skills/s/link.md: symlinks are not allowed",
        ),
        (
            lambda root: (root / "agents/a.md").unlink(),
            ".claude-plugin/plugin.json: agents/a.md is not in the package",
        ),
        (
            lambda root: _write(
                root, {"agents/a.md": AGENT.replace("Read", "Read  # why")}
            ),
            "agents/a.md: frontmatter value of tools needs quoting",
        ),
        (
            lambda root: _write(
                root, {"agents/a.md": AGENT.replace("tools:", "hooks:")}
            ),
            "agents/a.md: frontmatter key hooks is outside the allowed list",
        ),
        (
            lambda root: _write(
                root, {"skills/s/SKILL.md": "---\ntools:\n  - Read\n---\n"}
            ),
            "skills/s/SKILL.md: frontmatter line 'tools:' is not `key: value`",
        ),
        (
            lambda root: _write(
                root, {"scripts/entry.py": "from gauntlet import measurement\n"}
            ),
            "scripts/entry.py:1: imports gauntlet.measurement, which is not shipped",
        ),
        (
            lambda root: (root / "scripts/gauntlet/paths.py").unlink(),
            "scripts/gauntlet/core.py:1: imports gauntlet.paths, which is not shipped",
        ),
    ],
    ids=[
        *("valid", "bundle-exempt", "oversize", "count", "dev-path", "symlink"),
        *(
            "manifest-path",
            "frontmatter-comment",
            "frontmatter-key",
            "frontmatter-list",
        ),
        *("dev-import", "missing-module"),
    ],
)
def test_check_enforces_directory_limits(tmp_path, capsys, mutate, problem):
    _write(tmp_path, _package_files())
    mutate(tmp_path)
    code = MODULE.main(["check", str(tmp_path)])
    captured = capsys.readouterr()
    if problem is None:
        assert code == 0, captured.err
        assert captured.out.startswith("package checks passed")
    else:
        assert code == 2
        assert problem in captured.err.splitlines()


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda root, out: _write(out, {"stale": ""}), "output must be a new or empty"),
        (_track_symlink, "skills/s/link.md: symlinks are not shipped"),
        (
            lambda root, out: git(root, "rm", "-q", "--cached", "PRIVACY.md"),
            "PRIVACY.md: shipped but not tracked",
        ),
        (
            lambda root, out: _write(root, {"SECURITY.md": "[x](missing.md)\n"}),
            "SECURITY.md: link missing.md points at no tracked file",
        ),
        (
            lambda root, out: _write(
                root, {"agents/a.md": AGENT.replace("Read", "Read  # why")}
            ),
            "agents/a.md: frontmatter value of tools needs quoting",
        ),
    ],
    ids=["reused-output", "symlink", "untracked", "dangling", "checked"],
)
def test_build_refuses_what_it_cannot_ship(tmp_path, source, capsys, mutate, problem):
    out = tmp_path / "package"
    mutate(source, out)
    assert MODULE.main(["build", "--source", str(source), "--out", str(out)]) == 2
    assert any(problem in line for line in capsys.readouterr().err.splitlines())


def test_build_ships_the_allowlist_from_a_source_tree(tmp_path, source):
    out = tmp_path / "package"
    MODULE.build(source, out)
    assert _files(out) == [
        *(".claude-plugin/plugin.json", "LICENSE", "PRIVACY.md", "README.md"),
        *("SECURITY.md", "agents/a.md", "docs/assets/pipeline-overview-light.svg"),
        *(
            "scripts/entry.py",
            "scripts/gauntlet/__init__.py",
            "scripts/gauntlet/core.py",
        ),
        *("scripts/gauntlet/paths.py", "skills/s/SKILL.md", "workflows/pipeline.js"),
    ]
    assert (out / "README.md").read_text(encoding="utf-8") == (
        "# Demo\n\n![Overview](docs/assets/pipeline-overview-light.svg)\n\n"
        f"See [the tests]({BLOB}tests/t.py) and [the license](LICENSE).\n"
    )


def test_build_links_left_out_files_to_the_given_ref(tmp_path, source):
    out = tmp_path / "package"
    assert (
        MODULE.main(
            ["build", "--source", str(source), "--out", str(out), "--ref", "v1.2.3"]
        )
        == 0
    )
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert f"({BLOB.replace('/blob/main/', '/blob/v1.2.3/')}tests/t.py)" in readme
    assert "/blob/main/" not in readme


def test_publish_keeps_one_linear_commit_per_release(tmp_path: Path, source: Path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))

    def release(version: str) -> Path:
        _write(source, {".claude-plugin/plugin.json": _manifest(version)})
        out = tmp_path / f"package-{version}"
        MODULE.build(source, out)
        return out

    first = release("1.0.0")
    with pytest.raises(MODULE.InputError, match="malformed release tag"):
        MODULE.publish("1.0.0", first, source)
    assert MODULE.publish("v1.0.0", first, source).startswith("published v1.0.0")
    assert MODULE.publish("v1.0.0", first, source) == (
        "plugin-release already holds the v1.0.0 package"
    )
    second = release("1.1.0")
    with pytest.raises(MODULE.PackageError, match=r"does not match v1\.0\.0"):
        MODULE.publish("v1.0.0", second, source)
    MODULE.publish("v1.1.0", second, source)

    history = git(remote, "log", "--format=%H %P|%s", "plugin-release").splitlines()
    (newest, newest_subject), (oldest, oldest_subject) = (
        line.split("|") for line in history
    )
    assert [newest_subject, oldest_subject] == ["release: v1.1.0", "release: v1.0.0"]
    assert newest.split()[1:] == oldest.split()[:1] and oldest.split()[1:] == []
    listed = git(remote, "ls-tree", "-r", "--name-only", "plugin-release").splitlines()
    assert listed == _files(second)
    # The private index leaves the release checkout and its other branches alone.
    assert git(source, "status", "--porcelain") == "M .claude-plugin/plugin.json"
    assert (
        git(remote, "for-each-ref", "--format=%(refname)")
        == "refs/heads/plugin-release"
    )
