#!/usr/bin/env python3
"""Build, check and publish the runtime-only plugin package for Anthropic's directory.

Usage:
    python3 .github/plugin_package.py build --out DIR [--source DIR] [--ref REF]
    python3 .github/plugin_package.py check DIR
    python3 .github/plugin_package.py publish --tag vX.Y.Z

The directory installs only the folder it tracks and checks every file in it, so each release
publishes this package to its own branch while marketplace users keep installing `main`.
`build` links the files it leaves out to REF on GitHub (default main). `publish` builds from a
checkout of the tag, links to the tag, and appends one commit to the package branch.
"""

import ast
import html
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_cli import (
    ArgumentParser,
    CommandFailure,
    InputError,
    entry,
    exit_code,
    run_command,
    validate_tag,
)

ROOT = Path(__file__).resolve().parents[1]
RELEASE_BRANCH = "plugin-release"
# A manual retry names its own tag, so publish ships only commits that reached main.
MAIN_REF = "refs/remotes/origin/main"
MANIFEST = ".claude-plugin/plugin.json"
RELEASE_SUBJECT = re.compile(r"release: v([0-9]+)\.([0-9]+)\.([0-9]+)")

# Everything the package ships besides the agent files plugin.json lists. A directory ships
# whole, so a new runtime module or reference needs no edit here.
SHIPPED = (
    MANIFEST,
    "skills",
    "scripts",
    "workflows/pipeline.js",
    "README.md",
    "LICENSE",
    "PRIVACY.md",
    "SECURITY.md",
    "docs/assets/pipeline-overview-light.svg",
)
# Maintainer tooling inside shipped directories that no shipped command runs. The contract
# generator needs workflows/src and Node, the style generator reads docs/style sources the
# package leaves out, the style hook serves this repository's own sessions, and only the
# benchmark decodes traces.
DEV_ONLY = frozenset(
    {
        "scripts/build_style_artifacts.py",
        "scripts/emit_style_context.py",
        "scripts/generate_contract_requirements.py",
        "scripts/gauntlet/contract_gen.py",
        "scripts/gauntlet/generate.py",
        "scripts/gauntlet/measurement.py",
        "scripts/gauntlet/ruff.toml",
        "scripts/gauntlet/style.py",
        "scripts/gauntlet/style_hook.py",
    }
)

MAX_FILES = 512
MAX_FILE_BYTES = 256 * 1024
# The workflow runtime has no module system, so the pipeline must stay one bundled file.
OVERSIZE_EXEMPT = frozenset({"workflows/pipeline.js"})
FORBIDDEN_PREFIXES = ("tests/", "bench/", ".github/", "docs/research/")
COMPONENT_KEYS = (
    *("agents", "skills", "commands", "workflows"),
    *("outputStyles", "hooks", "mcpServers", "lspServers"),
)
AGENT_KEYS = frozenset(
    {
        *("name", "description", "model", "effort", "maxTurns", "autoCompactWindow"),
        *("tools", "disallowedTools", "skills", "memory", "background"),
        *("omitClaudeMd", "isolation", "color", "experimental"),
    }
)

FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*\r?\n?")
CODE_SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
# A destination is either <bracketed> or a run without spaces that may hold balanced parens.
INLINE_TARGET = re.compile(r"\]\(\s*(?:<([^<>\n]*)>|((?:[^\s()<>]|\([^\s()<>]*\))+))")
# Footnote definitions ([^1]: text) hold prose, not a destination.
REFERENCE_TARGET = re.compile(
    r"^ {0,3}\[(?!\^)[^\]\n]+\]:[ \t]*(?:<([^<>\n]*)>|(\S+))", re.MULTILINE
)
ANCHOR = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
HREF = re.compile(
    r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE
)
PICTURE = re.compile(r"<picture\b.*?</picture>", re.IGNORECASE | re.DOTALL)
IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_ATTRIBUTE = re.compile(r'\b(alt|src)="([^"]*)"', re.IGNORECASE)
HTML_IMAGE = re.compile(r"<(?:img|picture|source)\b", re.IGNORECASE)
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
FRONTMATTER_LINE = re.compile(r"([A-Za-z][\w-]*): (.+)")
SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")


