"""Measurement identity, immutable attempts, and admissible review outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, get_args

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.runner.epochs import (  # noqa: E402
    DEFAULT_ROOT,
    JsonValue,
    confined,
    digest,
    file_hash,
    load_epoch,
    read_json,
)
from bench.runner.observations import (  # noqa: E402
    ScoreRef,
    ScoreRefWire,
    artifact_allowed,
    copy_exclusive,
    exclusive_json,
)

AuthMode = Literal["api", "subscription"]
CheckpointStatus = Literal["pending", "ok", "timeout", "invalid", "drifted", "failed"]
ValidityState = Literal["valid", "invalid", "pending", "operational_failure"]
InvalidReason = Literal[
    "cli_mismatch",
    "model_mismatch",
    "effort_mismatch",
    "judge_mismatch",
    "epoch_mismatch",
    "window_mismatch",
    "input_drift",
    "artifact_missing",
    "pipeline_degraded",
    "scoring_error",
    "usage_limit",
    "subscription_stall_unknown",
    "reset_not_confirmed",
    "missing_provenance",
    "provider_transient",
    "review_failure_unconfirmed",
    "pre_dispatch_failure",
]
EFFORT_KEYS = (
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SUBAGENT_EFFORT",
    "MAX_THINKING_TOKENS",
)
LIMITATIONS = (
    "No real usage-limit/reset fixture exists; production positive-signature recognition is disabled.",
    "Auto-update control and effective effort behavior require installed-CLI verification.",
    "Resolved service ids do not prove provider implementation immutability.",
)
POSITIVE_SIGNATURE_REGISTRY: tuple[str, ...] = ()
RETRY_REASONS = frozenset(
    {
        "usage_limit",
        "subscription_stall_unknown",
        "provider_transient",
        "review_failure_unconfirmed",
        "pre_dispatch_failure",
    }
)


class RunFingerprintWire(TypedDict):
    cli_version: str
    cli_realpath: str
    cli_sha256: str
    resolved_models: dict[str, str]
    allowed_auxiliary_model_ids: list[str]
    effort_env: dict[str, str | None]
    frontmatter_effort_sha256: str
    judge_pin: str
    adjudicator_pin: str
    golden_epoch_id: str
    golden_sha256: str
    scorer_sha256: str
    auth_mode: AuthMode
    bench_policy_sha256: str


class UnverifiedRunProvenanceWire(TypedDict):
    status: Literal["non_record"]
    cli_version: str | None
    cli_realpath: str | None
    cli_sha256: str | None
    resolved_models: dict[str, str] | None
    allowed_auxiliary_model_ids: list[str]
    effort_env: dict[str, str | None]
    frontmatter_effort_sha256: str
    judge_pin: str | None
    adjudicator_pin: str | None
    golden_epoch_id: str
    golden_sha256: str
    scorer_sha256: str
    auth_mode: AuthMode
    bench_policy_sha256: str
    requested_models: dict[str, str]


@dataclass(frozen=True, slots=True)
class RunFingerprint:
    cli_version: str
    cli_realpath: str
    cli_sha256: str
    resolved_models: tuple[tuple[str, str], ...]
    allowed_auxiliary_model_ids: tuple[str, ...]
    effort_env: tuple[tuple[str, str | None], ...]
    frontmatter_effort_sha256: str
    judge_pin: str
    adjudicator_pin: str
    golden_epoch_id: str
    golden_sha256: str
    scorer_sha256: str
    auth_mode: AuthMode
    bench_policy_sha256: str

    def wire(self) -> RunFingerprintWire:
        return {
            "cli_version": self.cli_version,
            "cli_realpath": self.cli_realpath,
            "cli_sha256": self.cli_sha256,
            "resolved_models": dict(self.resolved_models),
            "allowed_auxiliary_model_ids": list(self.allowed_auxiliary_model_ids),
            "effort_env": dict(self.effort_env),
            "frontmatter_effort_sha256": self.frontmatter_effort_sha256,
            "judge_pin": self.judge_pin,
            "adjudicator_pin": self.adjudicator_pin,
            "golden_epoch_id": self.golden_epoch_id,
            "golden_sha256": self.golden_sha256,
            "scorer_sha256": self.scorer_sha256,
            "auth_mode": self.auth_mode,
            "bench_policy_sha256": self.bench_policy_sha256,
        }


def fingerprint_from_wire(value) -> RunFingerprint:
    value = dict(value)
    for key in ("resolved_models", "effort_env"):
        value[key] = tuple(sorted(value[key].items()))
    value["allowed_auxiliary_model_ids"] = tuple(
        sorted(value["allowed_auxiliary_model_ids"])
    )
    result = RunFingerprint(**value)
    if (
        result.auth_mode not in get_args(AuthMode)
        or not result.cli_version
        or not result.judge_pin
    ):
        raise ValueError("incomplete fingerprint")
    if set(dict(result.effort_env)) != {*EFFORT_KEYS, "DISABLE_AUTOUPDATER"}:
        raise ValueError("incomplete effort fingerprint")
    if dict(result.effort_env)["DISABLE_AUTOUPDATER"] != "1":
        raise ValueError("auto-update must be disabled")
    if not result.resolved_models or any(
        not service_model(v)
        for v in (
            *dict(result.resolved_models).values(),
            *result.allowed_auxiliary_model_ids,
        )
    ):
        raise ValueError("unresolved model fingerprint")
    return result


class MeasurementWindowWire(TypedDict):
    schema_version: Literal[1]
    window_id: str
    opens_at: str
    closes_at: str
    expected: RunFingerprintWire
    pr_urls: list[str]
    planned_run_indices: list[int]
    max_parallel_prs: Literal[1]


@dataclass(frozen=True, slots=True)
class MeasurementWindow:
    schema_version: Literal[1]
    window_id: str
    opens_at: str
    closes_at: str
    expected: RunFingerprint
    pr_urls: tuple[str, ...]
    planned_run_indices: tuple[int, ...]
    max_parallel_prs: Literal[1]

    def wire(self) -> MeasurementWindowWire:
        return {
            "schema_version": self.schema_version,
            "window_id": self.window_id,
            "opens_at": self.opens_at,
            "closes_at": self.closes_at,
            "expected": self.expected.wire(),
            "pr_urls": list(self.pr_urls),
            "planned_run_indices": list(self.planned_run_indices),
            "max_parallel_prs": self.max_parallel_prs,
        }


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.astimezone(timezone.utc)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_window(path: Path) -> MeasurementWindow:
    return window_from_wire(read_json(Path(path)))


def window_from_wire(value) -> MeasurementWindow:
    result = MeasurementWindow(
        **{
            **value,
            "expected": fingerprint_from_wire(value["expected"]),
            "pr_urls": tuple(value["pr_urls"]),
            "planned_run_indices": tuple(value["planned_run_indices"]),
        }
    )
    if (
        result.schema_version != 1
        or result.max_parallel_prs != 1
        or not result.window_id
        or timestamp(result.opens_at) >= timestamp(result.closes_at)
        or not result.pr_urls
        or len(set(result.pr_urls)) != len(result.pr_urls)
        or not result.planned_run_indices
        or tuple(sorted(set(result.planned_run_indices))) != result.planned_run_indices
        or any(type(i) is not int or i < 0 for i in result.planned_run_indices)
    ):
        raise ValueError("invalid measurement window")
    return result


class ValidityWire(TypedDict):
    state: ValidityState
    reasons: list[InvalidReason]
    window_id: str | None
    observed: RunFingerprintWire | None
    evidence_refs: list[str]
    retry_not_before: str | None
    retry_of: str | None
    confirmed_reset_at: str | None
    failure_fingerprint: str | None
    retryable: bool
    limitations: list[str]


@dataclass(frozen=True, slots=True)
class Validity:
    state: ValidityState
    reasons: tuple[InvalidReason, ...] = ()
    window_id: str | None = None
    observed: RunFingerprint | None = None
    evidence_refs: tuple[str, ...] = ()
    retry_not_before: str | None = None
    retry_of: str | None = None
    confirmed_reset_at: str | None = None
    failure_fingerprint: str | None = None
    retryable: bool = False
    limitations: tuple[str, ...] = LIMITATIONS

    def wire(self) -> ValidityWire:
        return {
            "state": self.state,
            "window_id": self.window_id,
            "observed": self.observed.wire() if self.observed else None,
            "reasons": list(self.reasons),
            "evidence_refs": list(self.evidence_refs),
            "limitations": list(self.limitations),
            "retry_not_before": self.retry_not_before,
            "retry_of": self.retry_of,
            "confirmed_reset_at": self.confirmed_reset_at,
            "failure_fingerprint": self.failure_fingerprint,
            "retryable": self.retryable,
        }


def validity_from_wire(value):
    return Validity(
        **{
            **value,
            "observed": fingerprint_from_wire(value["observed"])
            if value["observed"]
            else None,
            **{k: tuple(value[k]) for k in ("reasons", "evidence_refs", "limitations")},
        }
    )


def service_model(value):
    return isinstance(value, str) and bool(
        re.fullmatch(r"claude-[A-Za-z0-9_.-]+(?:\[1m\])?", value)
    )


def cli_probe(path):
    result = subprocess.run(
        [str(path), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def model_probe(root, policy):
    script = """import {resolvedDispatchModels} from './workflows/src/registry.js';
