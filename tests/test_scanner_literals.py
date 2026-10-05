"""This guard mirrors plugin-scanner's hardcoded-secret and dynamic-execution rules;
the marketplace listing scan runs on the default branch with repository suppression
disabled; provider exemptions require synthetic payloads and nearby example context;
a hit means reshape the literal and keep its runtime value.
"""

import re
import subprocess
from dataclasses import dataclass
from itertools import pairwise
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
_AWS = r"aws_secret_access_key\s*[=:]\s*[\"']?(?P<payload>[A-Za-z0-9/+=]{40})"
PROVIDERS = tuple(
    re.compile(rx, flags)
    for rx, flags in (
        (_AWS, re.I),
        (r"AKIA(?P<payload>[0-9A-Z]{16})", 0),
        *(
            (rf"{p}_(?P<payload>[A-Za-z0-9]{{36}})", 0)
            for p in ("ghp", "gho", "ghu", "ghs")
        ),
        (r"github_pat_(?P<payload>[A-Za-z0-9_]{20,})", 0),
        (r"glpat-(?P<payload>[A-Za-z0-9\-]{20})", 0),
        (r"xox[bpas]-(?P<payload>[A-Za-z0-9\-]{10,})", 0),
        (r"xox[er]-(?P<payload>[A-Za-z0-9\-]{10,})", 0),
        (r"xapp-(?P<payload>[A-Za-z0-9\-]{10,})", 0),
        (r"(?<![A-Za-z0-9])sk-(?:proj-|ant-)?(?P<payload>[A-Za-z0-9_-]{20,})", 0),
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


def synthetic(payload: str) -> bool:
    payload = re.sub(r"[^a-z0-9]", "", payload.lower())
    longest = run = 1
    for previous, current in pairwise(payload):
        run = run + 1 if ord(current) == ord(previous) + 1 else 1
        longest = max(longest, run)
    return longest >= 8 and longest / len(payload) >= 0.6


def scan(rel: str, content: str, *, symlink: bool = False) -> list[Hit]:
    path, hits = Path(rel), list[Hit]()
    suffix = path.suffix.lower()
    example = (
        suffix in DOC_EXTS
        or bool({p.lower() for p in path.parts} & EXAMPLE_DIRS)
        or bool(TEST_FILE.search(path.name))
    )
    if (
        not symlink
        and suffix not in BINARY_EXTS
        and not set(path.parts[:-1]) & SECRET_SKIP_DIRS
    ):
        # Flag every header so no private-key body exemptions need mirroring.
        hits += [
            Hit(rel, content.count("\n", 0, m.start()) + 1, "hardcoded-secret")
            for m in KEY_HEADER.finditer(content)
        ]
        for rx in SECRETS:
            for m in rx.finditer(content):
                line = content.count("\n", 0, m.start()) + 1
                value = m.group(m.lastindex or 0)
                provider = (
                    m
                    if rx in PROVIDERS
                    else next((p for r in PROVIDERS if (p := r.fullmatch(value))), None)
                )
                context = "\n".join(content.splitlines()[max(0, line - 3) : line + 2])
                if example and (
                    (not provider and value.lower().startswith("example-"))
                    or (
                        provider
                        and synthetic(provider["payload"])
                        and re.search(r"\bexample\b", context, re.I)
                    )
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
    paths = [
        rel
        for rel in out.decode().split("\0")
        if rel
        and (ROOT / rel).is_file()
        and (ROOT / rel).resolve().is_relative_to(ROOT)
    ]
    hits = [
        h
        for rel in paths
        for h in scan(
            rel,
            (ROOT / rel).read_bytes().decode("utf-8", errors="ignore"),
            symlink=(ROOT / rel).is_symlink(),
        )
    ]
    assert "tests/test_scanner_literals.py" in paths
    assert not hits, "Reshape the literal and keep its runtime value:\n" + "\n".join(
        f"{h.path}:{h.line} {h.rule}" for h in hits
    )


@pytest.mark.parametrize(
    ("rel", "content", "rule"),
    [
        ("pkg/mod.py", "token" + '="ordinary-value"', "hardcoded-secret"),
        ("tests/test_a.py", "ghp_" + "A" * 36, "hardcoded-secret"),
        ("tests/test_a.py", "# example\n" + "ghp_" + "Az9" * 12, "hardcoded-secret"),
        (
            "tests/test_a.py",
            "# example\n" + "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "0123456789",
            None,
        ),
        ("pkg/mod.js", "ev" + "al(x)", "dynamic-execution"),
        ("tests/test_a.py", "api_key" + '="example-key"', None),
    ],
)
def test_strict_guard_sanity(rel: str, content: str, rule: Rule | None) -> None:
    assert [h.rule for h in scan(rel, content)] == ([rule] if rule else [])
