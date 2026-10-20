"""Deterministic lexical diagnostics for agent-read text, with sealed-token redaction."""

from __future__ import annotations

import argparse
import builtins
import hashlib
import importlib
import json
import re
import subprocess
import sys
import tempfile
import warnings
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from bench.runner.epochs import (
    GoldenEpoch,
    canonical,
    digest,
    load_epoch,
    read_json,
)

MatchMode = Literal["identifier", "literal_path"]
ExpiringDisposition = Literal[
    "known_cue_remove_in_wave_2a", "generic_identifier", "delete"
]
Disposition = Literal[
    "known_cue_remove_in_wave_2a",
    "generic_identifier",
    "generic_vocabulary",
    "delete",
]
DERIVATION_VERSION = "distinctive-project-symbol-spans-v4"
ROOT = Path(__file__).resolve().parents[2]
ALLOWANCE_CEILING_SHA256 = (
    "53359fe8e425fcbe7bc455a16a5f76921499507291ba907e0ce588f3f5ee1ec4"
)
# These standard modules print output, run site customization, or launch a browser on import.
STDLIB_IMPORT_SIDE_EFFECTS = frozenset(
    {"__hello__", "__phello__", "antigravity", "site", "this"}
)
STDLIB_PLATFORM_MODULES = frozenset(
    {
        "_aix_support",
        "_crypt",
        "_curses",
        "_curses_panel",
        "_msi",
        "_osx_support",
        "_overlapped",
        "_posixshmem",
        "_posixsubprocess",
        "_scproxy",
        "_winapi",
        "_wmi",
        "curses",
        "crypt",
        "fcntl",
        "grp",
        "msvcrt",
        "msilib",
        "nis",
        "nt",
        "ossaudiodev",
        "posix",
        "pty",
        "pwd",
        "readline",
        "resource",
        "spwd",
        "syslog",
        "termios",
        "tty",
        "winerror",
        "winreg",
        "winsound",
    }
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


class LeakAllowanceBaseWire(TypedDict):
    identifier_id: str
    surface_id: str
    source_path: str
    excerpt_sha256: str
    expected_count: int
    reason: str


class LeakAllowanceWire(LeakAllowanceBaseWire):
    disposition: Disposition
    expires_wave: Literal["2a"]


class GenericVocabularyAllowanceWire(LeakAllowanceBaseWire):
    disposition: Literal["generic_vocabulary"]


class PassageWire(TypedDict):
    passage_id: str
    surface_id: str
    source_path: str
    excerpt_sha256: str
    expected_count: int
    line_count: int
    disposition: ExpiringDisposition
    reason: str
    expires_wave: Literal["2a"]


class LeakAllowlistWire(TypedDict):
    schema_version: Literal[2]
    epoch_id: str
    entries: list[LeakAllowanceWire | GenericVocabularyAllowanceWire]
    passages: list[PassageWire]


class CeilingEntryWire(TypedDict):
    identifier_id: str
    source_path: str
    excerpt_sha256: str
    expected_count: int
    disposition: Disposition
    reason: str


class CeilingPassageWire(CeilingEntryWire):
    line_count: int


class LeakAllowanceCeilingWire(TypedDict):
    schema_version: Literal[3]
    derivation_version: str
    epoch_id: str
    golden_sha256: str
    holdout_salt: str
    entries: list[CeilingEntryWire]
    passages: list[CeilingPassageWire]


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
    source_path: str
    excerpt_sha256: str
    expected_count: int
    reason: str
    disposition: Disposition
    expires_wave: Literal["2a"] | None = None


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


@lru_cache(maxsize=1)
def runtime_identifiers() -> frozenset[str]:
    """Ask the runtimes, including qualified standard members, instead of curating words."""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            """
const names = new Set(Object.getOwnPropertyNames(globalThis));
for (const name of [...names]) names.add(`globalThis.${name}`);
const standards = { Object, Array, String, Promise, Map, Set, JSON, Math };
for (const name of Object.getOwnPropertyNames(globalThis)) {
  const value = Object.getOwnPropertyDescriptor(globalThis, name).value;
  if (typeof value === 'function' && (value === Error || value.prototype instanceof Error)) standards[name] = value;
}
for (const [name, value] of Object.entries(standards)) {
  for (const [prefix, target] of [[name, value], [`${name}.prototype`, value.prototype]]) {
    if (!target) continue;
    for (const member of Object.getOwnPropertyNames(target)) {
      names.add(member);
      names.add(`${prefix}.${member}`);
      names.add(`${name}.${member}`);
      if (target === value.prototype) names.add(`prototype.${member}`);
    }
  }
}
process.stdout.write(JSON.stringify([...names].sort()));
""",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    names = json.loads(result.stdout)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("runtime identifier enumeration failed")
    python_names = set(dir(builtins)) | set(sys.stdlib_module_names)
    skipped_modules = STDLIB_IMPORT_SIDE_EFFECTS | STDLIB_PLATFORM_MODULES
    for module_name in sys.stdlib_module_names - skipped_modules:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001 - optional stdlib imports can fail by platform.
                continue
        python_names.update(
            f"{module_name}.{member}"
            for member in dir(module)
            if not member.startswith("_")
        )
    return frozenset(names) | frozenset(python_names)


def derive_identifiers(
    epoch: GoldenEpoch, *, holdout_pr_urls: frozenset[str], holdout_salt: str
) -> DerivedLeakIdentifiers:
    _salt(holdout_salt)
    if not holdout_pr_urls <= {b.pr_url for b in epoch.bugs}:
        raise ValueError("unknown sealed membership")
    # File stems identify projects across benchmark forks and PR variants.
    project_by_pr = {}
    for path in sorted((epoch.root / "golden_comments").glob("*.json")):
        if not _safe(epoch.root, path):
            raise ValueError("unconfined golden project source")
        for pr in read_json(path):
            url = pr["url"]
            if url in project_by_pr:
                raise ValueError("duplicate golden project membership")
            project_by_pr[url] = path.stem
    if any(b.pr_url not in project_by_pr for b in epoch.bugs):
        raise ValueError("missing golden project membership")
    sources: dict[tuple[MatchMode, str], set[tuple[str, str]]] = defaultdict(set)
    projects = defaultdict(set)
    excluded = runtime_identifiers()
    sealed = set()
    for bug in epoch.bugs:
        for span in _spans(bug.comment):
            key = (span.mode, span.token)
            if span.mode == "identifier" and span.token in excluded:
                continue
            projects[key].add(project_by_pr[bug.pr_url])
            sources[key].add(
                (bug.bug_id, hashlib.sha256(bug.comment.encode()).hexdigest())
            )
            if bug.pr_url in holdout_pr_urls:
                sealed.add(key)
    sources = {key: refs for key, refs in sources.items() if len(projects[key]) == 1}
    sealed.intersection_update(sources)
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


def _shared_context_surfaces(root: Path) -> tuple[PromptSurface, ...]:
    producer = "scripts/gauntlet/shared_context.py"
    entry = root / "scripts/write_shared_context.py"
    if not entry.exists():
        return ()
    # Check import targets before invoking the script; symlinks cannot expand the read set.
    if not _safe(root, entry) or any(
        not _safe(root, path) for path in (root / "scripts/gauntlet").rglob("*.py")
    ):
        raise ValueError("unconfined context producer")
    with tempfile.TemporaryDirectory(prefix="leak-context-") as directory:
        output = Path(directory)
        (output / "code-gauntlet-project-rules-abc1234.md").write_text(
            "Synthetic project rules.\n", encoding="utf-8"
        )
        (output / "code-gauntlet-diff-abc1234.patch").write_text(
            "Synthetic diff.\n", encoding="utf-8"
        )
        subprocess.run(
            [
                sys.executable,
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
            for prefix, path in (
                ("generated", f"workflows/src/{producer}.js"),
                ("generated-bundle", "workflows/pipeline.js"),
            ):
                for kind in ("prompt", "schema"):
                    result[f"{prefix}/{scenario}/{label}/0/{kind}"] = path
    result["generated/shared-context/0"] = "scripts/gauntlet/shared_context.py"
    return result


def _passage_count(
    identifiers: DerivedLeakIdentifiers,
    surface: PromptSurface | None,
    passage: PassageWire | CeilingPassageWire,
) -> int:
    lines = (
        surface.text.split("\n")
        if surface and surface.source_path == passage["source_path"]
        else []
    )
    width = passage["line_count"]
    return sum(
        digest([identifiers.holdout_salt, "\n".join(lines[i : i + width])])
        == passage["excerpt_sha256"]
        for i in range(len(lines) - width + 1)
    )


def load_ceiling(root: Path = ROOT) -> LeakAllowanceCeilingWire:
    path = root / "bench/golden/leak_allowlist_ceiling.json"
    if not _safe(root, path):
        raise ValueError("unconfined allowance ceiling")
    ceiling: LeakAllowanceCeilingWire = read_json(path)
    if (
        ceiling.get("schema_version") != 3
        or digest(ceiling) != ALLOWANCE_CEILING_SHA256
    ):
        raise ValueError(
            "allowance ceiling changed; re-issue and pin it in the same change"
        )
    _salt(ceiling["holdout_salt"])
    return ceiling


def _bound_key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return (
        row.get("identifier_id", "passage:" + row.get("passage_id", "")),
        row["excerpt_sha256"],
        row["disposition"],
        row["reason"],
        row["source_path"],
    )


def _bounds(rows: Sequence[Mapping[str, Any]]) -> Counter[tuple[str, ...]]:
    counts: Counter[tuple[str, ...]] = Counter()
    for row in rows:
        if type(row["expected_count"]) is not int or row["expected_count"] < 1:
            raise ValueError("invalid ceiling count")
        counts[_bound_key(row)] += row["expected_count"]
    return counts


def _committed_allowlists(
    root: Path, ceiling: LeakAllowanceCeilingWire
) -> tuple[LeakAllowlistWire, ...]:
    """A removed or reduced committed allowance stays retired until an epoch re-issue."""
    paths = [
        "bench/golden/leak_allowlist.json",
        "bench/golden/leak_allowlist_ceiling.json",
    ]
    history = subprocess.run(
        ["git", "log", "--format=%H", "--", *paths],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.splitlines()
    accepted = []
    boundary_seen = False
    for revision in history:
        receipts = []
        for path in paths:
            result = subprocess.run(
                ["git", "show", f"{revision}:{path}"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.returncode:
                raise ValueError("incomplete allowance history")
            receipts.append(json.loads(result.stdout))
        previous, receipt = receipts
        if receipt.get("schema_version") != 3:
            boundary_seen = True
            break
        if (receipt["epoch_id"], receipt["golden_sha256"]) != (
            ceiling["epoch_id"],
            ceiling["golden_sha256"],
        ):
            boundary_seen = True
            break
        if receipt["holdout_salt"] != ceiling["holdout_salt"]:
            raise ValueError("persisted sealed salt cannot change within an epoch")
        if receipt.get("derivation_version") != ceiling["derivation_version"]:
            boundary_seen = True
            break
        accepted.append(previous)
    if not boundary_seen:
        raise ValueError(
            "incomplete allowance history; fetch history through the ceiling re-issue"
        )
    return tuple(accepted)


def _check_bounds(
    rows: Sequence[Mapping[str, Any]],
    ceiling_rows: Sequence[Mapping[str, Any]],
    previous: Sequence[Sequence[Mapping[str, Any]]],
) -> None:
    limits = _bounds(ceiling_rows)
    historical = [_bounds(old) for old in previous]
    for key, count in _bounds(rows).items():
        if count > limits[key] or any(count > old[key] for old in historical):
            raise ValueError("allowance added, restored or widened beyond ceiling")


def _check_ceiling(
    identifiers: DerivedLeakIdentifiers,
    surfaces: Sequence[PromptSurface],
    allowlist: LeakAllowlistWire,
    ceiling: LeakAllowanceCeilingWire,
    previous: Sequence[LeakAllowlistWire] = (),
) -> None:
    if (
        identifiers.epoch_id,
        identifiers.golden_sha256,
        identifiers.holdout_salt,
        identifiers.derivation_version,
    ) != (
        ceiling["epoch_id"],
        ceiling["golden_sha256"],
        ceiling["holdout_salt"],
        ceiling["derivation_version"],
    ):
        raise ValueError(
            "epoch, golden hash, salt or derivation mismatch; re-issue the allowance ceiling"
        )
    _check_bounds(
        allowlist["entries"], ceiling["entries"], [old["entries"] for old in previous]
    )
    _check_bounds(
        allowlist["passages"],
        ceiling["passages"],
        [old["passages"] for old in previous],
    )
    for passage in ceiling["passages"]:
        count = sum(
            _passage_count(identifiers, surface, passage)
            for surface in surfaces
            if surface.source_path == passage["source_path"]
        )
        if count > _bounds(allowlist.get("passages", []))[_bound_key(passage)]:
            raise ValueError("known passage still requires its disposition")


def scan(
    identifiers: DerivedLeakIdentifiers,
    surfaces: Sequence[PromptSurface],
    allowlist: LeakAllowlistWire,
) -> LeakReport:
    ceiling = load_ceiling()
    return _scan_with_ceiling(
        identifiers,
        surfaces,
        allowlist,
        ceiling=ceiling,
        previous=_committed_allowlists(ROOT, ceiling),
    )


def _scan_with_ceiling(
    identifiers: DerivedLeakIdentifiers,
    surfaces: Sequence[PromptSurface],
    allowlist: LeakAllowlistWire,
    *,
    ceiling: LeakAllowanceCeilingWire,
    previous: Sequence[LeakAllowlistWire] = (),
) -> LeakReport:
    _salt(identifiers.holdout_salt)
    if (
        allowlist.get("schema_version") != 2
        or allowlist.get("epoch_id") != identifiers.epoch_id
    ):
        raise ValueError("allowlist schema or epoch mismatch")
    _check_ceiling(identifiers, surfaces, allowlist, ceiling, previous)
    hidden = {x.identifier_id: x for x in identifiers.holdout_identifiers}
    plain = {(x.match_mode, x.token): x for x in identifiers.identifiers}
    entries = {}
    for row in allowlist["entries"]:
        entry = LeakAllowance(**row)
        expected_fields = set(LeakAllowanceBaseWire.__annotations__) | {"disposition"}
        if entry.disposition == "generic_vocabulary":
            if "expires_wave" in row:
                expected_fields.add("expires_wave")
        else:
            expected_fields.add("expires_wave")
        key = (entry.identifier_id, entry.surface_id, entry.excerpt_sha256)
        if (
            set(row) != expected_fields
            or (entry.disposition == "generic_vocabulary" and "expires_wave" in row)
            or (
                entry.disposition != "generic_vocabulary" and entry.expires_wave != "2a"
            )
            or key in entries
            or type(entry.expected_count) is not int
            or entry.expected_count < 1
            or entry.disposition
            not in {
                "known_cue_remove_in_wave_2a",
                "generic_identifier",
                "generic_vocabulary",
                "delete",
            }
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
            and entries[key].source_path == h.source_path
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


def mechanism_outputs(root: Path, explicit: Sequence[Path] = ()) -> tuple[Path, ...]:
    root = root.resolve()
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "**/leak_*"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    files = {root / name.decode() for name in tracked if name}
    for target in explicit:
        path = target if target.is_absolute() else root / target
        if not _safe(root, path):
            raise ValueError("unconfined mechanism output")
        files.add(path)
    return tuple(sorted(path for path in files if _safe(root, path) and path.is_file()))


def assert_no_holdout_tokens(
    derived: DerivedLeakIdentifiers,
    epoch: GoldenEpoch,
    files: Sequence[Path],
    *,
    root: Path = ROOT,
) -> None:
    """Check lexical spans, with confined inputs and sealed diagnostics."""
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
            location = path.relative_to(root).as_posix()
            if any(
                digest([derived.holdout_salt, s.mode, s.token]) in ids
                for s in _spans(location)
            ):
                location = digest(location)
            line = text.count("\n", 0, span.start) + 1
            raise ValueError(f"sealed token {identifier_id} at {location}:{line}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--generated-prompts", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        ceiling = load_ceiling(root)
        epoch = load_epoch(args.epoch, root=root / "bench/golden")
        membership = frozenset(read_json(root / "bench/golden/subsets.json")["holdout"])
        derived = derive_identifiers(
            epoch, holdout_pr_urls=membership, holdout_salt=ceiling["holdout_salt"]
        )
        generated = load_generated(args.generated_prompts)
        surfaces = collect_surfaces(root, generated)
        allowlist_path = args.allowlist or root / "bench/golden/leak_allowlist.json"
        if not allowlist_path.is_absolute():
            allowlist_path = root / allowlist_path
        if not _safe(root, allowlist_path):
            raise ValueError("unconfined allowlist")
        allowlist = read_json(allowlist_path)
        report = _scan_with_ceiling(
            derived,
            surfaces,
            allowlist,
            ceiling=ceiling,
            previous=_committed_allowlists(root, ceiling),
        )
        if args.out:
            output = args.out if args.out.is_absolute() else root / args.out
            if not _safe(root, output):
                raise ValueError("unconfined mechanism output")
            output.write_bytes(canonical(asdict(report)) + b"\n")
        assert_no_holdout_tokens(
            derived,
            epoch,
            mechanism_outputs(root, [args.out] if args.out else []),
            root=root,
        )
        print(
            f"LEAK_GUARD: {'PASS' if report.passed else 'FAIL'}; {len(report.hits)} hits; {len(report.unallowed_hits)} unallowed; {len(report.stale_allowances)} stale; {len(report.generation_coverage)} missing prompts"
        )
        return 0 if report.passed else 1
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("LEAK_GUARD: invalid input (details redacted)")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
