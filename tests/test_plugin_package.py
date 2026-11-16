"""Boundary tests for the directory package builder, checker and publisher."""

import importlib.util
import json
import subprocess
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


def _source_files() -> dict[str, str]:
    return {
        **_package_files(),
        ".claude-plugin/marketplace.json": "{}\n",
        "agents/AGENTS.md": "# agents\n",
        **dict.fromkeys(MODULE.DEV_ONLY, ""),
        "workflows/build.js": "",
        "README.md": "# Demo\n\n<picture>\n"
        '  <img alt="Overview" src="docs/assets/pipeline-overview-light.svg">\n'
        "</picture>\n\nSee [the tests](tests/t.py), [the docs](docs/), "
        "[the assets](docs/assets/) and [the license](LICENSE).\n",
        ".claude-plugin/icon.png": "png\n",
        "LICENSE": "license\n",
        "PRIVACY.md": "privacy\n",
        "SECURITY.md": "security\n",
        "docs/assets/pipeline-overview-light.svg": "<svg/>\n",
        "docs/guide.md": "guide\n",
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
    shipped = set(_files(out))
    assert capsys.readouterr().out.startswith(
        f"package checks passed: {len(shipped)} files\n"
    )
    manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_bytes())
    assert {agent[2:] for agent in manifest["agents"]} <= shipped
    assert not shipped & MODULE.DEV_ONLY
    assert not {"agents/AGENTS.md", ".claude-plugin/marketplace.json"} & shipped
    assert not any(
        path.startswith("workflows/") for path in shipped - {"workflows/pipeline.js"}
    )
    # The standard-layout table of the plugin manifest reference
    # (code.claude.com/docs/en/plugins/manifest-reference) lists these default locations,
    # which load without a manifest entry, so each one the repository has must ship.
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
TRACKED = SHIPPED | MODULE._with_parents(
    {"bench/README.md", "docs/guide.md", "docs/a(1).md"}
)


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
        ("README.md", "[^1]: A footnote, not a path.\n", None),
        (
            "README.md",
            "[![b](https://img.test/b.svg)](docs/guide.md)\n",
            f"[![b](https://img.test/b.svg)]({BLOB}docs/guide.md)\n",
        ),
        ("README.md", "[a](<docs/guide.md>)\n", f"[a](<{BLOB}docs/guide.md>)\n"),
        ("README.md", "[p](docs/a(1).md)\n", f"[p]({BLOB}docs/a%281%29.md)\n"),
        (
            "README.md",
            '<a href="docs/guide.md">g</a>\n',
            f'<a href="{BLOB}docs/guide.md">g</a>\n',
        ),
    ],
    ids=[
        *("readme", "nested", "picture", "code", "reference", "footnote", "badge"),
        *("angle", "parens", "anchor"),
    ],
)
def test_markdown_links_resolve_in_package_or_on_github(path, text, expected):
    rewritten = MODULE.rewrite_markdown(text, path, SHIPPED, TRACKED, BLOB)
    assert rewritten == (text if expected is None else expected)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[x](gone.md)\n", "README.md: link gone.md points at no tracked file"),
        ("[x](<gone file.md>)\n", "link gone file.md points at no tracked file"),
        ("[x](gone(1).md)\n", "link gone(1).md points at no tracked file"),
        ("<a href='gone.md'>x</a>\n", "link gone.md points at no tracked file"),
        ("![x](bench/README.md)\n", "image bench/README.md is not in the package"),
        ("[x](../outside.md)\n", "link ../outside.md leaves the repository"),
        ('<img src="docs/assets/a.svg">\n', "HTML image tag"),
        ("<picture><source></picture>\n", "<picture> has no <img src>"),
    ],
    ids=["dangling", "angle", "parens", "anchor", "image", "escape", "html", "picture"],
)
def test_markdown_rewrite_fails_on_links_that_would_break(text, problem):
    with pytest.raises(MODULE.PackageError) as raised:
        MODULE.rewrite_markdown(text, "README.md", SHIPPED, TRACKED, BLOB)
    assert any(problem in line for line in raised.value.problems)