class PackageError(InputError):
    """The package cannot be built or fails a directory check."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))

    def report(self) -> list[str]:
        return list(self.problems)


@dataclass(frozen=True, slots=True)
class PackageReport:
    file_count: int
    largest: tuple[tuple[str, int], ...]

    def lines(self) -> list[str]:
        sizes = [
            f"  {path}: {size} bytes"
            + (" (exempt from the size limit)" if path in OVERSIZE_EXEMPT else "")
            for path, size in self.largest
        ]
        return [f"package checks passed: {self.file_count} files", *sizes]


@dataclass(frozen=True, slots=True)
class Link:
    start: int
    end: int
    image: bool


@dataclass(frozen=True, slots=True)
class ReleaseHead:
    commit: str
    tree: str
    version: tuple[int, ...]


def _manifest(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError([f"{MANIFEST}: cannot read: {error}"]) from None
    if not isinstance(document, dict):
        raise PackageError([f"{MANIFEST}: must be a JSON object"])
    return document


def _paths(
    manifest: Mapping[str, object], keys: Iterable[str], problems: list[str]
) -> list[str]:
    """Return the plugin-relative paths under `keys`; each takes one path or a list."""
    paths: list[str] = []
    for key in keys:
        value = manifest.get(key)
        for item in value if isinstance(value, list) else [value]:
            # Inline objects, the plugin root and remote bundles name no packaged file.
            if not isinstance(item, str) or item == "." or SCHEME.match(item):
                continue
            if item.startswith("./"):
                paths.append(item[2:])
            else:
                problems.append(f"{MANIFEST}: {key} entry {item!r} must start with ./")
    return paths


def _with_parents(paths: Iterable[str]) -> frozenset[str]:
    found: set[str] = set()
    for path in paths:
        while path and path not in found:
            found.add(path)
            path = posixpath.dirname(path)
    return frozenset(found)


def _text(path: Path, relative: str, problems: list[str]) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        problems.append(f"{relative}: not valid UTF-8")
        return None


def select_paths(tracked: Iterable[str], agents: Iterable[str]) -> list[str]:
    """Return the tracked files the package ships, failing on an entry that matches none."""
    entries = (*SHIPPED, *agents)

    def under(path: str, entry: str) -> bool:
        return path == entry or path.startswith(entry + "/")

    chosen = sorted(
        path
        for path in tracked
        if path not in DEV_ONLY and any(under(path, entry) for entry in entries)
    )
    missing = [
        entry for entry in entries if not any(under(path, entry) for path in chosen)
    ]
    if missing:
        raise PackageError(f"{entry}: shipped but not tracked" for entry in missing)
    return chosen


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _mask_code(text: str) -> str:
    """Blank fenced blocks and code spans so only prose links are read; offsets stay put."""
    masked: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        if fence is None:
            opener = FENCE.match(line)
            if opener:
                fence = opener[1]
                masked.append(_blank(line))
            else:
                masked.append(CODE_SPAN.sub(lambda match: _blank(match[0]), line))
            continue
        closer = FENCE_CLOSE.fullmatch(line)
        if closer and closer[1][0] == fence[0] and len(closer[1]) >= len(fence):
            fence = None
        masked.append(_blank(line))
    return "".join(masked)


def _is_image(masked: str, close: int) -> bool:
    depth = 0
    for index in range(close, -1, -1):
        if index and masked[index - 1] == "\\":
            continue
        if masked[index] == "]":
            depth += 1
        elif masked[index] == "[":
            depth -= 1
            if not depth:
                return index > 0 and masked[index - 1] == "!"
    return False


def _destination(match: re.Match[str]) -> tuple[int, int]:
    group = next(
        index for index in range(1, match.re.groups + 1) if match[index] is not None
    )
    return match.span(group)


def _links(masked: str) -> list[Link]:
    links = [
        Link(*_destination(match), _is_image(masked, match.start()))
        for match in INLINE_TARGET.finditer(masked)
    ]
    links += [
        Link(*_destination(match), False) for match in REFERENCE_TARGET.finditer(masked)
    ]
    for tag in ANCHOR.finditer(masked):
        href = HREF.search(masked, tag.start(), tag.end())
        if href:
            links.append(Link(*_destination(href), False))
    return links


def _pictures_to_images(text: str, path: str, problems: list[str]) -> str:
    # The directory holds HTML image tags for review, and Markdown has no dark-mode source.
    pieces: list[str] = []
    last = 0
    for match in PICTURE.finditer(_mask_code(text)):
        img = IMG.search(text, match.start(), match.end())
        attributes = {
            name.lower(): html.unescape(value)
            for name, value in IMG_ATTRIBUTE.findall(img[0] if img else "")
        }
        if not attributes.get("src"):
            problems.append(f"{path}: <picture> has no <img src>")
            continue
        alt = re.sub(r"([\\\[\]])", r"\\\1", attributes.get("alt", ""))
        pieces += [text[last : match.start()], f"![{alt}]({attributes['src']})"]
        last = match.end()
    return "".join(pieces) + text[last:]


def rewrite_markdown(
    text: str,
    path: str,
    shipped: frozenset[str],
    tracked: frozenset[str],
    blob_base: str,
) -> str:
    """Point links at unshipped files to GitHub and fail on any that would dangle.

    `tracked` holds repository files and their parent directories. `shipped` holds the
    shipped files and only the directories whose every tracked file ships, so a link to a
    partly shipped directory goes to GitHub instead of the truncated copy.
    """
    problems: list[str] = []
    text = _pictures_to_images(text, path, problems)
    masked = _mask_code(text)
    if HTML_IMAGE.search(masked):
        problems.append(f"{path}: HTML image tag; the directory holds these for review")
    edits: list[tuple[int, int, str]] = []
    for link in _links(masked):
        target = text[link.start : link.end]
        if not target or SCHEME.match(target) or target.startswith("#"):
            continue
        cut = min(
            (target.find(mark) for mark in "#?" if mark in target), default=len(target)
        )
        relative, suffix = unquote(target[:cut]), target[cut:]
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), relative))
        if target.startswith("/") or resolved == "." or resolved.split("/")[0] == "..":
            problems.append(f"{path}: link {target} leaves the repository")
        elif resolved in shipped:
            continue
        elif link.image:
            # A GitHub blob URL serves a page, not the image, so the image must ship.
            problems.append(f"{path}: image {target} is not in the package")
        elif resolved not in tracked:
            problems.append(f"{path}: link {target} points at no tracked file")
        else:
            edits.append((link.start, link.end, blob_base + quote(resolved) + suffix))
    if problems:
        raise PackageError(problems)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _tracked_files(source: Path) -> list[str]:
    output = run_command(["git", "ls-files", "-z"], source)
    return [path for path in output.split("\0") if path]


def build(source: Path, out: Path, ref: str = "main") -> PackageReport:
    """Copy the shipped files of `source` into `out`, rewrite Markdown links, and check.

    Links to files left out of the package point at `ref` on GitHub; `publish` passes its tag
    so a packaged version links to the same revision it was built from.
    """
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PackageError([f"{out}: output must be a new or empty directory"])
    manifest = _manifest(source / MANIFEST)
    repository = manifest.get("repository")
    if not isinstance(repository, str) or not repository.startswith("https://"):
        raise PackageError([f"{MANIFEST}: repository must be an https URL"])
    problems: list[str] = []
    agents = _paths(manifest, ("agents",), problems)
    if problems:
        raise PackageError(problems)
    tracked = _tracked_files(source)
    selection = select_paths(tracked, agents)
    selected = frozenset(selection)
    partial = _with_parents(
        posixpath.dirname(path) for path in tracked if path not in selected
    )
    shipped, known = _with_parents(selection) - partial, _with_parents(tracked)
    blob_base = repository.rstrip("/") + "/blob/" + quote(ref, safe="") + "/"
    for relative in selection:
        origin, target = source / relative, out / relative
        if origin.is_symlink():
            problems.append(f"{relative}: symlinks are not shipped")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not relative.endswith(".md"):
            shutil.copy2(origin, target)
            continue
        text = _text(origin, relative, problems)
        if text is None:
            continue
        try:
            text = rewrite_markdown(text, relative, shipped, known, blob_base)
        except PackageError as error:
            problems += error.problems
            continue
        target.write_bytes(text.encode("utf-8"))
        shutil.copymode(origin, target)
    if problems:
        raise PackageError(problems)
    return check(out)


def _frontmatter_problems(
    relative: str, text: str, allowed: frozenset[str] | None
) -> list[str]:
    end = text.find("\n---\n", 3)
    if not text.startswith("---\n") or end < 0:
        return [f"{relative}: no frontmatter block"]
    problems: list[str] = []
    for line in text[4:end].split("\n"):
        match = FRONTMATTER_LINE.fullmatch(line)
        if not match:
            problems.append(
                f"{relative}: frontmatter line {line!r} is not `key: value`"
            )
            continue
        key, value = match.groups()
        if allowed is not None and key not in allowed:
            problems.append(
                f"{relative}: frontmatter key {key} is outside the allowed list"
            )
        if value.startswith('"'):
            try:
                quoted = isinstance(json.loads(value), str)
            except json.JSONDecodeError:
                quoted = False
        elif value.startswith("'"):
            quoted = SINGLE_QUOTED.fullmatch(value) is not None
        else:
            quoted = ":" not in value and "#" not in value
        if not quoted:
            problems.append(f"{relative}: frontmatter value of {key} needs quoting")
    return problems


def _bound_names(tree: ast.Module) -> frozenset[str]:
    # Over-approximates a module's names; it only has to tell a missing submodule
    # from an attribute that `from package import name` reads. "*" marks a star import.
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return frozenset(names)


def _import_problems(package: Path, files: Iterable[str]) -> list[str]:
    """Name every shipped import of a `gauntlet` module the package leaves out."""
    problems: list[str] = []
    shipped = frozenset(files)
    trees: dict[str, ast.Module] = {}
    for relative in sorted(shipped):
        if relative.startswith("scripts/") and relative.endswith(".py"):
            text = _text(package / relative, relative, problems)
            try:
                if text is not None:
                    trees[relative] = ast.parse(text)
            except SyntaxError as error:
                problems.append(f"{relative}:{error.lineno}: not valid Python")
    bound: dict[str, frozenset[str]] = {}

    def module_file(module: str) -> str | None:
        base = "scripts/" + module.replace(".", "/")
        return next(
            (p for p in (f"{base}.py", f"{base}/__init__.py") if p in shipped), None
        )

    def binds(relative: str, name: str) -> bool:
        if relative not in trees:
            return True  # its own read problem is already reported
        if relative not in bound:
            bound[relative] = _bound_names(trees[relative])
        return name in bound[relative] or "*" in bound[relative]

    for relative, tree in trees.items():
        parents = relative.split("/")[1:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [(alias.name, ()) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    prefix = parents[: len(parents) - node.level + 1]
                    module = ".".join([*prefix, *filter(None, [node.module])])
                imports = [(module, tuple(alias.name for alias in node.names))]
            else:
                continue
            for module, names in imports:
                if module.split(".")[0] != "gauntlet":
                    continue
                source = module_file(module)
                missing = (
                    [module]
                    if source is None
                    else [
                        f"{module}.{name}"
                        for name in names
                        if module_file(f"{module}.{name}") is None
                        and not binds(source, name)
                    ]
                )
                problems += [
                    f"{relative}:{node.lineno}: imports {name}, which is not shipped"
                    for name in missing
                ]
    return problems


def check(package: Path) -> PackageReport:
    """Apply the directory's file checks that `claude plugin validate` cannot run in CI."""
    problems: list[str] = []
    sizes: dict[str, int] = {}
    for directory, folders, names in os.walk(package):
        for name in sorted([*folders, *names]):
            path = Path(directory, name)
            relative = path.relative_to(package).as_posix()
            if path.is_symlink():
                problems.append(f"{relative}: symlinks are not allowed")
            elif path.is_file():
                sizes[relative] = path.stat().st_size
    if len(sizes) > MAX_FILES:
        problems.append(
            f"{len(sizes)} files; the directory accepts at most {MAX_FILES}"
        )
    for relative, size in sorted(sizes.items()):
        if size > MAX_FILE_BYTES and relative not in OVERSIZE_EXEMPT:
            problems.append(f"{relative}: {size} bytes exceeds {MAX_FILE_BYTES}")
        if relative.startswith(FORBIDDEN_PREFIXES):
            problems.append(f"{relative}: development path in the package")
    manifest = _manifest(package / MANIFEST)
    for component in _paths(manifest, COMPONENT_KEYS, problems):
        normal = posixpath.normpath(component)
        if normal.split("/")[0] == ".." or not (package / normal).exists():
            problems.append(f"{MANIFEST}: {component} is not in the package")
    agents = set(_paths(manifest, ("agents",), []))
    for relative in sorted(sizes):
        is_skill = relative.startswith("skills/") and relative.endswith("/SKILL.md")
        if relative in agents or is_skill:
            text = _text(package / relative, relative, problems)
            allowed = AGENT_KEYS if relative in agents else None
            if text is not None:
                problems += _frontmatter_problems(relative, text, allowed)
    problems += _import_problems(package, sizes)
    if problems:
        raise PackageError(problems)
    largest = sorted(sizes.items(), key=lambda item: (-item[1], item[0]))[:5]
    return PackageReport(file_count=len(sizes), largest=tuple(largest))


