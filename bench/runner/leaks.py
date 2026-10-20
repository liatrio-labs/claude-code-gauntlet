"""Deterministic lexical diagnostics for agent-read text, with sealed-token redaction."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import secrets
import subprocess
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypedDict, cast

from bench.runner.epochs import (
    GoldenEpoch,
    canonical,
    digest,
    load_epoch,
    read_json,
)

MatchMode = Literal["identifier", "literal_path"]
Disposition = Literal["known_cue_remove_in_wave_2a", "generic_identifier", "delete"]
DERIVATION_VERSION = "symbol-spans-v1"
ROOT = Path(__file__).resolve().parents[2]
ALLOWANCE_CEILING_SHA256 = (
    "86e258eb15bad23d68a0b4eeea01e71671e54f725294b632172ba9769244b898"
)


class LeakIdentifierWire(TypedDict):
    identifier_id: str
    token: str
    match_mode: MatchMode
    epoch_id: str
    bug_ids: list[str]
    golden_text_sha256s: list[str]


class HoldoutLeakIdentifierWire(TypedDict):
    identifier_id: str
    match_mode: MatchMode
    epoch_id: str
    bug_ids: list[str]


class DerivedLeakIdentifiersWire(TypedDict):
    schema_version: Literal[2]
    epoch_id: str
    golden_sha256: str
    derivation_version: str
    holdout_salt: str
    identifiers: list[LeakIdentifierWire]
    holdout_identifiers: list[HoldoutLeakIdentifierWire]


class LeakAllowanceWire(TypedDict):
    identifier_id: str
    surface_id: str
    excerpt_sha256: str
    expected_count: int
    reason: str
    disposition: Disposition
    expires_wave: Literal["2a"]


class PassageWire(TypedDict):
    passage_id: str
    surface_id: str
    excerpt_sha256: str
    expected_count: int
    line_count: int
    disposition: Disposition
    reason: str
    expires_wave: Literal["2a"]


class LeakAllowlistWire(TypedDict):
    schema_version: Literal[2]
    epoch_id: str
    entries: list[LeakAllowanceWire]
    passages: list[PassageWire]


class LeakAllowanceCeilingWire(TypedDict):
    schema_version: Literal[2]
    epoch_id: str
    golden_sha256: str
    entry_digests: list[str]
    passages: list[PassageWire]


class PromptSurfaceWire(TypedDict):
    surface_id: str
    source_path: str
    producer_path: str
    scenario: str
    text: str


class LeakHitWire(TypedDict):
    identifier_id: str
    surface_id: str
    source_path: str
    line: int
    column: int
    excerpt: str | None
    excerpt_sha256: str
    holdout: bool
    allowed: bool


class LeakReportWire(TypedDict):
    schema_version: Literal[2]
    epoch_id: str
    golden_sha256: str
    scanned_surfaces: list[str]
    surfaces_sha256: str
    hits: list[LeakHitWire]
    unallowed_hits: list[LeakHitWire]
    stale_allowances: list[str]
    generation_coverage: list[str]
    passed: bool


@dataclass(frozen=True, slots=True)
class LeakIdentifier:
    identifier_id: str
    token: str
    match_mode: MatchMode
    epoch_id: str
    bug_ids: tuple[str, ...]
    golden_text_sha256s: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HoldoutLeakIdentifier:
    identifier_id: str
    match_mode: MatchMode
    epoch_id: str
    bug_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedLeakIdentifiers:
    epoch_id: str
    golden_sha256: str
    derivation_version: str
    holdout_salt: str
    identifiers: tuple[LeakIdentifier, ...]
    holdout_identifiers: tuple[HoldoutLeakIdentifier, ...]


@dataclass(frozen=True, slots=True)
class LeakAllowance:
    identifier_id: str
    surface_id: str
    excerpt_sha256: str
    expected_count: int
    reason: str
    disposition: Disposition
    expires_wave: Literal["2a"]


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
    generation_coverage: tuple[str, ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class _Span:
    mode: MatchMode
    token: str
    start: int
    end: int


# Maximal lexical spans prevent a short token from matching inside a longer symbol.
_SPANS = re.compile(
    r"(?<![A-Za-z0-9_$/.-])(?:"
    r"(?:[A-Za-z0-9_.$@+-]+/)+[A-Za-z0-9_.$@+-]+|"
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*"
    r")(?![A-Za-z0-9_$])"
)
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2}[a-z]")


def _symbol(token: str) -> bool:
    return len(token) >= 5 and (
        "." in token
        or "_" in token.strip("_")
        or bool(_CAMEL.search(token))
        or bool(re.fullmatch(r"[A-Z][A-Z0-9_]{4,}", token))
    )


def _spans(text: str) -> tuple[_Span, ...]:
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
        starts = [match.start()]
        for part in parts[:-1]:
            starts.append(starts[-1] + len(part) + 1)
        for i in range(len(parts)):
            for j in range(i + 1, len(parts) + 1):
                candidate = ".".join(parts[i:j])
                if _symbol(candidate):
                    spans.add(
                        _Span(
                            "identifier",
                            candidate,
                            starts[i],
                            starts[i] + len(candidate),
                        )
                    )
    return tuple(sorted(spans, key=lambda s: (s.start, s.end, s.mode, s.token)))


def _salt(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("holdout salt must encode 32 bytes")


def derive_identifiers(
    epoch: GoldenEpoch, *, holdout_pr_urls: frozenset[str], holdout_salt: str
) -> DerivedLeakIdentifiers:
    _salt(holdout_salt)
    if not holdout_pr_urls <= {b.pr_url for b in epoch.bugs}:
        raise ValueError("unknown sealed membership")
    sources = defaultdict(set)
    sealed = set()
    for bug in epoch.bugs:
        for span in _spans(bug.comment):
            key = (span.mode, span.token)
            sources[key].add(
                (bug.bug_id, hashlib.sha256(bug.comment.encode()).hexdigest())
            )
            if bug.pr_url in holdout_pr_urls:
                sealed.add(key)
    # A longer public chain must not serialize a sealed sub-chain as plaintext.
    hidden_text = tuple(token for _, token in sealed)
    sealed.update(
        key for key in sources if any(token in key[1] for token in hidden_text)
    )
    plain, hidden = [], []
    for (mode, token), refs in sorted(sources.items()):
        ids = tuple(sorted(b for b, _ in refs))
        if (mode, token) in sealed:
            hidden.append(
                HoldoutLeakIdentifier(
                    digest([holdout_salt, mode, token]), mode, epoch.epoch_id, ids
                )
            )
        else:
            plain.append(
                LeakIdentifier(
                    digest([mode, token]),
                    token,
                    mode,
                    epoch.epoch_id,
                    ids,
                    tuple(sorted({h for _, h in refs})),
                )
            )
    return DerivedLeakIdentifiers(
        epoch.epoch_id,
        epoch.golden_sha256,
        DERIVATION_VERSION,
        holdout_salt,
        tuple(sorted(plain, key=lambda x: x.identifier_id)),
        tuple(sorted(hidden, key=lambda x: x.identifier_id)),
    )


def identifiers_wire(derived: DerivedLeakIdentifiers) -> DerivedLeakIdentifiersWire:
    # JSON round-tripping converts immutable collections to the wire's lists.
    return cast(
        DerivedLeakIdentifiersWire,
        json.loads(canonical({"schema_version": 2, **asdict(derived)})),
    )


def validate_identifiers(stored: object, derived: DerivedLeakIdentifiers) -> None:
    if stored != identifiers_wire(derived):
        raise ValueError("altered identifier corpus; rederive from the named epoch")


def _denied(path: Path) -> bool:
    return any(
        p.startswith(".env")
        or p.lower()
        in {
            ".git",
            ".codex",
            ".aws",
            "auth.json",
            "pairing",
            "backups",
            "credentials",
            "credentials.json",
            "credential",
            "secrets",
            "secrets.json",
        }
        or p.endswith(".gpg")
        for p in path.parts
    )


def _safe(root: Path, path: Path) -> bool:
    resolved = path.resolve()
    return (
        path.is_relative_to(root)
        and resolved.is_relative_to(root)
        and not _denied(path.relative_to(root))
        and not _denied(resolved.relative_to(root))
    )


def _walk(root: Path, path: Path, ancestors: frozenset[Path] = frozenset()):
    if not _safe(root, path) or not path.exists():
        return
    if path.is_file():
        yield path
    elif path.is_dir() and path.resolve() not in ancestors:
        for child in sorted(path.iterdir()):
            yield from _walk(root, child, ancestors | {path.resolve()})


def load_generated(path: Path) -> tuple[PromptSurface, ...]:
    try:
        result = []
        for row in read_json(path):
            if set(row) != {
                "surface_id",
                "producer_path",
                "scenario",
                "text",
            } or not all(isinstance(x, str) for x in row.values()):
                raise ValueError
            result.append(PromptSurface(source_path=row["producer_path"], **row))
        return tuple(result)
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid generated prompt document") from None


def collect_surfaces(
    root: Path, generated_prompts: Sequence[PromptSurface]
) -> tuple[PromptSurface, ...]:
    root = root.resolve()
    surfaces = []
    paths = [
        root / "AGENTS.md",
        root / ".claude-plugin/plugin.json",
        root / "workflows/pipeline.js",
    ]
    for directory in ("agents", "skills", "workflows", "scripts"):
        for path in _walk(root, root / directory):
            rel = path.relative_to(root)
            if (
                path.name == "AGENTS.md"
                or (directory in {"agents", "skills"} and path.suffix == ".md")
                or (rel.parts[:2] == ("workflows", "src") and path.suffix == ".js")
                or (directory == "scripts" and path.suffix == ".py")
            ):
                paths.append(path)
    for path in sorted(set(paths)):
        if not _safe(root, path) or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                raise ValueError("unparseable prompt producer") from None
            positions: Counter[tuple[int, int]] = Counter()
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    position = (node.lineno, node.col_offset)
                    ordinal = positions[position]
                    positions[position] += 1
                    # Python 3.10 gives f-string pieces the same source position.
                    suffix = f"#{ordinal}" if ordinal else ""
                    surfaces.append(
                        PromptSurface(
                            f"{relative}#L{node.lineno}C{node.col_offset + 1}{suffix}",
                            relative,
                            relative,
                            "python_literal",
                            node.value,
                            node.lineno,
                            node.col_offset + 1,
                        )
                    )
        else:
            surfaces.append(PromptSurface(relative, relative, relative, "static", text))
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


@lru_cache(maxsize=1)
def _discovery_agents(registry_sha256: str) -> tuple[str, ...]:
    # Read the public roster so a new dimension needs only its owning registry edit.
    try:
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import { agentSpecs } from './workflows/src/discover.js'; process.stdout.write(JSON.stringify(agentSpecs().map(s => s.agentType)));",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        agents = json.loads(result.stdout)
        if (
            not isinstance(agents, list)
            or not agents
            or not all(
                isinstance(a, str) and a.startswith("code-gauntlet:") for a in agents
            )
        ):
            raise ValueError
        return tuple(agents)
    except (ValueError, subprocess.SubprocessError, OSError):
        raise ValueError("discovery roster unavailable") from None


def _required_generation() -> dict[str, str]:
    result = {}
    agents = _discovery_agents(
        hashlib.sha256((ROOT / "workflows/src/registry.js").read_bytes()).hexdigest()
    )
    for scenario in ("legacy", "derived", "bucketed", "fallback"):
        labels = {a: "discover" for a in agents}
        labels.update(
            {"verify-slice-0": "verifyStage", "validate-batch-0": "reviewStages"}
        )
        labels.update({f"challenge-{i}": "reviewStages" for i in range(6)})
        if scenario == "bucketed":
            labels.update(
                {
                    "summarize-bucket-0": "discover",
                    "summarize-bucket-1": "discover",
                    "summarize-merge": "discover",
                }
            )
        else:
            labels["summarize"] = "discover"
            labels["artifact-writer"] = "persist"
        if scenario == "derived":
            labels["assemble-artifacts"] = "persist"
        for label, producer in labels.items():
            result[f"generated/{scenario}/{label}/0"] = f"workflows/src/{producer}.js"
    return result


def _passage_count(
    identifiers: DerivedLeakIdentifiers,
    surface: PromptSurface | None,
    passage: PassageWire,
) -> int:
    lines = surface.text.split("\n") if surface else []
    width = passage["line_count"]
    return sum(
        digest([identifiers.holdout_salt, "\n".join(lines[i : i + width])])
        == passage["excerpt_sha256"]
        for i in range(len(lines) - width + 1)
    )


def _check_ceiling(
    identifiers: DerivedLeakIdentifiers,
    surfaces: Sequence[PromptSurface],
    allowlist: LeakAllowlistWire,
) -> None:
    ceiling: LeakAllowanceCeilingWire = read_json(
        ROOT / "bench/golden/leak_allowlist_ceiling.json"
    )
    if digest(ceiling) != ALLOWANCE_CEILING_SHA256:
        raise ValueError("initial allowance receipt changed")
    if (identifiers.epoch_id, identifiers.golden_sha256) != (
        ceiling["epoch_id"],
        ceiling["golden_sha256"],
    ):
        return
    entries = allowlist["entries"] + allowlist.get("passages", [])
    if not {digest(row) for row in entries} <= set(ceiling["entry_digests"]):
        raise ValueError("allowance added or widened beyond initial receipt")
    by_id = {s.surface_id: s for s in surfaces}
    active = {digest(row) for row in allowlist.get("passages", [])}
    for passage in ceiling["passages"]:
        if (
            _passage_count(identifiers, by_id.get(passage["surface_id"]), passage)
            and digest(passage) not in active
        ):
            raise ValueError("known passage still requires its disposition")


def scan(
    identifiers: DerivedLeakIdentifiers,
    surfaces: Sequence[PromptSurface],
    allowlist: LeakAllowlistWire,
) -> LeakReport:
    _salt(identifiers.holdout_salt)
    if (
        allowlist.get("schema_version") != 2
        or allowlist.get("epoch_id") != identifiers.epoch_id
    ):
        raise ValueError("allowlist schema or epoch mismatch")
    _check_ceiling(identifiers, surfaces, allowlist)
    hidden = {x.identifier_id: x for x in identifiers.holdout_identifiers}
    plain = {(x.match_mode, x.token): x for x in identifiers.identifiers}
    entries = {}
    for row in allowlist["entries"]:
        if set(row) != set(LeakAllowanceWire.__annotations__):
            raise ValueError("invalid allowance fields")
        entry = LeakAllowance(**row)
        key = (entry.identifier_id, entry.surface_id, entry.excerpt_sha256)
        if (
            key in entries
            or type(entry.expected_count) is not int
            or entry.expected_count < 1
            or entry.expires_wave != "2a"
            or entry.disposition
            not in {"known_cue_remove_in_wave_2a", "generic_identifier", "delete"}
            or not entry.reason
        ):
            raise ValueError("invalid exact allowance")
        if entry.identifier_id in hidden and (
            entry.disposition != "delete" or entry.reason != "holdout_delete_only"
        ):
            raise ValueError("sealed allowance must be delete-only")
        entries[key] = entry
    hits = []
    for surface in sorted(surfaces, key=lambda s: s.surface_id):
        candidates = []
        redacted_lines = set()
        for span in _spans(surface.text):
            sealed_id = digest([identifiers.holdout_salt, span.mode, span.token])
            item = hidden.get(sealed_id) or plain.get((span.mode, span.token))
            if item is None:
                continue
            line = surface.text.count("\n", 0, span.start) + 1
            is_hidden = sealed_id in hidden
            if is_hidden:
                redacted_lines.add(line)
            candidates.append((span, item.identifier_id, line, is_hidden))
        lines = surface.text.split("\n")
        for span, identifier_id, line, is_hidden in candidates:
            excerpt = lines[line - 1]
            redact = line in redacted_lines
            fingerprint = (
                digest([identifiers.holdout_salt, excerpt])
                if redact
                else hashlib.sha256(excerpt.encode()).hexdigest()
            )
            hits.append(
                LeakHit(
                    identifier_id,
                    surface.surface_id,
                    surface.source_path,
                    surface.source_line + line - 1,
                    span.start
                    - surface.text.rfind("\n", 0, span.start)
                    + (surface.source_column - 1 if line == 1 else 0),
                    None if redact else excerpt,
                    fingerprint,
                    is_hidden,
                    False,
                )
            )
    counts = Counter((h.identifier_id, h.surface_id, h.excerpt_sha256) for h in hits)
    hits = [
        replace(
            h,
            allowed=(key := (h.identifier_id, h.surface_id, h.excerpt_sha256))
            in entries
            and counts[key] == entries[key].expected_count,
        )
        for h in hits
    ]
    stale = [
        digest(asdict(e)) for k, e in entries.items() if counts[k] != e.expected_count
    ]
    by_id = {s.surface_id: s for s in surfaces}
    for passage in allowlist.get("passages", []):
        if (
            set(passage) != set(PassageWire.__annotations__)
            or passage["disposition"]
            not in {"known_cue_remove_in_wave_2a", "generic_identifier", "delete"}
            or passage["expires_wave"] != "2a"
            or type(passage["line_count"]) is not int
            or passage["line_count"] < 1
            or type(passage["expected_count"]) is not int
            or passage["expected_count"] < 1
        ):
            raise ValueError("invalid passage disposition")
        count = _passage_count(identifiers, by_id.get(passage["surface_id"]), passage)
        if count != passage["expected_count"]:
            stale.append(digest(passage))
    required = _required_generation()
    missing = tuple(
        sorted(
            k
            for k, producer in required.items()
            if k not in by_id
            or by_id[k].producer_path != producer
            or by_id[k].source_path != producer
            or by_id[k].scenario != k.split("/")[1]
        )
    )
    unallowed = tuple(h for h in hits if not h.allowed)
    return LeakReport(
        2,
        identifiers.epoch_id,
        identifiers.golden_sha256,
        tuple(sorted(by_id)),
        digest([asdict(s) for s in sorted(surfaces, key=lambda s: s.surface_id)]),
        tuple(hits),
        unallowed,
        tuple(sorted(stale)),
        missing,
        not (unallowed or stale or missing),
    )


def assert_no_holdout_tokens(
    derived: DerivedLeakIdentifiers, epoch: GoldenEpoch, files: Sequence[Path]
) -> None:
    """Inspect bytes without allowing assertion rendering to disclose sealed input."""
    ids = {x.identifier_id for x in derived.holdout_identifiers}
    for bug in epoch.bugs:
        for span in _spans(bug.comment):
            identifier_id = digest([derived.holdout_salt, span.mode, span.token])
            if identifier_id in ids:
                for path in files:
                    if span.token.encode() in path.read_bytes():
                        location = path.as_posix()
                        if any(
                            s.token in location
                            for b in epoch.bugs
                            for s in _spans(b.comment)
                            if digest([derived.holdout_salt, s.mode, s.token]) in ids
                        ):
                            location = digest(location)
                        data = path.read_bytes()
                        line = data[: data.index(span.token.encode())].count(b"\n") + 1
                        raise ValueError(
                            f"sealed token {identifier_id} at {location}:{line}"
                        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--generated-prompts", type=Path)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--derive", action="store_true")
    parser.add_argument("--holdout-salt")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        corpus_path = root / "bench/golden/leak_identifiers.json"
        stored = read_json(corpus_path) if corpus_path.exists() else None
        salt = args.holdout_salt or (
            stored["holdout_salt"] if stored else secrets.token_hex(32)
        )
        if stored and salt != stored["holdout_salt"]:
            raise ValueError("persisted sealed salt cannot change")
        epoch = load_epoch(args.epoch, root=root / "bench/golden")
        membership = frozenset(read_json(root / "bench/golden/subsets.json")["holdout"])
        derived = derive_identifiers(
            epoch, holdout_pr_urls=membership, holdout_salt=salt
        )
        if args.derive:
            output = args.out or corpus_path
            output.write_bytes(canonical(identifiers_wire(derived)) + b"\n")
            return 0
        validate_identifiers(stored, derived)
        if args.generated_prompts is None:
            raise ValueError("generated prompts are required")
        generated = load_generated(args.generated_prompts)
        surfaces = collect_surfaces(root, generated)
        report = scan(
            derived,
            surfaces,
            read_json(args.allowlist or root / "bench/golden/leak_allowlist.json"),
        )
        if args.out:
            args.out.write_bytes(canonical(asdict(report)) + b"\n")
        print(
            f"LEAK_GUARD: {'PASS' if report.passed else 'FAIL'}; {len(report.hits)} hits; {len(report.unallowed_hits)} unallowed; {len(report.stale_allowances)} stale; {len(report.generation_coverage)} missing prompts"
        )
        return 0 if report.passed else 1
    except (ValueError, KeyError, TypeError, OSError):
        # Input-controlled exception messages can contain sealed text.
        print("LEAK_GUARD: invalid input (details redacted)")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
