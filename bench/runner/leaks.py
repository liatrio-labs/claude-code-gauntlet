"""Exact leak allowances and sealed lexical diagnostics over agent-read surfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner.decode import decode  # noqa: E402
from bench.runner.epochs import canonical, digest, load_epoch, read_json  # noqa: E402

MatchMode = Literal["identifier", "literal_path"]


def ordinary_vocabulary(token):
    return bool(
        re.fullmatch(
            r"JavaScript|TypeScript|Python|Java|Ruby|Kotlin|CSharp|POSIX|if/else|"
            r"NullPointerException|IllegalArgumentException|RuntimeException|IOException|"
            r"(?:Type|Value|Attribute|Key|Reference|Runtime|Syntax|NotImplemented|Assertion|Overflow|ZeroDivision)Error|"
            r"Exception|Error|Boolean|String|Object|Array|Promise|JSON|Math|Map|Set|RegExp|"
            r"time\.sleep|os\.path\.(?:join|exists|dirname|basename)|datetime\.now|"
            r"JSON\.(?:parse|stringify)|Object\.(?:keys|entries|values|assign)|Array\.(?:from|isArray)|"
            r"Promise\.(?:all|resolve|reject)|Number\.isNaN|console\.log|String\.prototype\.replace|Date\.now",
            token,
        )
    )


@dataclass(frozen=True, slots=True)
class LeakIdentifier:
    identifier_id: str
    token: str
    match_mode: MatchMode


@dataclass(frozen=True, slots=True)
class HoldoutLeakIdentifier:
    identifier_id: str
    match_mode: MatchMode


@dataclass(frozen=True, slots=True)
class DerivedLeakIdentifiers:
    epoch_id: str
    golden_sha256: str
    holdout_salt: str
    identifiers: tuple[LeakIdentifier, ...]
    holdout_identifiers: tuple[HoldoutLeakIdentifier, ...]


@dataclass(frozen=True, slots=True)
class PromptSurface:
    surface_id: str
    source_path: str
    producer_path: str
    scenario: str
    text: str
    source_line: int = 1
    source_column: int = 1


@dataclass(frozen=True, slots=True)
class LeakHit:
    identifier_id: str
    surface_id: str
    source_path: str
    line: int
    column: int
    excerpt: str | None
    excerpt_sha256: str
    holdout: bool
    allowed: bool


@dataclass(frozen=True, slots=True)
class LeakReport:
    schema_version: Literal[2]
    epoch_id: str
    golden_sha256: str
    scanned_surfaces: tuple[str, ...]
    surfaces_sha256: str
    hits: tuple[LeakHit, ...]
    unallowed_hits: tuple[LeakHit, ...]
    stale_allowances: tuple[str, ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class _Span:
    mode: MatchMode
    token: str
    start: int
    end: int


_SPANS = re.compile(
    r"(?<![A-Za-z0-9_$/.-])(?:(?:[A-Za-z0-9_.$@+-]+/)+[A-Za-z0-9_.$@+-]+|[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)(?![A-Za-z0-9_$])"
)
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")


def _symbol(token: str) -> bool:
    return len(token) >= 5 and (
        "." in token
        or "_" in token.strip("_")
        or bool(_CAMEL.search(token))
        or bool(re.fullmatch(r"[A-Z][A-Z0-9_]{4,}", token))
    )


def _spans(text):
    spans = set()
    for match in _SPANS.finditer(text):
        token = match.group().rstrip(".")
        if "/" in token:
            if len(token) >= 5:
                spans.add(
                    _Span(
                        "literal_path", token, match.start(), match.start() + len(token)
                    )
                )
            continue
        parts = token.split(".")
        for i in range(len(parts)):
            start = match.start() + sum(len(p) + 1 for p in parts[:i])
            for j in range(i + 1, len(parts) + 1):
                candidate = ".".join(parts[i:j])
                if _symbol(candidate):
                    spans.add(
                        _Span("identifier", candidate, start, start + len(candidate))
                    )
    return tuple(sorted(spans, key=lambda s: (s.start, s.end, s.mode, s.token)))


def derive_identifiers(
    epoch, *, holdout_pr_urls: frozenset[str], holdout_salt: str
) -> DerivedLeakIdentifiers:
    if not re.fullmatch(r"[0-9a-f]{64}", holdout_salt):
        raise ValueError("holdout salt must encode 32 bytes")
    if holdout_pr_urls != frozenset(epoch.subsets["holdout"]):
        raise ValueError("unknown sealed membership")
    project_by_pr = epoch.project_by_pr
    if any(b.pr_url not in project_by_pr for b in epoch.bugs):
        raise ValueError("missing golden project membership")
    projects = defaultdict(set)
    sealed = set()
    for bug in epoch.bugs:
        for span in _spans(bug.comment):
            key = (span.mode, span.token)
            if ordinary_vocabulary(span.token):
                continue
            projects[key].add(project_by_pr[bug.pr_url])
            if bug.pr_url in holdout_pr_urls:
                sealed.add(key)
    sources = {key for key, owners in projects.items() if len(owners) == 1}
    sealed.intersection_update(sources)
    hidden_text = tuple(token for _, token in sealed)
    sealed.update(
        key for key in sources if any(token in key[1] for token in hidden_text)
    )
    plain = tuple(
        LeakIdentifier(digest([mode, token]), token, mode)
        for mode, token in sorted(sources - sealed)
    )
    hidden = tuple(
        HoldoutLeakIdentifier(digest([holdout_salt, mode, token]), mode)
        for mode, token in sorted(sealed)
    )
    return DerivedLeakIdentifiers(
        epoch.epoch_id, epoch.golden_sha256, holdout_salt, plain, hidden
    )


def _safe(root: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(root)


def _walk(root, path):
    paths = [path] if path.is_file() else path.rglob("*")
    return (p for p in paths if _safe(root, p) and p.is_file())


def collect_surfaces(root: Path, generated_prompts) -> tuple[PromptSurface, ...]:
    root = root.resolve()
    surfaces = []
    paths = [
        path
        for path in _walk(root, root / "agents")
        if path.parent == root / "agents"
        and path.suffix == ".md"
        and path.name != "AGENTS.md"
    ]
    paths.extend(path for path in _walk(root, root / "skills") if path.suffix == ".md")
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix()
        surfaces.append(
            PromptSurface(
                relative, relative, relative, "static", path.read_text(encoding="utf-8")
            )
        )
    surfaces.extend(_shared_context_surfaces(root))
    surfaces.extend(_workflow_literal_surfaces(root))
    for surface in generated_prompts:
        if (
            not _safe(root, root / surface.producer_path)
            or not (root / surface.producer_path).is_file()
        ):
            raise ValueError("missing or unconfined generated prompt producer")
        surfaces.append(surface)
    if len({s.surface_id for s in surfaces}) != len(surfaces):
        raise ValueError("duplicate prompt surface id")
    return tuple(sorted(surfaces, key=lambda s: s.surface_id))


def _workflow_literal_surfaces(root: Path) -> tuple[PromptSurface, ...]:
    modules = [
        {
            "path": path.relative_to(root).as_posix(),
            "text": path.read_text(encoding="utf-8"),
        }
        for path in _walk(root, root / "workflows/src")
        if path.parent == root / "workflows/src" and path.suffix == ".js"
    ]
    if not modules:
        return ()
    result = subprocess.run(
        [
            "node",
            "--expose-internals",
            str(ROOT / "workflows/test/tools/emit_workflow_literals.mjs"),
        ],
        input=json.dumps(modules),
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return tuple(PromptSurface(**row) for row in json.loads(result.stdout))


def _shared_context_surfaces(root: Path) -> tuple[PromptSurface, ...]:
    producer = "scripts/gauntlet/shared_context.py"
    entry = root / "scripts/write_shared_context.py"
    if not entry.exists():
        return ()
    if not _safe(root, entry) or any(
        not _safe(root, path) for path in (root / "scripts/gauntlet").rglob("*.py")
    ):
        raise ValueError("unconfined context producer")
    with tempfile.TemporaryDirectory(prefix="leak-context-") as directory:
        output = Path(directory)
        for name in ("project-rules-abc1234.md", "diff-abc1234.patch"):
            (output / ("code-gauntlet-" + name)).write_text(
                "Synthetic input.\n", encoding="utf-8"
            )
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import runpy,secrets,sys; secrets.token_hex=lambda n: 'a'*(2*n); "
                "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                str(entry),
                "--output-dir",
                directory,
                "--head-sha",
                "abc1234",
            ],
            input=b"Synthetic risk classification and code status.\n",
            capture_output=True,
            check=True,
        )
        text = (output / "code-gauntlet-context-abc1234.md").read_text(encoding="utf-8")
    return (
        PromptSurface(
            "generated/shared-context/0", producer, producer, "shared-context", text
        ),
    )


def generated_surfaces(root=ROOT):
    result = subprocess.run(
        ["node", "workflows/test/tools/emit_agent_prompts.mjs"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return tuple(
        decode(PromptSurface, {**r, "source_path": r["producer_path"]})
        for r in json.loads(result.stdout)
    )


def scan(identifiers, surfaces, allowlist):
    if (
        allowlist["schema_version"] != 3
        or allowlist["epoch_id"] != identifiers.epoch_id
        or allowlist["golden_sha256"] != identifiers.golden_sha256
        or allowlist["holdout_salt"] != identifiers.holdout_salt
    ):
        raise ValueError("allowance epoch/hash mismatch")
    wave = allowlist["current_wave"]
    waves = ("0a", "0b", "1", "2a", "2b", "3", "4", "closed")
    if wave not in waves:
        raise ValueError("invalid allowance wave")
    if waves.index(wave) >= waves.index("2a") and (
        allowlist["entries"]
        or any(p["expected_count"] != 0 for p in allowlist["passages"])
    ):
        raise ValueError(
            "identifier allowances must be empty and passage counts zero from Wave 2a"
        )
    plain = {(i.match_mode, i.token): i for i in identifiers.identifiers}
    hidden = {i.identifier_id: i for i in identifiers.holdout_identifiers}
    by_id = {s.surface_id: s for s in surfaces}
    if len(by_id) != len(surfaces):
        raise ValueError("duplicate surface")
    hits = []
    for surface in surfaces:
        spans = [
            (span, digest([identifiers.holdout_salt, span.mode, span.token]))
            for span in _spans(surface.text)
        ]
        candidates = [
            (span, hidden.get(sid) or plain.get((span.mode, span.token)), sid in hidden)
            for span, sid in spans
        ]
        candidates = [
            (span, item, sealed, surface.text.count("\n", 0, span.start))
            for span, item, sealed in candidates
            if item
        ]
        redacted = {line for _, _, sealed, line in candidates if sealed}
        lines = surface.text.split("\n")
        for span, item, sealed, line in candidates:
            text = lines[line]
            fingerprint = (
                digest([identifiers.holdout_salt, text])
                if line in redacted
                else hashlib.sha256(text.encode()).hexdigest()
            )
            hits.append(
                LeakHit(
                    item.identifier_id,
                    surface.surface_id,
                    surface.source_path,
                    surface.source_line + line,
                    span.start
                    - surface.text.rfind("\n", 0, span.start)
                    + (surface.source_column - 1 if line == 0 else 0),
                    None if line in redacted else text,
                    fingerprint,
                    sealed,
                    False,
                )
            )

    def key(row):
        return (
            row["identifier_id"],
            row["surface_id"],
            row["source_path"],
            row["excerpt_sha256"],
        )

    entries = {key(e): e for e in allowlist["entries"]}
    if len(entries) != len(allowlist["entries"]) or any(
        type(e["expected_count"]) is not int or e["expected_count"] <= 0
        for e in entries.values()
    ):
        raise ValueError("invalid exact allowance")
    counts = Counter(key(asdict(h)) for h in hits)
    hits = tuple(
        replace(
            h,
            allowed=not h.holdout
            and (k := key(asdict(h))) in entries
            and counts[k] == entries[k]["expected_count"],
        )
        for h in hits
    )
    stale = [digest(e) for k, e in entries.items() if counts[k] != e["expected_count"]]
    for passage in allowlist["passages"]:
        surface = by_id.get(passage["surface_id"])
        width, expected = passage["line_count"], passage["expected_count"]
        if (
            type(width) is not int
            or width < 1
            or type(expected) is not int
            or expected < 0
        ):
            raise ValueError("invalid passage allowance")
        lines = (
            surface.text.split("\n")
            if surface and surface.source_path == passage["source_path"]
            else []
        )
        count = sum(
            digest([identifiers.holdout_salt, "\n".join(lines[i : i + width])])
            == passage["excerpt_sha256"]
            for i in range(len(lines) - width + 1)
        )
        if count != expected:
            stale.append(digest(passage))
    unallowed = tuple(h for h in hits if not h.allowed)
    return LeakReport(
        2,
        identifiers.epoch_id,
        identifiers.golden_sha256,
        tuple(sorted(by_id)),
        digest([asdict(by_id[k]) for k in sorted(by_id)]),
        hits,
        unallowed,
        tuple(sorted(stale)),
        not (unallowed or stale),
    )


def scan_repository(root=ROOT):
    policy = read_json(root / "bench/golden/leak_allowlist.json")
    epoch = load_epoch(root=root / "bench/golden")
    derived = derive_identifiers(
        epoch,
        holdout_pr_urls=frozenset(epoch.subsets["holdout"]),
        holdout_salt=policy["holdout_salt"],
    )
    surfaces = collect_surfaces(root, generated_surfaces(root))
    report = scan(derived, surfaces, policy)
    for path in (root / "bench/golden").glob("leak_*.json"):
        assert_no_holdout_tokens(derived, epoch, [path], root=root)
    return report


def assert_no_holdout_tokens(derived, epoch, files, *, root: Path = ROOT) -> None:
    root = root.resolve()
    if any(not _safe(root, path) for path in files):
        raise ValueError("unconfined mechanism output")
    ids = {x.identifier_id for x in derived.holdout_identifiers}
    for path in files:
        text = path.read_text(encoding="utf-8")
        for span in _spans(text):
            identifier_id = digest([derived.holdout_salt, span.mode, span.token])
            if identifier_id not in ids:
                continue
            line = text.count("\n", 0, span.start) + 1
            raise ValueError(
                f"sealed token {identifier_id} in file {digest(str(path))}:{line}"
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = scan_repository()
    if args.out:
        args.out.write_bytes(canonical(asdict(report)) + b"\n")
    print(
        f"LEAK_GUARD: {'PASS' if report.passed else 'FAIL'}; {len(report.hits)} hits; {len(report.unallowed_hits)} unallowed; {len(report.stale_allowances)} stale"
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
