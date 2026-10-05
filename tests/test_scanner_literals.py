"""This is a twin of plugin-scanner's hardcoded-secret and dynamic-execution rules;
the marketplace listing scan runs on the default branch with repository suppression
disabled; this guard exempts less, so anything that passes it passes the scanner;
a hit means reshape the literal and keep its runtime value.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

ROOT = Path(__file__).resolve().parents[1]
Rule = Literal["hardcoded-secret", "dynamic-execution"]


@dataclass(frozen=True, slots=True)
class Hit:
    path: str
    line: int
    rule: Rule


_Q = r"""[=:]\s*["']([^\s"']{8,})"""
GENERICS = tuple(
    re.compile(name + r"\s*" + _Q, re.I)
    for name in ("password", "secret", "token", "api_?key")
) + tuple(re.compile(name + r"\s*" + _Q) for name in ("API_KEY", "PRIVATE_KEY"))
_AWS = r"aws_secret_access_key\s*[=:]\s*[\"']?([A-Za-z0-9/+=]{40})"
PROVIDERS = tuple(
    re.compile(rx, flags)
    for rx, flags in (
        (_AWS, re.I),
        (r"AKIA[0-9A-Z]{16}", 0),
        *((rf"{p}_[A-Za-z0-9]{{36}}", 0) for p in ("ghp", "gho", "ghu", "ghs")),
        (r"github_pat_[A-Za-z0-9_]{20,}", 0),
        (r"glpat-[A-Za-z0-9\-]{20}", 0),
        (r"xox[bpas]-[A-Za-z0-9\-]{10,}", 0),
        (r"xox[er]-[A-Za-z0-9\-]{10,}", 0),
        (r"xapp-[A-Za-z0-9\-]{10,}", 0),
        (r"(?<![A-Za-z0-9])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}", 0),
    )
)
SECRETS = GENERICS + PROVIDERS
KEY_HEADER = re.compile(
    "-----BEGIN (?P<label>(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY)" + "-----"
)
DOC_EXTS = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".asciidoc"}
BINARY_EXTS = {
    *(".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2"),
    *(".ttf", ".eot", ".otf", ".zip", ".tar", ".gz", ".7z", ".rar"),
    *(".wasm", ".pyc", ".so", ".dylib"),
}
SECRET_SKIP_DIRS = {
    *("node_modules", ".git", "dist", ".next", "coverage", ".turbo"),
    *("__pycache__", ".venv", "venv"),
}
EVAL_SKIP_DIRS = SECRET_SKIP_DIRS - {".turbo"}
CODE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}
EVAL_RES = (re.compile(r"\beval\s*\("), re.compile(r"new\s+Function\s*\("))
EXAMPLE_DIRS = {
    *("docs", "doc", "skills", "skill", "prompts", "prompt"),
    *("instructions", "instruction", "examples", "example", "samples", "sample"),
    *("guides", "guide", "tutorials", "tutorial", "rules", "tests", "test"),
    *("__tests__", "fixtures", "fixture"),
}
TEST_FILE = re.compile(r"(?:^test_|\.test\.[^.]+$|\.spec\.[^.]+$|_test\.[^.]+$)", re.I)


def scan(rel: str, content: str) -> list[Hit]:
    path, hits = Path(rel), list[Hit]()
    suffix = path.suffix.lower()
    example = (
        suffix in DOC_EXTS
        or bool({p.lower() for p in path.parts} & EXAMPLE_DIRS)
        or bool(TEST_FILE.search(path.name))
    )
    if suffix not in BINARY_EXTS and not set(path.parts[:-1]) & SECRET_SKIP_DIRS:
        # Flag every header so no private-key body exemptions need mirroring.
        hits += [
            Hit(rel, content.count("\n", 0, m.start()) + 1, "hardcoded-secret")
            for m in KEY_HEADER.finditer(content)
        ]
        for rx in SECRETS:
            for m in rx.finditer(content):
                line = content.count("\n", 0, m.start()) + 1
                value = m.group(m.lastindex or 0)
                provider = rx in PROVIDERS or any(r.fullmatch(value) for r in PROVIDERS)
                context = "\n".join(content.splitlines()[max(0, line - 3) : line + 2])
                if example and (
                    (not provider and value.lower().startswith("example-"))
                    or (provider and re.search(r"\bexample\b", context, re.I))
                ):
                    continue
                hits.append(Hit(rel, line, "hardcoded-secret"))
    if path.suffix in CODE_EXTS and not set(path.parts) & EVAL_SKIP_DIRS:
        hits += [
            Hit(rel, content.count("\n", 0, m.start()) + 1, "dynamic-execution")
            for rx in EVAL_RES
            for m in rx.finditer(content)
        ]
    return hits


def test_tracked_files_have_no_scanner_hits() -> None:
    out = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"])
    hits = [
        h
        for rel in out.decode().split("\0")
        if rel and (ROOT / rel).is_file() and not (ROOT / rel).is_symlink()
        for h in scan(rel, (ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not hits, "Reshape the literal and keep its runtime value:\n" + "\n".join(
        f"{h.path}:{h.line} {h.rule}" for h in hits
    )


@pytest.mark.parametrize(
    ("rel", "content", "rule"),
    [
        ("pkg/mod.py", "token" + '="ordinary-value"', "hardcoded-secret"),
        ("tests/test_a.py", "ghp_" + "A" * 36, "hardcoded-secret"),
        ("pkg/mod.js", "ev" + "al(x)", "dynamic-execution"),
        ("tests/test_a.py", "api_key" + '="example-key"', None),
    ],
)
def test_strict_guard_sanity(rel: str, content: str, rule: Rule | None) -> None:
    assert [h.rule for h in scan(rel, content)] == ([rule] if rule else [])
