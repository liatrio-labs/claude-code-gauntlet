"""Content-bound, evidence-only location replay over immutable saved artifacts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypedDict, cast
from unittest.mock import patch

from gauntlet import fix_tasks, location, patches
from gauntlet.delivery import gate, post
from gauntlet.delivery.input import validate_review_input
from gauntlet.diff import (
    AnnotatedDiff,
    ContextMapEntry,
    annotate_diff,
    parse_diff,
    patch_report_policy,
)
from gauntlet.forge import JsonFetch, Poster
from gauntlet.registry import LocationStatus
from gauntlet.verify import decide, wire

ADAPTER_VERSION = "evidence-lexical-v2"
ROOT = Path(__file__).resolve().parents[2]
ExtractionKind = Literal["backtick", "fenced", "code_like", "none"]
AnchorMatchKind = Literal[
    "matches_source",
    "no_anchor",
    "ellipsis",
    "flattened_multiline",
    "paraphrase_or_other_mismatch",
]
_IDENTIFIER = re.compile(r"[^\W\d]\w*", re.UNICODE)
_FENCE = re.compile(r"(?m)^(`{3,}|~{3,})[^\n]*\n")
_RUN = re.compile(r"`+")
_OPERATORS = re.compile(r"(?:\?\.|::|=>|===?|!==?|<=?|>=?|&&|\|\||[-+*/%&|^]=?|=|\?)")
_SHA = re.compile(r"[0-9a-f]{40}")


class InputError(ValueError):
    """Inputs cannot establish the frozen experiment's identity."""


class RecordKey(TypedDict):
    artifact: str
    artifact_index: int


class MeasuredRow(RecordKey):
    id: str
    file: str
    line_start: int
    line_end: int | None
    agent: str
    evidence: str | None
    source_mirror: str
    source_sha: str
    context_path: str
    output_path: str
    category: str
    context_in_range: bool
    outcome: str


class OracleSite(RecordKey):
    file: str
    source_sha: str
    expected_start: int
    head_text: str
    head_sha256: str


class Expectations(TypedDict):
    version: int
    measured_sha256: str
    previously_correct: list[OracleSite]
    hand_checked: list[OracleSite]
    canary: RecordKey
    fence: RecordKey


class FrozenSpan(RecordKey):
    evidence_sha256: str
    adapter_version: str
    extraction_kind: ExtractionKind
    offsets: list[int] | None


class ManifestHeader(TypedDict):
    adapter_version: str
    expectations_sha256: str
    input_sha256: dict[str, str]


class ReplayRecord(RecordKey):
    id: str
    file: str
    role: str
    old_start: int
    old_end: int | None
    new_start: int | None
    new_end: int | None
    oracle_start: int | None
    status: LocationStatus
    reason: location.ResolutionReason
    rule: str | None
    rule_b_only: bool
    saved_context_text: str | None
    chosen_head_text: str | None
    rule_b_agrees: bool | None
    anchor: str | None
    offsets: list[int] | None
    extraction_kind: ExtractionKind
    source_sha256: str
    map_sha256: str
    input_sha256: str
    oracle_sha256: str
    semantic_selected: bool
    inline: bool
    fence: bool
    task: bool


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    inline: frozenset[str]
    fences: frozenset[str]
    patches: frozenset[str]
    tasks: frozenset[str]
    violations: tuple[str, ...]


class AnchorDiagnosis(TypedDict):
    anchor_matches_any_source: bool
    anchor_matches_expected_source: bool
    matching_starts: list[int]
    classification: AnchorMatchKind


class MissedPositive(RecordKey):
    expected: int
    actual: int | None
    reason: location.ResolutionReason
    anchor: str | None
    diagnosis: AnchorDiagnosis