const policy=JSON.parse(process.argv[1]);
console.log(JSON.stringify(resolvedDispatchModels(policy)));"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(policy)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def frontmatter_effort(root):
    fields = []
    for path in sorted((Path(root) / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        header = text.split("---", 2)[1] if text.startswith("---") else ""
        fields.append(
            (
                path.name,
                [
                    line
                    for line in header.splitlines()
                    if re.match(r"^effort\s*:", line)
                ],
            )
        )
    return digest(fields)


def capture_fingerprint(
    *,
    cli_path,
    child_model,
    auth_mode,
    epoch,
    judge_pin,
    adjudicator_pin,
    env,
    policy,
    root=ROOT,
    auxiliary_models=(),
    cli_probe=cli_probe,
    model_probe=model_probe,
):
    if any(
        env.get(key) and env[key].lower() not in ("0", "false")
        for key in (
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
        )
    ):
        raise ValueError(
            "measurement refuses unresolved ambient provider/auth overrides"
        )
    if not service_model(child_model):
        raise ValueError("measurement requires an explicit resolved child model id")
    models = dict(model_probe(root, policy))
    models["orchestrator"] = child_model
    path = Path(cli_path).resolve(strict=True)
    value = RunFingerprint(
        cli_probe(path),
        str(path),
        file_hash(path),
        tuple(sorted(models.items())),
        tuple(sorted(auxiliary_models)),
        tuple(
            sorted(
                {
                    **{k: env.get(k) for k in EFFORT_KEYS},
                    "DISABLE_AUTOUPDATER": "1",
                }.items()
            )
        ),
        frontmatter_effort(root),
        judge_pin,
        adjudicator_pin,
        epoch.epoch_id,
        epoch.golden_sha256,
        epoch.scorer_sha256,
        auth_mode,
        digest(policy),
    )
    return fingerprint_from_wire(value.wire())


def check_window(expected, observed, *, now):
    reasons = []
    mapping = {
        "cli_mismatch": ("cli_version", "cli_realpath", "cli_sha256"),
        "model_mismatch": ("resolved_models", "allowed_auxiliary_model_ids"),
        "effort_mismatch": ("effort_env", "frontmatter_effort_sha256"),
        "judge_mismatch": ("judge_pin", "adjudicator_pin"),
        "epoch_mismatch": ("golden_epoch_id", "golden_sha256", "scorer_sha256"),
        "window_mismatch": ("auth_mode", "bench_policy_sha256"),
    }
    for reason, fields in mapping.items():
        if any(getattr(expected.expected, f) != getattr(observed, f) for f in fields):
            reasons.append(reason)
    if (
        not timestamp(expected.opens_at)
        <= timestamp(now)
        <= timestamp(expected.closes_at)
        and "window_mismatch" not in reasons
    ):
        reasons.append("window_mismatch")
    return Validity(
        "invalid" if reasons else "valid", tuple(reasons), expected.window_id, observed
    )


def recheck_cli(fingerprint, *, probe=cli_probe):
    path = Path(fingerprint.cli_realpath)
    if (
        file_hash(path) != fingerprint.cli_sha256
        or probe(path) != fingerprint.cli_version
    ):
        raise ValueError("cli_mismatch: pinned CLI changed before child dispatch")


def same_pr_set(actual, expected):
    return len(actual) == len(expected) and set(actual) == set(expected)


class ChildFailureEventWire(TypedDict):
    source: Literal["child_result"]
    authenticated: Literal[True]
    code: str
    phase: Literal["child_result"]
    diagnostic_sha256: str


def _normalize_diagnostic(text):
    # Keep semantic diagnostics while removing per-request transport metadata.
    patterns = (
        (r"\b(?:request|req)[_ -]?id[\"']?\s*[:=]?\s*[\"']?[\w.-]+", "request_id=<id>"),
        (r"\breq_[A-Za-z0-9_-]+\b", "<request-id>"),
        (r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<uuid>"),
        (
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
            "<timestamp>",
        ),
        (
            r"\b(timestamp|time)[\"']?\s*[:=]\s*[\"']?\d{10,}(?:\.\d+)?",
            r"\1=<timestamp>",
        ),
        (
            r"\b(byte(?:[_ -]?offset)?|offset)[\"']?\s*[:=]?\s*[\"']?\d+\b",
            r"\1=<offset>",
        ),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _child_failure_event(envelope) -> ChildFailureEventWire | None:
    if (
        not envelope
        or envelope.get("type") != "result"
        or envelope.get("is_error") is not True
    ):
        return None
    status = envelope.get("api_error_status")
    terminal = envelope.get("terminal_reason")
    subtype = envelope.get("subtype")
    errors = envelope.get("errors")
    diagnostic: list[JsonValue] = [
        _normalize_diagnostic(value) if isinstance(value, str) else None
        for value in (terminal, subtype)
    ]
    diagnostic.append(
        sorted(_normalize_diagnostic(e) for e in errors if isinstance(e, str))
        if isinstance(errors, list)
        else []
    )
    code = str(status) if type(status) is int else terminal or subtype
    if not code and not any(diagnostic):
        return None
    return {
        "source": "child_result",
        "authenticated": True,
        "code": _normalize_diagnostic(str(code or "child_failure")),
        "phase": "child_result",
        "diagnostic_sha256": digest(diagnostic),
    }


def classify_attempt(
    result,
    *,
    auth_mode,
    trusted_events=(),
    signature_registry=POSITIVE_SIGNATURE_REGISTRY,
    now=None,
):
    if getattr(result, "validity", None) is not None:
        return result.validity
    reason = result.reason or ""
    known = {
        "plugin_mutated_by_child": "input_drift",
        "incomplete_sha_entry": "input_drift",
        "input_drift": "input_drift",
        "missing_payload": "artifact_missing",
        "no_payload": "artifact_missing",
        "naive_output_unparseable": "artifact_missing",
        "config_echo_missing": "missing_provenance",
        "identity_mismatch": "missing_provenance",
        "config_echo_mismatch": "missing_provenance",
        "askuserquestion_detected": "pipeline_degraded",
        "claude_not_found": "cli_mismatch",
    }
    specific = next((v for k, v in known.items() if k in reason), None)
    if specific == "input_drift":
        return Validity("invalid", (specific,))
    if (
        result.status != "ok"
        and (not result.child_dispatched or reason == "mirror_error")
        and result.cost_usd == 0
        and not result.per_model.entries
    ):
        return Validity(
            "invalid",
            ("pre_dispatch_failure",),
            evidence_refs=("pre-dispatch:no-child-spend",),
            retryable=True,
        )
    if specific:
        return Validity("invalid", (specific,))
    if not specific and any(
        k in reason
        for k in ("identity", "auth", "setup", "cli_", "scoring", "input_drift")
    ):
        specific = "scoring_error" if "scoring" in reason else "missing_provenance"
    if specific:
        return Validity("invalid", (specific,))
    if result.status == "ok":
        return Validity("valid")
    # Only collector-authenticated child envelopes enter this interface; prompt/tool text is never searched.
    events = [
        e
        for e in trusted_events
        if e.get("source") == "child_result" and e.get("authenticated") is True
    ]
    for event in events:
        if (
            auth_mode == "subscription"
            and event.get("signature") in signature_registry
            and event.get("quota_state") == "exhausted"
            and event.get("reset_at")
        ):
            timestamp(event["reset_at"])
            return Validity(
                "invalid",
                ("usage_limit",),
                evidence_refs=(digest(event),),
                retry_not_before=event["reset_at"],
                retryable=True,
            )
    failure = next(
        (
            e
            for e in events
            if e.get("phase") and e.get("code") and e.get("diagnostic_sha256")
        ),
        None,
    )
    fingerprint = (
        digest([failure["code"], failure["phase"], failure["diagnostic_sha256"]])
        if failure
        else None
    )
    transient = failure and (
        failure["code"] in ("overloaded", "rate_limit", "retryable_rate_limit")
        or (
            str(failure["code"]).isdigit()
            and (int(failure["code"]) == 429 or int(failure["code"]) >= 500)
        )
    )
    cause = (
        "provider_transient"
        if auth_mode == "api" and transient
        else (
            "review_failure_unconfirmed"
            if failure or auth_mode == "api"
            else "subscription_stall_unknown"
        )
    )
    retry_at = (
        failure.get("retry_not_before") if failure and auth_mode == "api" else None
    )
    if retry_at:
        timestamp(retry_at)
    evidence = ("child-auth:" + auth_mode,) + (
        ()
        if not failure
        else (
            "event-sha256:" + digest(failure),
            "provider-code:" + str(failure["code"]),
            "phase:" + str(failure["phase"]),
            "diagnostic-sha256:" + failure["diagnostic_sha256"],
        )
    )
    return Validity(
        "invalid",
        (cause,),
        evidence_refs=evidence,
        retry_not_before=retry_at,
        failure_fingerprint=fingerprint,
        retryable=True,
    )


class AttemptWire(TypedDict):
    schema_version: Literal[1]
    attempt_id: str
    run_id: str
    pr_url: str
    started_at: str
    completed_at: str
    checkpoint_status: CheckpointStatus
    checkpoint_reason: str | None
    validity: ValidityWire
    source_artifact_hashes: dict[str, str]
    sequence: int
    requested_models: dict[str, str]
    observed_model_ids: list[str]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    schema_version: Literal[1]
    attempt_id: str
    run_id: str
    pr_url: str
    started_at: str
    completed_at: str
    checkpoint_status: CheckpointStatus
    checkpoint_reason: str | None
    validity: Validity
    source_artifact_hashes: tuple[tuple[str, str], ...]
    sequence: int
    requested_models: tuple[tuple[str, str], ...]
    observed_model_ids: tuple[str, ...]

    def wire(self) -> AttemptWire:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "pr_url": self.pr_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "checkpoint_status": self.checkpoint_status,
            "checkpoint_reason": self.checkpoint_reason,
            "sequence": self.sequence,
            "validity": self.validity.wire(),
            "source_artifact_hashes": dict(self.source_artifact_hashes),
            "requested_models": dict(self.requested_models),
            "observed_model_ids": list(self.observed_model_ids),
        }


def attempt_from_wire(value):
    result = AttemptRecord(
        **{
            **value,
            "validity": validity_from_wire(value["validity"]),
            "source_artifact_hashes": tuple(
                sorted(value["source_artifact_hashes"].items())
            ),
            "requested_models": tuple(sorted(value["requested_models"].items())),
            "observed_model_ids": tuple(value["observed_model_ids"]),
        }
    )
    if (
        result.schema_version != 1
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", result.attempt_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", result.run_id)
        or result.checkpoint_status not in get_args(CheckpointStatus)
        or result.validity.state not in get_args(ValidityState)
        or set(result.validity.reasons) - set(get_args(InvalidReason))
        or result.sequence < 1
        or timestamp(result.started_at) > timestamp(result.completed_at)
    ):
        raise ValueError("invalid immutable attempt schema")
    return result


def load_attempt(path):
    path = Path(path)
    result = attempt_from_wire(read_json(path))
    if result.attempt_id != path.parent.name:
        raise ValueError("attempt identity mismatch")
    for name, expected in result.source_artifact_hashes:
        if (
            not artifact_allowed(Path(name))
            or file_hash(confined(path.parent, name)) != expected
        ):
            raise ValueError("attempt artifact hash mismatch")
    if result.validity.state == "operational_failure":
        originals = list(
            path.parents[3].glob(f"attempts/*/{result.validity.retry_of}/attempt.json")
        )
        if len(originals) != 1 or originals[0] == path:
            raise ValueError(
                "operational failure lacks independent confirmation evidence"
            )
        original = attempt_from_wire(read_json(originals[0]))
        if (
            original.sequence >= result.sequence
            or "original-attempt-sha256:" + digest(original.wire())
            not in result.validity.evidence_refs
        ):
            raise ValueError("operational failure original evidence changed")
        retry = replace(
            result, validity=replace(result.validity, state="invalid", retryable=True)
        )
        confirm_failure(original, retry, reset_evidence=result.validity.evidence_refs)
    return result


def snapshot_attempt(
    run_dir,
    pr_dir,
    *,
    run_id,
    pr_url,
    result,
    validity,
    started_at,
    requested_models=(),
    observed_model_ids=(),
):
    run_dir, pr_dir = Path(run_dir), Path(pr_dir)
    parent = run_dir / "attempts" / hashlib.sha256(pr_url.encode()).hexdigest()[:20]
    sequence = len(list(parent.glob("*/attempt.json"))) + 1
    aid = f"a{sequence:04d}-{uuid.uuid4().hex}"
    target = parent / aid
    hashes = {}
    if not pr_dir.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("attempt PR directory escapes run")
    pr_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(
        pr_dir / "attempt-result.json",
        {
            "status": result.status,
            "reason": result.reason,
            "cost_usd": result.cost_usd,
            "per_model": result.model_usage_wire(),
            "requested_models": dict(requested_models),
            "observed_model_ids": list(observed_model_ids),
        },
    )
    for source in sorted(pr_dir.rglob("*")):
        relative = source.relative_to(pr_dir)
        if (
            not source.is_file()
            or source.is_symlink()
            or not artifact_allowed(relative)
            or any(p in ("superseded", "worktree") for p in relative.parts)
        ):
            continue
        if not source.resolve().is_relative_to(pr_dir.resolve()):
            raise ValueError("attempt artifact escapes PR")
        name = "artifacts/" + relative.as_posix()
        copy_exclusive(source, target / name)
        hashes[name] = file_hash(target / name)
    record = AttemptRecord(
        1,
        aid,
        run_id,
        pr_url,
        started_at,
        utc_now(),
        result.status,
        result.reason,
        validity,
        tuple(sorted(hashes.items())),
        sequence,
        tuple(sorted(dict(requested_models).items())),
        tuple(sorted(observed_model_ids)),
    )
    exclusive_json(target / "attempt.json", record.wire())
    return record


def retryable_attempts(attempts, *, now):
    return tuple(
        a
        for a in attempts
        if a.validity.retryable
        and set(a.validity.reasons) <= RETRY_REASONS
        and (
            not a.validity.retry_not_before
            or timestamp(now) >= timestamp(a.validity.retry_not_before)
        )
    )


def check_retry(original, *, now, reset_evidence=None, reset_confirmed_at=None):
    value = original.validity
    if not value.retryable or not set(value.reasons) <= RETRY_REASONS:
        raise ValueError("unrelated invalid attempt cannot retry")
    if value.reasons == ("pre_dispatch_failure",):
        return None
    if value.retry_not_before and timestamp(now) < timestamp(value.retry_not_before):
        wait = (timestamp(value.retry_not_before) - timestamp(now)).total_seconds()
        raise ValueError(f"retry refused: remaining wait {wait:g} seconds")
    auth = (
        value.observed.auth_mode
        if value.observed
        else next(
            (
                r.removeprefix("child-auth:")
                for r in value.evidence_refs
                if r.startswith("child-auth:")
            ),
            None,
        )
    )
    if auth == "subscription" or set(value.reasons) & {
        "subscription_stall_unknown",
        "usage_limit",
    }:
        if not reset_evidence or not reset_confirmed_at:
            raise ValueError(
                "reset_not_confirmed: explicit public reset evidence required"
            )
        if timestamp(reset_confirmed_at) > timestamp(now) or timestamp(
            reset_confirmed_at
        ) <= timestamp(original.completed_at):
            raise ValueError(
                "reset_not_confirmed: reset must follow original attempt and precede retry"
            )
        if not artifact_allowed(Path(reset_evidence)):
            raise ValueError("denied reset evidence path")
        evidence = Path(reset_evidence)
        if evidence.is_symlink() or not artifact_allowed(evidence.resolve()):
            raise ValueError("denied reset evidence path")
        if not evidence.is_file():
            raise ValueError("reset evidence missing")
        return file_hash(evidence)
    return None


class CellSelectionWire(TypedDict):
    arm: str
    run_index: int
    pr_url: str
    attempt_id: str
    score_ref: ScoreRefWire | None
    failure_confirmation_ref: str | None


@dataclass(frozen=True, slots=True)
class CellSelection:
    arm: str
    run_index: int
    pr_url: str
    attempt_id: str
    score_ref: ScoreRef | None
    failure_confirmation_ref: str | None = None
    attempt: AttemptRecord | None = None
    observations: tuple[MatrixObservation, ...] = ()


def confirm_failure(original, retry, *, reset_evidence):
    if (
        original.pr_url != retry.pr_url
        or retry.validity.retry_of != original.attempt_id
    ):
        raise ValueError("failure confirmation must link the same PR attempt")
    if original.attempt_id == retry.attempt_id:
        raise ValueError("failure confirmation must be independent")
    if retry.validity.state == "valid":
        return CellSelection("", 0, retry.pr_url, retry.attempt_id, None, attempt=retry)
    no_fingerprint = (
        original.validity.failure_fingerprint is None
        and retry.validity.failure_fingerprint is None
    )
    if (
        "usage_limit" in retry.validity.reasons
        or "usage_limit" in original.validity.reasons
        or (
            not no_fingerprint
            and original.validity.failure_fingerprint
            != retry.validity.failure_fingerprint
        )
    ):
        raise ValueError("failure not confirmed: changed or definitive quota cause")
    if (
        not original.validity.retryable
        or not retry.validity.retryable
        or not set(original.validity.reasons)
        <= RETRY_REASONS - {"pre_dispatch_failure"}
        or not set(retry.validity.reasons) <= RETRY_REASONS - {"pre_dispatch_failure"}
        or timestamp(retry.started_at) < timestamp(original.completed_at)
    ):
        raise ValueError("failure not retryable")
    if (
        (
            original.validity.observed
            and original.validity.observed.auth_mode == "subscription"
        )
        or "child-auth:subscription" in original.validity.evidence_refs
        or "subscription_stall_unknown" in original.validity.reasons
    ):
        if not reset_evidence or not retry.validity.confirmed_reset_at:
            raise ValueError("reset_not_confirmed")
        if (
            not timestamp(original.completed_at)
            < timestamp(retry.validity.confirmed_reset_at)
            <= timestamp(retry.started_at)
        ):
            raise ValueError("reset_not_confirmed: confirmation does not precede retry")
    cross_auth = (
        original.validity.observed
        and retry.validity.observed
        and original.validity.observed.auth_mode != retry.validity.observed.auth_mode
    )
    target = original if cross_auth else retry
    evidence = tuple(
        sorted(
            {
                *target.validity.evidence_refs,
                *(
                    [
                        "failure-confirmation:recurring-same-pr-without-stable-fingerprint",
                        "original-failure-reason:"
                        + ",".join(original.validity.reasons),
                        "retry-failure-reason:" + ",".join(retry.validity.reasons),
                    ]
                    if no_fingerprint
                    else []
                ),
                *(
                    ["confirmation-source:" + retry.run_id + "/" + retry.attempt_id]
                    if cross_auth
                    else []
                ),
                "original-attempt-sha256:" + digest(original.wire()),
                "confirmation-attempt-sha256:" + digest(retry.wire()),
            }
        )
    )
    confirmed = replace(
        target,
        validity=replace(
            target.validity,
            state="operational_failure",
            retryable=False,
            evidence_refs=evidence,
            confirmed_reset_at=retry.validity.confirmed_reset_at,
        ),
    )
    return CellSelection(
        "",
        0,
        target.pr_url,
        target.attempt_id,
        None,
        f"{retry.run_id}/{retry.attempt_id}" if cross_auth else original.attempt_id,
        confirmed,
    )


class FailureConfirmationWire(TypedDict):
    schema_version: Literal[1]
    original: AttemptWire
    confirmation: AttemptWire
    reset_evidence_refs: list[str]
    original_sha256: str
    confirmation_sha256: str


def record_failure_confirmation(original_path, retry_path, *, reset_evidence):
    """Publish cross-auth confirmation without rewriting either review attempt."""
    original_path, retry_path = Path(original_path), Path(retry_path)
    original, retry = load_attempt(original_path), load_attempt(retry_path)
    selected = confirm_failure(original, retry, reset_evidence=reset_evidence)
    if selected.attempt_id != original.attempt_id:
        raise ValueError("same-auth confirmation belongs to a fresh retry snapshot")
    receipt: FailureConfirmationWire = {
        "schema_version": 1,
        "original": original.wire(),
        "confirmation": retry.wire(),
        "reset_evidence_refs": list(reset_evidence),
        "original_sha256": file_hash(original_path),
        "confirmation_sha256": file_hash(retry_path),
    }
    exclusive_json(
        original_path.parents[3]
        / "failure-confirmations"
        / (original.attempt_id + ".json"),
        receipt,
    )
    from bench.runner.checkpoint import Checkpoint

    run_dir = original_path.parents[3]
    Checkpoint(run_dir).mark(
        original.pr_url,
        original.checkpoint_status,
        {
            "attempt_id": original.attempt_id,
            "attempt_ref": str(original_path.relative_to(run_dir)),
            "validity": selected.attempt.validity.wire(),
            "countable_operational_failure": True,
            "failure_confirmation_ref": selected.failure_confirmation_ref,
        },
    )
    return selected


def failure_confirmation_ref(attempt):
    return next(
        (
            ref.removeprefix("confirmation-source:")
            for ref in attempt.validity.evidence_refs
            if ref.startswith("confirmation-source:")
        ),
        attempt.validity.retry_of,
    )


def confirmed_failure(attempt):
    return bool(failure_confirmation_ref(attempt)) and (
        bool(attempt.validity.failure_fingerprint)
        or "failure-confirmation:recurring-same-pr-without-stable-fingerprint"
        in attempt.validity.evidence_refs
    )


def selected_attempt(path):
    path = Path(path)
    original = load_attempt(path)
    receipt_path = (
        path.parents[3] / "failure-confirmations" / (original.attempt_id + ".json")
    )
    if not receipt_path.is_file():
        return original
    receipt = read_json(receipt_path)
    retry = attempt_from_wire(receipt["confirmation"])
    if (
        receipt.get("schema_version") != 1
        or receipt["original"] != original.wire()
        or receipt["original_sha256"] != file_hash(path)
        or Path(retry.run_id).name != retry.run_id
        or Path(retry.attempt_id).name != retry.attempt_id
    ):
        raise ValueError("invalid immutable failure confirmation")
    sources = list(
        (path.parents[3].parent / retry.run_id / "attempts").glob(
            f"*/{retry.attempt_id}/attempt.json"
        )
    )
    if (
        len(sources) != 1
        or file_hash(sources[0]) != receipt["confirmation_sha256"]
        or load_attempt(sources[0]) != retry
    ):
        raise ValueError("failure confirmation source changed")
    return confirm_failure(
        original, retry, reset_evidence=receipt["reset_evidence_refs"]
    ).attempt


@dataclass(frozen=True, slots=True)
class MatrixObservation:
    arm: str
    run_index: int
    pr_url: str
    bug_id: str
    detected: bool
    run_id: str
    attempt_id: str
    delivered_dimensions: tuple[str, ...] = ()
    discovery_dimensions: tuple[str, ...] = ()
    stage_outcomes: tuple[tuple[str, bool | None], ...] = ()
    provenance_available: bool = False


@dataclass(frozen=True, slots=True)
class CompleteMatrix:
    selections: tuple[CellSelection, ...]
    observations: tuple[MatrixObservation, ...]
    arms: tuple[str, str]
    run_indices_by_arm: tuple[tuple[str, tuple[int, ...]], ...]
    stage_observations: tuple = ()


def validate_matrix(selections, window, population, *, expected_run_indices):
    selections = tuple(selections)
    arms = tuple(sorted({s.arm for s in selections}))
    if (
        len(arms) != 2
        or tuple(window.planned_run_indices[: len(expected_run_indices)])
        != expected_run_indices
    ):
        raise ValueError("invalid arms or planned run prefix")
    expected = {
        (a, r, p) for a in arms for r in expected_run_indices for p in window.pr_urls
    }
    actual = [(s.arm, s.run_index, s.pr_url) for s in selections]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("incomplete or duplicate PR/run matrix")
    run_ids = {}
    epoch = load_epoch(window.expected.golden_epoch_id, root=DEFAULT_ROOT)
    expected_bugs = {
        p: {b.bug_id for b in epoch.bugs if b.pr_url == p} for p in window.pr_urls
    }
    all_observations = []
    for s in selections:
        attempt = s.attempt
        if (
            not attempt
            or not s.score_ref
            or s.attempt_id != attempt.attempt_id
            or s.pr_url != attempt.pr_url
        ):
            raise ValueError("missing selected attempt or score")
        v = attempt.validity
        if (
            v.state not in ("valid", "operational_failure")
            or v.window_id != window.window_id
            or v.observed != window.expected
        ):
            raise ValueError("invalid selected outcome or measurement identity")
        if v.state == "operational_failure" and not s.failure_confirmation_ref:
            raise ValueError("unconfirmed operational failure")
        rows = s.observations
        if (
            len(rows) != len(expected_bugs[s.pr_url])
            or {r.bug_id for r in rows} != expected_bugs[s.pr_url]
            or any(
                (r.arm, r.run_index, r.pr_url, r.run_id, r.attempt_id)
                != (s.arm, s.run_index, s.pr_url, attempt.run_id, attempt.attempt_id)
                or type(r.detected) is not bool
                for r in rows
            )
            or (v.state == "operational_failure" and any(r.detected for r in rows))
        ):
            raise ValueError(
                "incomplete/duplicate/non-Boolean bug matrix or nonzero operational failure"
            )
        all_observations.extend(rows)
        key = (s.arm, s.run_index)
        if (
            run_ids.setdefault(key, attempt.run_id) != attempt.run_id
            or s.score_ref.run_id != attempt.run_id
        ):
            raise ValueError("run identity mismatch")
    if len(set(run_ids.values())) != len(run_ids):
        raise ValueError("rescoring does not create independent review runs")
    return CompleteMatrix(
        selections,
        tuple(all_observations),
        arms,
        tuple((a, expected_run_indices) for a in arms),
    )


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    exclusive_json(pending, value)
    os.replace(pending, path)


def benchmark_policy(timeout_s, *, resolve_limits=True):
    from bench.runner.invoke import BENCH_PINS

    limits = None
    if resolve_limits:
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                "import {LIMIT_DEFAULTS} from './workflows/src/registry.js'; console.log(JSON.stringify(LIMIT_DEFAULTS));",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        limits = json.loads(result.stdout)
    return {
        "provider": "firstParty",
        "subagentModel": None,
        "pins": dict(BENCH_PINS),
        "timeout_s": timeout_s,
        "stage_limits": limits,
        "capacity_policy_sha256": file_hash(ROOT / "workflows/src/capacity.js"),
    }


def agent_read_hash(root=ROOT):
    paths = [
        *Path(root).glob("agents/*.md"),
        *Path(root).glob("skills/**/*.md"),
        *Path(root).glob("workflows/src/*.js"),
    ]
    return digest(
        [
            (str(p.relative_to(root)), file_hash(p))
            for p in sorted(paths)
            if artifact_allowed(p)
        ]
    )


def run_provenance(
    *, child_model, child_auth, timeout_s, epoch_id=None, window=None, root=ROOT
):
    epoch = load_epoch(epoch_id, root=DEFAULT_ROOT)
    pins = read_json(epoch.root / "baselines.json")
    policy = benchmark_policy(timeout_s, resolve_limits=window is not None)
    cli = window.expected.cli_realpath if window else shutil.which("claude")
    try:
        fp = capture_fingerprint(
            cli_path=cli,
            child_model=child_model,
            auth_mode=child_auth,
            epoch=epoch,
            judge_pin=pins["judge_pin"],
            adjudicator_pin=pins.get("adjudicator_pin") or pins["judge_pin"],
            env=os.environ,
            policy=policy,
            root=root,
            auxiliary_models=window.expected.allowed_auxiliary_model_ids
            if window
            else (),
        )
        if window:
            checked = check_window(window, fp, now=utc_now())
            if checked.state != "valid":
                raise ValueError("window preflight: " + ", ".join(checked.reasons))
            return fp.wire()
        value = fp.wire()
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        if window:
            raise
        path = Path(cli).resolve() if cli else None
        try:
            version = cli_probe(path) if path and path.is_file() else None
        except (OSError, subprocess.SubprocessError):
            version = None
        value = {
            "cli_version": version,
            "cli_realpath": str(path) if path else None,
            "cli_sha256": file_hash(path) if path and path.is_file() else None,
            "resolved_models": None,
            "allowed_auxiliary_model_ids": [],
            "effort_env": {
                **{k: os.environ.get(k) for k in EFFORT_KEYS},
                "DISABLE_AUTOUPDATER": "1",
            },
            "frontmatter_effort_sha256": frontmatter_effort(root),
            "judge_pin": pins.get("judge_pin"),
            "adjudicator_pin": pins.get("adjudicator_pin") or pins.get("judge_pin"),
            "golden_epoch_id": epoch.epoch_id,
            "golden_sha256": epoch.golden_sha256,
            "scorer_sha256": epoch.scorer_sha256,
            "auth_mode": child_auth,
            "bench_policy_sha256": digest(policy),
        }
    return {
        **value,
        "status": "non_record",
        "requested_models": {"orchestrator": child_model},
    }


def collect_attempt(
    run_dir,
    pr_dir,
    *,
    url,
    result,
    started_at,
    manifest,
    prior=None,
    reset_at=None,
    reset_ref=None,
):
    from bench.runner import check, invoke

    manifest = {"run_id": Path(run_dir).name, **manifest}
    provenance = manifest.get("provenance") or {}
    fp = (
        fingerprint_from_wire(provenance)
        if provenance and provenance.get("status") != "non_record"
        else None
    )
    envelope = None
    if result.raw_json_path and Path(result.raw_json_path).is_file():
        envelope = invoke.parse_result_envelope(
            Path(result.raw_json_path).read_text(encoding="utf-8")
        )
    observed = tuple(sorted((envelope or {}).get("modelUsage", {})))
    event = _child_failure_event(envelope)
    events = [event] if event else []
    value = classify_attempt(
        result,
        auth_mode=manifest.get("child_auth", "api"),
        trusted_events=events,
        now=utc_now(),
    )
    value = replace(
        value,
        observed=fp,
        window_id=manifest.get("window_id"),
        retry_of=prior.attempt_id if prior else None,
        confirmed_reset_at=reset_at,
        evidence_refs=value.evidence_refs
        + (("reset-sha256:" + reset_ref,) if reset_ref else ()),
    )
    if fp and manifest.get("measurement_window"):
        postflight = check_window(
            load_window(Path(manifest["measurement_window"])), fp, now=utc_now()
        )
        reasons = set(postflight.reasons)
        try:
            recheck_cli(fp)
        except (OSError, ValueError, subprocess.SubprocessError):
            reasons.add("cli_mismatch")
        if reasons:
            value = replace(
                value,
                state="invalid",
                reasons=tuple(sorted({*value.reasons, *reasons})),
                retryable=False,
            )
    if result.status == "ok":
        errors = (
            check.check_attempt(Path(pr_dir), naive=manifest.get("anchor") == "naive")
            if fp
            else ()
        )
        if not result.payload_path or not Path(result.payload_path).is_file():
            errors = (*errors, "artifact_missing")
        if fp:
            allowed = set(dict(fp.resolved_models).values()) | set(
                fp.allowed_auxiliary_model_ids
            )
            if manifest.get("anchor") != "naive":
                maps = []
                for path in (Path(pr_dir) / "workflows").glob("*.json"):
                    data = read_json(path)
                    if not invoke.record_identifies_repo_bundle(data, ROOT):
                        continue
                    holder = invoke._record_args_holder(data)
                    args = holder.get("args") if holder else None
                    if isinstance(args, str):
                        args = json.loads(args)
                    if not isinstance(args, dict) or args.get("outputDir") != str(
                        Path(run_dir) / "output"
                    ):
                        continue
                    result_envelope = data.get("result")
                    if isinstance(result_envelope, dict):
                        model_map = result_envelope.get("resolvedPolicy", {}).get(
                            "resolvedModels"
                        )
                        if isinstance(model_map, dict):
                            maps.append(model_map)
                expected_models = {
                    k: val for k, val in fp.resolved_models if k != "orchestrator"
                }
                if not maps:
                    errors = (*errors, "missing_provenance")
                elif any(model_map != expected_models for model_map in maps):
                    errors = (*errors, "model_mismatch")
            if set(observed) - allowed:
                errors = (*errors, "model_mismatch")
            if not observed:
                errors = (*errors, "missing_provenance")
            traces = list(Path(pr_dir).glob("code-gauntlet-measurement-*.json"))
            if traces:
                try:
                    trace = read_json(traces[0])
                    resolved = trace.get("resolved_models", {})
                except (ValueError, OSError, AttributeError):
                    trace = {}
                    resolved = {}
                    value = replace(
                        value,
                        limitations=(
                            *value.limitations,
                            "Unreadable optional trace; stage analysis unavailable.",
                        ),
                    )
                if any(
                    dict(fp.resolved_models).get(k) != v for k, v in resolved.items()
                ):
                    errors = (*errors, "model_mismatch")
                caps = (
                    [s for s in trace.get("snapshots", []) if s.get("cap_hit")]
                    if isinstance(trace, dict)
                    else []
                )
                if caps:
                    policy = benchmark_policy(
                        (manifest.get("env_fingerprint") or {}).get("timeout_s")
                    )
                    declared = {
                        "challenge": policy["stage_limits"]["challengeCap"],
                        "select": int(policy["pins"]["pr_comment_cap"]),
                    }
                    value = replace(
                        value,
                        evidence_refs=(
                            *value.evidence_refs,
                            *(
                                "cap-hit:" + s["stage"] + ":" + str(s.get("cap"))
                                for s in caps
                            ),
                        ),
                    )
                    if any(
                        s.get("cap") != declared.get(s.get("stage"))
                        or s.get("stage") not in declared
                        for s in caps
                    ):
                        errors = (*errors, "pipeline_degraded")
        if errors:
            value = replace(
                value,
                state="invalid",
                reasons=tuple(sorted(set(errors))),
                retryable=False,
            )
    if not fp and value.state == "valid":
        value = replace(value, state="pending", reasons=("missing_provenance",))
    if prior and value.state == "invalid" and value.retryable:
        provisional = AttemptRecord(
            1,
            "unpublished",
            manifest["run_id"],
            url,
            started_at,
            utc_now(),
            result.status,
            result.reason,
            value,
            (),
            prior.sequence + 1,
            (),
            observed,
        )
        with suppress(ValueError):
            value = confirm_failure(
                prior, provisional, reset_evidence=reset_ref
            ).attempt.validity
    if result.status == "ok" and value.state == "invalid":
        result = replace(result, status="invalid")
    return snapshot_attempt(
        run_dir,
        pr_dir,
        run_id=manifest["run_id"],
        pr_url=url,
        result=result,
        validity=value,
        started_at=started_at,
        requested_models=tuple(
            (
                manifest.get("requested_models")
                or {"orchestrator": manifest.get("child_model") or "inherit"}
            ).items()
        ),
        observed_model_ids=observed,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-window")
    for flag in (
        "out",
        "cli-path",
        "child-model",
        "judge-pin",
        "adjudicator-pin",
        "epoch",
        "opens-at",
        "closes-at",
        "window-id",
        "policy",
        "pr-urls",
        "run-indices",
    ):
        freeze.add_argument("--" + flag, required=True)
    freeze.add_argument("--auth-mode", choices=get_args(AuthMode), required=True)
    freeze.add_argument("--auxiliary-models", default="")
    args = parser.parse_args(argv)
    try:
        epoch = load_epoch(args.epoch, root=DEFAULT_ROOT)
        fp = capture_fingerprint(
            cli_path=args.cli_path,
            child_model=args.child_model,
            auth_mode=args.auth_mode,
            epoch=epoch,
            judge_pin=args.judge_pin,
            adjudicator_pin=args.adjudicator_pin,
            env=os.environ,
            policy=read_json(Path(args.policy)),
            auxiliary_models=tuple(filter(None, args.auxiliary_models.split(","))),
        )
        window = MeasurementWindow(
            1,
            args.window_id,
            args.opens_at,
            args.closes_at,
            fp,
            tuple(read_json(Path(args.pr_urls))),
            tuple(int(i) for i in args.run_indices.split(",")),
            1,
        )
        window_from_wire(window.wire())
        exclusive_json(Path(args.out), window.wire())
    except (ValueError, OSError, TypeError, subprocess.SubprocessError) as exc:
        parser.exit(2, str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