def _link(root: Path) -> Path:
    link = root / "skills/s/link.md"
    try:
        link.symlink_to(root / "LICENSE")
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    return link


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
            _link,
            "skills/s/link.md: symlinks are not allowed",
        ),
        (
            lambda root: (root / "agents/a.md").unlink(),
            ".claude-plugin/plugin.json: agents/a.md is not in the package",
        ),
        (
            lambda root: _write(
                root,
                {
                    ".claude-plugin/plugin.json": _manifest().replace(
                        "./agents", "agents"
                    )
                },
            ),
            ".claude-plugin/plugin.json: agents entry 'agents/a.md' must start with ./",
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
            # The second of two problems: a failure prints every problem it found.
            "skills/s/SKILL.md: frontmatter line '  - Read' is not `key: value`",
        ),
        (
            lambda root: (root / "agents/a.md").write_bytes(b"---\nname: \xff\n---\n"),
            "agents/a.md: not valid UTF-8",
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
        (
            lambda root: _write(root, {"scripts/entry.py": "def (\n"}),
            "scripts/entry.py:1: not valid Python",
        ),
    ],
    ids=[
        *("valid", "bundle-exempt", "oversize", "count", "dev-path", "symlink"),
        *("manifest-path", "manifest-prefix", "frontmatter-comment"),
        *("frontmatter-key", "frontmatter-list", "undecodable"),
        *("dev-import", "missing-module", "syntax"),
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
        (
            lambda root, out: (_link(root), git(root, "add", "--all")),
            "skills/s/link.md: symlinks are not shipped",
        ),
        (
            lambda root, out: git(root, "rm", "-q", "--cached", "PRIVACY.md"),
            "PRIVACY.md: shipped but not tracked",
        ),
        (
            lambda root, out: git(
                root, "rm", "-q", "--cached", "scripts/gauntlet/style.py"
            ),
            "scripts/gauntlet/style.py: in DEV_ONLY but not tracked",
        ),
        (
            lambda root, out: _write(root, {"SECURITY.md": "[x](missing.md)\n"}),
            "SECURITY.md: link missing.md points at no tracked file",
        ),
    ],
    ids=["reused-output", "symlink", "untracked", "stale-dev-only", "dangling"],
)
def test_build_refuses_what_it_cannot_ship(tmp_path, source, capsys, mutate, problem):
    out = tmp_path / "package"
    mutate(source, out)
    assert MODULE.main(["build", "--source", str(source), "--out", str(out)]) == 2
    assert any(problem in line for line in capsys.readouterr().err.splitlines())


def _raise(error: BaseException):
    def raiser(*args, **kwargs):
        raise error

    return raiser


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        (
            subprocess,
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
            "command output is not valid UTF-8",
        ),
        (subprocess, FileNotFoundError(), "command failed (127): git ls-files -z"),
        (MODULE.shutil, PermissionError(), "file operation failed: "),
    ],
    ids=["undecodable-output", "missing-command", "file-error"],
)
def test_cli_reports_failures_on_stderr_with_exit_2(
    tmp_path, source, capsys, monkeypatch, target, error, message
):
    attribute = "run" if target is subprocess else "copy2"
    monkeypatch.setattr(target, attribute, _raise(error))
    out = tmp_path / "package"
    assert MODULE.main(["build", "--source", str(source), "--out", str(out)]) == 2
    assert capsys.readouterr().err == message + "\n"


def test_a_failure_without_a_message_still_prints(capsys):
    assert MODULE.exit_code(_raise(MODULE.InputError())) == 2
    assert capsys.readouterr().err == "InputError\n"