def _release_head(repo: Path) -> ReleaseHead | None:
    branch = f"refs/heads/{RELEASE_BRANCH}"
    if not run_command(["git", "ls-remote", "origin", branch], repo).strip():
        return None
    run_command(["git", "fetch", "--no-tags", "origin", branch], repo)
    output = run_command(["git", "log", "-1", "--format=%H %T %s", "FETCH_HEAD"], repo)
    commit, tree, subject = output.rstrip("\n").split(" ", 2)
    # Only this command writes the branch, so another head means someone pushed by hand.
    match = RELEASE_SUBJECT.fullmatch(subject)
    if not match:
        raise InputError(
            f"{RELEASE_BRANCH} head {commit[:12]} is not a release commit: {subject!r}"
        )
    return ReleaseHead(commit, tree, tuple(int(part) for part in match.groups()))


def _package_tree(package: Path, repo: Path) -> str:
    git_dir = run_command(["git", "rev-parse", "--absolute-git-dir"], repo).strip()
    with tempfile.TemporaryDirectory(prefix="plugin-package-index-") as scratch:
        # A private index stages the package without touching the checkout or its index.
        index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        git = ["git", f"--git-dir={git_dir}", f"--work-tree={package}"]
        run_command([*git, "add", "--all", "--force", "--", "."], package, index)
        return run_command([*git, "write-tree"], package, index).strip()