class ReplaySummary(TypedDict):
    records: int
    artifacts: int
    statuses: dict[LocationStatus, int]
    anchors_from_evidence: int
    extraction: dict[ExtractionKind, int]
    relocations_by_rule: dict[str | None, int]
    by_role_status_rule: dict[str, int]
    previously_correct: int
    unchanged_correct_sites: int
    previously_correct_became_unconfirmed: dict[ExtractionKind, int]
    known_recoveries: int
    missed_positives: list[MissedPositive]
    rule_b_only: int
    rule_b_disagreements: int
    range_eliminations: int
    false_moves: int
    fence_inspection: ReplayRecord
    violations: int
    records_sha256: str


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encoded(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(encoded(value))


def write_jsonl(path: Path, rows: list[Any]) -> None:
    path.write_bytes(
        b"".join(
            (json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n").encode()
            for row in rows
        )
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(content: bytes) -> Any:
    return json.loads(content, object_pairs_hook=_unique_object)


def safe_path(value: str, *, source: bool = False) -> Path:
    path = Path(value)
    parts = PurePosixPath(value).parts
    forbidden = {
        ".env",
        "auth.json",
        "pairing",
        ".codex",
        ".aws",
        ".ssh",
        ".gnupg",
        ".netrc",
        "credentials",
    }
    if (
        not value
        or "\0" in value
        or "\\" in value
        or ".." in parts
        or any(
            p in forbidden or p.startswith(".env.") or p.endswith(".gpg") for p in parts
        )
        or (
            source
            and (path.is_absolute() or ":" in parts[0] or "." in value.split("/"))
        )
    ):
        raise InputError(f"unsafe path: {value!r}")
    if not source:
        if not path.is_absolute():
            raise InputError(f"input path must be absolute: {value}")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise InputError(f"symlink input: {value}")
    return path


def key(row: Mapping[str, Any]) -> tuple[str, int]:
    artifact, index = row.get("artifact"), row.get("artifact_index")
    if not isinstance(artifact, str) or type(index) is not int or index < 0:
        raise InputError("missing artifact/index identity")
    safe_path(artifact)
    return artifact, index


def _eligible(text: str) -> bool:
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return (
        1 <= len(lines) <= 4
        and len(text) <= 400
        and sum(not c.isspace() for c in text) >= 12
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    start: int
    end: int
    kind: ExtractionKind


def _string_end(text: str, start: int) -> int:
    quote = text[start]
    at = start + 1
    while at < len(text):
        if text[at] == "\\":
            at += 2
        elif text[at] == quote:
            return at + 1
        elif text[at] == "\n":
            return start
        else:
            at += 1
    return start


def _group_end(text: str, start: int) -> int:
    stack = [text[start]]
    at = start + 1
    while at < len(text) and len(text[start:at]) <= 400:
        char = text[at]
        if char in "\"'":
            end = _string_end(text, at)
            if end == at:
                return at
            at = end
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if {"(": ")", "[": "]", "{": "}"}[stack[-1]] != char:
                return at
            stack.pop()
            if not stack:
                return at + 1
        at += 1
    # Incomplete expressions can still be exact evidence fragments.
    return at


def _expression_end(text: str, start: int) -> int:
    match = _IDENTIFIER.match(text, start)
    assert match is not None
    at = match.end()
    has_syntax = False
    while at < len(text):
        gap = at
        while gap < len(text) and text[gap] in " \t":
            gap += 1
        if gap < len(text) and text[gap] in "([{":
            end = _group_end(text, gap)
            if end == gap:
                break
            at, has_syntax = end, True
            continue
        if text[at : at + 1] == "." and (member := _IDENTIFIER.match(text, at + 1)):
            at, has_syntax = member.end(), True
            continue
        operator = _OPERATORS.match(text, gap)
        if operator:
            operand = operator.end()
            while operand < len(text) and text[operand] in " \t":
                operand += 1
            if member := _IDENTIFIER.match(text, operand):
                at, has_syntax = member.end(), True
                continue
            if operand < len(text) and text[operand] in "\"'":
                end = _string_end(text, operand)
                if end != operand:
                    at, has_syntax = end, True
                    continue
            number = re.match(r"\d+(?:\.\d+)?", text[operand:])
            if number:
                at, has_syntax = operand + number.end(), True
                continue
        break
    if has_syntax and text[at : at + 1] == ";":
        at += 1
    return at if has_syntax else start


def extract_quote(evidence: str) -> Candidate | None:
    """Choose one unchanged contiguous evidence slice, without consulting source."""
    candidates: list[Candidate] = []
    blocked: list[tuple[int, int]] = []
    cursor = 0
    while opener := _FENCE.search(evidence, cursor):
        run = opener[1]
        closer = re.compile(
            rf"(?m)^{re.escape(run[0])}{{{len(run)},}}[ \t]*(?:\n|$)"
        ).search(evidence, opener.end())
        end = closer.end() if closer else len(evidence)
        blocked.append((opener.start(), end))
        if closer:
            candidates.append(Candidate(opener.end(), closer.start(), "fenced"))
        cursor = end
    # Mask quotations instead of deleting them, preserving Unicode offsets.
    available = list(evidence)
    for start, end in blocked:
        available[start:end] = " " * (end - start)
    outside = "".join(available)
    runs = list(_RUN.finditer(outside))
    index = 0
    while index < len(runs):
        opener = runs[index]
        closer_index = next(
            (j for j in range(index + 1, len(runs)) if runs[j][0] == opener[0]),
            None,
        )
        if closer_index is None:
            blocked.append((opener.start(), len(evidence)))
            break
        closer = runs[closer_index]
        candidates.append(Candidate(opener.end(), closer.start(), "backtick"))
        blocked.append((opener.start(), closer.end()))
        index = closer_index + 1
    for start, end in blocked:
        available[start:end] = " " * (end - start)
    outside = "".join(available)
    for match in _IDENTIFIER.finditer(outside):
        end = _expression_end(outside, match.start())
        if end > match.start():
            candidates.append(Candidate(match.start(), end, "code_like"))
    # Adjacent complete lexical lines form one contiguous quote, including indent.
    code_lines: list[tuple[int, int]] = []
    offset = 0
    for line in outside.split("\n"):
        first = offset + len(line) - len(line.lstrip(" \t"))
        line_match = _IDENTIFIER.match(outside, first)
        if line_match is not None and _expression_end(outside, first) == offset + len(
            line.rstrip(" \t")
        ):
            code_lines.append((offset, offset + len(line)))
        else:
            code_lines.append((-1, -1))
        offset += len(line) + 1
    for index, (start, end) in enumerate(code_lines):
        if start < 0:
            continue
        candidates.append(Candidate(start, end, "code_like"))
        for next_start, next_end in code_lines[index + 1 : index + 4]:
            if next_start != end + 1:
                break
            end = next_end
            candidates.append(Candidate(start, end, "code_like"))
    eligible = [c for c in candidates if _eligible(evidence[c.start : c.end])]
    return min(eligible, key=lambda c: (c.start - c.end, c.start)) if eligible else None


@dataclass(frozen=True, slots=True)
class Inputs:
    map_dir: Path
    rows: tuple[MeasuredRow, ...]
    files: dict[str, bytes] = field(default_factory=dict, compare=False)

    def read(self, path: str | Path) -> bytes:
        name = str(path)
        if name not in self.files:
            file = safe_path(name)
            if not file.is_file():
                raise InputError(f"missing input: {name}")
            self.files[name] = file.read_bytes()
        return self.files[name]

    def json(self, path: str | Path) -> Any:
        return decode(self.read(path))

    @property
    def hashes(self) -> dict[str, str]:
        return {name: digest(content) for name, content in sorted(self.files.items())}


def load_inputs(map_dir: Path) -> Inputs:
    map_dir = safe_path(str(map_dir))
    measured = map_dir / "finding-evidence.jsonl"
    content = measured.read_bytes()
    rows = tuple(decode(line) for line in content.splitlines() if line)
    inputs = Inputs(map_dir, rows, {str(measured): content})
    inventory = [
        decode(line)
        for line in inputs.read(map_dir / "run-inventory.jsonl").splitlines()
    ]
    runs = {str(safe_path(run["path"])): run for run in inventory}
    if len(runs) != len(inventory):
        raise InputError("duplicate run inventory")
    seen = set()
    for row in rows:
        identity = key(row)
        if identity in seen:
            raise InputError(f"duplicate record key: {identity}")
        seen.add(identity)
        if row.get("evidence") is not None and not isinstance(row["evidence"], str):
            raise InputError(f"evidence must be text: {identity}")
        safe_path(row["file"], source=True)
        mirror = safe_path(row["source_mirror"])
        if not mirror.is_dir() or not _SHA.fullmatch(row["source_sha"]):
            raise InputError(f"missing source identity: {identity}")
        artifact = Path(row["artifact"])
        run_path = str(artifact.parent.parent)
        if run_path not in runs:
            raise InputError(f"artifact outside run inventory: {identity}")
        manifest = inputs.json(Path(run_path) / "run.json")
        identities = [
            item
            for item in manifest.get("pr_identities", {}).values()
            if f"pr-{item['owner']}-{item['repo']}-{item['pr_number']}"
            == artifact.parent.name
        ]
        if len(identities) != 1 or identities[0]["head_sha"] != row["source_sha"]:
            raise InputError(f"run/source identity mismatch: {identity}")
        original = inputs.json(artifact).get("findings", [])
        if row["artifact_index"] >= len(original):
            raise InputError(f"missing original record: {identity}")
        finding = original[row["artifact_index"]]
        if any(
            finding.get(k) != row.get(k)
            for k in ("id", "file", "line_start", "line_end")
        ):
            raise InputError(f"original record identity changed: {identity}")
        output = inputs.json(row["output_path"])
        if output.get("status") != "ok":
            raise InputError(f"missing saved Verify result: {identity}")
        saved_result = output.get("result", {})
        saved_records = [
            f for kind in ("verified", "eliminated") for f in saved_result.get(kind, [])
        ]
        original_ids = [f.get("id") for f in original]
        saved_ids = [f.get("id") for f in saved_records]
        if len(set(original_ids)) != len(original_ids) or Counter(
            original_ids
        ) != Counter(saved_ids):
            raise InputError(f"saved Verify membership changed: {identity}")
        inputs.read(row["context_path"])
        inputs.read(
            artifact.parent / f"code-gauntlet-diff-{row['source_sha'][:8]}.patch"
        )
        trace = artifact.parent / "code-gauntlet-measurement-trace.json"
        if trace.is_file():
            inputs.read(trace)
        else:
            inputs.read(
                artifact.parent
                / f"code-gauntlet-checkpoint-all-{row['source_sha'][:8]}.json"
            )
        delivery_path = (
            artifact.parent / f"code-gauntlet-post-review-{row['source_sha'][:8]}.json"
        )
        if delivery_path.is_file():
            inputs.read(delivery_path)
    for artifact in {row["artifact"] for row in rows}:
        indices = {row["artifact_index"] for row in rows if row["artifact"] == artifact}
        if indices != set(range(len(inputs.json(artifact)["findings"]))):
            raise InputError(f"incomplete artifact membership: {artifact}")
    for run_path, run in runs.items():
        artifacts = {
            r["artifact"]
            for r in rows
            if str(Path(r["artifact"]).parent.parent) == run_path
        }
        if len(artifacts) != run["inputs"]:
            raise InputError(f"inventory count changed: {run_path}")
    return inputs


def read_source(row: Mapping[str, Any]) -> bytes:
    safe_path(row["file"], source=True)
    mirror = safe_path(row["source_mirror"])
    sha = row["source_sha"]
    if not _SHA.fullmatch(sha):
        raise InputError("source needs a full revision")
    command = ["git", f"--git-dir={mirror}"]
    tree = subprocess.run(
        [*command, "ls-tree", "-z", sha, "--", f":(literal){row['file']}"],
        capture_output=True,
        check=True,
    ).stdout
    if (
        not tree.startswith((b"100644 blob ", b"100755 blob "))
        or tree.count(b"\0") != 1
    ):
        raise InputError(f"missing regular source blob: {row['file']}")
    blob = subprocess.run(
        [*command, "show", f"{sha}:{row['file']}"],
        capture_output=True,
        check=True,
    ).stdout
    if b"\0" in blob:
        raise InputError(f"binary source: {row['file']}")
    blob.decode("utf-8", "strict")
    return blob


def load_expectations(
    inputs: Inputs, path: Path, *, check_sources: bool
) -> Expectations:
    oracle = inputs.json(path)
    if oracle.get("version") != 1 or oracle.get("measured_sha256") != digest(
        inputs.read(inputs.map_dir / "finding-evidence.jsonl")
    ):
        raise InputError("oracle does not bind measured inputs")
    historical = {
        key(row): row for row in inputs.rows if row["category"] == "new-supported"
    }
    expected = {key(row): row for row in oracle["previously_correct"]}
    positives = {key(row): row for row in oracle["hand_checked"]}
    required = {
        key(row)
        for row in inputs.rows
        if row["category"] == "context-supported" and row["context_in_range"]
    }
    required.add(key(oracle["canary"]))
    if (
        len(expected) != len(oracle["previously_correct"])
        or len(positives) != len(oracle["hand_checked"])
        or expected.keys() != historical.keys()
        or positives.keys() != required
        or expected.keys() & positives.keys()
        or key(oracle["fence"]) not in positives
    ):
        raise InputError("oracle membership must be fixed, unique and disjoint")
    rows = {key(row): row for row in inputs.rows}
    for identity, site in {**expected, **positives}.items():
        row = rows.get(identity)
        if (
            row is None
            or site["file"] != row["file"]
            or site["source_sha"] != row["source_sha"]
            or not location.positive_safe_integer(site["expected_start"])
            or (identity in historical and site["expected_start"] != row["line_start"])
        ):
            raise InputError(f"oracle identity/start mismatch: {identity}")
        if check_sources:
            content = read_source(row)
            if (
                digest(content) != site["head_sha256"]
                or content.decode("utf-8") != site["head_text"]
            ):
                raise InputError(f"oracle source changed: {identity}")
            if site["expected_start"] > len(location.source_lines(site["head_text"])):
                raise InputError(f"oracle start outside source: {identity}")
    return cast(Expectations, oracle)


def freeze_quotes(inputs: Inputs, expectations: Path, destination: Path) -> None:
    load_expectations(inputs, expectations, check_sources=False)
    spans: list[FrozenSpan] = []
    for row in sorted(inputs.rows, key=key):
        evidence = row.get("evidence") or ""
        candidate = extract_quote(evidence)
        spans.append(
            {
                "artifact": row["artifact"],
                "artifact_index": row["artifact_index"],
                "evidence_sha256": digest(evidence.encode()),
                "adapter_version": ADAPTER_VERSION,
                "extraction_kind": candidate.kind if candidate else "none",
                "offsets": [candidate.start, candidate.end] if candidate else None,
            }
        )
    header: ManifestHeader = {
        "adapter_version": ADAPTER_VERSION,
        "expectations_sha256": digest(inputs.read(expectations)),
        "input_sha256": inputs.hashes,
    }
    # Exclusive creation prevents silently replacing a reviewed freeze.
    with destination.open("xb") as handle:
        handle.write(encoded(header).replace(b"\n", b"") + b"\n")
        for span in spans:
            handle.write((json.dumps(span, sort_keys=True) + "\n").encode())


def load_spans(
    inputs: Inputs, expectations: Path, path: Path
) -> dict[tuple[str, int], FrozenSpan]:
    documents = [
        decode(line) for line in safe_path(str(path)).read_bytes().splitlines()
    ]
    header, *spans = documents
    if (
        header.get("adapter_version") != ADAPTER_VERSION
        or header.get("expectations_sha256") != digest(inputs.read(expectations))
        or header.get("input_sha256") != inputs.hashes
    ):
        raise InputError("frozen input/oracle hashes changed")
    result = {key(span): span for span in spans}
    if len(result) != len(spans) or result.keys() != {key(row) for row in inputs.rows}:
        raise InputError("frozen quote keys changed")
    for row in inputs.rows:
        span = result[key(row)]
        evidence = row.get("evidence") or ""
        offsets = span.get("offsets")
        if span.get("adapter_version") != ADAPTER_VERSION or span.get(
            "evidence_sha256"
        ) != digest(evidence.encode()):
            raise InputError(f"frozen evidence changed: {key(row)}")
        candidate = extract_quote(evidence)
        if offsets is None:
            if span.get("extraction_kind") != "none":
                raise InputError("unavailable extraction must have kind none")
            continue
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(type(x) is not int for x in offsets)
            or not 0 <= offsets[0] < offsets[1] <= len(evidence)
            or candidate is None
            or offsets != [candidate.start, candidate.end]
            or span.get("extraction_kind") != candidate.kind
        ):
            raise InputError(f"frozen extraction differs from adapter: {key(row)}")
    return result


def reconstruct_map(
    raw_patch: bytes, context: bytes
) -> tuple[AnnotatedDiff, dict[int, ContextMapEntry]]:
    indexed = annotate_diff(raw_patch)
    raw_lines = raw_patch.decode("utf-8", "strict").replace("\r\n", "\n").split("\n")
    if raw_lines[-1] == "":
        raw_lines.pop()
    saved = context.decode("utf-8", "strict").split("\n")
    openings = [
        i
        for i, line in enumerate(saved)
        if line.startswith("<untrusted-code-content ")
        and i + 1 < len(saved)
        and saved[i + 1] == raw_lines[0]
    ]
    if len(openings) != 1:
        raise InputError("saved context has no unique diff frame")
    offset = openings[0] + 1
    for index, raw in enumerate(raw_lines):
        displayed = saved[offset + index] if offset + index < len(saved) else None
        if displayed != raw:
            raise InputError(
                f"saved context/patch disagreement at physical row {offset + index + 1}"
            )
    entries = {index + offset: entry for index, entry in indexed.entries.items()}
    return indexed, entries


def saved_head_row(context: bytes, claimed: object) -> str | None:
    """Audit physical saved rows without consulting the reconstructed map/walker."""
    if not location.positive_safe_integer(claimed):
        return None
    claimed = cast(int, claimed)
    lines = context.decode("utf-8", "strict").split("\n")
    if claimed > len(lines):
        return None
    raw = lines[claimed - 1]
    openings = [
        i
        for i, line in enumerate(lines[: claimed - 1])
        if line.startswith("<untrusted-code-content ")
    ]
    if not openings or not lines[openings[-1] + 1].startswith(("diff --git ", "--- ")):
        return None
    if any(
        line.startswith("</untrusted-code-content ")
        for line in lines[openings[-1] + 1 : claimed - 1]
    ):
        return None
    raw = re.sub(r"^[ \t]*[0-9]+(?:\t|\u2192)", "", raw, count=1)
    if generated := re.match(r"^N[1-9][0-9]* [+ ]\|", raw):
        return raw[generated.end() :]
    if raw.startswith(("+++ ", "--- ", "O")):
        return None
    return raw[1:] if raw.startswith(("+", " ")) else None


def _walk_findings(value: Any):
    if isinstance(value, dict):
        if all(k in value for k in ("id", "file", "line_start")):
            yield value
        else:
            for child in value.values():
                yield from _walk_findings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_findings(child)


def held_findings(
    inputs: Inputs, rows: list[MeasuredRow], spans: dict[tuple[str, int], FrozenSpan]
) -> list[dict[str, Any]]:
    parent = Path(rows[0]["artifact"]).parent
    trace_path = parent / "code-gauntlet-measurement-trace.json"
    rich: dict[tuple[Any, ...], dict[str, Any]] = {}
    if str(trace_path) in inputs.files:
        versions = inputs.json(trace_path)["versions"]
        found = (version["finding"] for version in versions)
    else:
        found = _walk_findings(
            inputs.json(
                parent
                / f"code-gauntlet-checkpoint-all-{rows[0]['source_sha'][:8]}.json"
            )
        )
    for finding in found:
        rich.setdefault(
            tuple(finding.get(k) for k in ("id", "file", "line_start")), finding
        )
    original = inputs.json(rows[0]["artifact"])["findings"]
    findings = []
    for row in rows:
        finding = dict(rich.get((row["id"], row["file"], row["line_start"]), {}))
        finding.update(original[row["artifact_index"]])
        # Saved prose remains authoritative; location metadata belongs to this run.
        for name in (
            "anchor",
            "location_status",
            "claimed_line_start",
            "claimed_line_end",
            "line",
            "end_line",
        ):
            finding.pop(name, None)
        finding["title"] = row.get("title") or finding.get("title") or row["id"]
        finding["evidence"] = row.get("evidence") or ""
        finding["agent"] = row["agent"]
        span = spans[key(row)]
        if span["offsets"] is not None:
            start, end = span["offsets"]
            finding["anchor"] = finding["evidence"][start:end]
        findings.append(finding)
    return findings


def verify_saved(argv: list[str] | None = None) -> int:
    """Replay the public CLI with captured revisions in read-only bare mirrors."""
    args = sys.argv[1:] if argv is None else argv
    mirror, sha, mutation, *command = args
    safe_path(mirror)
    if not _SHA.fullmatch(sha):
        raise InputError("invalid replay revision")
    source = location.SourceAdapter(Path(mirror), "branch", sha)
    with (
        patch.object(decide, "resolve_repo_root", return_value=mirror),
        patch.object(location.SourceAdapter, "capture", return_value=source),
        contextlib.ExitStack() as stack,
    ):
        if mutation == "bypass":

            def bypass(request, *_args):
                quotes = [
                    q
                    for q in location.anchor_candidates(request.get("anchor"))
                    if location.valid_quote(q)
                ]
                if not quotes or not location.positive_safe_integer(
                    request.get("line_start")
                ):
                    return location.Resolution(
                        None, None, "unconfirmed", "invalid_anchor"
                    )
                start = request["line_start"]
                return location.Resolution(
                    start, start + len(quotes[-1].lines) - 1, "confirmed", "claimed"
                )

            stack.enter_context(
                patch.object(decide, "resolve_location", side_effect=bypass)
            )
        return wire.CLI.invoke(command)


_JS_VERIFY = r"""
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [root, jobPath, output] = process.argv.slice(1);
const job = JSON.parse(readFileSync(jobPath, 'utf8'));
const { verifyStage } = await import(pathToFileURL(`${root}/workflows/src/verifyStage.js`));
const { shellSplit } = await import(pathToFileURL(`${root}/workflows/test/helpers/shellWords.js`));
const ctx = { agent: async (prompt) => {
  const command = shellSplit(prompt.split('\n').at(-1));
  execFileSync(command[0], ['-c',
    'from bench.runner.location_replay import verify_saved; raise SystemExit(verify_saved())',
    job.mirror, job.headShaShort, job.mutation, ...command.slice(2)], {
      cwd: job.mirror, env: { ...process.env, PYTHONPATH: `${root}:${root}/scripts` },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  return JSON.parse(readFileSync(command[command.indexOf('--output') + 1], 'utf8'));
} };
const result = await verifyStage(ctx, job);
if (!result.verified || result.inputProof.degradedFindings) throw Error(JSON.stringify(result));
const { writeFileSync } = await import('node:fs');
writeFileSync(output, JSON.stringify(result.findings));
"""


def replay_wire(
    findings: list[dict[str, Any]],
    row: MeasuredRow,
    directory: Path,
    map_path: Path,
    *,
    bypass: bool = False,
) -> list[dict[str, Any]]:
    job = {
        "findings": findings,
        "pluginRoot": str(ROOT),
        "python": "python",
        "headShaShort": row["source_sha"],
        "mirror": row["source_mirror"],
        "nonce": "frozen-replay",
        "limits": {"verifySliceSize": 25},
        "policy": {},
        "mutation": "bypass" if bypass else "normal",
        "verify": {
            "targetKind": "branch",
            "baseBranch": row["source_sha"],
            "diffPath": str(
                Path(row["artifact"]).parent
                / f"code-gauntlet-diff-{row['source_sha'][:8]}.patch"
            ),
            "contextMap": str(map_path),
            "contextFile": row["context_path"],
            "inputPathBase": str(directory / "wire-input"),
            "outputPathBase": str(directory / "wire-output"),
        },
    }
    write_json(directory / "job.json", job)
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            _JS_VERIFY,
            str(ROOT),
            str(directory / "job.json"),
            str(directory / "joined.json"),
        ],
        capture_output=True,
    )
    if result.returncode:
        raise InputError(
            f"Verify replay failed: {result.stderr.decode('utf-8', 'replace')}"
        )
    return cast(list[dict[str, Any]], decode((directory / "joined.json").read_bytes()))


class OfflineForge:
    platform: Literal["github"] = "github"

    def ensure_available(self) -> None:
        pass

    def poster(self) -> Poster:
        return Poster("offline", None)

    def diff(self, target):
        raise AssertionError("saved diff facts must be supplied")

    def review_entries(self, target) -> JsonFetch:
        return JsonFetch([], None)

    def submit(self, request):
        raise AssertionError("replay may only capture dry-run requests")


def replacement_safe(finding: Mapping[str, Any], texts: list[str]) -> bool:
    span = location.canonical_range(finding)
    start, end = finding.get("claimed_line_start"), finding.get("claimed_line_end")
    return (
        span is not None
        and location.positive_safe_integer(start)
        and location.positive_safe_integer(end)
        and cast(int, end) - cast(int, start) == span[1] - span[0]
        and location.anchor_matches_span(finding, texts, whole=True)
    )


def delivery(
    findings: list[dict[str, Any]], row: MeasuredRow, raw_patch: bytes, directory: Path
) -> DeliveryResult:
    text = raw_patch.decode("utf-8")
    facts = parse_diff(text, policy=patch_report_policy(text))
    data = validate_review_input(
        {
            "owner": "offline",
            "repo": "saved",
            "pr_number": 1,
            "sha": row["source_sha"],
            "findings": findings,
            "review_body": "Offline location replay.",
        }
    )
    session = post.DeliverySession(dry_run=True)
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        post.post_github(data, facts, forge=OfflineForge(), session=session)
    write_json(directory / "forge.json", session.dry_run_payload("github"))
    plan = post.plan_delivery(data, facts, platform="github")
    inline, fenced = set(), set()
    violations = []
    anchored = [group for group in plan.groups if isinstance(group, post.AnchoredGroup)]
    captured = cast(list[post.GitHubComment], session.captured[0].payload["comments"])
    if len(captured) != len(anchored):
        violations.append("inline_payload_count")
    for comment, group in zip(captured, anchored, strict=False):
        finding = group.primary.finding
        start = comment.get("start_line", comment.get("line"))
        end = comment.get("line")
        span = location.canonical_range(finding)
        texts = (
            [
                facts.line_texts.get((comment.get("path"), n), "")
                for n in range(start, end + 1)
            ]
            if location.positive_safe_integer(start)
            and location.positive_safe_integer(end)
            and 0 <= end - start < 4
            else []
        )
        if (
            span != (start, end)
            or comment.get("path") != group.primary.anchor.path
            or comment.get("side") != "RIGHT"
            or not location.anchor_matches_span(finding, texts)
        ):
            violations.append("unsafe_inline_payload")
        if re.search(
            r"(?m)^`{3,}suggestion(?:[:\n]|$)", comment["body"]
        ) and not replacement_safe(finding, texts):
            violations.append("unsafe_rendered_fence")
    for group_plan in plan.groups:
        if isinstance(group_plan, post.AnchoredGroup):
            primary = group_plan.primary
            inline.add(str(primary.finding["id"]))
            if (
                "suggested_fix_code" in primary.finding
                and primary.verdict is not None
                and primary.verdict.keep
            ):
                fenced.add(str(primary.finding["id"]))
    sha = row["source_sha"]
    write_json(directory / f"code-gauntlet-findings-{sha}.json", findings)
    (directory / f"code-gauntlet-diff-{sha}.patch").write_bytes(raw_patch)
    with contextlib.redirect_stderr(io.StringIO()):
        receipt, code = patches._execute(
            argparse.Namespace(output_dir=str(directory), head_sha=sha)
        )
    if code:
        raise InputError(f"patch receipt failed: {receipt}")
    if receipt["path"] is None:
        raise InputError("patch receipt has no output path")
    receipt["path"] = Path(receipt["path"]).name
    write_json(directory / "patch-receipt.json", receipt)
    notes: list[str] = []
    source = location.SourceAdapter(Path(row["source_mirror"]), "branch", sha)
    # Task generation must use the same captured source, without creating a checkout.
    with patch.object(location.SourceAdapter, "capture", return_value=source):
        tasks, rejected = fix_tasks.build_tasks(findings, row["source_mirror"], notes)
    write_json(
        directory / "tasks.json", {"tasks": tasks, "rejected": rejected, "notes": notes}
    )
    task_ids = {
        str(cast(dict[str, Any], t["metadata"])["review_context"]["finding_id"])
        for t in tasks
    }
    expected_tasks = {
        str(f["id"]) for f in findings if location.location_allows(f, "fixTask")
    }
    if task_ids != expected_tasks or len(tasks) != len(task_ids):
        violations.append("unsafe_fix_tasks")
    patch_ids = [
        str(f["id"])
        for f in findings
        if "suggested_fix_code" in f
        and location.location_allows(f, "patch")
        and gate.evaluate_fix(
            location.normalize_location(f),
            apply_range=location.canonical_range(f),
            facts=facts,
        ).keep
    ]
    if receipt["kept"] != len(patch_ids):
        violations.append("unsafe_patch_receipt")
    patch_text = (directory / receipt["path"]).read_text(encoding="utf-8")
    patch_sites = re.findall(r"(?m)^## `+([^`\n]+)`+:([0-9]+)-([0-9]+) ", patch_text)
    expected_sites = [
        (f["file"], str(f["line_start"]), str(f["line_end"]))
        for f in findings
        if str(f["id"]) in patch_ids
    ]
    if Counter(patch_sites) != Counter(expected_sites):
        violations.append("unsafe_patch_payload")
    write_json(
        directory / "decisions.json",
        {
            "inline": sorted(inline),
            "fence": sorted(fenced),
            "patch": patch_ids,
            "task": sorted(task_ids),
        },
    )
    return DeliveryResult(
        frozenset(inline),
        frozenset(fenced),
        frozenset(patch_ids),
        frozenset(task_ids),
        tuple(violations),
    )


def _rule(reason: str) -> str | None:
    return {"claimed": "a", "context_map": "b", "unique": "c", "unique_hunk": "d"}.get(
        reason
    )


def diagnose_anchor(
    finding: Mapping[str, Any], head: tuple[str, ...], expected: int
) -> AnchorDiagnosis:
    """Separate literal source support from coordinate resolution in replay misses."""
    quotes = [
        q
        for q in location.anchor_candidates(finding.get("anchor"))
        if location.valid_quote(q)
    ]
    starts = sorted(
        {
            start
            for quote in quotes
            for start in range(1, len(head) - len(quote.lines) + 2)
            if location.anchor_matches_span(
                {
                    **finding,
                    "line_start": start,
                    "line_end": start + len(quote.lines) - 1,
                    "location_status": "confirmed",
                },
                head[start - 1 : start + len(quote.lines) - 1],
            )
        }
    )
    classification: AnchorMatchKind
    if starts:
        classification = "matches_source"
    elif not quotes:
        classification = "no_anchor"
    elif any("..." in line or "\u2026" in line for q in quotes for line in q.lines):
        classification = "ellipsis"
    elif any(
        len(q.lines) == 1
        and " ".join(q.lines[0].split()) in " ".join(" ".join(head).split())
        for q in quotes
    ):
        classification = "flattened_multiline"
    else:
        classification = "paraphrase_or_other_mismatch"
    return {
        "anchor_matches_any_source": bool(starts),
        "anchor_matches_expected_source": expected in starts,
        "matching_starts": starts,
        "classification": classification,
    }


def replay(
    inputs: Inputs,
    spans: dict[tuple[str, int], FrozenSpan],
    oracle: Expectations,
    expectations_path: Path,
    output: Path,
) -> int:
    output.mkdir(exist_ok=False)
    payload_root = output / "payloads"
    payload_root.mkdir()
    sites = {
        key(site): site
        for site in oracle["previously_correct"] + oracle["hand_checked"]
    }
    positives = {key(site) for site in oracle["hand_checked"]}
    historical = {key(site) for site in oracle["previously_correct"]}
    violations: list[dict[str, Any]] = []
    missed_positives: list[MissedPositive] = []
    records: list[ReplayRecord] = []
    sources: dict[tuple[str, str, str], bytes] = {}
    mutation_controls = []
    for number, artifact in enumerate(sorted({row["artifact"] for row in inputs.rows})):
        rows = sorted((r for r in inputs.rows if r["artifact"] == artifact), key=key)
        first = rows[0]
        directory = payload_root / f"{number:03d}"
        directory.mkdir()
        raw_patch = inputs.read(
            Path(artifact).parent
            / f"code-gauntlet-diff-{first['source_sha'][:8]}.patch"
        )
        context = inputs.read(first["context_path"])
        indexed, entries = reconstruct_map(raw_patch, context)
        map_wire = {
            "version": 1,
            "sha": first["source_sha"],
            "status": "available",
            "context_sha256": digest(context),
            "diff_sha256": digest(raw_patch),
            "entries": {str(i): entry for i, entry in entries.items()},
        }
        map_path = directory / "context-map.json"
        write_json(map_path, map_wire)
        findings = held_findings(inputs, rows, spans)
        resolved = []
        for row, finding in zip(rows, findings, strict=True):
            source_key = (row["source_mirror"], row["source_sha"], row["file"])
            if source_key not in sources:
                sources[source_key] = read_source(row)
            blob = sources[source_key]
            head = location.source_lines(blob.decode("utf-8"))
            result = location.resolve_location(
                finding,
                head,
                entries,
                indexed.hunks.get(row["file"], ()),
                indexed.old_spans.get(row["file"], ()),
            )
            resolved.append(result)
            identity = key(row)
            site = sites.get(identity)
            # Rule a is disabled too: this is specifically the c-then-d counterfactual.
            without_b = location.resolve_location(
                {**finding, "line_start": None},
                head,
                {},
                indexed.hunks.get(row["file"], ()),
                indexed.old_spans.get(row["file"], ()),
            )
            b_only = result.reason == "context_map" and without_b.start != result.start
            displayed = saved_head_row(context, row["line_start"]) if b_only else None
            chosen = head[result.start - 1] if result.start else None
            agrees = (
                (displayed is not None and displayed.strip() == chosen.strip())
                if b_only and chosen is not None
                else None
            )
            if agrees is False:
                violations.append(
                    {
                        **dict(
                            zip(("artifact", "artifact_index"), identity, strict=True)
                        ),
                        "violation": "rule_b_context_disagreement",
                        "saved": displayed,
                        "head": chosen,
                        "start": result.start,
                    }
                )
            canonical = {
                **finding,
                "location_status": result.status,
                "line_start": result.start,
                "line_end": result.end,
            }
            if result.start is not None and not location.anchor_matches_span(
                canonical, head[result.start - 1 : result.end]
            ):
                violations.append(
                    {"key": identity, "violation": "resolved_quote_mismatch"}
                )
            if (
                identity in historical
                and result.start is not None
                and result.start != sites[identity]["expected_start"]
            ):
                violations.append(
                    {
                        "key": identity,
                        "violation": "previously_correct_moved",
                        "expected": sites[identity]["expected_start"],
                        "actual": result.start,
                    }
                )
            if (
                identity in positives
                and result.start != sites[identity]["expected_start"]
            ):
                missed_positives.append(
                    {
                        "artifact": artifact,
                        "artifact_index": row["artifact_index"],
                        "expected": sites[identity]["expected_start"],
                        "actual": result.start,
                        "reason": result.reason,
                        "anchor": finding.get("anchor"),
                        "diagnosis": diagnose_anchor(
                            finding, head, sites[identity]["expected_start"]
                        ),
                    }
                )
                violations.append(
                    {
                        "key": identity,
                        "violation": "positive_not_recovered",
                        "expected": sites[identity]["expected_start"],
                        "actual": result.start,
                    }
                )
            quote_span = spans[identity]
            records.append(
                {
                    "artifact": artifact,
                    "artifact_index": row["artifact_index"],
                    "id": row["id"],
                    "file": row["file"],
                    "role": row["agent"],
                    "old_start": row["line_start"],
                    "old_end": row["line_end"],
                    "new_start": result.start,
                    "new_end": result.end,
                    "oracle_start": site["expected_start"] if site else None,
                    "status": result.status,
                    "reason": result.reason,
                    "rule": _rule(result.reason),
                    "rule_b_only": b_only,
                    "saved_context_text": displayed,
                    "chosen_head_text": chosen,
                    "rule_b_agrees": agrees,
                    "anchor": finding.get("anchor"),
                    "offsets": quote_span["offsets"],
                    "extraction_kind": quote_span["extraction_kind"],
                    "source_sha256": digest(blob),
                    "map_sha256": digest(encoded(map_wire)),
                    "input_sha256": digest(inputs.read(artifact)),
                    "oracle_sha256": digest(inputs.read(expectations_path)),
                    "semantic_selected": False,
                    "inline": False,
                    "fence": False,
                    "task": False,
                }
            )
        joined = replay_wire(findings, first, directory, map_path)
        if len(joined) != len(rows):
            raise InputError(f"Verify join lost records: {artifact}")
        for row, result, finding in zip(rows, resolved, joined, strict=True):
            if (
                finding.get("line_start"),
                finding.get("line_end"),
                finding.get("location_status"),
            ) != (result.start, result.end, result.status):
                violations.append(
                    {"key": key(row), "violation": "wire_resolution_disagreement"}
                )
        if any(key(row) in positives for row in rows):
            mutated_dir = directory / "mutation"
            mutated_dir.mkdir()
            mutated = replay_wire(findings, first, mutated_dir, map_path, bypass=True)
            for row, finding in zip(rows, mutated, strict=True):
                if key(row) in positives:
                    expected = sites[key(row)]["expected_start"]
                    actual = finding.get("line_start")
                    mutation_controls.append(
                        {
                            "key": key(row),
                            "expected": expected,
                            "bypassed": actual,
                            "broken": actual != expected,
                        }
                    )
        delivery_path = (
            Path(artifact).parent
            / f"code-gauntlet-post-review-{first['source_sha'][:8]}.json"
        )
        saved_delivery = (
            inputs.json(delivery_path)
            if str(delivery_path) in inputs.files
            else {"findings": []}
        )
        saved_keys = {(f.get("id"), f.get("file")) for f in saved_delivery["findings"]}
        selected = [f for f in joined if (f["id"], f["file"]) in saved_keys]
        delivered = delivery(selected, first, raw_patch, directory)
        violations.extend(
            {"artifact": artifact, "violation": reason}
            for reason in delivered.violations
        )
        for record, finding in zip(records[-len(rows) :], joined, strict=True):
            record["semantic_selected"] = (finding["id"], finding["file"]) in saved_keys
            record["inline"] = finding["id"] in delivered.inline
            record["fence"] = finding["id"] in delivered.fences | delivered.patches
            record["task"] = finding["id"] in delivered.tasks
            if record["status"] == "unconfirmed" and any(
                record[k] for k in ("inline", "fence", "task")
            ):
                violations.append(
                    {"key": key(record), "violation": "unconfirmed_delivery"}
                )
            if record["fence"]:
                blob = sources[
                    (first["source_mirror"], first["source_sha"], finding["file"])
                ]
                head = location.source_lines(blob.decode("utf-8"))
                span = location.canonical_range(finding)
                claimed_start, claimed_end = (
                    finding.get("claimed_line_start"),
                    finding.get("claimed_line_end"),
                )
                if (
                    span is None
                    or not location.anchor_matches_span(
                        finding, head[span[0] - 1 : span[1]], whole=True
                    )
                    or not location.positive_safe_integer(claimed_start)
                    or not location.positive_safe_integer(claimed_end)
                    or cast(int, claimed_end) - cast(int, claimed_start)
                    != span[1] - span[0]
                ):
                    violations.append(
                        {"key": key(record), "violation": "unsafe_fence_or_patch"}
                    )
    totals = Counter(record["status"] for record in records)
    extraction = Counter(record["extraction_kind"] for record in records)
    unconfirmed_correct = Counter(
        record["extraction_kind"]
        for record in records
        if key(record) in historical and record["status"] == "unconfirmed"
    )
    recovered = sum(
        key(r) in positives and r["new_start"] == r["oracle_start"] for r in records
    )
    summary: ReplaySummary = {
        "records": len(records),
        "artifacts": len({r["artifact"] for r in records}),
        "statuses": dict(totals),
        "anchors_from_evidence": sum(r["anchor"] is not None for r in records),
        "extraction": dict(extraction),
        "relocations_by_rule": dict(
            Counter(r["rule"] for r in records if r["status"] == "relocated")
        ),
        "by_role_status_rule": dict(
            Counter(f"{r['role']}/{r['status']}/{r['rule']}" for r in records)
        ),
        "previously_correct": len(historical),
        "unchanged_correct_sites": sum(
            key(r) in historical and r["new_start"] == r["oracle_start"]
            for r in records
        ),
        "previously_correct_became_unconfirmed": dict(unconfirmed_correct),
        "known_recoveries": recovered,
        "missed_positives": missed_positives,
        "rule_b_only": sum(r["rule_b_only"] for r in records),
        "rule_b_disagreements": sum(r["rule_b_agrees"] is False for r in records),
        "range_eliminations": 0,
        "false_moves": sum(
            v["violation"] == "previously_correct_moved" for v in violations
        ),
        "fence_inspection": next(r for r in records if key(r) == key(oracle["fence"])),
        "violations": 0,
        "records_sha256": "",
    }
    if not (
        len(records) == 2086
        and summary["artifacts"] == 146
        and len(historical) == 366
        and len(positives) == 20
    ):
        violations.append({"violation": "fixed_denominator_mismatch"})
    if not mutation_controls or not all(c["broken"] for c in mutation_controls):
        violations.append({"violation": "resolver_bypass_not_detected"})
    write_jsonl(output / "records.jsonl", records)
    write_jsonl(output / "violations.jsonl", violations)
    write_json(output / "mutation.json", {"whole_resolver_bypass": mutation_controls})
    summary["violations"] = len(violations)
    summary["records_sha256"] = digest((output / "records.jsonl").read_bytes())
    write_json(output / "summary.json", summary)
    report = [
        "# Offline location replay",
        "",
        f"Records: {len(records)}. Artifacts: {summary['artifacts']}.",
        f"Anchors: {summary['anchors_from_evidence']} / {len(records)}. No anchor: {extraction['none']} / {len(records)}.",
        f"Extraction: {json.dumps(dict(extraction), sort_keys=True)}.",
        f"Statuses: {json.dumps(dict(totals), sort_keys=True)}.",
        f"Relocations: {json.dumps(summary['relocations_by_rule'], sort_keys=True)}.",
        f"Range eliminations: {summary['range_eliminations']}.",
        f"Previously correct unchanged: {summary['unchanged_correct_sites']} / {len(historical)}.",
        f"Previously correct became unconfirmed by extraction: {json.dumps(dict(unconfirmed_correct), sort_keys=True)}.",
        f"Known recoveries: {recovered} / {len(positives)}.",
        f"Rule-b-only: {summary['rule_b_only']}. Disagreements: {summary['rule_b_disagreements']}.",
        f"False moves: {summary['false_moves']}. Violations: {len(violations)}.",
        f"Saved fence: {json.dumps(summary['fence_inspection'], ensure_ascii=True, sort_keys=True)}.",
        "",
        "Exit 0 requires all fixed-denominator and behavioral criteria.",
        "",
        "## Missed positives",
        "",
        "Classification describes the frozen quote; it never repairs or replaces it.",
        "Paraphrase or other mismatch is a residual diagnosis, not a semantic judgment.",
        "",
        *(
            "- " + json.dumps(miss, ensure_ascii=True, sort_keys=True)
            for miss in missed_positives
        ),
    ]
    (output / "report.md").write_text("\n".join(report) + "\n", encoding="ascii")
    print(
        f"REPLAY: {len(records)} records; {recovered}/{len(positives)} recoveries; {len(violations)} violations"
    )
    return 1 if violations else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--freeze-quotes", type=Path)
    mode.add_argument("--quote-spans", type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        inputs = load_inputs(args.map_dir)
        if args.freeze_quotes:
            if args.output_dir:
                raise InputError("freeze mode does not produce replay output")
            freeze_quotes(inputs, args.expectations, args.freeze_quotes)
            print(f"FROZEN: {len(inputs.rows)} evidence slices; {ADAPTER_VERSION}")
            return 0
        if args.output_dir is None:
            raise InputError("run mode requires --output-dir")
        oracle = load_expectations(inputs, args.expectations, check_sources=True)
        spans = load_spans(inputs, args.expectations, args.quote_spans)
        return replay(inputs, spans, oracle, args.expectations, args.output_dir)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"INPUT ERROR: {exc}", file=sys.stderr)
        return 2