def test_build_ships_the_allowlist_from_a_source_tree(tmp_path, source):
    out = tmp_path / "package"
    MODULE.build(source, out)
    assert _files(out) == [
        *(".claude-plugin/icon.png", ".claude-plugin/plugin.json", "LICENSE"),
        *("PRIVACY.md", "README.md"),
        *("SECURITY.md", "agents/a.md", "docs/assets/pipeline-overview-light.svg"),
        *(
            "scripts/entry.py",
            "scripts/gauntlet/__init__.py",
            "scripts/gauntlet/core.py",
        ),
        *("scripts/gauntlet/paths.py", "skills/s/SKILL.md", "workflows/pipeline.js"),
    ]
    # docs/ ships only its assets, so a link to it goes to GitHub; docs/assets/ ships whole.
    assert (out / "README.md").read_text(encoding="utf-8") == (
        "# Demo\n\n![Overview](docs/assets/pipeline-overview-light.svg)\n\n"
        f"See [the tests]({BLOB}tests/t.py), [the docs]({BLOB}docs), "
        "[the assets](docs/assets/) and [the license](LICENSE).\n"
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


def _land_on_main(source: Path) -> None:
    git(source, "push", "-q", "origin", "HEAD:refs/heads/main")


def _release(source: Path, version: str) -> None:
    _write(source, {".claude-plugin/plugin.json": _manifest(version)})
    git(source, "commit", "-q", "--all", "-m", f"release {version}")
    git(source, "tag", f"v{version}")
    _land_on_main(source)


@pytest.fixture
def remote(tmp_path: Path, source: Path) -> Path:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    # With no tracking refspec, only publish's own fetch can set origin/main.
    git(source, "config", "--unset-all", "remote.origin.fetch")
    git(source, "tag", "v1.0.0")
    _land_on_main(source)
    return remote


def _failing_git(monkeypatch: pytest.MonkeyPatch, prefix: list[str], code: int) -> None:
    real = MODULE.run_command

    def run(argv, cwd=None, env=None):
        if argv[: len(prefix)] == prefix:
            raise MODULE.CommandFailure(tuple(argv), code, "fatal: injected")
        return real(argv, cwd, env)

    monkeypatch.setattr(MODULE, "run_command", run)


def test_publish_shows_a_git_error_instead_of_an_ancestry_verdict(
    source, remote, monkeypatch
):
    _failing_git(monkeypatch, ["git", "merge-base"], 128)
    with pytest.raises(MODULE.CommandFailure) as raised:
        MODULE.publish("v1.0.0", source)
    assert raised.value.returncode == 128


def test_publish_keeps_the_build_error_when_cleanup_also_fails(
    source, remote, monkeypatch, capsys
):
    monkeypatch.setattr(MODULE, "build", _raise(MODULE.PackageError(["boom"])))
    _failing_git(monkeypatch, ["git", "worktree", "remove"], 1)
    with pytest.raises(MODULE.PackageError, match="boom"):
        MODULE.publish("v1.0.0", source)
    assert capsys.readouterr().err.startswith(
        "also failed to remove the build worktree: "
        "command failed (1): git worktree remove --force"
    )


def test_publish_builds_each_tag_into_one_linear_commit(source: Path, remote: Path):
    git(source, "tag", "v9.0.0")
    with pytest.raises(MODULE.InputError, match="malformed release tag"):
        MODULE.publish("1.0.0", source)
    assert MODULE.publish("v1.0.0", source)[-1].startswith("published v1.0.0")
    assert MODULE.publish("v1.0.0", source)[-1] == (
        "plugin-release already holds the v1.0.0 package"
    )
    with pytest.raises(MODULE.PackageError, match=r"does not match v9\.0\.0"):
        MODULE.publish("v9.0.0", source)
    # A tag on a commit that never reached main does not publish, even by hand.
    git(source, "checkout", "-q", "-b", "side")
    _write(source, {".claude-plugin/plugin.json": _manifest("2.0.0")})
    git(source, "commit", "-q", "--all", "-m", "side")
    git(source, "tag", "v2.0.0")
    git(source, "checkout", "-q", "-")
    with pytest.raises(MODULE.InputError, match=r"v2\.0\.0 is not on main"):
        MODULE.publish("v2.0.0", source)
    _release(source, "1.1.0")
    MODULE.publish("v1.1.0", source)
    with pytest.raises(MODULE.InputError, match=r"already holds v1\.1\.0"):
        MODULE.publish("v1.0.0", source)

    history = git(remote, "log", "--format=%H %P|%s", "plugin-release").splitlines()
    (newest, newest_subject), (oldest, oldest_subject) = (
        line.split("|") for line in history
    )
    assert [newest_subject, oldest_subject] == ["release: v1.1.0", "release: v1.0.0"]
    assert newest.split()[1:] == oldest.split()[:1] and oldest.split()[1:] == []
    listed = git(remote, "ls-tree", "-r", "--name-only", "plugin-release").splitlines()
    assert "README.md" in listed and "tests/t.py" not in listed
    assert f"{BLOB.replace('/main/', '/v1.1.0/')}tests/t.py" in git(
        remote, "show", "plugin-release:README.md"
    )
    # Worktrees and the private index leave the checkout and its refs as they were.
    assert git(source, "status", "--porcelain") == ""
    assert git(source, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(remote, "for-each-ref", "--format=%(refname)").splitlines() == [
        "refs/heads/main",
        "refs/heads/plugin-release",
    ]

    # Only publish writes the branch, so a hand-pushed head stops the next release.
    tree = git(remote, "rev-parse", "plugin-release^{tree}")
    hand = git(remote, "commit-tree", tree, "-p", "plugin-release", "-m", "hotfix")
    git(remote, "update-ref", "refs/heads/plugin-release", hand)
    _release(source, "1.2.0")
    with pytest.raises(
        MODULE.InputError,
        match=r"is not a release commit: 'hotfix'\. Reset plugin-release to its last",
    ):
        MODULE.publish("v1.2.0", source)