def publish(tag: str, repo: Path) -> list[str]:
    """Build the package from `tag` and append it to the package branch as one commit."""
    validate_tag(tag)
    tagged = run_command(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], repo
    ).strip()
    # Fetch first: the release commit was pushed moments ago by semantic-release.
    run_command(["git", "fetch", "--quiet", "origin", "main"], repo)
    try:
        run_command(["git", "merge-base", "--is-ancestor", tagged, MAIN_REF], repo)
    except CommandFailure as error:
        raise InputError(
            f"{tag} is not on main; only released commits publish"
        ) from error
    with tempfile.TemporaryDirectory(prefix="plugin-package-") as scratch:
        source, package = Path(scratch, "source"), Path(scratch, "package")
        worktree = ["git", "worktree"]
        run_command([*worktree, "add", "--detach", str(source), tagged], repo)
        try:
            report = build(source, package, tag)
        finally:
            run_command([*worktree, "remove", "--force", str(source)], repo)
        version = _manifest(package / MANIFEST).get("version")
        if version != tag[1:]:
            raise PackageError([f"package version {version!r} does not match {tag}"])
        head = _release_head(repo)
        tree = _package_tree(package, repo)
    if head and head.tree == tree:
        return [*report.lines(), f"{RELEASE_BRANCH} already holds the {tag} package"]
    if head and head.version >= tuple(int(part) for part in tag[1:].split(".")):
        held = ".".join(map(str, head.version))
        raise InputError(f"{RELEASE_BRANCH} already holds v{held}; publish a newer tag")
    commit = run_command(
        [
            *("git", "commit-tree", tree),
            *(["-p", head.commit] if head else []),
            *("-m", f"release: {tag}"),
        ],
        repo,
    ).strip()
    # A plain push only fast-forwards, so the branch keeps one linear commit per release.
    run_command(
        ["git", "push", "origin", f"{commit}:refs/heads/{RELEASE_BRANCH}"], repo
    )
    return [*report.lines(), f"published {tag} to {RELEASE_BRANCH} as {commit}"]


def _parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("--source", type=Path, default=ROOT)
    build_command.add_argument("--out", type=Path, required=True)
    build_command.add_argument("--ref", default="main")
    check_command = commands.add_parser("check")
    check_command.add_argument("package", type=Path)
    publish_command = commands.add_parser("publish")
    publish_command.add_argument("--tag", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    def operation() -> int:
        args = _parser().parse_args(argv)
        if args.command == "publish":
            lines = publish(args.tag, ROOT)
        elif args.command == "build":
            lines = build(args.source, args.out, args.ref).lines()
        else:
            lines = check(args.package).lines()
        print("\n".join(lines))
        return 0

    return exit_code(operation)


if __name__ == "__main__":
    entry(main)
