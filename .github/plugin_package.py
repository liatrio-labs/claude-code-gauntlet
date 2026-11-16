#!/usr/bin/env python3
"""Build, check and publish the runtime-only plugin package for Anthropic's directory.

Usage:
    python3 .github/plugin_package.py build --out DIR [--source DIR]
    python3 .github/plugin_package.py check DIR
    python3 .github/plugin_package.py publish --tag vX.Y.Z --package DIR

The directory installs only the folder it tracks and checks every file in it, so the release
job publishes this package to its own branch while marketplace users keep installing `main`.
"""

import argparse
import ast
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[1]
RELEASE_BRANCH = "plugin-release"
MANIFEST = ".claude-plugin/plugin.json"
TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")

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
# Maintainer tooling inside shipped directories. The generators need workflows/src and Node,
# the style hook serves this repository's own sessions, and only the benchmark decodes traces.
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
INLINE_TARGET = re.compile(r"\]\(\s*([^\s()<>]+)")
REFERENCE_TARGET = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*<?([^\s<>]+)", re.MULTILINE)
PICTURE = re.compile(r"<picture\b.*?</picture>", re.IGNORECASE | re.DOTALL)
IMG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMG_ATTRIBUTE = re.compile(r'\b(alt|src)="([^"]*)"', re.IGNORECASE)
HTML_IMAGE = re.compile(r"<(?:img|picture|source)\b", re.IGNORECASE)
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
FRONTMATTER_LINE = re.compile(r"([A-Za-z][\w-]*): (.+)")
SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'")


class InputError(Exception):
    """An argument or external response cannot describe a release."""


class PackageError(Exception):
    """The package cannot be built or fails a directory check."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True, slots=True)
class CommandFailure(Exception):
    argv: tuple[str, ...]
    returncode: int
    stderr: str


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


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def run_command(
    argv: list[str], cwd: Path, env: Mapping[str, str] | None = None
) -> str:
    """Run the only subprocess boundary and return stdout as text."""
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as error:
        raise CommandFailure(tuple(argv), 127, str(error)) from None
    if result.returncode:
        raise CommandFailure(tuple(argv), result.returncode, result.stderr.strip())
    return result.stdout


def _manifest(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError([f"{MANIFEST}: cannot read: {error}"]) from None
    if not isinstance(document, dict):
        raise PackageError([f"{MANIFEST}: must be a JSON object"])
    return document


def _paths(manifest: Mapping[str, object], *keys: str) -> list[str]:
    """Return the plugin-relative paths under `keys`; each takes one path or a list."""
    paths: list[str] = []
    for key in keys:
        value = manifest.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item.startswith("./"):
                paths.append(item[2:])
    return paths


def _with_parents(paths: Iterable[str]) -> frozenset[str]:
    found: set[str] = set()
    for path in paths:
        while path and path not in found:
            found.add(path)
            path = posixpath.dirname(path)
    return frozenset(found)


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

    `shipped` and `tracked` hold repository-relative files and their parent directories.
    """
    problems: list[str] = []
    text = _pictures_to_images(text, path, problems)
    masked = _mask_code(text)
    if HTML_IMAGE.search(masked):
        problems.append(f"{path}: HTML image tag; the directory holds these for review")
    targets = [
        (match.start(1), match.end(1), _is_image(masked, match.start()))
        for match in INLINE_TARGET.finditer(masked)
    ] + [
        (match.start(1), match.end(1), False)
        for match in REFERENCE_TARGET.finditer(masked)
    ]
    edits: list[tuple[int, int, str]] = []
    for start, end, image in targets:
        target = text[start:end]
        if SCHEME.match(target) or target.startswith("#"):
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
        elif image:
            # A GitHub blob URL serves a page, not the image, so the image must ship.
            problems.append(f"{path}: image {target} is not in the package")
        elif resolved not in tracked:
            problems.append(f"{path}: link {target} points at no tracked file")
        else:
            edits.append((start, end, blob_base + quote(resolved) + suffix))
    if problems:
        raise PackageError(problems)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _tracked_files(source: Path) -> list[str]:
    output = run_command(["git", "ls-files", "-z"], source)
    return [path for path in output.split("\0") if path]


def build(source: Path, out: Path) -> PackageReport:
    """Copy the shipped files of `source` into `out`, rewrite Markdown links, and check."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise PackageError([f"{out}: output must be a new or empty directory"])
    manifest = _manifest(source / MANIFEST)
    repository = manifest.get("repository")
    if not isinstance(repository, str) or not repository.startswith("https://"):
        raise PackageError([f"{MANIFEST}: repository must be an https URL"])
    tracked = _tracked_files(source)
    selection = select_paths(tracked, _paths(manifest, "agents"))
    shipped, known = _with_parents(selection), _with_parents(tracked)
    blob_base = repository.rstrip("/") + "/blob/main/"
    problems: list[str] = []
    for relative in selection:
        origin, target = source / relative, out / relative
        if origin.is_symlink():
            problems.append(f"{relative}: symlinks are not shipped")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".md"):
            try:
                text = rewrite_markdown(
                    origin.read_bytes().decode("utf-8"),
                    relative,
                    shipped,
                    known,
                    blob_base,
                )
            except PackageError as error:
                problems += error.problems
                continue
            target.write_bytes(text.encode("utf-8"))
            shutil.copymode(origin, target)
        else:
            shutil.copy2(origin, target)
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


def _module_file(scripts: Path, module: str) -> Path | None:
    base = scripts.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _bound_names(path: Path) -> frozenset[str]:
    # Over-approximates a module's names; it only has to tell a missing submodule
    # from an attribute that `from package import name` reads. "*" marks a star import.
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes().decode("utf-8"))):
        if isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return frozenset(names)


def _import_problems(package: Path, files: Iterable[str]) -> list[str]:
    """Name every shipped import of a `gauntlet` module the package leaves out."""
    scripts = package / "scripts"
    bound: dict[Path, frozenset[str]] = {}

    def binds(path: Path, name: str) -> bool:
        if path not in bound:
            bound[path] = _bound_names(path)
        return name in bound[path] or "*" in bound[path]

    problems: list[str] = []
    for relative in files:
        if not (relative.startswith("scripts/") and relative.endswith(".py")):
            continue
        tree = ast.parse((package / relative).read_bytes().decode("utf-8"))
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
                source = _module_file(scripts, module)
                missing = (
                    [module]
                    if source is None
                    else [
                        f"{module}.{name}"
                        for name in names
                        if _module_file(scripts, f"{module}.{name}") is None
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
    for component in _paths(manifest, *COMPONENT_KEYS):
        normal = posixpath.normpath(component)
        if normal.split("/")[0] == ".." or not (package / normal).exists():
            problems.append(f"{MANIFEST}: {component} is not in the package")
    agents = set(_paths(manifest, "agents"))
    for relative in sorted(sizes):
        is_skill = relative.startswith("skills/") and relative.endswith("/SKILL.md")
        if relative in agents or is_skill:
            text = (package / relative).read_bytes().decode("utf-8")
            allowed = AGENT_KEYS if relative in agents else None
            problems += _frontmatter_problems(relative, text, allowed)
    problems += _import_problems(package, sorted(sizes))
    if problems:
        raise PackageError(problems)
    largest = sorted(sizes.items(), key=lambda item: (-item[1], item[0]))[:5]
    return PackageReport(file_count=len(sizes), largest=tuple(largest))


def publish(tag: str, package: Path, repo: Path) -> str:
    """Commit `package` as the next release on the package branch and push it."""
    if not TAG_PATTERN.fullmatch(tag):
        raise InputError(f"malformed release tag: {tag!r}; expected vX.Y.Z")
    check(package)
    version = _manifest(package / MANIFEST).get("version")
    if version != tag[1:]:
        raise PackageError([f"package version {version!r} does not match {tag}"])
    branch = f"refs/heads/{RELEASE_BRANCH}"
    parent = None
    if run_command(["git", "ls-remote", "origin", branch], repo).strip():
        run_command(["git", "fetch", "--no-tags", "origin", branch], repo)
        parent = run_command(["git", "rev-parse", "FETCH_HEAD"], repo).strip()
    git_dir = run_command(["git", "rev-parse", "--absolute-git-dir"], repo).strip()
    with tempfile.TemporaryDirectory(prefix="plugin-package-") as scratch:
        # A private index stages the package without touching the checkout or its index.
        index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        git = ["git", f"--git-dir={git_dir}", f"--work-tree={package}"]
        run_command([*git, "add", "--all", "--force", "--", "."], package, index)
        tree = run_command([*git, "write-tree"], package, index).strip()
    if parent:
        current = run_command(["git", "rev-parse", f"{parent}^{{tree}}"], repo).strip()
        if current == tree:
            return f"{RELEASE_BRANCH} already holds the {tag} package"
    commit = run_command(
        [
            *("git", "commit-tree", tree),
            *(["-p", parent] if parent else []),
            *("-m", f"release: {tag}"),
        ],
        repo,
    ).strip()
    # A plain push only fast-forwards, so the branch keeps one linear commit per release.
    run_command(["git", "push", "origin", f"{commit}:{branch}"], repo)
    return f"published {tag} to {RELEASE_BRANCH} as {commit}"


def _parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("--source", type=Path, default=ROOT)
    build_command.add_argument("--out", type=Path, required=True)
    check_command = commands.add_parser("check")
    check_command.add_argument("package", type=Path)
    publish_command = commands.add_parser("publish")
    publish_command.add_argument("--tag", required=True)
    publish_command.add_argument("--package", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "publish":
            print(publish(args.tag, args.package, ROOT))
            return 0
        report = (
            build(args.source, args.out)
            if args.command == "build"
            else check(args.package)
        )
        print("\n".join(report.lines()))
        return 0
    except PackageError as error:
        for problem in error.problems:
            print(problem, file=sys.stderr)
        return 2
    except InputError as error:
        print(str(error).splitlines()[0], file=sys.stderr)
        return 2
    except CommandFailure as error:
        lines = [line.strip() for line in error.stderr.splitlines() if line.strip()]
        detail = f": {lines[-1]}" if lines else ""
        command = " ".join(error.argv)
        print(
            f"command failed ({error.returncode}): {command}{detail}", file=sys.stderr
        )
        return 2
    except OSError as error:
        print(f"file operation failed: {str(error).splitlines()[0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from gauntlet.cli import utf8_stdio

    utf8_stdio()
    raise SystemExit(main())
